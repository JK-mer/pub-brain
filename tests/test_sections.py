"""Splitting is deterministic, so the risk is not crashes but silent wrongness:
sections that drop prose, or a digest misread as a single argument.
"""

import sqlite3
import unittest

from pubbrain import db, sections

BRIEF_BODY = """Intro paragraph that belongs to nobody in particular and runs
long enough to survive the minimum-length filter applied to every section.

## Xi grants North Korea his first official visit

Body of the first story, with enough words in it to clear the threshold that
drops bare labels from the section list entirely.

## China's export control researchers eye technology

Body of the second story, also long enough to be kept as a real section rather
than discarded as a heading with nothing underneath it.

### METRIX

A standing feature rather than a story, but still long enough to be kept as its
own section for indexing purposes here.
"""

PUB = {
    "slug": "brief-1", "url": "https://merics.org/en/merics-briefs/brief-1",
    "title": "North Korea visit + Export controls + Hukou reform",
    "subtitle": None, "date_published": "2026-06-12", "pub_type": "MERICS Briefs",
    "series": None, "access": "public", "pdf_url": None,
    "og_description": None, "people": [], "site_tags": [],
}


class TestSplit(unittest.TestCase):
    def test_text_before_the_first_heading_is_kept(self):
        """Otherwise a publication's opening paragraphs vanish from the index."""
        out = sections.split(BRIEF_BODY)
        self.assertIsNone(out[0]["heading"])
        self.assertIn("Intro paragraph", out[0]["body"])

    def test_headings_and_levels_are_recorded(self):
        out = sections.split(BRIEF_BODY)
        self.assertEqual([s["level"] for s in out], [None, 2, 2, 3])
        self.assertEqual(out[1]["heading"], "Xi grants North Korea his first official visit")

    def test_a_bare_label_is_not_a_section(self):
        """MERICS Briefs wrap stories in label headings ("Analysis", "Update")
        that have no prose of their own."""
        out = sections.split("## Analysis\n\n## Real story\n\n" + "word " * 40)
        self.assertEqual([s["heading"] for s in out], ["Real story"])

    def test_positions_are_contiguous_after_filtering(self):
        out = sections.split(BRIEF_BODY)
        self.assertEqual([s["position"] for s in out], list(range(len(out))))

    def test_a_body_with_no_headings_is_one_section(self):
        out = sections.split("word " * 50)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["heading"])

    def test_empty_body_yields_nothing(self):
        self.assertEqual(sections.split(""), [])
        self.assertEqual(sections.split(None), [])


class TestIndependence(unittest.TestCase):
    def test_a_brief_with_several_level_two_headings_is_a_digest(self):
        """In a Brief `##` is a story and `###` is a standing feature — the
        opposite of what the nesting suggests. Verified against the
        `+`-separated title: 98% have at least as many `##` as named topics."""
        out = sections.split(BRIEF_BODY)
        self.assertTrue(sections.has_independent_topics("MERICS Briefs", PUB["title"], out))

    def test_a_report_is_never_split_into_independent_topics(self):
        """Its headings are one argument's structure; treating them as separate
        recall units would produce fragments of a single thesis."""
        out = sections.split(BRIEF_BODY)
        self.assertFalse(sections.has_independent_topics("Report", "A long report", out))
        self.assertFalse(sections.has_independent_topics("Comment", "A comment", out))

    def test_a_single_story_brief_is_not_a_digest(self):
        out = sections.split("## The only story\n\n" + "word " * 40)
        self.assertFalse(sections.has_independent_topics("MERICS Briefs", "One thing", out))

    def test_topics_are_read_from_a_plus_separated_title(self):
        self.assertEqual(sections.topics_from_title("A + B + C"), ["A", "B", "C"])
        self.assertEqual(sections.topics_from_title("No plus here"), [])


