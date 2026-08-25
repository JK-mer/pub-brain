"""Plain-language blurbs (#38).

The failure this layer can actually have is a blurb that says nothing the
one-liner did not. No test can judge register, so the tests here pin the two
things that *are* checkable: the worklist never rewrites a blurb it already
has, and a restatement of the one-liner is rejected and re-asked.
"""

import functools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pubbrain import blurb, db, enrich
from pubbrain.web import create_app

BASE = {
    "slug": "s", "url": "u", "title": "T", "subtitle": None,
    "date_published": "2025-01-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [], "site_tags": [],
}
SUMMARY = {
    "summary_one_liner": "Beijing subsidises legacy chipmakers, pushing prices "
                         "down and squeezing European suppliers out.",
    "summary_short": "The piece argues that state support for older chip "
                     "production has created a glut. European firms cannot "
                     "match the prices. It calls for a tariff response.",
    "key_findings": ["Legacy chip capacity grew 40% in three years.",
                     "European market share halved."],
    "entities": {"people": [], "organizations": [], "places": [], "policies": []},
}
GOOD = ("China is pouring money into factories that make older, simpler "
        "computer chips — the kind in cars and washing machines, not phones. "
        "So many are being made that prices have collapsed, and European "
        "manufacturers cannot sell at those prices. The piece argues Europe "
        "should answer with tariffs before its own chipmakers disappear.")


# What `enrich_one` returns; `upsert_primary_enrichment` requires all of it.
META_IN = {"model": "m", "provider": "p", "prompt_version": 1, "words_sent": 100}


def reply(content, **kw):
    return {"content": content, "prompt_tokens": 10, "completion_tokens": 20,
            "seconds": 1.0, "model": "test-model", **kw}


class TestValidation(unittest.TestCase):
    def test_a_good_blurb_passes(self):
        self.assertEqual(
            blurb.validate({"blurb": GOOD}, SUMMARY["summary_one_liner"]), [])

    def test_a_restatement_of_the_one_liner_is_refused(self):
        """The named failure mode: the one-liner with shorter words."""
        echo = ("Beijing subsidises legacy chipmakers, which pushes prices down "
                "and squeezes European suppliers out of the market entirely, "
                "hurting suppliers across Europe and pushing prices lower.")
        problems = blurb.validate({"blurb": echo}, SUMMARY["summary_one_liner"])
        self.assertTrue(any("repeats the one-liner" in p for p in problems),
                        problems)

    def test_too_short_and_too_long_are_both_refused(self):
        self.assertTrue(blurb.validate({"blurb": "Chips are cheap now."}, None))
        self.assertTrue(blurb.validate({"blurb": "word " * 200}, None))

    def test_the_banned_opener_is_refused(self):
        problems = blurb.validate(
            {"blurb": "This publication looks at " + "word " * 40}, None)
        self.assertTrue(any("This publication" in p for p in problems))

    def test_a_non_object_is_refused_without_crashing(self):
        self.assertTrue(blurb.validate("not json", None))
        self.assertTrue(blurb.validate({"blurb": ""}, None))

    def test_the_ratio_ignores_short_words(self):
        """'the', 'and' and 'China' appear in almost every pair; counting them
        would make every honest blurb look derivative."""
        self.assertEqual(
            blurb._shared_ratio("the and but for", "the and but for"), 0.0)
        self.assertEqual(
            blurb._shared_ratio("China Beijing Europe", "China Beijing Europe"),
            0.0, "corpus-ubiquitous words are not evidence of restatement")


class TestAsking(unittest.TestCase):
    def setUp(self):
        self.rec = {**SUMMARY, "title": "Legacy chips", "pub_type": "Report",
                    "date_published": "2025-01-01"}

    def test_the_prompt_carries_the_summary_and_not_the_body(self):
        prompt = blurb.build_prompt(self.rec)
        self.assertIn("Legacy chip capacity grew 40%", prompt)
        self.assertIn(SUMMARY["summary_one_liner"], prompt)

    def test_a_bad_answer_is_re_asked_with_the_reason(self):
        seen = []

        def chat(messages, **kw):
            seen.append(messages[-1]["content"])
            return reply(json.dumps({"blurb": "Too short."})
                         if len(seen) == 1 else json.dumps({"blurb": GOOD}))

        text, meta = blurb.blurb_one(self.rec, chat=chat)
        self.assertEqual(text, GOOD)
        self.assertIn("at least", seen[-1])
        self.assertEqual(meta["completion_tokens"], 40)

    def test_giving_up_raises_rather_than_storing_something_unusable(self):
        with self.assertRaises(enrich.Invalid):
            blurb.blurb_one(self.rec, attempts=2,
                            chat=lambda m, **kw: reply("not json at all"))


