"""Topic mapping (#4). A topic outside the frozen vocabulary is not a
formatting slip — it lands in the table, matches no glossary entry and appears
in no filter, and nothing downstream would ever say so. So the gate and the
slug-keyed storage are what these tests pin down.
"""

import json
import sqlite3
import unittest

from pubbrain import db, topicmap, topics

REC = {
    "id": 1, "title": "Chip controls", "subtitle": None,
    "pub_type": "Report", "date_published": "2025-06-01",
    "summary_one_liner": "Beijing's chip subsidies outpace export controls.",
    "summary_short": "A short summary of the argument.",
    "key_findings": json.dumps(["Subsidies rose", "Controls lag"]),
}
META = {"model": "test-model", "prompt_version": 1}


def reply(data, **kw):
    return {"content": json.dumps(data) if isinstance(data, dict) else data,
            "prompt_tokens": 100, "completion_tokens": 20, "seconds": 0.5,
            "finish_reason": "stop", "model": "test-model", **kw}


class TestValidate(unittest.TestCase):
    def test_slugs_from_the_vocabulary_pass(self):
        self.assertEqual(
            topicmap.validate({"topics": ["semiconductors", "china-us"]}), [])

    def test_an_invented_slug_is_rejected_and_named(self):
        problems = topicmap.validate({"topics": ["semiconductors", "covid-19"]})
        self.assertEqual(len(problems), 1)
        self.assertIn("covid-19", problems[0])

    def test_a_display_name_is_not_a_slug(self):
        """The likeliest model error: answering with the label it was shown
        beside the slug. It must fail rather than store an unmatchable string."""
        self.assertTrue(topicmap.validate({"topics": ["Semiconductors"]}))

    def test_empty_and_overlong_and_repeated_are_caught(self):
        self.assertTrue(topicmap.validate({"topics": []}))
        self.assertTrue(topicmap.validate(
            {"topics": topics.slugs()[:topicmap.MAX_TOPICS + 1]}))
        self.assertTrue(topicmap.validate({"topics": ["taiwan", "taiwan"]}))

    def test_malformed_shapes_are_caught(self):
        self.assertTrue(topicmap.validate(None))
        self.assertTrue(topicmap.validate({"topics": "taiwan"}))
        self.assertTrue(topicmap.validate({}))

    def test_json_inside_a_code_fence_is_still_read(self):
        data = {"topics": ["taiwan"]}
        self.assertEqual(topicmap.parse(f"```json\n{json.dumps(data)}\n```"), data)
        self.assertIsNone(topicmap.parse("no object here"))

    def test_every_vocabulary_slug_appears_in_the_prompt(self):
        """The model can only return what it was shown."""
        block = topicmap.build_system()
        for slug in topics.slugs():
            self.assertIn(slug, block)


