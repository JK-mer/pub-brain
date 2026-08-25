"""The index is rebuilt wholesale after every pipeline step, so it must not
drift or duplicate — and podcasts, which have no body text, must stay findable.
"""

import sqlite3
import unittest

from pubbrain import db, queries

REPORT = {
    "slug": "tariff-report", "url": "https://merics.org/en/report/tariff-report",
    "title": "Tariff pressure on Grünberg", "subtitle": "A subtitle",
    "date_published": "2025-03-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": "On trade defence.",
    "people": [], "site_tags": [],
}
PODCAST = {
    **REPORT, "slug": "ep-12", "url": "https://merics.org/en/podcast/ep-12",
    "title": "Episode 12: chips", "subtitle": None, "pub_type": "Podcast",
    "og_description": "A conversation about semiconductors.",
}


class TestFts(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        self.report_id = db.upsert_publication(self.conn, REPORT)
        db.upsert_publication(self.conn, PODCAST)
        db.upsert_text(self.conn, self.report_id, "Beijing imposed tariffs on imports.", 5)
        db.rebuild_fts(self.conn)

    def test_rebuilding_twice_does_not_duplicate(self):
        db.rebuild_fts(self.conn)
        self.assertEqual(db.rebuild_fts(self.conn), 2)
        self.assertEqual(len(db.search(self.conn, "tariff")), 1)

    def test_publication_without_body_text_is_indexed_via_its_description(self):
        hits = db.search(self.conn, "semiconductors")
        self.assertEqual([h["title"] for h in hits], ["Episode 12: chips"])
        self.assertIn("[semiconductors]", hits[0]["snippet"])

    def test_description_is_dropped_when_a_body_exists(self):
        """merics.org puts the whole article in og:description (#15). Indexing
        it alongside the body stored every text twice, above the original."""
        db.rebuild_fts(self.conn)
        row = self.conn.execute(
            "SELECT description, body FROM publication_fts WHERE rowid = ?",
            (self.report_id,),
        ).fetchone()
        self.assertIsNone(row["description"])
        self.assertTrue(row["body"])
        # ...and the description's own words are no longer searchable for it
        self.assertEqual(db.search(self.conn, '"trade defence"'), [])

    def test_stemming_and_diacritic_folding(self):
        self.assertEqual(len(db.search(self.conn, "tariff")), 1)   # matches "tariffs"
        self.assertEqual(len(db.search(self.conn, "Grunberg")), 1)  # matches "Grünberg"

    def test_a_title_hit_outranks_a_passing_mention_in_the_body(self):
        db.upsert_text(
            self.conn, self.report_id,
            "Beijing imposed tariffs. " * 40 + "The chips sector matters.", 200,
        )
        db.rebuild_fts(self.conn)
        self.assertEqual(db.search(self.conn, "chips")[0]["title"], "Episode 12: chips")

    def test_a_hyphenated_term_must_be_quoted(self):
        """docs/schema.md tells the reader to quote 'de-risking'. If FTS5 ever
        stops splitting on the hyphen, that advice needs revisiting."""
        db.upsert_text(self.conn, self.report_id, "A policy of de-risking.", 4)
        db.rebuild_fts(self.conn)
        with self.assertRaises(sqlite3.OperationalError):
            db.search(self.conn, "de-risking")
        self.assertEqual(len(db.search(self.conn, '"de risking"')), 1)

    def test_a_deleted_publication_leaves_no_orphan_row(self):
        self.conn.execute("DELETE FROM publications WHERE id = ?", (self.report_id,))
        self.assertEqual(db.rebuild_fts(self.conn), 1)
        self.assertEqual(db.search(self.conn, "tariff"), [])


if __name__ == "__main__":
    unittest.main()


class TestGoneClassification(unittest.TestCase):
    """#13: the sitemap outlives the pages, and a dead URL must stop being
    retried without being mistaken for 'not a publication'."""

    def test_definitive_codes_are_settled_not_retryable(self):
        from pubbrain.cli import DEFINITIVE_HTTP
        self.assertEqual(DEFINITIVE_HTTP, {403, 404, 410})
        # 5xx and 429 stay retryable — those are wobbles, not answers.
        for transient in (429, 500, 502, 503):
            self.assertNotIn(transient, DEFINITIVE_HTTP)

    def test_gone_urls_leave_the_scrape_worklist(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        for url, status in (("u/a", "pending"), ("u/b", "failed"),
                            ("u/c", "gone"), ("u/d", "done")):
            db.upsert_sitemap_url(conn, url, "/en/podcast/", "2025-01-01",
                                  "publication")
            db.mark_url(conn, url, status)
        todo = [r["url"] for r in db.pending_urls(conn)]
        self.assertEqual(sorted(todo), ["u/a", "u/b"])

    def test_a_gone_url_is_not_re_queued_by_a_sitemap_refresh(self):
        """Otherwise every sync-sitemap would resurrect five dead pages."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        db.upsert_sitemap_url(conn, "u/x", "/en/podcast/", "2025-01-01", "publication")
        db.mark_url(conn, "u/x", "gone")
        db.upsert_sitemap_url(conn, "u/x", "/en/podcast/", "2026-02-02", "publication")
        self.assertEqual(conn.execute(
            "SELECT status FROM sitemap_urls WHERE url = 'u/x'").fetchone()[0], "gone")
        self.assertEqual(db.pending_urls(conn), [])

    def test_gone_pages_are_named_in_the_coverage_caveats(self):
        """They were real publications; an absence claim has to own them."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        db.upsert_sitemap_url(conn, "u/x", "/en/podcast/", "2025-01-01", "publication")
        db.mark_url(conn, "u/x", "gone")
        self.assertTrue(any("no longer resolve" in c
                            for c in queries.coverage_caveats(conn)))
