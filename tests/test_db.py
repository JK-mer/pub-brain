"""The pipeline is re-run routinely, so the upserts must not duplicate."""

import sqlite3
import unittest

from pubbrain import db

RECORD = {
    "slug": "foo", "url": "https://merics.org/en/report/foo", "title": "Foo",
    "subtitle": None, "date_published": "2025-01-01", "pub_type": "Report",
    "series": None, "access": "public", "pdf_url": None, "og_description": None,
    "people": [{"name": "Jane Doe", "slug": "jane-doe", "is_internal": True,
                "job_title": "Senior Analyst", "role": "author"}],
    "site_tags": ["Trade"],
}


class TestUpsert(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_rescraping_the_same_page_does_not_duplicate(self):
        db.upsert_publication(self.conn, RECORD)
        db.upsert_publication(self.conn, RECORD)
        self.assertEqual(self.count("publications"), 1)
        self.assertEqual(self.count("people"), 1)
        self.assertEqual(self.count("publication_people"), 1)
        self.assertEqual(self.count("site_tags"), 1)

    def test_dropped_author_is_unlinked_on_rescrape(self):
        db.upsert_publication(self.conn, RECORD)
        db.upsert_publication(self.conn, {**RECORD, "people": [], "site_tags": []})
        self.assertEqual(self.count("publication_people"), 0)
        self.assertEqual(self.count("publication_site_tags"), 0)
        self.assertEqual(self.count("people"), 1)  # the person record survives

    def test_external_authors_share_no_slug_but_stay_distinct(self):
        db.upsert_publication(self.conn, {**RECORD, "people": [
            {"name": "Ann Extern", "slug": None, "is_internal": False,
             "job_title": None, "role": "author"},
            {"name": "Bob Extern", "slug": None, "is_internal": False,
             "job_title": None, "role": "author"},
        ]})
        self.assertEqual(self.count("people"), 2)


class TestWorklist(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        self.url = "https://merics.org/en/report/foo"

    def status(self):
        return self.conn.execute(
            "SELECT status FROM sitemap_urls WHERE url = ?", (self.url,)
        ).fetchone()["status"]

    def test_unchanged_lastmod_leaves_a_done_page_alone(self):
        db.upsert_sitemap_url(self.conn, self.url, "report", "2025-01-01T00:00Z", "publication")
        db.mark_url(self.conn, self.url, "done", lastmod="2025-01-01T00:00Z")
        db.upsert_sitemap_url(self.conn, self.url, "report", "2025-01-01T00:00Z", "publication")
        self.assertEqual(self.status(), "done")
        self.assertEqual(db.pending_urls(self.conn), [])

    def test_changed_lastmod_requeues_the_page(self):
        db.upsert_sitemap_url(self.conn, self.url, "report", "2025-01-01T00:00Z", "publication")
        db.mark_url(self.conn, self.url, "done", lastmod="2025-01-01T00:00Z")
        db.upsert_sitemap_url(self.conn, self.url, "report", "2026-02-02T00:00Z", "publication")
        self.assertEqual(self.status(), "pending")

    def test_failed_pages_are_retried(self):
        db.upsert_sitemap_url(self.conn, self.url, "report", None, "publication")
        db.mark_url(self.conn, self.url, "failed", error="boom")
        self.assertEqual(len(db.pending_urls(self.conn)), 1)


if __name__ == "__main__":
    unittest.main()


class TestConcurrentWrites(unittest.TestCase):
    """The backlog is worked while `map-topics` runs in the background. WAL
    permits one writer, so without a busy timeout the workbench dies with
    "database is locked" mid-click."""

    def test_a_busy_timeout_is_set(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            self.assertGreaterEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            conn.close()

    def test_a_second_connection_waits_instead_of_failing(self):
        import tempfile, threading, time
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            a = db.connect(path)
            a.execute("CREATE TABLE t (x INTEGER)")
            a.commit()
            a.execute("BEGIN IMMEDIATE")
            a.execute("INSERT INTO t VALUES (1)")
            outcome = []

            def writer():
                # opened in this thread: sqlite3 connections cannot be shared
                b = db.connect(path)
                try:
                    b.execute("INSERT INTO t VALUES (2)")
                    b.commit()
                    outcome.append("wrote")
                except Exception as exc:
                    outcome.append(f"{type(exc).__name__}: {exc}")
                finally:
                    b.close()

            t = threading.Thread(target=writer)
            t.start()
            time.sleep(0.5)                     # blocked on the lock, not dead
            self.assertEqual(outcome, [], "should still be waiting")
            a.commit()
            t.join(timeout=20)
            self.assertEqual(outcome, ["wrote"])
            a.close()
