"""The v11 -> v13 upgrade, run against a populated database (#18).

Every other test starts from an empty schema and so never exercises the step
that can actually destroy data: rewriting `publication_enrichment` and
re-keying `reviews` while 1,108 summaries and 31 verdicts are sitting in them.
The failure mode is total — a migration that forgets `is_primary = 1` leaves
the view empty and the whole catalog loses its summaries at once.
"""

import sqlite3
import unittest

from pubbrain import db

UP_TO_11 = 11


def build_v11(conn):
    """A database at the schema version this change had to upgrade from."""
    for i, sql in enumerate(db.MIGRATIONS[:UP_TO_11], start=1):
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {i}")
    conn.commit()


class TestUpgradeWithData(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        build_v11(self.conn)
        for pid in (3, 7, 11):
            self.conn.execute(
                "INSERT INTO publications (id, slug, url, title, pub_type, scraped_at)"
                " VALUES (?, ?, ?, ?, 'Report', '2025-01-01')",
                (pid, f"s{pid}", f"u{pid}", f"Title {pid}"))
            self.conn.execute(
                "INSERT INTO publication_enrichment (publication_id,"
                " summary_one_liner, summary_short, key_findings, entities, model,"
                " provider, prompt_version, words_sent, enriched_at)"
                " VALUES (?, ?, 'short', '[]', '{}', 'model-a', 'default', 1, 10,"
                " '2026-08-01')", (pid, f"one-liner for {pid}"))
        # Verdicts keyed on publication ids, as v11 stored them.
        for pid, verdict in ((3, "confirmed"), (11, "flagged")):
            db.upsert_review(self.conn, "enrichment", pid, verdict, f"note {pid}")
        self.conn.commit()

    def migrate(self):
        db.migrate(self.conn)

    def test_every_existing_summary_becomes_its_publications_primary(self):
        self.migrate()
        rows = self.conn.execute(
            "SELECT publication_id, is_primary FROM publication_enrichment "
            "ORDER BY publication_id").fetchall()
        self.assertEqual([r["publication_id"] for r in rows], [3, 7, 11])
        self.assertTrue(all(r["is_primary"] for r in rows))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM primary_enrichment").fetchone()[0],
            3)

    def test_the_summaries_themselves_survive_intact(self):
        self.migrate()
        row = self.conn.execute(
            "SELECT * FROM primary_enrichment WHERE publication_id = 7").fetchone()
        self.assertEqual(row["summary_one_liner"], "one-liner for 7")
        self.assertEqual(row["model"], "model-a")
        self.assertEqual(row["enriched_at"], "2026-08-01")

    def test_verdicts_are_re_keyed_onto_the_summary_they_reviewed(self):
        """Remapping in place would transiently collide with
        UNIQUE(scope, subject_id) — an enrichment id can equal a publication id
        that has not been remapped yet. Hence the table rebuild."""
        self.migrate()
        rows = {r["verdict"]: r["subject_id"] for r in self.conn.execute(
            "SELECT verdict, subject_id FROM reviews")}
        self.assertEqual(len(rows), 2)
        for verdict, subject in rows.items():
            row = self.conn.execute(
                "SELECT publication_id FROM publication_enrichment WHERE id = ?",
                (subject,)).fetchone()
            self.assertIsNotNone(row, f"{verdict} points at no summary")
        confirmed = self.conn.execute(
            "SELECT e.publication_id FROM reviews r "
            "JOIN publication_enrichment e ON e.id = r.subject_id "
            "WHERE r.verdict = 'confirmed'").fetchone()
        self.assertEqual(confirmed["publication_id"], 3)

    def test_the_verdict_notes_are_not_lost(self):
        self.migrate()
        notes = {r["note"] for r in self.conn.execute("SELECT note FROM reviews")}
        self.assertEqual(notes, {"note 3", "note 11"})

    def test_the_invariant_is_live_immediately_after_upgrading(self):
        self.migrate()
        row = self.conn.execute(
            "SELECT id FROM publication_enrichment WHERE publication_id = 3").fetchone()
        self.conn.execute(
            "INSERT INTO publication_enrichment (publication_id, is_primary,"
            " summary_one_liner, summary_short, key_findings, entities, model,"
            " provider, prompt_version, words_sent, enriched_at)"
            " VALUES (3, 0, 'candidate', 's', '[]', '{}', 'model-b', 'default',"
            " 1, 10, '2026-08-08')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE publication_enrichment SET is_primary = 1 "
                "WHERE model = 'model-b'")
        self.assertTrue(self.conn.execute(
            "SELECT is_primary FROM publication_enrichment WHERE id = ?",
            (row["id"],)).fetchone()["is_primary"])

    def test_upgrading_is_idempotent(self):
        self.migrate()
        self.migrate()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM primary_enrichment").fetchone()[0],
            3)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0],
                         len(db.MIGRATIONS))


if __name__ == "__main__":
    unittest.main()