class TestMapOne(unittest.TestCase):
    def test_a_bad_answer_is_re_asked_with_the_problem(self):
        seen = []

        def chat(messages, **kw):
            seen.append(messages[-1]["content"])
            if len(seen) == 1:
                return reply({"topics": ["not-a-topic"]})
            return reply({"topics": ["taiwan"]})

        slugs, meta = topicmap.map_one(REC, "m", "p", chat=chat)
        self.assertEqual(slugs, ["taiwan"])
        self.assertEqual(meta["attempts"], 2)
        self.assertIn("not-a-topic", seen[1])

    def test_it_raises_rather_than_storing_an_unusable_mapping(self):
        with self.assertRaises(topicmap.Invalid):
            topicmap.map_one(REC, "m", "p", attempts=2,
                             chat=lambda m, **kw: reply({"topics": ["nope"]}))

    def test_tokens_accumulate_across_attempts(self):
        _, meta = topicmap.map_one(
            REC, "m", "p", chat=lambda m, **kw: reply({"topics": ["taiwan"]}))
        self.assertEqual(meta["prompt_tokens"], 100)
        self.assertEqual(meta["prompt_version"], topicmap.PROMPT_VERSION)


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        # Before any statement opens a transaction — inside one it is ignored,
        # and the cascade test would then pass against nothing.
        self.conn.execute("PRAGMA foreign_keys = ON")
        db.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO publications (id, slug, url, title, pub_type, scraped_at) "
            "VALUES (1, 's', 'u', 'Chip controls', 'Report', '2025-01-01')")
        db.upsert_primary_enrichment(
            self.conn, 1,
            {"summary_one_liner": "one", "summary_short": "short",
             "key_findings": [], "entities": {}},
            {"model": "m", "provider": "p", "prompt_version": 1, "words_sent": 10})

    def test_position_records_the_models_ranking(self):
        db.replace_topics(self.conn, 1, ["semiconductors", "china-us"], META)
        self.assertEqual(db.topics_for_publications(self.conn, [1])[1],
                         ["semiconductors", "china-us"])

    def test_remapping_replaces_rather_than_accumulates(self):
        db.replace_topics(self.conn, 1, ["semiconductors", "china-us"], META)
        db.replace_topics(self.conn, 1, ["taiwan"], META)
        self.assertEqual(db.topics_for_publications(self.conn, [1])[1], ["taiwan"])
        self.assertEqual(db.topic_counts(self.conn), {"taiwan": 1})

    def test_primary_only_counts_the_first_topic(self):
        db.replace_topics(self.conn, 1, ["semiconductors", "china-us"], META)
        self.assertEqual(db.topic_counts(self.conn, primary_only=True),
                         {"semiconductors": 1})

    def test_the_worklist_skips_what_is_mapped_and_remap_returns_it(self):
        self.assertEqual([r["id"] for r in db.pending_topic_mapping(self.conn)], [1])
        db.replace_topics(self.conn, 1, ["taiwan"], META)
        self.assertEqual(db.pending_topic_mapping(self.conn), [])
        self.assertEqual(
            [r["id"] for r in db.pending_topic_mapping(self.conn, remap=True)], [1])

    def test_a_publication_with_no_summary_is_never_in_the_worklist(self):
        """Podcasts have no enrichment row, so this path cannot reach them —
        the topic surfaces must state that scope rather than imply coverage."""
        self.conn.execute(
            "INSERT INTO publications (id, slug, url, title, pub_type, scraped_at) "
            "VALUES (2, 's2', 'u2', 'A podcast', 'Podcast', '2025-01-01')")
        self.assertEqual([r["id"] for r in db.pending_topic_mapping(self.conn)], [1])

    def test_deleting_a_publication_takes_its_topics(self):
        db.replace_topics(self.conn, 1, ["taiwan"], META)
        self.conn.execute("DELETE FROM publication_enrichment WHERE publication_id = 1")
        self.conn.execute("DELETE FROM publications WHERE id = 1")
        self.assertEqual(db.topic_counts(self.conn), {})


class TestVocabularyIdentity(unittest.TestCase):
    def test_slugs_are_unique_and_url_safe(self):
        slugs = topics.slugs()
        self.assertEqual(len(slugs), len(topics.names()))
        self.assertEqual(len(slugs), len(set(slugs)))
        for s in slugs:
            self.assertRegex(s, r"^[a-z0-9-]+$")

    def test_a_duplicate_slug_fails_loudly(self):
        """Against a copy, never the real file. Mutating `topics.yaml` in place
        is visible to any concurrently running process — an earlier version of
        this test corrupted one record of a live `map-topics` run — and leaves
        the repo dirty if the restore never happens."""
        import tempfile
        from pathlib import Path
        from unittest import mock
        original = topics.PATH.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "topics.yaml"
            broken.write_text(original.replace("slug: taiwan", "slug: china-us"),
                              encoding="utf-8")
            with mock.patch.object(topics, "PATH", broken):
                with self.assertRaises(ValueError):
                    topics.load()
        # the real vocabulary is untouched and still loads
        self.assertEqual(len(topics.slugs()), 26)


if __name__ == "__main__":
    unittest.main()
