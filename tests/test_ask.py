"""The ask page (#24) is the one surface where a model writes prose the user
will act on, so what is pinned here is the boundary around it: that a citation
resolves to something actually retrieved, that a fabricated one is exposed
rather than rendered as a link, and that model output cannot inject markup.

Every test stubs the model. Nothing here reaches the network.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pubbrain import ask, db, embed, enrich, llm
from pubbrain.web import create_app

REPORT = {
    "slug": "tariff-report", "url": "https://merics.org/en/report/tariff-report",
    "title": "Tariff pressure on Europe", "subtitle": None,
    "date_published": "2025-03-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [], "site_tags": [],
}
ENRICHMENT = {
    "summary_one_liner": "Beijing's tariffs squeeze European carmakers.",
    "summary_short": "A short summary of the tariff piece.",
    "key_findings": ["Tariffs rose"], "entities": {"orgs": ["EU"]},
}
META = {"model": "test-model", "provider": "test", "prompt_version": 1,
        "words_sent": 100}


def stub_chat(content):
    """A chat() that returns fixed content in the real response shape."""
    return lambda *a, **k: {"content": content, "model": "stub", "seconds": 0.1,
                            "prompt_tokens": 10, "completion_tokens": 5,
                            "finish_reason": "stop", "total_tokens": 15}


class AskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.pub_id = db.upsert_publication(self.conn, REPORT)
        db.upsert_text(self.conn, self.pub_id, "Beijing imposed steep tariffs.", 4)
        db.upsert_primary_enrichment(self.conn, self.pub_id, ENRICHMENT, META)
        db.rebuild_fts(self.conn)
        self.conn.commit()


class TestCitations(AskTest):
    def test_a_citation_of_a_retrieved_record_becomes_a_link(self):
        out = ask.answer(self.conn, "tariffs",
                         chat=stub_chat(f"Europe is squeezed [{self.pub_id}]."))
        self.assertEqual([c["id"] for c in out["cited"]], [self.pub_id])
        self.assertEqual(out["invalid"], [])
        self.assertTrue(any("cite" in s for s in out["segments"]))

    def test_a_citation_of_something_never_retrieved_is_exposed(self):
        """The failure worth catching: a fabricated id renders identically to a
        real one unless it is checked against what retrieval actually returned."""
        out = ask.answer(self.conn, "tariffs",
                         chat=stub_chat("Something else entirely [4242]."))
        self.assertEqual(out["invalid"], [4242])
        self.assertEqual(out["cited"], [])
        self.assertTrue(any(s.get("text", "").find("[4242]") >= 0
                            for s in out["segments"]))

    def test_the_same_record_cited_twice_is_listed_once(self):
        out = ask.answer(self.conn, "tariffs",
                         chat=stub_chat(f"[{self.pub_id}] and again [{self.pub_id}]."))
        self.assertEqual(len(out["cited"]), 1)

    def test_text_around_citations_survives_intact(self):
        out = ask.answer(self.conn, "tariffs",
                         chat=stub_chat(f"Before [{self.pub_id}] after."))
        text = "".join(s.get("text", "") for s in out["segments"])
        self.assertEqual(text, "Before  after.")

    def test_grouped_citations_are_split_into_separate_links(self):
        """Models write [8, 443] as readily as [8] [443]. Matching only the
        single form left grouped ones unlinked AND unchecked — they were
        neither resolved nor reported, which is the worst of both."""
        other = db.upsert_publication(self.conn, {
            **REPORT, "slug": "second", "title": "Tariffs again",
            "url": "https://merics.org/en/comment/second"})
        db.upsert_text(self.conn, other, "More on tariffs.", 3)
        db.upsert_primary_enrichment(self.conn, other, ENRICHMENT, META)
        db.rebuild_fts(self.conn)
        self.conn.commit()
        out = ask.answer(self.conn, "tariffs",
                         chat=stub_chat(f"Both say so [{self.pub_id}, {other}]."))
        self.assertEqual(sorted(c["id"] for c in out["cited"]),
                         sorted([self.pub_id, other]))
        self.assertEqual(out["invalid"], [])

    def test_a_grouped_citation_can_be_half_invented(self):
        out = ask.answer(self.conn, "tariffs",
                         chat=stub_chat(f"Both [{self.pub_id}, 9999]."))
        self.assertEqual([c["id"] for c in out["cited"]], [self.pub_id])
        self.assertEqual(out["invalid"], [9999])

    def test_an_answer_with_no_citations_still_renders(self):
        out = ask.answer(self.conn, "tariffs",
                         chat=stub_chat("The excerpts do not cover that."))
        self.assertEqual(out["cited"], [])
        self.assertEqual(out["segments"][0]["text"],
                         "The excerpts do not cover that.")


class TestRetrieval(AskTest):
    def test_a_search_that_finds_nothing_says_so_without_calling_the_model(self):
        """An empty retrieval must not reach the model at all — asked with no
        excerpts it would answer from its own knowledge of China, which is
        exactly the claim this tool must never make."""
        def explode(*a, **k):
            raise AssertionError("the model was called with no excerpts")
        out = ask.answer(self.conn, "zzzznothingmatches", chat=explode)
        self.assertEqual(out["cited"], [])
        self.assertIn("failed search", out["segments"][0]["text"])

    def test_the_prompt_carries_ids_the_model_can_cite(self):
        seen = {}

        def capture(messages, **k):
            seen["prompt"] = messages[-1]["content"]
            return stub_chat("ok")(messages, **k)

        ask.answer(self.conn, "tariffs", chat=capture)
        self.assertIn(f"[{self.pub_id}]", seen["prompt"])
        self.assertIn("Tariff pressure on Europe", seen["prompt"])

    def test_a_record_without_a_summary_is_labelled_in_the_prompt(self):
        conn = self.conn
        podcast = db.upsert_publication(conn, {
            **REPORT, "slug": "ep-1", "url": "https://merics.org/en/podcast/ep-1",
            "title": "Episode 1: tariffs", "pub_type": "Podcast",
            "og_description": "Tariffs discussed."})
        db.rebuild_fts(conn)
        conn.commit()
        seen = {}

        def capture(messages, **k):
            seen["prompt"] = messages[-1]["content"]
            return stub_chat("ok")(messages, **k)

        ask.answer(conn, "tariffs", chat=capture)
        self.assertIn(str(podcast), seen["prompt"])
        self.assertIn("no body text", seen["prompt"])


class TestSectionExcerpts(AskTest):
    """The fixtures above have no sections, which is why a live question was
    what exposed `matching_sections` not returning the section body at all."""

    def setUp(self):
        super().setUp()
        from pubbrain import sections
        body = ("Intro text long enough to survive the twenty-word minimum "
                "that drops bare labels from the section list before anything "
                "downstream sees them at all.\n\n"
                "## Tariffs on European carmakers\n\n" + "tariff " * 40 + "\n\n"
                "## Something unrelated about shipping\n\n" + "ship " * 40)
        db.upsert_text(self.conn, self.pub_id, body, len(body.split()))
        found = sections.split(body)
        for sec in found:
            sec["is_boilerplate"] = sections.is_boilerplate(sec["heading"])
        db.replace_sections(self.conn, self.pub_id, found)
        db.rebuild_fts(self.conn)
        db.rebuild_section_fts(self.conn)
        self.conn.commit()

    def test_the_matching_section_text_reaches_the_prompt(self):
        seen = {}

        def capture(messages, **k):
            seen["prompt"] = messages[-1]["content"]
            return stub_chat("ok")(messages, **k)

        ask.answer(self.conn, "tariff", chat=capture)
        self.assertIn("matching section", seen["prompt"])
        self.assertIn("Tariffs on European carmakers", seen["prompt"])
        self.assertIn("tariff tariff", seen["prompt"])      # the body, not just a heading

    def test_brackets_in_source_text_cannot_become_citations(self):
        """A footnote marker copied out of an article would otherwise mint a
        citation pointing at whatever publication that number happens to be."""
        db.replace_sections(self.conn, self.pub_id, [{
            "position": 0, "heading": "Findings", "level": 2,
            "body": "The tariff study [12] reports a rise. " + "tariff " * 30,
            "word_count": 37, "independent": 0, "is_boilerplate": 0}])
        db.rebuild_section_fts(self.conn)
        self.conn.commit()
        seen = {}

        def capture(messages, **k):
            seen["prompt"] = messages[-1]["content"]
            return stub_chat("ok")(messages, **k)

        ask.answer(self.conn, "tariff", chat=capture)
        self.assertIn("(12)", seen["prompt"])
        self.assertNotIn("[12]", seen["prompt"])


class TestAskPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        self.pub_id = db.upsert_publication(conn, REPORT)
        db.upsert_text(conn, self.pub_id, "Beijing imposed steep tariffs.", 4)
        db.upsert_primary_enrichment(conn, self.pub_id, ENRICHMENT, META)
        db.rebuild_fts(conn)
        conn.commit()
        conn.close()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def test_the_empty_page_offers_the_form_without_calling_anything(self):
        page = self.client.get("/ask").get_data(as_text=True)
        self.assertIn("Ask the catalog", page)

    def test_a_citation_links_to_the_publication_on_merics_org(self):
        """Owner's requirement (#44). The href comes from the retrieved row, not
        from the model — a fabricated URL would render identically to a real
        one, which is the failure the id check already exists to catch."""
        with mock.patch("pubbrain.llm.chat_with_backoff",
                        stub_chat(f"Squeezed [{self.pub_id}].")):
            page = self.client.get("/ask?q=tariffs").get_data(as_text=True)
        self.assertIn(f'href="{REPORT["url"]}"', page)
        self.assertIn("Tariff pressure on Europe", page)
        # The record page stays one click away — it holds the summary, blurb,
        # chapters and topics that merics.org does not.
        self.assertIn(f'href="/pub/{self.pub_id}"', page)

    def test_an_uncited_id_is_still_plain_text_with_no_link(self):
        """The guarantee that survived the change: a citation the retrieval
        never produced must not become a link to anything."""
        with mock.patch("pubbrain.llm.chat_with_backoff",
                        stub_chat("Invented [99999].")):
            page = self.client.get("/ask?q=tariffs").get_data(as_text=True)
        self.assertIn("[99999]", page)
        self.assertNotIn('href="/pub/99999"', page)
        self.assertIn("not among the retrieved", page)


    def test_model_output_cannot_inject_markup(self):
        """The answer is untrusted text rendered as segments precisely so Jinja
        escapes it; returning HTML would put a prompt-injected script on the
        page of a tool whose whole point is reading retrieved documents."""
        with mock.patch("pubbrain.llm.chat_with_backoff",
                        stub_chat("<script>alert(1)</script>")):
            page = self.client.get("/ask?q=tariffs").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_a_missing_key_is_reported_rather_than_crashing(self):
        with mock.patch("pubbrain.llm.chat_with_backoff",
                        side_effect=llm.NoApiKey("no keyring entry")):
            resp = self.client.get("/ask?q=tariffs")
        self.assertEqual(resp.status_code, 502)
        self.assertIn("no keyring entry", resp.get_data(as_text=True))


class TestRegenerate(unittest.TestCase):
    NEW = ('{"summary_one_liner": "A sharper line about European carmakers.",'
           ' "summary_short": "Rewritten.", "key_findings": ["a", "b", "c"],'
           ' "entities": {"orgs": ["EU"]}}')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.pub_id = db.upsert_publication(self.conn, REPORT)
        db.upsert_text(self.conn, self.pub_id, "Beijing imposed steep tariffs.", 4)
        db.upsert_primary_enrichment(self.conn, self.pub_id, ENRICHMENT, META)
        db.upsert_review(self.conn, "enrichment", self.pub_id, "flagged", "wrong")
        db.store_embeddings(self.conn, "one_liner", embed.MODEL,
                            [{"source_id": self.pub_id,
                              "publication_id": self.pub_id}],
                            [[0.0] * embed.DIM], embed.pack)
        self.conn.commit()

    def _regenerate(self):
        with mock.patch("pubbrain.embed.embed_documents",
                        side_effect=embed.OllamaUnreachable("down")):
            return enrich.regenerate(self.conn, self.pub_id, model="m",
                                     provider="default",
                                     chat=stub_chat(self.NEW))

    def test_the_new_summary_lands_beside_the_old_one_not_over_it(self):
        """The point of #18: a rewrite adds a candidate and leaves the working
        summary serving search until someone chooses."""
        out = self._regenerate()
        self.assertEqual(out["before"]["summary_one_liner"],
                         ENRICHMENT["summary_one_liner"])
        self.assertFalse(out["is_primary"])
        rows = db.enrichments_for(self.conn, self.pub_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["summary_one_liner"],
                         ENRICHMENT["summary_one_liner"])   # still primary
        self.assertIn("sharper line", rows[1]["summary_one_liner"])

    def test_only_the_primary_is_visible_to_everything_else(self):
        """The silent-doubling guard: a second summary must not make the
        publication appear twice, nor inflate any count."""
        from pubbrain import queries
        self._regenerate()
        self.conn.commit()
        rows, total, _ = queries.list_publications(self.conn)
        self.assertEqual([r["id"] for r in rows].count(self.pub_id), 1)
        self.assertEqual(total, 1)
        self.assertEqual(queries.status_report(self.conn)["enriched"], 1)
        self.assertEqual(len(queries.publications_by_id(
            self.conn, [self.pub_id])), 1)

    def test_regenerating_touches_neither_the_flag_nor_the_vector(self):
        """Nothing about which summary is served has changed yet, so nothing
        that describes the served summary may be invalidated."""
        out = self._regenerate()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE source_type = 'one_liner'"
        ).fetchone()[0], 1)
        self.assertIsNone(out.get("note"))

    def test_promoting_swaps_which_summary_serves(self):
        out = self._regenerate()
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.5] * embed.DIM]):
            enrich.promote(self.conn, self.pub_id, out["candidate_id"])
        rows = db.enrichments_for(self.conn, self.pub_id)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["is_primary"])
        self.assertIn("sharper line", rows[0]["summary_one_liner"])
        self.assertEqual(sum(r["is_primary"] for r in rows), 1)

    def test_promoting_puts_the_topics_back_on_the_worklist(self):
        """Topics are read off the primary summary, so they describe the demoted
        one after a promotion (#43) — and `map-topics` would never revisit it,
        because its worklist is "has a summary and no topics". Delete the
        DELETE in `promote` and this fails."""
        db.replace_topics(self.conn, self.pub_id, ["macroeconomy-growth-model"],
                          {"model": "m", "prompt_version": 1})
        self.assertEqual(db.pending_topic_mapping(self.conn), [])
        out = self._regenerate()
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.5] * embed.DIM]):
            enrich.promote(self.conn, self.pub_id, out["candidate_id"])
        self.assertEqual([r["id"] for r in db.pending_topic_mapping(self.conn)],
                         [self.pub_id])

    def test_a_second_primary_is_rejected_by_the_database(self):
        """The invariant is enforced by SQLite, not by discipline — a code path
        that forgot would fail loudly rather than corrupt the catalog."""
        out = self._regenerate()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE publication_enrichment SET is_primary = 1 WHERE id = ?",
                (out["candidate_id"],))

    def test_promoting_deletes_the_vector_of_the_summary_it_replaced(self):
        """A vector describing a summary nobody searches does not look stale —
        it looks like a confident wrong match. Missing is recoverable."""
        out = self._regenerate()
        with mock.patch("pubbrain.embed.embed_documents",
                        side_effect=embed.OllamaUnreachable("down")):
            promoted = enrich.promote(self.conn, self.pub_id, out["candidate_id"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE source_type = 'one_liner'"
        ).fetchone()[0], 0)
        self.assertIn("could not be rebuilt", promoted["note"])

    def test_the_missing_vector_is_reported_by_status(self):
        from pubbrain import queries
        out = self._regenerate()
        with mock.patch("pubbrain.embed.embed_documents",
                        side_effect=embed.OllamaUnreachable("down")):
            enrich.promote(self.conn, self.pub_id, out["candidate_id"])
        self.assertTrue(any("summaries have no vector" in w
                            for w in queries.status_report(self.conn)["warnings"]))

    def test_the_vector_is_rebuilt_on_promotion_when_ollama_is_up(self):
        out = self._regenerate()
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.5] * embed.DIM]):
            promoted = enrich.promote(self.conn, self.pub_id, out["candidate_id"])
        self.assertIsNone(promoted["note"])
        row = self.conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE source_type = 'one_liner'"
        ).fetchone()[0]
        self.assertEqual(row, 1)

    def test_the_verdict_stays_with_the_text_it_reviewed(self):
        """`reviews.subject_id` names the summary row, so a flag written about
        the old wording does not silently transfer to the new one."""
        original = db.primary_enrichment_id(self.conn, self.pub_id)
        out = self._regenerate()
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.5] * embed.DIM]):
            enrich.promote(self.conn, self.pub_id, out["candidate_id"])
        verdict = self.conn.execute(
            "SELECT subject_id FROM reviews WHERE scope = 'enrichment'").fetchone()
        self.assertEqual(verdict["subject_id"], original)
        self.assertNotEqual(verdict["subject_id"], out["candidate_id"])

    def test_a_candidate_can_be_dismissed_but_the_primary_cannot(self):
        out = self._regenerate()
        with self.assertRaises(ValueError):
            db.dismiss_enrichment(
                self.conn, db.primary_enrichment_id(self.conn, self.pub_id))
        self.assertEqual(db.dismiss_enrichment(self.conn, out["candidate_id"]),
                         self.pub_id)
        rows = db.enrichments_for(self.conn, self.pub_id)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_primary"])

    def test_the_first_summary_of_all_lands_primary_not_candidate(self):
        """A publication holding only candidates has no summary at all, so the
        very first generation must never be one."""
        fresh = db.upsert_publication(self.conn, {
            **REPORT, "slug": "new-one", "url": "https://merics.org/en/report/new-one",
            "title": "Another report"})
        db.upsert_text(self.conn, fresh, "Some body text here.", 4)
        out = enrich.regenerate(self.conn, fresh, model="m", provider="default",
                                chat=stub_chat(self.NEW))
        self.assertTrue(out["is_primary"])
        self.assertIsNone(out["before"])
        self.assertEqual(db.primary_enrichment_id(self.conn, fresh),
                         out["candidate_id"])

    def test_an_unusable_response_leaves_everything_alone(self):
        """Same principle as the backfill: a missing summary is recoverable, a
        bad one is not — so a failed rewrite must not consume the good row."""
        with self.assertRaises(enrich.Invalid):
            enrich.regenerate(self.conn, self.pub_id, model="m",
                              provider="default", attempts=1,
                              chat=stub_chat("not json at all"))
        row = self.conn.execute(
            "SELECT summary_one_liner FROM publication_enrichment "
            "WHERE publication_id = ?", (self.pub_id,)).fetchone()
        self.assertEqual(row["summary_one_liner"], ENRICHMENT["summary_one_liner"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)

    def test_a_record_without_body_text_cannot_be_regenerated(self):
        podcast = db.upsert_publication(self.conn, {
            **REPORT, "slug": "ep-1", "url": "https://merics.org/en/podcast/ep-1",
            "title": "Episode 1", "pub_type": "Podcast"})
        with self.assertRaises(ValueError):
            enrich.regenerate(self.conn, podcast, model="m", provider="default",
                              chat=stub_chat(self.NEW))


class TestRegeneratePage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        self.pub_id = db.upsert_publication(conn, REPORT)
        db.upsert_text(conn, self.pub_id, "Beijing imposed steep tariffs.", 4)
        db.upsert_primary_enrichment(conn, self.pub_id, ENRICHMENT, META)
        conn.commit()
        conn.close()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def test_the_button_does_not_wait_for_a_flag(self):
        """This asserted the opposite until #39. Gating regeneration on a flag
        made "show me another reading" require claiming the current one is
        wrong, which filled the Flags page with publications nothing is wrong
        with — and left the #18 cross-model comparison unreachable."""
        page = self.client.get(f"/pub/{self.pub_id}").get_data(as_text=True)
        self.assertIn("dlg-regen", page)
        self.client.post(f"/pub/{self.pub_id}/flag", data={"note": "wrong"})
        page = self.client.get(f"/pub/{self.pub_id}").get_data(as_text=True)
        self.assertIn("dlg-regen", page)

    def test_the_page_shows_old_and_new_side_by_side(self):
        self.client.post(f"/pub/{self.pub_id}/flag", data={"note": "wrong"})
        new = ('{"summary_one_liner": "A sharper line.", "summary_short": "x",'
               ' "key_findings": ["a"], "entities": {}}')
        with mock.patch("pubbrain.llm.chat_with_backoff", stub_chat(new)), \
             mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.1] * embed.DIM]):
            page = self.client.post(
                f"/pub/{self.pub_id}/regenerate").get_data(as_text=True)
        self.assertIn("squeeze European carmakers", page)      # before
        self.assertIn("A sharper line.", page)                 # after

    def test_a_failed_rewrite_reports_and_changes_nothing(self):
        self.client.post(f"/pub/{self.pub_id}/flag", data={"note": "wrong"})
        with mock.patch("pubbrain.llm.chat_with_backoff",
                        side_effect=llm.NoApiKey("no keyring entry")):
            resp = self.client.post(f"/pub/{self.pub_id}/regenerate")
        self.assertEqual(resp.status_code, 502)
        self.assertIn("still stands", resp.get_data(as_text=True))
        conn = db.connect(self.db_path)
        row = conn.execute("SELECT summary_one_liner FROM publication_enrichment "
                           "WHERE publication_id = ?", (self.pub_id,)).fetchone()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)
        conn.close()
        self.assertEqual(row["summary_one_liner"], ENRICHMENT["summary_one_liner"])


if __name__ == "__main__":
    unittest.main()
