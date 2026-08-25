"""A bad summary that reaches the database is worse than a failed row: the
one-liner is the recall unit, and nothing downstream re-checks it. So the
validation gate and the resume worklist are what these tests pin down.
"""

import functools
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pubbrain import db, embed, enrich, llm
from pubbrain.web import create_app

PUB = {
    "slug": "chip-report", "url": "https://merics.org/en/report/chip-report",
    "title": "Chip controls", "subtitle": None, "date_published": "2025-06-01",
    "pub_type": "Report", "series": None, "access": "public", "pdf_url": None,
    "og_description": "On export controls.", "people": [], "site_tags": [],
}
GOOD = {
    "summary_one_liner": "Beijing's chip subsidies outpace export controls.",
    "summary_short": "A short summary of the argument. And the so-what.",
    "key_findings": ["Subsidies rose", "Controls lag"],
    "entities": {"people": [], "organizations": ["SMIC"], "places": ["Beijing"]},
}


def reply(data, **kw):
    """A fake llm response carrying `data` as its JSON content."""
    return {"content": json.dumps(data) if isinstance(data, dict) else data,
            "prompt_tokens": 100, "completion_tokens": 50, "seconds": 1.0,
            "finish_reason": "stop", "model": "test-model", **kw}


class TestValidate(unittest.TestCase):
    def test_a_good_response_has_no_problems(self):
        self.assertEqual(enrich.validate(GOOD), [])

    def test_an_over_long_one_liner_is_rejected_with_its_length(self):
        """The default model runs 26-30 words unprompted (#14), so this is the gate that
        actually fires in production — not a theoretical malformed-JSON case."""
        long_one = {**GOOD, "summary_one_liner": "word " * (enrich.ONE_LINER_MAX_WORDS + 1)}
        problems = enrich.validate(long_one)
        self.assertEqual(len(problems), 1)
        self.assertIn(str(enrich.ONE_LINER_MAX_WORDS + 1), problems[0])

    def test_a_one_liner_exactly_at_the_cap_passes(self):
        at_cap = {**GOOD, "summary_one_liner": " ".join(["word"] * enrich.ONE_LINER_MAX_WORDS)}
        self.assertEqual(enrich.validate(at_cap), [])

    def test_malformed_shapes_are_caught(self):
        self.assertTrue(enrich.validate(None))
        self.assertTrue(enrich.validate({**GOOD, "key_findings": "not a list"}))
        self.assertTrue(enrich.validate({**GOOD, "entities": ["not an object"]}))
        self.assertTrue(enrich.validate({**GOOD, "summary_short": "  "}))

    def test_json_inside_a_code_fence_is_still_read(self):
        self.assertEqual(enrich.parse(f"```json\n{json.dumps(GOOD)}\n```"), GOOD)
        self.assertIsNone(enrich.parse(""))
        self.assertIsNone(enrich.parse("no object here"))


class TestRetryLoop(unittest.TestCase):
    def setUp(self):
        self.rec = {"title": "T", "subtitle": None, "pub_type": "Report",
                    "date_published": "2025-01-01", "og_description": None,
                    "body": "Beijing and SMIC.", "word_count": 3}

    def test_a_rejected_answer_is_re_asked_with_the_problem_quoted_back(self):
        sent = []

        def chat(messages, **kw):
            sent.append(list(messages))
            if len(sent) == 1:
                return reply({**GOOD, "summary_one_liner": "word " * 40})
            return reply(GOOD)

        data, meta = enrich.enrich_one(self.rec, "m", "default", chat=chat)
        self.assertEqual(data, GOOD)
        self.assertEqual(meta["attempts"], 2)
        # the retry carries the bad answer and the reason, not a cold re-ask
        self.assertEqual(sent[1][-2]["role"], "assistant")
        self.assertIn("40 words", sent[1][-1]["content"])

    def test_tokens_and_time_accumulate_across_attempts(self):
        data, meta = enrich.enrich_one(
            self.rec, "m", "default",
            chat=lambda m, **kw: reply({**GOOD, "summary_one_liner": "w " * 40})
            if len(m) < 4 else reply(GOOD),
        )
        self.assertEqual(meta["attempts"], 2)
        self.assertEqual(meta["prompt_tokens"], 200)   # a retry is not free
        self.assertEqual(meta["completion_tokens"], 100)

    def test_giving_up_raises_rather_than_storing_something_unusable(self):
        with self.assertRaises(enrich.Invalid):
            enrich.enrich_one(self.rec, "m", "default", attempts=2,
                              chat=lambda m, **kw: reply("not json at all"))

    def test_the_body_is_capped_but_the_metadata_is_not(self):
        rec = {**self.rec, "body": "word " * 9000, "word_count": 9000}
        prompt = enrich.build_prompt(rec, cap_words=100)
        self.assertEqual(prompt.count("word"), 100)
        self.assertIn("Title: T", prompt)

    def test_the_whole_article_in_og_description_is_not_sent_twice(self):
        """merics.org puts the article in og:description (#15); sending both
        doubled every prompt."""
        rec = {**self.rec, "og_description": "THE ARTICLE AGAIN"}
        self.assertNotIn("THE ARTICLE AGAIN", enrich.build_prompt(rec))
        # ...but it is the only text a paywalled record has
        self.assertIn("THE ARTICLE AGAIN", enrich.build_prompt({**rec, "body": ""}))

    def test_grounding_counts_invented_names(self):
        self.assertEqual(enrich.grounding({"o": ["Beijing", "Paris"]}, "Beijing acts"), 0.5)
        self.assertIsNone(enrich.grounding({}, "text"))