class TestStorageAndSearch(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        self.pub_id = db.upsert_publication(self.conn, PUB)
        db.upsert_text(self.conn, self.pub_id, BRIEF_BODY, len(BRIEF_BODY.split()))
        found = sections.split(BRIEF_BODY)
        for s in found:
            s["independent"] = True
        db.replace_sections(self.conn, self.pub_id, found)
        db.rebuild_section_fts(self.conn)
        db.rebuild_fts(self.conn)

    def test_re_extracting_replaces_rather_than_appends(self):
        before = self.conn.execute("SELECT COUNT(*) FROM publication_sections").fetchone()[0]
        db.replace_sections(self.conn, self.pub_id, sections.split(BRIEF_BODY))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM publication_sections").fetchone()[0], before)

    def test_search_reports_which_section_matched(self):
        hits = db.search(self.conn, "export")
        where = db.matching_sections(self.conn, "export", [h["id"] for h in hits])
        self.assertIn("export control", where[self.pub_id]["heading"].lower())

    def test_every_section_body_is_a_substring_of_its_publication(self):
        """This is what makes a section hit imply a publication hit, and so what
        lets ranking stay on publication_fts. Asserting it via matching_sections
        would prove nothing — that query filters on the ids handed to it, so it
        cannot return an outsider by construction. Check the source instead.
        """
        body = self.conn.execute(
            "SELECT body FROM publication_text WHERE publication_id = ?",
            (self.pub_id,)).fetchone()["body"]
        rows = self.conn.execute(
            "SELECT body FROM publication_sections WHERE publication_id = ?",
            (self.pub_id,)).fetchall()
        self.assertTrue(rows)
        for r in rows:
            self.assertIn(r["body"], body)

    def test_deleting_a_publication_takes_its_sections_with_it(self):
        self.conn.execute("DELETE FROM publications WHERE id = ?", (self.pub_id,))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM publication_sections").fetchone()[0], 0)


class TestChunking(unittest.TestCase):
    """A section longer than the embedder's window is truncated silently (#34),
    so the split has to happen here or not at all."""

    def _long(self, words, para_len=100):
        paras = []
        made = 0
        while made < words:
            n = min(para_len, words - made)
            paras.append(" ".join(f"w{made + i}" for i in range(n)))
            made += n
        return "## Heading\n\n" + "\n\n".join(paras)

    def test_a_short_section_is_left_whole_and_unmarked(self):
        """An authored section carries no chunk marker, which is how the
        workbench tells a real section from a slice the pipeline cut."""
        out = sections.split(self._long(200))
        self.assertEqual(len(out), 1)
        self.assertNotIn("chunk_index", out[0])

    def test_an_oversized_section_becomes_numbered_windows(self):
        out = sections.split(self._long(3000))
        self.assertGreater(len(out), 1)
        self.assertEqual([s["chunk_index"] for s in out], list(range(len(out))))
        self.assertTrue(all(s["chunk_total"] == len(out) for s in out))

    def test_no_window_exceeds_what_the_embedder_reads(self):
        from pubbrain import embed
        for s in sections.split(self._long(9000)):
            self.assertLessEqual(s["word_count"], embed.MAX_WORDS)

    def test_windows_keep_the_parent_heading(self):
        """A chunk's heading is the only context it carries, and section_fts
        weights headings above body."""
        for s in sections.split(self._long(3000)):
            self.assertEqual(s["heading"], "Heading")

    def test_no_prose_is_lost_in_the_windowing(self):
        body = self._long(3000)
        seen = set()
        for s in sections.split(body):
            seen.update(s["body"].split())
        self.assertEqual(len(seen), 3000)

    def test_windows_overlap_so_a_sentence_cut_in_half_still_retrieves(self):
        out = sections.split(self._long(3000))
        first, second = set(out[0]["body"].split()), set(out[1]["body"].split())
        self.assertTrue(first & second)

    def test_positions_stay_a_single_sequence_across_chunked_sections(self):
        body = self._long(3000) + "\n\n## Second\n\n" + " ".join(
            f"x{i}" for i in range(50))
        out = sections.split(body)
        self.assertEqual([s["position"] for s in out], list(range(len(out))))

    def test_a_paragraph_longer_than_the_window_is_still_cut(self):
        """One wall of text with no blank line must not defeat the split."""
        out = sections.split("## H\n\n" + " ".join(f"w{i}" for i in range(4000)))
        self.assertGreater(len(out), 1)
        self.assertLessEqual(max(s["word_count"] for s in out),
                             sections.CHUNK_WORDS + sections.CHUNK_OVERLAP)


if __name__ == "__main__":
    unittest.main()
