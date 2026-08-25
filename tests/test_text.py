"""Body-text extraction from cached pages."""

import unittest
from pathlib import Path

from pubbrain.text import NoBodyText, extract

FIXTURES = Path(__file__).parent / "fixtures"


class TestExtract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract((FIXTURES / "report.html").read_text(encoding="utf-8"))

    def test_returns_the_body_prose(self):
        self.assertGreater(self.result["word_count"], 3000)
        self.assertIn("Biden", self.result["text"])

    def test_headings_survive_as_markdown(self):
        self.assertIn("## Main Findings and Conclusions", self.result["text"])

    def test_teaser_text_from_related_articles_is_excluded(self):
        # These teasers sit outside the page's own article; their prose must not leak in.
        self.assertNotIn("Trump's remarks after China Summit", self.result["text"])

    def test_author_and_download_blocks_are_dropped(self):
        self.assertNotIn("Former Head of Global China Research", self.result["text"])
        self.assertNotIn("pdf - 409.7 KB", self.result["text"])

    def test_no_duplicated_blocks(self):
        lines = [ln for ln in self.result["text"].split("\n\n") if len(ln) > 120]
        self.assertEqual(len(lines), len(set(lines)))


class TestPagesWithoutProse(unittest.TestCase):
    def test_paywalled_page_has_no_body(self):
        html = (FIXTURES / "paywalled-executive-memo.html").read_text(encoding="utf-8")
        with self.assertRaises(NoBodyText):
            extract(html)

    def test_empty_html_has_no_body(self):
        with self.assertRaises(NoBodyText):
            extract("<html><body><h1>Nothing</h1></body></html>")


if __name__ == "__main__":
    unittest.main()
