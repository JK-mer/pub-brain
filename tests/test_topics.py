"""The vocabulary file must stay well-formed — the glossary renders it and
enrichment validates against it, so a malformed edit should fail loudly here."""

import unittest

from pubbrain import topics


class TestTopics(unittest.TestCase):
    def test_vocabulary_loads_and_is_complete(self):
        clusters = topics.load()
        self.assertGreaterEqual(len(clusters), 4)
        for c in clusters:
            self.assertTrue(c["cluster"])
            self.assertTrue(c["topics"])

    def test_names_are_unique_and_panel_sized(self):
        names = topics.names()
        self.assertEqual(len(names), len(set(names)))
        # the #4 target band: ~20-30 topics
        self.assertGreaterEqual(len(names), 20)
        self.assertLessEqual(len(names), 30)

    def test_every_topic_carries_both_blurbs(self):
        for c in topics.load():
            for t in c["topics"]:
                self.assertTrue(t["entails"].strip(), t["name"])
                self.assertTrue(t["why"].strip(), t["name"])


if __name__ == "__main__":
    unittest.main()