class TestWorklist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.pid = db.upsert_publication(self.conn, {
            **BASE, "slug": "a", "url": "u/a", "title": "Legacy chips"})
        self.bare = db.upsert_publication(self.conn, {
            **BASE, "slug": "b", "url": "u/b", "title": "A podcast",
            "pub_type": "Podcast"})
        db.upsert_primary_enrichment(self.conn, self.pid, SUMMARY,
                                     META_IN)
        self.conn.commit()

    def _eid(self):
        return self.conn.execute(
            "SELECT id FROM primary_enrichment WHERE publication_id = ?",
            (self.pid,)).fetchone()["id"]

    def test_only_records_with_a_summary_are_offered(self):
        """A podcast has nothing to rewrite; sending it would invent one."""
        self.assertEqual([r["id"] for r in blurb.pending(self.conn)], [self.pid])

    def test_a_record_with_a_blurb_drops_off_the_list(self):
        blurb.save(self.conn, self.pid, GOOD, self._eid(), {})
        self.assertEqual(blurb.pending(self.conn), [])

    def test_promoting_a_different_summary_makes_the_blurb_stale(self):
        """Otherwise a blurb written from a dismissed reading sits under the
        new one looking current."""
        blurb.save(self.conn, self.pid, GOOD, self._eid(), {})
        other = db.add_enrichment(self.conn, self.pid,
                                  {**SUMMARY, "summary_one_liner": "A rival read."},
                                  {**META_IN, "model": "m2"})
        from pubbrain import enrich as e
        e.promote(self.conn, self.pid, other)
        self.conn.commit()
        self.assertTrue(blurb.for_publication(self.conn, self.pid)["stale"])
        self.assertEqual([r["id"] for r in blurb.pending(self.conn, stale=True)],
                         [self.pid])
        self.assertEqual(blurb.pending(self.conn), [],
                         "a stale blurb is not a missing one — --stale opts in")

    def test_saving_twice_replaces_rather_than_duplicates(self):
        blurb.save(self.conn, self.pid, GOOD, self._eid(), {})
        blurb.save(self.conn, self.pid, "A second try. " + "word " * 40,
                   self._eid(), {})
        rows = self.conn.execute(
            "SELECT blurb FROM publication_blurbs").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["blurb"].startswith("A second try"))

    def test_deleting_the_publication_takes_the_blurb(self):
        blurb.save(self.conn, self.pid, GOOD, self._eid(), {})
        self.conn.execute("DELETE FROM publications WHERE id = ?", (self.pid,))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM publication_blurbs"
                              ).fetchone()["c"], 0)

    def test_it_is_not_indexed_for_search(self):
        """Plain wording would dilute a keyword index built on the terms the
        documents use — the same reason the one-liner is not written this way."""
        blurb.save(self.conn, self.pid, "washing machines " * 30, self._eid(), {})
        db.reindex_one(self.conn, self.pid)
        self.assertEqual(db.search(self.conn, "washing"), [])


class TestTheWorkbench(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        self.pid = db.upsert_publication(conn, {
            **BASE, "slug": "a", "url": "u/a", "title": "Legacy chips"})
        db.upsert_primary_enrichment(conn, self.pid, SUMMARY,
                                     META_IN)
        eid = conn.execute("SELECT id FROM primary_enrichment "
                           "WHERE publication_id = ?", (self.pid,)).fetchone()["id"]
        blurb.save(conn, self.pid, GOOD, eid, {"model": "model-b"})
        conn.commit()
        conn.close()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def test_the_record_page_shows_it(self):
        page = self.client.get(f"/pub/{self.pid}").get_data(as_text=True)
        self.assertIn("washing machines", page)
        self.assertIn("in plain language", page)

    def test_the_listing_shows_one_liners_until_asked(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Beijing subsidises legacy chipmakers", page)
        self.assertNotIn("washing machines", page)

    def test_the_listing_swaps_them_in_on_request(self):
        page = self.client.get("/?plain=1").get_data(as_text=True)
        self.assertIn("washing machines", page)


if __name__ == "__main__":
    unittest.main()


class TestQuotaHandling(unittest.TestCase):
    """#45: a quota error must end the run, not be waited out.

    The backoff sleeps and re-asks — and each retry is a request, so it eats
    the window as it recovers. Every pass here is resumable, so stopping costs
    only the current record.
    """

    def setUp(self):
        self.rec = {**SUMMARY, "title": "Legacy chips", "pub_type": "Report",
                    "date_published": "2025-01-01"}

    def _quota_error(self):
        import requests
        resp = requests.Response()
        resp.status_code = 429
        return requests.HTTPError("429 Too Many Requests", response=resp)

    def test_no_wait_raises_instead_of_sleeping(self):
        """`sleep` is stubbed to fail: a regression must turn the suite red, not
        slow. Without `network_retries=0` this would sleep 73s and re-ask."""
        import requests
        from pubbrain import llm

        def boom(_):
            raise AssertionError("slept instead of giving up")

        def always_429(messages, **kw):
            raise self._quota_error()

        with mock.patch("pubbrain.llm.chat", side_effect=always_429):
            with self.assertRaises(requests.HTTPError):
                blurb.blurb_one(
                    self.rec, network_retries=0,
                    chat=functools.partial(llm.chat_with_backoff, sleep=boom))

    def test_the_default_still_waits(self):
        """The weekly job (#8) wants the patience; flipping the default would
        trade a visible cost for a silent 3am failure."""
        from pubbrain import llm
        slept = []

        calls = [self._quota_error(), self._quota_error(),
                 reply(json.dumps({"blurb": GOOD}))]

        def flaky(messages, **kw):
            out = calls.pop(0)
            if isinstance(out, Exception):
                raise out
            return out

        with mock.patch("pubbrain.llm.chat", side_effect=flaky):
            text, _ = blurb.blurb_one(
                self.rec,
                chat=functools.partial(llm.chat_with_backoff,
                                       sleep=slept.append))
        self.assertEqual(text, GOOD)
        self.assertEqual(len(slept), 2)
