"""The upcoming layer (#56).

Two things are worth pinning here and the second is the reason the layer has
its own tables. First the small arithmetic: status is derived from the two
lifecycle columns, and planned-vs-actual only reports what it can measure.
Second, and load-bearing: **note text is internal knowledge and must not
leave this machine.** It is not published material like everything else in
the catalog, so it may not reach the FTS index, an embedding, an MCP tool
response, or any Insights view — and the tripwires below are written against
a distinctive marker string so a future feature that copies note text
somewhere fails loudly rather than quietly exfiltrating it.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pubbrain import db, mcp_server, paths, queries, topics

# Nothing else in the corpus can produce this, so finding it anywhere outside
# the upcoming_* tables is proof that note text escaped.
MARKER = "zzqqinternalmarker56"      # one bare token: FTS5 reads a hyphen as syntax

REPORT = {
    "slug": "tariff-report", "url": "https://merics.org/en/report/tariff-report",
    "title": "Tariff pressure on Europe", "subtitle": None,
    "date_published": "2025-03-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [{"slug": "a-hmaidi", "name": "Antonia Hmaidi",
                "is_internal": True, "job_title": "Analyst", "role": "author"}],
    "site_tags": ["Trade"],
}
ENRICHMENT = {
    "summary_one_liner": "Beijing's tariffs squeeze European carmakers.",
    "summary_short": "A short summary of the tariff piece.",
    "key_findings": ["Tariffs rose"], "entities": {"orgs": ["EU"]},
}
META = {"model": "test-model", "provider": "test", "prompt_version": 1,
        "words_sent": 100}


class UpcomingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.pub_id = db.upsert_publication(self.conn, REPORT)
        db.upsert_text(self.conn, self.pub_id, "Beijing imposed tariffs.", 3)
        db.upsert_primary_enrichment(self.conn, self.pub_id, ENRICHMENT, META)
        db.replace_topics(self.conn, self.pub_id, topics.slugs()[:2], META)
        db.rebuild_fts(self.conn)
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def note(self, title=f"A report about {MARKER}", **kw):
        kw.setdefault("note", f"heard about it here: {MARKER}")
        return db.add_upcoming_note(self.conn, title, **kw)


class TestLifecycle(UpcomingTest):
    def test_status_is_derived_from_the_lifecycle_columns(self):
        expected = self.note("still coming")
        landed = self.note("landed one")
        shelved = self.note("shelved one")
        db.link_upcoming_note(self.conn, landed, self.pub_id)
        db.shelve_upcoming_note(self.conn, shelved, "the author left")
        by_id = {n["id"]: n["status"]
                 for n in queries.upcoming_notes(self.conn)["open"]
                 + queries.upcoming_notes(self.conn)["closed"]}
        self.assertEqual(by_id[expected], "expected")
        self.assertEqual(by_id[landed], "landed")
        self.assertEqual(by_id[shelved], "shelved")

    def test_a_note_cannot_be_landed_and_shelved_at_once(self):
        note_id = self.note()
        db.shelve_upcoming_note(self.conn, note_id, "not happening")
        # The CHECK is the backstop; the helpers refuse first, with a reason.
        with self.assertRaises(ValueError):
            db.link_upcoming_note(self.conn, note_id, self.pub_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE upcoming_notes SET landed_publication_id = ? WHERE id = ?",
                (self.pub_id, note_id))

    def test_reopening_undoes_a_shelve_or_a_link(self):
        """Both are one click on a repeated row, so both must be reversible —
        otherwise the only fix is hand-written SQL."""
        note_id = self.note()
        db.link_upcoming_note(self.conn, note_id, self.pub_id)
        db.reopen_upcoming_note(self.conn, note_id)
        self.assertEqual(queries.upcoming_notes(self.conn)["open"][0]["id"], note_id)

    def test_a_quarter_finer_than_a_quarter_is_refused(self):
        """Storing a month would fabricate precision the owner does not have."""
        for bad in ("2026-11", "2026-11-04", "Q4", "2026-Q5"):
            with self.assertRaises(ValueError, msg=bad):
                self.note(expected=bad)
        self.assertIsNone(db._quarter("  "))

    def test_topics_are_capped_and_validated_against_the_vocabulary(self):
        note_id = self.note(topic_slugs=topics.slugs()[:5])
        self.assertEqual(len(db.upcoming_note_topics(self.conn, [note_id])[note_id]),
                         db.UPCOMING_MAX_TOPICS)
        with self.assertRaises(ValueError):
            self.note("bad topic", topic_slugs=["not-a-topic"])

    def test_editing_keeps_created_at_because_lead_time_is_measured_from_it(self):
        note_id = self.note("first wording")
        before = db.upcoming_note(self.conn, note_id)["created_at"]
        db.edit_upcoming_note(self.conn, note_id, "second wording")
        after = db.upcoming_note(self.conn, note_id)
        self.assertEqual(after["created_at"], before)
        self.assertEqual(after["working_title"], "second wording")


class TestPlannedAgainstActual(UpcomingTest):
    def landed(self, slug, date, created, expected):
        pub_id = db.upsert_publication(self.conn, dict(
            REPORT, slug=slug, url=f"https://merics.org/en/report/{slug}",
            title=slug, date_published=date))
        note_id = self.note(slug, expected=expected)
        db.link_upcoming_note(self.conn, note_id, pub_id)
        self.conn.execute("UPDATE upcoming_notes SET created_at = ? WHERE id = ?",
                          (created, note_id))
        return note_id

    def test_nothing_is_reported_below_three_landings(self):
        """A hit rate over two notes is a coin toss wearing a percentage."""
        self.landed("a", "2026-03-01", "2026-01-01", "2026-Q1")
        self.landed("b", "2026-06-01", "2026-01-01", "2026-Q2")
        self.assertIsNone(queries.upcoming_notes(self.conn)["stats"])

    def test_lead_time_and_hit_rate_on_known_dates(self):
        self.landed("a", "2026-03-01", "2026-01-01", "2026-Q1")   # 59d, right
        self.landed("b", "2026-06-01", "2026-05-02", "2026-Q1")   # 30d, wrong
        self.landed("c", "2026-09-01", "2026-06-01", "2026-Q3")   # 92d, right
        self.note("shelved one")
        db.shelve_upcoming_note(
            self.conn, queries.upcoming_notes(self.conn)["open"][0]["id"], "dropped")
        stats = queries.upcoming_notes(self.conn)["stats"]
        self.assertEqual(stats["median_lead_days"], 59)
        self.assertEqual((stats["on_time"], stats["judged"]), (2, 3))
        self.assertEqual(stats["shelved"], 1)

    def test_a_note_written_after_the_piece_is_not_counted_as_foresight(self):
        """Backfilling a note for something already published is legitimate —
        it is just not evidence about seeing things coming, and a negative
        lead would drag the median below zero."""
        self.landed("a", "2026-03-01", "2026-01-01", "2026-Q1")   # 59d
        self.landed("b", "2026-06-01", "2026-04-02", "2026-Q2")   # 60d
        self.landed("c", "2026-02-01", "2026-06-01", "2026-Q1")   # noted after
        stats = queries.upcoming_notes(self.conn)["stats"]
        self.assertEqual(stats["lead_sample"], 2)
        self.assertEqual(stats["median_lead_days"], 59.5)


class TestPrivacyTripwires(UpcomingTest):
    """Note text is internal knowledge. These are the tests that fail when it
    stops being internal."""

    def test_the_marker_lives_only_in_the_upcoming_tables(self):
        """A whole-database sweep, not a targeted check: the failure this
        guards against is a *future* feature copying note text into a table
        nobody thought to test. Blobs are searched too, which is what catches
        the FTS shadow tables."""
        note_id = self.note(topic_slugs=topics.slugs()[:1],
                            person_ids=[self.conn.execute(
                                "SELECT id FROM people").fetchone()["id"]])
        db.rebuild_fts(self.conn)
        db.rebuild_section_fts(self.conn)
        self.conn.commit()
        needle = MARKER.encode()
        leaks = []
        for table in [r["name"] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")]:
            if table.startswith("upcoming_"):
                continue
            for row in self.conn.execute(f"SELECT * FROM \"{table}\""):
                for value in tuple(row):
                    if isinstance(value, str) and MARKER in value:
                        leaks.append(table)
                    elif isinstance(value, bytes) and needle in value:
                        leaks.append(table)
        self.assertEqual(leaks, [], f"note text reached {set(leaks)}")
        # …and it really is stored, so the sweep is not passing on an empty DB.
        self.assertIn(MARKER, db.upcoming_note(self.conn, note_id)["working_title"])

    def test_keyword_search_cannot_find_a_note(self):
        self.note()
        db.rebuild_fts(self.conn)
        self.conn.commit()
        self.assertEqual(db.search(self.conn, MARKER), [])
        hits, _ = queries.hybrid_find(self.conn, MARKER, with_vectors=False)
        self.assertEqual(hits, [])

    def test_no_mcp_tool_response_contains_note_text(self):
        """The MCP server feeds LLM conversations and #48 wants it remote one
        day, so it is the exfiltration path that matters most."""
        self.note(topic_slugs=topics.slugs()[:1])
        db.rebuild_fts(self.conn)
        self.conn.commit()
        with mock.patch.object(paths, "DB_PATH", self.db_path):
            # Searching *for* the marker echoes the query back, so those two
            # are checked for results rather than swept for the string.
            self.assertEqual(mcp_server.search(MARKER)["hits"], [])
            self.assertFalse(mcp_server.coverage_check(MARKER)["found"])
            responses = [
                mcp_server.search("report"),
                mcp_server.publication(self.pub_id),
                mcp_server.person("Hmaidi"),
                mcp_server.list_publications(),
                mcp_server.coverage_check("tariffs"),
                mcp_server.status(),
                mcp_server.glossary(),
                mcp_server.flag_summary(self.pub_id, "a flag"),
            ]
        # Every read tool is covered; `find` needs ollama and is exercised by
        # the sweep above, which reaches whatever it would rank over.
        covered = {"search", "publication", "person", "list_publications",
                   "coverage_check", "status", "glossary", "flag_summary", "find"}
        self.assertEqual({t.__name__ for t in mcp_server.TOOLS} - covered, set())
        for response in responses:
            self.assertNotIn(MARKER, json.dumps(response, default=str))

    def test_insights_are_unchanged_by_the_presence_of_notes(self):
        """`_insight_scope` names publications and nothing else; this is the
        assertion that the views built on it stayed that way."""
        before = (queries.topic_graph(self.conn), queries.topic_time(self.conn),
                  queries.who_knows_what(self.conn))
        self.note(topic_slugs=topics.slugs()[:2],
                  person_ids=[self.conn.execute(
                      "SELECT id FROM people").fetchone()["id"]])
        self.conn.commit()
        after = (queries.topic_graph(self.conn), queries.topic_time(self.conn),
                 queries.who_knows_what(self.conn))
        self.assertEqual(json.dumps(before, default=str),
                         json.dumps(after, default=str))

    def test_notes_are_not_embeddable(self):
        """Nothing offers note text to the embedder: its worklists are keyed
        on publications, and a local vector would still be a copy of the text
        in a table the sweep above cannot read."""
        self.note()
        self.conn.commit()
        for source in ("section", "one_liner"):
            pending = db.pending_embeddings(self.conn, source, "test-model")
            self.assertNotIn(MARKER, json.dumps([dict(r) for r in pending],
                                                default=str))


class TestInsightsSurfacing(UpcomingTest):
    """The timeline's leading edge and the map's dashed rings (#56). Both read
    one dedicated endpoint; the published-record endpoints stay clean, which is
    what `test_insights_are_unchanged_by_the_presence_of_notes` holds."""

    def test_only_open_notes_reach_the_edge(self):
        """A landed note belongs to the published record and a shelved one
        never happened — neither is something the institute is about to say."""
        open_id = self.note("still coming", expected="2026-Q4")
        landed = self.note("landed one", expected="2026-Q4")
        shelved = self.note("shelved one", expected="2026-Q4")
        db.link_upcoming_note(self.conn, landed, self.pub_id)
        db.shelve_upcoming_note(self.conn, shelved, "dropped")
        edge = queries.upcoming_edge(self.conn, today="2026-08-10")
        self.assertEqual([n["id"] for n in edge["notes"]], [open_id])

    def test_the_axis_reaches_the_furthest_note(self):
        """Otherwise a note past the next quarter is drawn nowhere and the
        view under-reports what is coming without saying so."""
        self.note("far out", expected="2027-Q2")
        edge = queries.upcoming_edge(self.conn, today="2026-08-10")
        self.assertEqual(edge["edge"], ["2026-Q3", "2027-Q2"])

    def test_a_note_past_the_horizon_is_counted_not_dropped(self):
        self.note("next year", expected="2026-Q4")
        self.note("one day", expected="2031-Q1")
        edge = queries.upcoming_edge(self.conn, today="2026-08-10")
        self.assertEqual(edge["edge"], ["2026-Q3", "2026-Q4"])
        self.assertEqual(edge["beyond"], 1)

    def test_an_undated_note_counts_for_the_map_but_not_the_axis(self):
        """It has a topic, so the ring is honest; it has no quarter, so the
        timeline has nowhere to put it."""
        self.note("no idea when", topic_slugs=topics.slugs()[:1])
        edge = queries.upcoming_edge(self.conn, today="2026-08-10")
        self.assertEqual(edge["by_topic"][topics.slugs()[0]], 1)
        self.assertEqual(edge["edge"], ["2026-Q3", "2026-Q4"])

    def test_the_published_endpoints_carry_no_note_text(self):
        from pubbrain.web import create_app
        self.note(topic_slugs=topics.slugs()[:1], expected="2026-Q4")
        self.conn.commit()
        app = create_app(self.db_path)
        app.testing = True
        client = app.test_client()
        for url in ("/insights/data/topic-time.json",
                    "/insights/data/topic-graph.json"):
            self.assertNotIn(MARKER, client.get(url).get_data(as_text=True), url)
        edge = client.get("/insights/data/upcoming-edge.json")
        self.assertIn(MARKER, edge.get_data(as_text=True))


class TestWorkbench(UpcomingTest):
    def setUp(self):
        super().setUp()
        from pubbrain.web import create_app
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def test_a_note_can_be_added_edited_and_linked_from_the_page(self):
        self.conn.commit()
        self.client.post("/upcoming/new", data={
            "working_title": "Overcapacity report", "note": "from the meeting",
            "expected": "2026-Q4", "topic": [topics.slugs()[0], "", ""]})
        page = self.client.get("/upcoming/").get_data(as_text=True)
        self.assertIn("Overcapacity report", page)
        self.assertIn("2026-Q4", page)

        note_id = db.connect(self.db_path).execute(
            "SELECT id FROM upcoming_notes").fetchone()["id"]
        self.client.post(f"/upcoming/{note_id}/edit", data={
            "working_title": "Overcapacity report, second edition",
            "expected": "", "topic": ["", "", ""]})
        self.client.post(f"/upcoming/{note_id}/link",
                         data={"publication_id": str(self.pub_id)})
        page = self.client.get("/upcoming/").get_data(as_text=True)
        self.assertIn("Tariff pressure on Europe", page)     # what it landed as
        self.assertIn("Overcapacity report, second edition", page)

    def test_shelving_without_a_reason_is_refused(self):
        self.conn.commit()
        self.client.post("/upcoming/new", data={"working_title": "A thing"})
        note_id = db.connect(self.db_path).execute(
            "SELECT id FROM upcoming_notes").fetchone()["id"]
        page = self.client.post(f"/upcoming/{note_id}/shelve", data={"reason": " "},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("shelving takes a reason", page)

    def test_the_quarter_select_keeps_a_quarter_that_has_passed(self):
        """Editing a note filed under an old quarter must not silently re-file
        it: a stored value missing from the select submits something else."""
        self.conn.execute(
            "INSERT INTO upcoming_notes (working_title, expected, created_at, "
            "updated_at) VALUES ('old one', '2019-Q1', '2019-01-01', '2019-01-01')")
        self.conn.commit()
        note_id = db.connect(self.db_path).execute(
            "SELECT id FROM upcoming_notes").fetchone()["id"]
        page = self.client.get(f"/upcoming/?edit={note_id}").get_data(as_text=True)
        self.assertIn('value="2019-Q1" selected', page)


if __name__ == "__main__":
    unittest.main()
