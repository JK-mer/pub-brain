"""The shortlist (#25): the first human signal in the catalog.

It changes search ranking, which makes two things load-bearing — that the boost
is visible wherever it acts, and that it can be switched off so retrieval can
still be measured on its own.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pubbrain import db, embed, queries
from pubbrain.web import create_app

BASE = {
    "slug": "s", "url": "u", "title": "T", "subtitle": None,
    "date_published": "2025-01-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [], "site_tags": [],
}
META = {"model": "m", "provider": "p", "prompt_version": 1, "words_sent": 10}


def enrichment(one_liner):
    return {"summary_one_liner": one_liner, "summary_short": "s",
            "key_findings": [], "entities": {}}


class ShortlistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.ids = []
        for i in range(6):
            pid = db.upsert_publication(self.conn, {
                **BASE, "slug": f"p{i}", "url": f"u/p{i}",
                "title": f"Tariffs piece {i}"})
            db.upsert_text(self.conn, pid, f"Tariffs and more tariffs {i}.", 5)
            db.upsert_primary_enrichment(
                self.conn, pid, enrichment(f"Tariffs, reading {i}."), META)
            self.ids.append(pid)
        db.rebuild_fts(self.conn)
        self.conn.commit()


class TestTheSignal(ShortlistTest):
    def test_marking_stores_the_note(self):
        db.set_shortlist(self.conn, self.ids[0], "  made waves  ")
        rows = db.shortlist_rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "made waves")

    def test_remarking_updates_the_note_and_keeps_the_original_date(self):
        db.set_shortlist(self.conn, self.ids[0], "first")
        first = db.shortlist_rows(self.conn)[0]["added_at"]
        db.set_shortlist(self.conn, self.ids[0], "second")
        rows = db.shortlist_rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "second")
        self.assertEqual(rows[0]["added_at"], first)

    def test_a_mark_with_no_note_is_allowed(self):
        db.set_shortlist(self.conn, self.ids[0], "")
        self.assertIsNone(db.shortlist_rows(self.conn)[0]["note"])

    def test_it_survives_a_summary_being_replaced(self):
        """Why this is not a `reviews` scope: a verdict describes a generated
        row and dies with it (#18), but 'this mattered' describes the work."""
        db.set_shortlist(self.conn, self.ids[0], "keeps mattering")
        cid = db.add_enrichment(self.conn, self.ids[0],
                                enrichment("A rewritten line."), META)
        db.promote_enrichment(self.conn, cid)
        self.assertEqual(db.shortlisted_ids(self.conn), {self.ids[0]})
        self.assertEqual(db.shortlist_rows(self.conn)[0]["note"], "keeps mattering")

    def test_deleting_the_publication_takes_the_mark(self):
        db.set_shortlist(self.conn, self.ids[0], "x")
        self.conn.execute("DELETE FROM publication_enrichment WHERE publication_id = ?",
                          (self.ids[0],))
        self.conn.execute("DELETE FROM publications WHERE id = ?", (self.ids[0],))
        self.assertEqual(db.shortlisted_ids(self.conn), set())


class TestRankingBoost(ShortlistTest):
    """Keyword-only (no ollama in tests), which is enough: the boost is applied
    to the fused score regardless of which rankers contributed."""

    def find(self, **kw):
        hits, _ = queries.hybrid_find(self.conn, "tariffs", limit=6,
                                      with_vectors=False, **kw)
        return [h["publication"]["id"] for h in hits]

    def test_a_shortlisted_record_moves_up(self):
        before = self.find()
        last = before[-1]
        db.set_shortlist(self.conn, last, "matters")
        after = self.find()
        self.assertLess(after.index(last), before.index(last))

    def test_the_boost_cannot_beat_a_clearly_better_match(self):
        """'Wins a near-tie, not outranks a clearly better match.'

        Worst case on purpose: keyword-only, where one ranker supplies the whole
        list and RRF gaps are uniform, so the boost moves a result exactly
        SHORTLIST_POSITIONS places. Five was tried first and took the fifth
        result to the top.
        """
        before = self.find()
        for pos in range(queries.SHORTLIST_POSITIONS + 1, len(before)):
            db.set_shortlist(self.conn, before[pos], "matters")
            moved = self.find().index(before[pos])
            db.clear_shortlist(self.conn, before[pos])
            self.assertGreater(moved, 0, f"result {pos} reached the top")
            self.assertGreaterEqual(moved, pos - queries.SHORTLIST_POSITIONS)

    def test_the_boost_can_be_switched_off(self):
        """Without this, scripts/eval_retrieval.py would measure retrieval plus
        curation and the recorded baseline would stop being comparable."""
        before = self.find(boost_shortlist=False)
        db.set_shortlist(self.conn, before[-1], "matters")
        self.assertEqual(self.find(boost_shortlist=False), before)

    def test_every_hit_reports_whether_it_is_shortlisted(self):
        db.set_shortlist(self.conn, self.ids[0], "matters")
        hits, _ = queries.hybrid_find(self.conn, "tariffs", limit=6,
                                      with_vectors=False)
        starred = [h["shortlisted"] for h in hits
                   if h["publication"]["id"] == self.ids[0]]
        self.assertEqual(starred, [True])
        self.assertTrue(any(h["shortlisted"] is False for h in hits))

    def test_hits_are_still_badged_when_the_boost_is_off(self):
        """Otherwise a reader of unboosted results cannot see the marks at all."""
        db.set_shortlist(self.conn, self.ids[0], "matters")
        hits, _ = queries.hybrid_find(self.conn, "tariffs", limit=6,
                                      with_vectors=False, boost_shortlist=False)
        self.assertTrue(any(h["shortlisted"] for h in hits))


class TestTheWorkbench(ShortlistTest):
    def setUp(self):
        super().setUp()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def test_starring_from_the_page_persists_with_its_note(self):
        resp = self.client.post(f"/pub/{self.ids[0]}/shortlist",
                                data={"note": "made waves internally"})
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/shortlist").get_data(as_text=True)
        self.assertIn("Tariffs piece 0", page)
        self.assertIn("made waves internally", page)

    def test_removing_takes_it_off_the_list(self):
        self.client.post(f"/pub/{self.ids[0]}/shortlist", data={"note": "x"})
        self.client.post(f"/pub/{self.ids[0]}/shortlist", data={"action": "remove"})
        self.assertNotIn("Tariffs piece 0",
                         self.client.get("/shortlist").get_data(as_text=True))

    def test_the_catalog_badges_a_shortlisted_row(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("★", page)
        self.client.post(f"/pub/{self.ids[0]}/shortlist", data={"note": "x"})
        self.assertIn("★", self.client.get("/").get_data(as_text=True))

    def test_search_results_show_the_badge_where_the_boost_acts(self):
        """A result that ranks higher for a reason the page does not show
        reads as the ranking being wrong."""
        self.client.post(f"/pub/{self.ids[0]}/shortlist", data={"note": "x"})
        with mock.patch("pubbrain.embed.embed_query",
                        side_effect=embed.OllamaUnreachable("down")):
            page = self.client.get("/?q=tariffs").get_data(as_text=True)
        self.assertIn("★", page)

    def test_the_filter_narrows_to_the_shortlist(self):
        self.client.post(f"/pub/{self.ids[0]}/shortlist", data={"note": "x"})
        page = self.client.get("/?shortlisted=1").get_data(as_text=True)
        self.assertIn("Tariffs piece 0", page)
        self.assertNotIn("Tariffs piece 1", page)

    def test_an_unknown_publication_cannot_be_starred(self):
        self.assertEqual(
            self.client.post("/pub/9999/shortlist", data={"note": "x"}).status_code,
            404)

    def test_the_empty_state_explains_the_note_is_the_point(self):
        page = self.client.get("/shortlist").get_data(as_text=True)
        self.assertIn("Nothing shortlisted yet", page)


if __name__ == "__main__":
    unittest.main()