class TestWorklist(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        # Must precede the first write: the pragma is a no-op inside a transaction.
        self.conn.execute("PRAGMA foreign_keys = ON")
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        self.pub_id = db.upsert_publication(self.conn, PUB)
        db.upsert_text(self.conn, self.pub_id, "Beijing restricts Xinjiang exports.", 4)
        self.podcast_id = db.upsert_publication(
            self.conn, {**PUB, "slug": "ep-1", "url": "https://merics.org/en/podcast/ep-1",
                        "title": "Episode 1", "pub_type": "Podcast"})
        db.rebuild_fts(self.conn)

    def test_a_publication_without_body_text_is_never_queued(self):
        """Podcasts carry no prose, so there is nothing to summarize — they must
        not sit in the worklist forever failing."""
        ids = [r["id"] for r in db.pending_enrichment(self.conn)]
        self.assertEqual(ids, [self.pub_id])

    def test_an_enriched_publication_drops_out_of_the_worklist(self):
        db.upsert_primary_enrichment(self.conn, self.pub_id, GOOD, {
            "model": "m", "provider": "default", "prompt_version": 1,
            "words_sent": 4, "attempts": 1})
        self.assertEqual(db.pending_enrichment(self.conn), [])

    def test_the_sensitive_slice_selects_on_content(self):
        neutral = db.upsert_publication(
            self.conn, {**PUB, "slug": "x", "url": "https://merics.org/en/report/x"})
        db.upsert_text(self.conn, neutral, "A note on quarterly growth figures.", 6)
        db.rebuild_fts(self.conn)
        ids = [r["id"] for r in db.pending_enrichment(self.conn, match=enrich.SENSITIVE_QUERY)]
        self.assertEqual(ids, [self.pub_id])   # the Xinjiang one, not the growth one

    def test_lists_survive_the_round_trip(self):
        db.upsert_primary_enrichment(self.conn, self.pub_id, GOOD, {
            "model": "m", "provider": "default", "prompt_version": 1,
            "words_sent": 4, "attempts": 1})
        row = self.conn.execute(
            "SELECT * FROM publication_enrichment WHERE publication_id = ?",
            (self.pub_id,)).fetchone()
        self.assertEqual(json.loads(row["key_findings"]), GOOD["key_findings"])
        self.assertEqual(json.loads(row["entities"])["organizations"], ["SMIC"])
        self.assertEqual(row["model"], "m")

    def test_re_enriching_replaces_rather_than_duplicates(self):
        meta = {"model": "m", "provider": "default", "prompt_version": 1,
                "words_sent": 4, "attempts": 1}
        db.upsert_primary_enrichment(self.conn, self.pub_id, GOOD, meta)
        db.upsert_primary_enrichment(self.conn, self.pub_id,
                             {**GOOD, "summary_one_liner": "Rewritten."},
                             {**meta, "model": "m2"})
        rows = self.conn.execute("SELECT * FROM publication_enrichment").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary_one_liner"], "Rewritten.")
        self.assertEqual(rows[0]["model"], "m2")

    def test_deleting_a_publication_takes_its_enrichment_with_it(self):
        db.upsert_primary_enrichment(self.conn, self.pub_id, GOOD, {
            "model": "m", "provider": "default", "prompt_version": 1,
            "words_sent": 4, "attempts": 1})
        self.conn.execute("DELETE FROM publications WHERE id = ?", (self.pub_id,))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM publication_enrichment").fetchone()[0], 0)


class TestBackoff(unittest.TestCase):
    def test_a_rate_limit_is_retried_and_a_bad_request_is_not(self):
        import requests

        from pubbrain import llm

        calls, slept = [], []

        def fail_then_succeed(messages, **kw):
            calls.append(1)
            if len(calls) < 3:
                response = requests.Response()
                response.status_code = 429
                raise requests.HTTPError(response=response)
            return reply(GOOD)

        llm.chat_with_backoff.__globals__["chat"] = fail_then_succeed
        try:
            out = llm.chat_with_backoff([], base_delay=0.01, sleep=slept.append)
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(slept), 2)
            self.assertTrue(out["content"])

            def bad_model(messages, **kw):
                response = requests.Response()
                response.status_code = 400
                raise requests.HTTPError(response=response)

            llm.chat_with_backoff.__globals__["chat"] = bad_model
            with self.assertRaises(requests.HTTPError):
                llm.chat_with_backoff([], base_delay=0.01, sleep=slept.append)
            self.assertEqual(len(slept), 2)   # not retried
        finally:
            llm.chat_with_backoff.__globals__["chat"] = llm.chat

    def test_a_rate_limit_waits_in_minutes_not_seconds(self):
        """A 429 is a spent quota window. An unattended backfill has to ride it
        out — seconds of backoff would abandon the run at the first window."""
        import requests

        from pubbrain import llm

        response = requests.Response()
        response.status_code = 429
        exc = requests.HTTPError(response=response)
        first = llm._retry_delay(exc, 0, base=5.0, rate_limited=True)
        self.assertGreaterEqual(first, 60)
        # and it is capped, so one bad window cannot stall the run all night
        self.assertLessEqual(
            llm._retry_delay(exc, 20, base=5.0, rate_limited=True),
            llm.RATE_LIMIT_MAX_DELAY * 1.25)

    def test_payment_required_is_treated_as_a_spent_window(self):
        """Some providers return 402 as well as 429 for a momentarily spent quota.
        Taking it at face value cost 21 publications in the #5 backfill."""
        from pubbrain import llm

        self.assertIn(402, llm.RETRY_STATUS)
        self.assertIn(402, llm.QUOTA_STATUS)

    def test_retry_after_is_honoured_over_our_own_guess(self):
        import requests

        from pubbrain import llm

        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "42"
        delay = llm._retry_delay(requests.HTTPError(response=response), 0, 5.0, True)
        self.assertEqual(delay, 42.0)


