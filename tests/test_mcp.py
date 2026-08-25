"""The MCP tools are what a model sees instead of the database, so the things
worth pinning are the honesty rules, not the plumbing: an absence claim must
carry its limits, a failed query must not read as a negative, and a record with
no summary must not silently disappear.

`create_server` is exercised separately and skips without the SDK; everything
else imports `mcp_server` directly, which needs no SDK by design.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pubbrain import db, embed, mcp_server, paths

REPORT = {
    "slug": "tariff-report", "url": "https://merics.org/en/report/tariff-report",
    "title": "Tariff pressure on Europe", "subtitle": None,
    "date_published": "2025-03-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [{"slug": "a-hmaidi", "name": "Antonia Hmaidi",
                "is_internal": True, "job_title": "Analyst", "role": "author"}],
    "site_tags": ["Trade"],
}
PODCAST = {
    **REPORT, "slug": "ep-12", "url": "https://merics.org/en/podcast/ep-12",
    "title": "Episode 12: chips", "pub_type": "Podcast", "people": [],
    "og_description": "A conversation about semiconductors.", "site_tags": [],
}
ENRICHMENT = {
    "summary_one_liner": "Beijing's tariffs squeeze European carmakers.",
    "summary_short": "A short summary of the tariff piece.",
    "key_findings": ["Tariffs rose"], "entities": {"orgs": ["EU"]},
}
META = {"model": "test-model", "provider": "test", "prompt_version": 1,
        "words_sent": 100}


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        self.report_id = db.upsert_publication(conn, REPORT)
        self.podcast_id = db.upsert_publication(conn, PODCAST)
        db.upsert_text(conn, self.report_id, "Beijing imposed steep tariffs.", 4)
        db.upsert_primary_enrichment(conn, self.report_id, ENRICHMENT, META)
        db.upsert_sitemap_url(conn, "https://merics.org/en/legacy-piece", "",
                              None, "root-level")
        db.rebuild_fts(conn)
        conn.commit()
        conn.close()
        # Tools open their own connection per call, so the path is redirected
        # rather than a connection injected.
        patcher = mock.patch.object(paths, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestHonesty(ToolTest):
    def test_a_negative_carries_its_measured_limits(self):
        out = mcp_server.coverage_check("submarine cables")
        self.assertFalse(out["found"])
        self.assertTrue(out["caveats"])
        self.assertIn("root-level legacy", " ".join(out["caveats"]))
        # graded since #55: the report leads with the measured verdict and
        # must not flatten to a bare negative
        self.assertIn("verdict band", out["how_to_report"].lower())
        self.assertIn("measured", out["verdict"])

    def test_caveats_drop_out_when_the_gap_closes(self):
        """The wording is measured per call precisely so a closed issue stops
        being cited — a hand-written paragraph would go on claiming the gap."""
        conn = db.connect(self.db_path)
        conn.execute("UPDATE sitemap_urls SET status = 'excluded' "
                     "WHERE scope = 'root-level'")
        conn.commit()
        conn.close()
        joined = " ".join(mcp_server.coverage_check("anything")["caveats"])
        self.assertNotIn("root-level legacy", joined)

    def test_a_broken_query_is_an_error_not_an_absence(self):
        """`fts_safe` neutralises most malformed input, but not a caller's own
        unterminated quote. Reporting that as "no coverage" would be the exact
        failure this tool exists to prevent."""
        out = mcp_server.coverage_check('"unclosed')
        self.assertIsNotNone(out["error"])
        self.assertNotIn("found", out)
        self.assertIn("not evidence of absence", out["note"])

    def test_search_reports_syntax_failure_rather_than_empty_results(self):
        out = mcp_server.search('"unclosed')
        self.assertIn("error", out)
        self.assertEqual(out["hits"], [])

    def test_a_hyphenated_term_searches_instead_of_raising(self):
        out = mcp_server.search("de-risking")
        self.assertIsNone(out.get("error"))

    def test_a_question_asked_in_plain_english_searches(self):
        """A model calling these tools types questions, not match expressions.
        Punctuation that is syntax to FTS5 must not cost it the search."""
        for q in ("What about tariffs?", "China's tariffs, 2025."):
            self.assertIsNone(mcp_server.search(q).get("error"), q)
            self.assertIsNone(mcp_server.coverage_check(q).get("error"), q)

    def test_person_answers_carry_the_credit_caveats(self):
        out = mcp_server.person("Hmaidi")
        self.assertEqual(out["name"], "Antonia Hmaidi")
        self.assertIn("Tariff pressure on Europe",
                      [p["title"] for p in out["by_role"]["author"]])
        self.assertTrue(any("#12" in c for c in out["caveats"]))

    def test_every_scoped_tool_states_what_enrichment_misses(self):
        for out in (mcp_server.find("tariffs"), mcp_server.search("tariffs"),
                    mcp_server.list_publications(), mcp_server.status()):
            self.assertIn("Podcasts", out["scope"])


class TestCoverage(ToolTest):
    def test_all_three_probes_report_independently(self):
        out = mcp_server.coverage_check("Trade")
        self.assertTrue(out["found"])
        self.assertTrue(out["site_tags"])          # tag probe hit
        self.assertEqual(out["full_text_matches"], 0)

    def test_a_person_name_counts_as_coverage(self):
        """The query may name an analyst rather than a subject — a probe the
        full-text index alone would answer 'no' to."""
        out = mcp_server.coverage_check("Hmaidi")
        self.assertTrue(out["found"])
        self.assertTrue(out["people_matching_the_term"])

    def test_a_positive_is_reported_as_a_floor(self):
        out = mcp_server.coverage_check("tariffs")
        self.assertTrue(out["found"])
        self.assertIn("floor", out["how_to_report"])


class TestRecordsWithoutSummaries(ToolTest):
    def test_the_podcast_is_listed_despite_having_no_summary(self):
        titles = [p["title"] for p in
                  mcp_server.list_publications()["publications"]]
        self.assertIn("Episode 12: chips", titles)

    def test_a_record_without_a_summary_says_so(self):
        out = mcp_server.publication(self.podcast_id)
        self.assertIsNone(out["enrichment"])
        self.assertIn("no body text", out["note"])

    def test_a_missing_publication_is_an_error_not_an_empty_record(self):
        self.assertIn("error", mcp_server.publication(9999))


class TestFind(ToolTest):
    def test_find_degrades_visibly_when_ollama_is_down(self):
        conn = db.connect(self.db_path)
        db.store_embeddings(conn, "one_liner", embed.MODEL,
                            [{"source_id": self.report_id,
                              "publication_id": self.report_id}],
                            [[0.0] * embed.DIM], embed.pack)
        conn.commit()
        conn.close()
        with mock.patch("pubbrain.embed.embed_query",
                        side_effect=embed.OllamaUnreachable("down")):
            out = mcp_server.find("tariffs")
        self.assertTrue(out["hits"])
        self.assertIn("ollama is not running", " ".join(out["notes"]))

    def test_an_empty_vector_index_is_stated_not_absorbed(self):
        out = mcp_server.find("tariffs")
        self.assertIn("no stored vectors", " ".join(out["notes"]))


class TestBodyTruncation(ToolTest):
    def test_a_long_body_is_cut_and_the_cut_is_declared(self):
        conn = db.connect(self.db_path)
        db.upsert_text(conn, self.report_id, "tariff " * 3000, 3000)
        conn.commit()
        conn.close()
        out = mcp_server.publication(self.report_id)
        self.assertTrue(out["text_truncated"])
        self.assertEqual(len(out["body_text"].split()), mcp_server.BODY_WORD_CAP)

    def test_a_short_body_is_not_marked_truncated(self):
        self.assertFalse(mcp_server.publication(self.report_id)["text_truncated"])


class TestTheOnlyWriteTool(ToolTest):
    def test_flagging_writes_the_row_the_workbench_reads(self):
        out = mcp_server.flag_summary(self.report_id, "too bullish")
        self.assertTrue(out["flagged"])
        conn = db.connect(self.db_path)
        row = conn.execute("SELECT * FROM reviews").fetchone()
        conn.close()
        self.assertEqual(row["verdict"], "flagged")
        self.assertEqual(row["note"], "too bullish")

    def test_flagging_again_replaces_rather_than_stacks(self):
        mcp_server.flag_summary(self.report_id, "first")
        mcp_server.flag_summary(self.report_id, "second")
        conn = db.connect(self.db_path)
        rows = conn.execute("SELECT * FROM reviews").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "second")

    def test_a_record_with_no_summary_cannot_be_flagged(self):
        self.assertIn("error", mcp_server.flag_summary(self.podcast_id, "x"))

    def test_no_other_tool_can_write(self):
        """One write tool is the whole exposure argument for putting this
        behind a tunnel later — a second one added without noticing would
        undo it silently."""
        self.assertEqual([t.__name__ for t in mcp_server.TOOLS
                          if t.__name__ == "flag_summary"], ["flag_summary"])
        conn = db.connect(self.db_path)
        before = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        conn.close()
        for tool, args in [(mcp_server.find, ("x",)), (mcp_server.search, ("x",)),
                           (mcp_server.publication, (self.report_id,)),
                           (mcp_server.person, ("Hmaidi",)),
                           (mcp_server.list_publications, ()),
                           (mcp_server.coverage_check, ("x",)),
                           (mcp_server.status, ()), (mcp_server.glossary, ())]:
            tool(*args)
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
                         before)
        conn.close()


class TestGlossary(ToolTest):
    def test_the_vocabulary_renders_and_admits_it_is_unmapped(self):
        out = mcp_server.glossary()
        names = [t["name"] for c in out["clusters"] for t in c["topics"]]
        self.assertIn("China-Russia", names)
        self.assertIn("Not yet mapped", out["note"])


class TestPersonLookup(ToolTest):
    def test_an_unknown_name_is_an_error(self):
        self.assertIn("error", mcp_server.person("Nobody Here"))

    def test_an_id_works_as_well_as_a_name(self):
        by_name = mcp_server.person("Hmaidi")
        self.assertEqual(mcp_server.person(str(by_name["id"]))["name"],
                         by_name["name"])


class TestStatus(ToolTest):
    def test_a_stale_index_is_warned_about(self):
        conn = db.connect(self.db_path)
        conn.execute("DELETE FROM publication_fts")
        conn.commit()
        conn.close()
        self.assertTrue(any("index-fts" in w
                            for w in mcp_server.status()["warnings"]))


class TestServerRegistration(unittest.TestCase):
    def test_every_tool_registers_with_a_description(self):
        """A tool whose docstring went missing is invisible to the model in the
        way that matters — it still works, it just never gets chosen."""
        try:
            server = mcp_server.create_server()
        except ImportError:
            self.skipTest("mcp SDK not installed")
        registered = {t.name: t for t in server._tool_manager.list_tools()}
        self.assertEqual(set(registered), {t.__name__ for t in mcp_server.TOOLS})
        for name, tool in registered.items():
            self.assertTrue((tool.description or "").strip(), name)


if __name__ == "__main__":
    unittest.main()
