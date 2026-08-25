"""The topic map draws what the mapping trusts (#49).

Edges count co-assignment at position <= 2 only — the tail of a multi-story
Brief is stretched from single clauses, and counting it gave 307 of 325
possible pairs. Podcasts and chapters are excluded from every Insights
number, the same scope rule stated once on the hub.
"""

import tempfile
import unittest
from pathlib import Path

from pubbrain import db, queries, topics
from pubbrain.web import create_app

BASE = {
    "slug": "s", "url": "u", "title": "T", "subtitle": None,
    "date_published": "2025-01-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "site_tags": [], "people": [],
}


class TestTopicGraph(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        # Three real vocabulary slugs — synthetic ones would be dropped.
        self.t1, self.t2, self.t3 = list(topics.labels())[:3]

    def add(self, slug, pub_type, topic_positions, parent_id=None):
        pid = db.upsert_publication(self.conn, {
            **BASE, "slug": slug, "url": f"u/{slug}", "pub_type": pub_type})
        if parent_id:
            self.conn.execute(
                "UPDATE publications SET parent_id = ? WHERE id = ?",
                (parent_id, pid))
        for topic_slug, pos in topic_positions:
            self.conn.execute(
                "INSERT INTO publication_topics "
                "(publication_id, topic_slug, position, model, prompt_version, "
                " mapped_at) VALUES (?, ?, ?, 'test', 1, '2025')",
                (pid, topic_slug, pos))
        return pid

    def graph(self):
        return queries.topic_graph(self.conn)

    def node(self, slug):
        return next((n for n in self.graph()["nodes"] if n["slug"] == slug), None)

    def test_weights_follow_type_not_row_count(self):
        self.add("r", "Report", [(self.t1, 1)])
        self.add("c", "Comment", [(self.t2, 1)])
        self.assertEqual(self.node(self.t1)["weighted"], 8)
        self.assertEqual(self.node(self.t2)["weighted"], 1)

    def test_tail_positions_make_no_edges(self):
        """The hairball guard: positions 3-4 count for the node, not the edge."""
        self.add("a", "Report", [(self.t1, 1), (self.t2, 3)])
        self.add("b", "Report", [(self.t1, 1), (self.t2, 4)])
        self.assertEqual(self.graph()["edges"], [])
        self.assertEqual(self.node(self.t2)["pubs"], 2)   # still a node input

    def test_trusted_positions_do_make_an_edge(self):
        self.add("a", "Report", [(self.t1, 1), (self.t2, 2)])
        self.add("b", "Comment", [(self.t2, 1), (self.t1, 2)])
        edges = self.graph()["edges"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["n"], 2)

    def test_a_single_shared_publication_is_below_the_floor(self):
        self.add("a", "Report", [(self.t1, 1), (self.t2, 2)])
        self.assertEqual(self.graph()["edges"], [])

    def test_podcasts_are_excluded_everywhere(self):
        self.add("p", "Podcast", [(self.t1, 1), (self.t2, 2)])
        self.add("q", "Podcast", [(self.t1, 1), (self.t2, 2)])
        self.assertIsNone(self.node(self.t1))
        self.assertEqual(self.graph()["edges"], [])

    def test_chapters_count_through_their_parent_meaning_not_at_all(self):
        parent = self.add("r", "Report", [(self.t1, 1)])
        self.add("ch", "Report", [(self.t1, 1), (self.t3, 2)], parent_id=parent)
        self.assertEqual(self.node(self.t1)["pubs"], 1)
        self.assertIsNone(self.node(self.t3))

    def test_about_counts_first_rank_only(self):
        self.add("a", "Report", [(self.t1, 1), (self.t2, 2)])
        self.assertEqual(self.node(self.t1)["about"], 1)
        self.assertEqual(self.node(self.t2)["about"], 0)

    def test_spotlight_is_about_not_touches(self):
        self.add("a", "Report", [(self.t1, 1)])
        self.add("b", "Report", [(self.t1, 2), (self.t2, 1)])
        titles = [r["title"] for r in queries.topic_spotlight(self.conn, self.t1)]
        self.assertEqual(len(titles), 1)

    def test_pair_lists_the_intersection(self):
        self.add("a", "Report", [(self.t1, 1), (self.t2, 2)])
        self.add("b", "Report", [(self.t1, 1)])
        rows = queries.topic_pair(self.conn, self.t1, self.t2)
        self.assertEqual(len(rows), 1)

    def test_routes_serve(self):
        self.add("a", "Report", [(self.t1, 1), (self.t2, 2)])
        self.conn.commit()
        app = create_app(self.db_path)
        with app.test_client() as client:
            self.assertEqual(client.get("/insights/").status_code, 200)
            self.assertEqual(client.get("/insights/map").status_code, 200)
            graph = client.get("/insights/data/topic-graph.json").get_json()
            self.assertTrue(graph["nodes"])
            spot = client.get(
                f"/insights/data/topic.json?slug={self.t1}").get_json()
            self.assertEqual(len(spot["publications"]), 1)


class TestTopicTime(TestTopicGraph):
    """Reuses the fixture helpers; tests the quarterly series (#49)."""

    def test_primary_only_and_quarter_bins(self):
        self.add("a", "Report", [(self.t1, 1)])
        self.conn.execute("UPDATE publications SET date_published = '2024-02-10' "
                          "WHERE slug = 'a'")
        self.add("b", "Comment", [(self.t1, 2), (self.t2, 1)])
        self.conn.execute("UPDATE publications SET date_published = '2024-11-01' "
                          "WHERE slug = 'b'")
        out = queries.topic_time(self.conn)
        self.assertEqual(out["quarters"][0], "2024-Q1")
        self.assertEqual(out["quarters"][-1], "2024-Q4")
        t1 = next(t for t in out["topics"] if t["slug"] == self.t1)
        # touches (position 2) never count here
        self.assertEqual(sum(t1["n"]), 1)
        self.assertEqual(t1["w"][0], 8)

    def test_axis_is_contiguous_with_real_zeros(self):
        self.add("a", "Report", [(self.t1, 1)])
        self.conn.execute("UPDATE publications SET date_published = '2023-01-01' "
                          "WHERE slug = 'a'")
        self.add("b", "Report", [(self.t1, 1)])
        self.conn.execute("UPDATE publications SET date_published = '2024-01-01' "
                          "WHERE slug = 'b'")
        out = queries.topic_time(self.conn)
        self.assertEqual(len(out["quarters"]), 5)   # 23Q1..24Q1, nothing skipped
        t1 = next(t for t in out["topics"] if t["slug"] == self.t1)
        self.assertEqual(t1["n"], [1, 0, 0, 0, 1])

    def test_podcasts_and_chapters_stay_out(self):
        parent = self.add("r", "Report", [(self.t1, 1)])
        self.add("pod", "Podcast", [(self.t1, 1)])
        self.add("ch", "Report", [(self.t1, 1)], parent_id=parent)
        out = queries.topic_time(self.conn)
        t1 = next(t for t in out["topics"] if t["slug"] == self.t1)
        self.assertEqual(sum(t1["n"]), 1)

    def test_route_serves(self):
        self.add("a", "Report", [(self.t1, 1)])
        self.conn.commit()
        app = create_app(self.db_path)
        with app.test_client() as client:
            self.assertEqual(client.get("/insights/time").status_code, 200)
            data = client.get("/insights/data/topic-time.json").get_json()
            self.assertTrue(data["quarters"])


class TestKeywordTime(TestTopicGraph):
    """The drill-down freed from the vocabulary (#49): any term, two scopes."""

    def seed(self):
        self.add("head", "Report", [])
        self.conn.execute("UPDATE publications SET title = 'Beidaihe watch', "
                          "date_published = '2024-08-01' WHERE slug = 'head'")
        self.add("body", "Comment", [])
        self.conn.execute("UPDATE publications SET date_published = '2025-02-01' "
                          "WHERE slug = 'body'")
        bid = self.conn.execute(
            "SELECT id FROM publications WHERE slug = 'body'").fetchone()[0]
        self.conn.execute(
            "INSERT INTO publication_text (publication_id, body, word_count, "
            "source, extracted_at) VALUES (?, 'Leaders met at Beidaihe again.', "
            "5, 'html', '2025')", (bid,))
        db.rebuild_fts(self.conn)

    def test_about_scope_is_headline_and_summary_only(self):
        self.seed()
        about = queries.keyword_time(self.conn, "Beidaihe")
        deep = queries.keyword_time(self.conn, "Beidaihe", deep=True)
        self.assertEqual(about["total"], 1)
        self.assertEqual(deep["total"], 2)

    def test_summaries_count_as_about(self):
        self.seed()
        pid = self.conn.execute(
            "SELECT id FROM publications WHERE slug = 'body'").fetchone()[0]
        self.conn.execute(
            "INSERT INTO publication_enrichment (publication_id, is_primary, "
            "summary_one_liner, summary_short, key_findings, entities, model, "
            "provider, prompt_version, words_sent, enriched_at) "
            "VALUES (?, 1, 'Beidaihe signals', 's', '[]', '{}', 'm', 'p', 1, "
            "5, '2025')", (pid,))
        self.assertEqual(queries.keyword_time(self.conn, "Beidaihe")["total"], 2)

    def test_axis_spans_the_catalog_not_the_hits(self):
        self.seed()
        out = queries.keyword_time(self.conn, "Beidaihe")
        self.assertEqual(out["quarters"][0], "2024-Q3")
        self.assertEqual(out["quarters"][-1], "2025-Q1")
        self.assertEqual(sum(out["n"]), 1)

    def test_a_chapter_hit_counts_as_its_parent_once(self):
        parent = self.add("rep", "Report", [])
        self.conn.execute("UPDATE publications SET date_published = "
                          "'2024-01-01' WHERE id = ?", (parent,))
        for i in (1, 2):
            cid = self.add(f"ch{i}", "Report", [], parent_id=parent)
            self.conn.execute(
                "INSERT INTO publication_text (publication_id, body, "
                "word_count, source, extracted_at) "
                "VALUES (?, 'gallium controls chapter', 3, 'html', '2025')",
                (cid,))
        db.rebuild_fts(self.conn)
        out = queries.keyword_time(self.conn, "gallium", deep=True)
        self.assertEqual(out["total"], 1)         # one report, not two chapters

    def test_podcasts_stay_out(self):
        self.add("pod", "Podcast", [])
        self.conn.execute("UPDATE publications SET title = 'Beidaihe podcast' "
                          "WHERE slug = 'pod'")
        db.rebuild_fts(self.conn)
        self.assertEqual(queries.keyword_time(self.conn, "Beidaihe")["total"], 0)

    def test_hostile_query_is_safe(self):
        self.seed()
        out = queries.keyword_time(self.conn, 'de-risking? AND "')
        self.assertEqual(out["total"], 0)

    def test_route_serves(self):
        self.seed()
        self.conn.commit()
        app = create_app(self.db_path)
        with app.test_client() as client:
            d = client.get(
                "/insights/data/keyword-time.json?q=Beidaihe&deep=1").get_json()
            self.assertEqual(d["total"], 2)


class TestWhoKnowsWhat(TestTopicGraph):
    """Analyst x topic (#49): primary-topic credits, honest rows."""

    def person(self, slug_, name, role="author"):
        return {"slug": slug_, "name": name, "is_internal": True,
                "job_title": None, "role": role}

    def test_primary_only_and_roles(self):
        pid = self.add("a", "Report", [(self.t1, 1), (self.t2, 2)])
        db.upsert_publication(self.conn, {
            **BASE, "slug": "a", "url": "u/a",
            "people": [self.person("ana", "Ana Analyst"),
                       self.person("hosty", "Hosty Host", role="host")]})
        for ts, pos in [(self.t1, 1), (self.t2, 2)]:
            self.conn.execute(
                "INSERT OR IGNORE INTO publication_topics (publication_id, "
                "topic_slug, position, model, prompt_version, mapped_at) "
                "VALUES (?, ?, ?, 'test', 1, '2025')", (pid, ts, pos))
        out = queries.who_knows_what(self.conn, current_only=False)
        names = [p["name"] for p in out["people"]]
        self.assertIn("Ana Analyst", names)
        self.assertNotIn("Hosty Host", names)          # hosting is not output
        ana = out["people"][names.index("Ana Analyst")]
        self.assertEqual(ana["cells"], {self.t1: 1})   # primary only
        self.assertEqual([t["slug"] for t in out["topics"]], [self.t1])

    def test_current_filter_and_totals(self):
        self.add("a", "Report", [(self.t1, 1)])
        db.upsert_publication(self.conn, {
            **BASE, "slug": "a", "url": "u/a",
            "people": [self.person("ana", "Ana Analyst")]})
        self.conn.execute("UPDATE people SET is_current = 0")
        self.assertEqual(queries.who_knows_what(self.conn)["people"], [])
        out = queries.who_knows_what(self.conn, current_only=False)
        self.assertEqual(out["people"][0]["credits_total"], 1)

    def test_tombstones_never_appear(self):
        pid = self.add("a", "Report", [(self.t1, 1)])
        db.upsert_publication(self.conn, {
            **BASE, "slug": "a", "url": "u/a",
            "people": [self.person("ana", "Ana Analyst")]})
        dup = db.add_person(self.conn, "Ana Duplicate")
        ana = self.conn.execute(
            "SELECT id FROM people WHERE slug = 'ana'").fetchone()[0]
        db.credit_person(self.conn, pid, dup)
        db.merge_person(self.conn, dup, ana)
        out = queries.who_knows_what(self.conn, current_only=False)
        self.assertEqual(len(out["people"]), 1)

    def test_route_serves(self):
        self.add("a", "Report", [(self.t1, 1)])
        db.upsert_publication(self.conn, {
            **BASE, "slug": "a", "url": "u/a",
            "people": [self.person("ana", "Ana Analyst")]})
        self.conn.commit()
        app = create_app(self.db_path)
        with app.test_client() as client:
            self.assertEqual(client.get("/insights/matrix").status_code, 200)
            d = client.get("/insights/data/matrix.json?all=1").get_json()
            self.assertEqual(len(d["people"]), 1)


if __name__ == "__main__":
    unittest.main()