if __name__ == "__main__":
    unittest.main()


class TestHyphenSafety(unittest.TestCase):
    """docs/schema.md documents that hyphens break FTS5 — and the retrieval
    eval walked into it anyway, scoring two syntax errors as ranking failures.
    Programmatic callers go through fts_safe."""

    def test_a_natural_question_survives(self):
        """The /ask page (#24) feeds whole questions to retrieval. A trailing
        '?' raises, and the keyword half was being dropped with only a note to
        show for it — found by asking the live catalog a real question."""
        self.assertEqual(db.fts_safe("What about export controls?"),
                         'What about export "controls"')
        self.assertEqual(db.fts_safe("China's rare earths, in 2025."),
                         '"China s" rare "earths" in "2025"')

    def test_deliberate_syntax_is_left_alone(self):
        self.assertEqual(db.fts_safe("chip* AND export"), "chip* AND export")
        self.assertEqual(db.fts_safe("tariffs NOT steel"), "tariffs NOT steel")

    def test_hyphenated_terms_become_quoted_phrases(self):
        self.assertEqual(db.fts_safe("de-risking"), '"de risking"')
        self.assertEqual(db.fts_safe("14th Five-Year Plan"), '14th "Five Year" Plan')

    def test_ordinary_queries_are_untouched(self):
        self.assertEqual(db.fts_safe("rare earth"), "rare earth")
        self.assertEqual(db.fts_safe('"already quoted"'), '"already quoted"')
        self.assertEqual(db.fts_safe(""), "")

    def test_the_quoted_form_actually_runs(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        self.addCleanup(conn.close)
        pid = db.upsert_publication(conn, PUB)
        db.upsert_text(conn, pid, "A policy of de-risking and Five-Year planning.", 8)
        db.rebuild_fts(conn)
        with self.assertRaises(sqlite3.OperationalError):
            db.search(conn, "de-risking")
        self.assertEqual(len(db.search(conn, db.fts_safe("de-risking"))), 1)


class TestModelCatalogue(unittest.TestCase):
    """#39. The picker is drawn on every record page, so it must not make an
    HTTP call, and it must reach a model the provider has."""

    def setUp(self):
        llm._MODEL_CACHE.clear()
        self.addCleanup(llm._MODEL_CACHE.clear)

    def test_the_shortlist_needs_no_network(self):
        """A dropdown on every record page cannot wait on a provider."""
        with mock.patch.object(llm, "models",
                               side_effect=AssertionError("must not be called")):
            self.assertTrue(llm.offered("default"))

    def test_the_provider_default_comes_first_and_appears_once(self):
        for provider in llm.PROVIDERS:
            offered = llm.offered(provider)
            default = llm.PROVIDERS[provider]["default_model"]
            self.assertEqual(offered[0], default)
            self.assertEqual(offered.count(default), 1)

    def test_shortlisted_models_are_reachable(self):
        """The whole complaint behind #39: it was on the subscription and
        offered by nothing."""
        self.assertIn("provider/small-instruct", llm.offered("default"))

    def test_every_offered_model_is_one_the_provider_carries(self):
        """A shortlist can go stale where a live listing cannot. This is the
        only thing that catches a typo or a retired model id."""
        for provider in llm.PROVIDERS:
            try:
                available = set(llm.models(provider, timeout=20))
            except Exception as exc:               # noqa: BLE001
                self.skipTest(f"{provider} unreachable: {exc}")
            missing = set(llm.offered(provider)) - available
            self.assertFalse(missing, f"{provider} no longer carries {missing}")

    def test_an_unreachable_provider_reports_no_catalogue_size(self):
        with mock.patch.object(llm, "models",
                               side_effect=RuntimeError("connection refused")):
            self.assertIsNone(llm.catalogue_size("default"))

    def test_the_catalogue_is_counted_once_per_process(self):
        with mock.patch.object(llm, "models", return_value=["m"]) as listed:
            llm.catalogue_size("default")
            llm.catalogue_size("default")
        self.assertEqual(listed.call_count, 1)


class TestLongReportPrompt(unittest.TestCase):
    """A long report is summarised from its executive summary (#18).

    Truncating a 56,000-word report to the cap sends cover, contents and
    foreword — front matter, and the argument only by luck.
    """

    # Long enough to clear MIN_EXEC_WORDS: a real one runs 217-640 words.
    REAL_EXEC = ("The party has subordinated growth to strategic goals. " * 30)

    def _rec(self, words, exec_body=None, outline=None):
        return {"id": 1, "title": "A long report", "subtitle": None,
                "pub_type": "Report", "date_published": "2025-01-01",
                "og_description": None, "body": "filler " * words,
                "word_count": words, "exec_body": exec_body, "outline": outline}

    def test_a_long_report_sends_its_executive_summary_not_its_opening(self):
        prompt = enrich.build_prompt(
            self._rec(56000, exec_body=self.REAL_EXEC,
                      outline="Introduction\n1. Doctrine\n2. Practice"))
        self.assertIn("subordinated growth to strategic goals", prompt)
        self.assertIn("1. Doctrine", prompt)
        self.assertNotIn("filler", prompt)
        self.assertIn("56,000-word document", prompt)

    def test_a_long_report_without_one_still_falls_back_to_the_cap(self):
        """58 of the 90 have no such heading; the cap is all they have."""
        prompt = enrich.build_prompt(self._rec(56000))
        self.assertIn("filler", prompt)
        self.assertEqual(len(prompt.split("Body:\n")[1].split()),
                         enrich.BODY_WORD_CAP)

    def test_a_short_document_is_sent_whole_even_with_an_executive_summary(self):
        """Nothing is being chosen under the cap, and picking the summary there
        would throw away text that fits."""
        prompt = enrich.build_prompt(
            self._rec(200, exec_body="A summary.", outline="One\nTwo"))
        self.assertIn("filler", prompt)
        self.assertNotIn("A summary.", prompt)

    def test_words_sent_records_what_was_sent_not_what_existed(self):
        """This column is how a summary written from too little is found later,
        so it must not report the cap when the prompt was a tenth of it."""
        rec = self._rec(56000, exec_body=self.REAL_EXEC, outline="Introduction")
        _, meta = enrich.enrich_one(
            rec, "m", "p",
            chat=lambda messages, **kw: {
                "content": json.dumps(GOOD), "prompt_tokens": 1,
                "completion_tokens": 1, "seconds": 0.1, "model": "m"})
        self.assertLess(meta["words_sent"], 500,
                        "the cap would have been 6,000")

    def test_a_heading_with_nothing_under_it_falls_back_to_the_cap(self):
        """A divider page reading "Executive Summary" is not one. On the
        56,033-word security report the section under that heading is 13
        words — sending it would be far worse than the opening."""
        prompt = enrich.build_prompt(
            self._rec(56000, exec_body="Executive Summary", outline="Introduction"))
        self.assertIn("filler", prompt)
        self.assertNotIn("sent as its executive summary", prompt)


class TestExecutiveSummary(unittest.TestCase):
    """A long report is read through its own executive summary (#18).

    Owner, 2026-08-09: copying it in verbatim is accurate but "not digestible",
    so the authors' summary is the model's *input*. The point is that the input
    is right, not that a step is saved.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.pid = db.upsert_publication(self.conn, {
            "slug": "long", "url": "https://merics.org/en/report/long",
            "title": "A long report", "subtitle": None,
            "date_published": "2025-01-01", "pub_type": "Report", "series": None,
            "access": "public", "pdf_url": None, "og_description": None,
            "people": [], "site_tags": []})
        db.upsert_text(self.conn, self.pid, "filler " * 20000, 20000)
        # The worklist means "its summary is not its executive summary", so a
        # record with no summary at all is `enrich`'s job, not this one.
        db.upsert_primary_enrichment(self.conn, self.pid, GOOD,
                                     {"model": "m", "provider": "p",
                                      "prompt_version": 1, "words_sent": 300})

    def _sections(self, exec_words):
        db.replace_sections(self.conn, self.pid, [
            {"position": 0, "heading": "Executive Summary", "level": 2,
             "body": "the argument " * exec_words, "word_count": exec_words * 2,
             "independent": 0, "is_boilerplate": 0},
            {"position": 1, "heading": "1. Introduction", "level": 2,
             "body": "filler " * 500, "word_count": 500,
             "independent": 0, "is_boilerplate": 0}])

    def test_the_executive_summary_is_what_the_model_reads(self):
        self._sections(200)
        prompt = enrich.build_prompt(db.enrichment_row(self.conn, self.pid))
        self.assertIn("the argument", prompt)
        self.assertIn("1. Introduction", prompt)   # the contents go too
        self.assertNotIn("filler", prompt)

    def test_a_heading_with_nothing_under_it_falls_back_to_the_opening(self):
        """The 56,033-word security report has 13 words under "Executive
        Summary" — a divider page. Sending that would be far worse than the
        cap. Lower MIN_EXEC_WORDS to 1 and this fails."""
        self._sections(5)
        prompt = enrich.build_prompt(db.enrichment_row(self.conn, self.pid))
        self.assertIn("filler", prompt)
        self.assertNotIn("executive summary", prompt.lower())

    def test_a_short_document_is_still_sent_whole(self):
        """Under the cap the model already reads everything the executive
        summary was written from, so choosing would only throw text away."""
        db.upsert_text(self.conn, self.pid, "filler " * 300, 300)
        self._sections(200)
        prompt = enrich.build_prompt(db.enrichment_row(self.conn, self.pid))
        self.assertIn("filler", prompt)

    def test_the_worklist_skips_a_section_too_short_to_use(self):
        """The dry run must not promise records the apply would refuse."""
        self._sections(5)
        self.assertEqual(enrich.pending_executive_summary(self.conn), [])
        self._sections(200)
        self.assertEqual(enrich.pending_executive_summary(self.conn), [self.pid])

    def test_a_record_with_no_summary_at_all_is_not_this_pass_s_job(self):
        self._sections(200)
        self.conn.execute("DELETE FROM publication_enrichment")
        self.assertEqual(enrich.pending_executive_summary(self.conn), [])


class TestHandWrittenSummary(unittest.TestCase):
    """The summary can be written by hand (#46).

    Every other field was already correctable — body text, credits, membership,
    the shortlist note. The summary, which the whole tool rests on and which is
    the most likely to be wrong, could only be produced by a model.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.pid = db.upsert_publication(self.conn, {
            "slug": "a", "url": "https://merics.org/en/report/a", "title": "T",
            "subtitle": None, "date_published": "2025-01-01",
            "pub_type": "Report", "series": None, "access": "public",
            "pdf_url": None, "og_description": None, "people": [], "site_tags": []})
        db.upsert_text(self.conn, self.pid, "body " * 300, 300)
        db.upsert_primary_enrichment(self.conn, self.pid, GOOD,
                                     {"model": "model-a", "provider": "default",
                                      "prompt_version": 1, "words_sent": 300})

    def _primary(self):
        return self.conn.execute(
            "SELECT * FROM primary_enrichment WHERE publication_id = ?",
            (self.pid,)).fetchone()

    def test_it_becomes_the_summary_being_served(self):
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.1] * embed.DIM]):
            _, problems = enrich.write_summary(
                self.conn, self.pid, "A line I wrote myself about the report.",
                "The longer version, in my own words.")
        self.assertEqual(problems, [])
        row = self._primary()
        self.assertEqual(row["model"], db.HAND_WRITTEN_MODEL)
        self.assertEqual(row["summary_short"], "The longer version, in my own words.")

    def test_the_model_version_is_demoted_not_deleted(self):
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.1] * embed.DIM]):
            enrich.write_summary(self.conn, self.pid, "Mine.", "My summary.")
        rows = db.enrichments_for(self.conn, self.pid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r["is_primary"] for r in rows), 1)
        self.assertIn("model-a", [r["model"] for r in rows])

    def test_editing_again_updates_rather_than_stacking(self):
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.1] * embed.DIM]):
            enrich.write_summary(self.conn, self.pid, "First.", "First go.")
            enrich.write_summary(self.conn, self.pid, "Second.", "Second go.")
        rows = db.enrichments_for(self.conn, self.pid)
        self.assertEqual(len(rows), 2, "one hand-written row, one demoted model")
        self.assertEqual(self._primary()["summary_short"], "Second go.")

    def test_the_thirty_word_cap_applies_to_a_person_too(self):
        """It is the retrieval unit whoever wrote it, and a 60-word one-liner
        degrades ranking silently."""
        _, problems = enrich.write_summary(
            self.conn, self.pid, "word " * 40, "A summary.")
        self.assertTrue(any("30" in p for p in problems), problems)
        self.assertEqual(self._primary()["model"], "model-a", "nothing was written")

    def test_an_empty_field_is_refused(self):
        for one, short in (("", "text"), ("a line", "  ")):
            _, problems = enrich.write_summary(self.conn, self.pid, one, short)
            self.assertTrue(problems)

    def test_the_findings_and_entities_carry_over(self):
        """They are extracted claims, not prose. Retyping them to fix a
        sentence is how a correction stops being worth making."""
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.1] * embed.DIM]):
            enrich.write_summary(self.conn, self.pid, "Mine.", "My summary.")
        self.assertEqual(json.loads(self._primary()["key_findings"]),
                         GOOD["key_findings"])

    def test_the_vector_and_the_topics_are_invalidated(self):
        """Both describe the summary that just changed. A stale vector does not
        look stale — it looks like a confident wrong match."""
        db.replace_topics(self.conn, self.pid, ["macroeconomy-growth-model"],
                          {"model": "m", "prompt_version": 1})
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.1] * embed.DIM]):
            enrich.write_summary(self.conn, self.pid, "Mine.", "My summary.")
        self.assertEqual([r["id"] for r in db.pending_topic_mapping(self.conn)],
                         [self.pid])

    def test_editing_rebuilds_the_vector_too(self):
        """The edit path deletes the one-liner vector; it must also rebuild it.
        It did not, so a second edit left the record on keyword-only ranking
        until someone happened to run `embed` (found in adversarial review).
        Replace `resummarised` with a bare DELETE and this fails."""
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.1] * embed.DIM]) as embedder:
            enrich.write_summary(self.conn, self.pid, "First.", "First go.")
            embedder.reset_mock()
            enrich.write_summary(self.conn, self.pid, "Second line here.",
                                 "Second go.")
            self.assertTrue(embedder.called, "the edit never re-embedded")
        self.assertEqual(len(db.pending_embeddings(self.conn, "one_liner",
                                                   embed.MODEL)), 0)

    def test_a_pipeline_cannot_overwrite_it(self):
        """Both callers of `upsert_primary_enrichment` only reach records with
        no summary, so this cannot fire today — which is why it is worth
        having. A hand-written summary is the only copy."""
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.1] * embed.DIM]):
            enrich.write_summary(self.conn, self.pid, "Mine.", "My summary.")
        with self.assertRaises(ValueError):
            db.upsert_primary_enrichment(
                self.conn, self.pid, GOOD,
                {"model": "model-a", "provider": "s", "prompt_version": 1,
                 "words_sent": 300})


class TestWritingFromTheWorkbench(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        self.pid = db.upsert_publication(conn, {
            "slug": "a", "url": "https://merics.org/en/report/a", "title": "T",
            "subtitle": None, "date_published": "2025-01-01",
            "pub_type": "Report", "series": None, "access": "public",
            "pdf_url": None, "og_description": None, "people": [], "site_tags": []})
        db.upsert_text(conn, self.pid, "body " * 300, 300)
        db.upsert_primary_enrichment(conn, self.pid, GOOD,
                                     {"model": "model-a", "provider": "s",
                                      "prompt_version": 1, "words_sent": 300})
        conn.commit()
        conn.close()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def test_saving_from_the_page_serves_it(self):
        with mock.patch("pubbrain.embed.embed_documents",
                        return_value=[[0.1] * embed.DIM]):
            self.client.post(f"/pub/{self.pid}/summary",
                             data={"one_liner": "A line I wrote.",
                                   "short": "The argument, in my words."})
        page = self.client.get(f"/pub/{self.pid}").get_data(as_text=True)
        self.assertIn("The argument, in my words.", page)
        self.assertIn(db.HAND_WRITTEN_MODEL, page)

    def test_a_rejected_summary_says_why_and_changes_nothing(self):
        resp = self.client.post(f"/pub/{self.pid}/summary",
                                data={"one_liner": "word " * 40, "short": "x"})
        self.assertIn("error=", resp.headers["Location"])
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute(
            "SELECT model FROM primary_enrichment WHERE publication_id = ?",
            (self.pid,)).fetchone()["model"], "model-a")
        conn.close()

    def test_an_unknown_publication_is_404(self):
        self.assertEqual(self.client.post(
            "/pub/99999/summary", data={"one_liner": "a", "short": "b"}
        ).status_code, 404)


class TestQuotaStop(unittest.TestCase):
    """A quota error ends the run instead of being waited out (#45).

    The backoff buys "roughly an hour of patience", which is right for the
    unattended weekly job and wrong when the window is nearly empty: each retry
    is another request, so it eats the budget as it recovers.
    """

    def _quota_error(self):
        import requests
        resp = requests.Response()
        resp.status_code = 429
        return requests.HTTPError("429", response=resp)

    def _rec(self):
        return {"id": 1, "title": "T", "subtitle": None, "pub_type": "Report",
                "date_published": "2025-01-01", "og_description": None,
                "body": "word " * 300, "word_count": 300,
                "exec_body": None, "outline": None}

    def test_enrich_gives_up_instead_of_sleeping(self):
        """`sleep` raises, so a regression turns the suite red rather than
        making it take 73 seconds."""
        import requests

        def slept(_):
            raise AssertionError("waited out a quota error")

        with mock.patch("pubbrain.llm.chat",
                        side_effect=lambda *a, **k: (_ for _ in ()).throw(
                            self._quota_error())):
            with self.assertRaises(requests.HTTPError):
                enrich.enrich_one(
                    self._rec(), "m", "p", network_retries=0,
                    chat=functools.partial(llm.chat_with_backoff, sleep=slept))

    def test_the_default_still_waits_for_the_weekly_job(self):
        slept, calls = [], [self._quota_error(), reply(GOOD)]

        def flaky(messages, **kw):
            out = calls.pop(0)
            if isinstance(out, Exception):
                raise out
            return out

        with mock.patch("pubbrain.llm.chat", side_effect=flaky):
            data, _ = enrich.enrich_one(
                self._rec(), "m", "p",
                chat=functools.partial(llm.chat_with_backoff, sleep=slept.append))
        self.assertEqual(data["summary_one_liner"], GOOD["summary_one_liner"])
        self.assertEqual(len(slept), 1)

