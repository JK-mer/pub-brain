"""PDF import (#6). The guards matter more than the extraction: a wrong body
is worse than a missing one, and nothing downstream would report it."""

import sqlite3
import unittest
from unittest import mock

from pubbrain import cli, db, pdf

BASE = {
    "slug": "s", "url": "u", "title": "T", "subtitle": None,
    "date_published": "2025-01-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [], "site_tags": [],
}
SHARED_PDF = "https://merics.org/files/ETNC_Report_2026.pdf"


class TestClean(unittest.TestCase):
    def test_hard_wrapped_lines_become_paragraphs(self):
        """A PDF breaks lines at the page margin, not at paragraphs. Left as
        is, every line is its own block and sections.split sees fragments."""
        out = pdf.clean("A sentence that runs\nacross two lines.\n\nA second one\nhere.")
        self.assertEqual(out, "A sentence that runs across two lines.\n\nA second one here.")

    def test_blank_runs_do_not_produce_empty_paragraphs(self):
        self.assertEqual(pdf.clean("one\n\n\n\ntwo"), "one\n\ntwo")


BODY_H = 9.3
HEAD_H = 16.6


_WORDS = ("beijing party economy policy europe trade security council reform "
          "market export leadership tech growth risk").split()


def _prose(page, block, n=12):
    """A block of body copy, as pdftotext reports it: one row per laid-out
    line. Two properties the real thing has and a naive generator does not:
    body lines outnumber headings (or the modal height lands on the wrong one),
    and no two lines are alike (or they read as a running header)."""
    return [(page, block, BODY_H,
             " ".join(_WORDS[(block * 7 + i * 3 + j) % len(_WORDS)]
                      for j in range(9)))
            for i in range(n)]


def _lines(*spec):
    """(page, block, height, text) rows as `_bbox_lines` yields them. A tuple
    is taken verbatim; a (page, block) pair expands into a block of prose."""
    out = []
    for item in spec:
        out.extend(_prose(*item) if len(item) == 2 else [item])
    return out


class TestHeadingRecovery(unittest.TestCase):
    """A PDF has no heading markup, only type size (#34). Recovering it is what
    lets sections.split bound a report the way it bounds an HTML page."""

    def _markdown(self, rows):
        with mock.patch("pubbrain.pdf._bbox_lines", return_value=rows):
            return pdf.to_markdown("ignored")

    def test_a_taller_line_becomes_a_heading(self):
        md = self._markdown(_lines(
            (1, 1, HEAD_H, "1. Introduction"), (1, 2),
            (2, 3, HEAD_H, "2. Findings"), (2, 4)))
        self.assertIn("## 1. Introduction", md)
        self.assertIn("## 2. Findings", md)
        self.assertNotIn("#", md.split("## 2. Findings")[1])

    def test_a_heading_wrapping_two_lines_is_joined(self):
        md = self._markdown(_lines(
            (1, 1, HEAD_H, "1. Europe needs to brace itself for"),
            (1, 1, HEAD_H, "China's emergence"),
            (1, 2)))
        self.assertIn("## 1. Europe needs to brace itself for China's emergence", md)

    def test_the_divider_page_repeat_is_dropped(self):
        """Every MERICS report prints a chapter title on its own divider page
        and again where the chapter starts. Kept, it splits the chapter in two
        and leaves an empty section."""
        md = self._markdown(_lines(
            (1, 1, HEAD_H, "2. Domestic factors"),
            (2, 2, HEAD_H, "2. Domestic factors"),
            (2, 3)))
        self.assertEqual(md.count("2. Domestic factors"), 1)

    def test_a_rotated_chart_label_is_not_a_heading(self):
        """Rotated axis text gets a bounding box as tall as the words are long,
        which reads as an enormous font."""
        md = self._markdown(_lines(
            (1, 1, HEAD_H, "1. Introduction"),
            (1, 2, BODY_H * 8, "% of total world count"),
            (1, 3)))
        self.assertNotIn("% of total world count", [
            l.lstrip("# ") for l in md.splitlines() if l.startswith("#")])

    def test_chart_credits_and_page_footers_are_not_headings(self):
        md = self._markdown(_lines(
            (1, 1, HEAD_H, "1. Introduction"),
            (1, 2, HEAD_H, "© MERICS"),
            (1, 3, HEAD_H, "Exhibit 3"),
            (1, 4)))
        self.assertEqual([l for l in md.splitlines() if l.startswith("#")],
                         ["## 1. Introduction"])

    def test_running_heads_are_stripped_from_the_body(self):
        rows = [(p, p * 2, BODY_H, f"MERICS | PAPERS ON CHINA No 4 | {p}")
                for p in range(1, 11)]
        for p in range(1, 11):
            rows += _prose(p, p * 2 + 1)
        rows.insert(0, (1, 0, HEAD_H, "1. Introduction"))
        md = self._markdown(sorted(rows, key=lambda r: r[1]))
        self.assertNotIn("PAPERS ON CHINA", md)

    def test_a_document_whose_body_reads_as_headings_falls_back_to_flat(self):
        """*Shaky China* sets footnotes and captions in the most common size,
        so the mode lands below the body copy and all 744 body lines read as
        headings. Bad structure is worse than none — `import_text` then takes
        the flat path and the chunker."""
        rows = [(1, i, 7.0, f"footnote {i} in the smallest size on the page")
                for i in range(40)]
        rows += [(2, 40 + i, 12.0, f"a line of ordinary body copy number {i}")
                 for i in range(25)]
        self.assertEqual(self._markdown(rows), "")

    def test_the_density_guard_does_not_reject_a_real_report(self):
        """The guard must not be so eager that it discards good structure: a
        chapter heading every few hundred words is exactly the normal case."""
        titles = ["Introduction", "Domestic factors", "Diplomat", "Soldier",
                  "Trader", "Conclusion"]
        rows = []
        for chapter, title in enumerate(titles):
            rows.append((chapter, chapter * 2, HEAD_H, f"{chapter}. {title}"))
            rows += _prose(chapter, chapter * 2 + 1, n=40)
        md = self._markdown(rows)
        self.assertIn("## 0. Introduction", md)
        self.assertIn("## 5. Conclusion", md)

    def test_no_geometry_at_all_yields_no_markdown(self):
        self.assertEqual(self._markdown([]), "")

    def test_depth_follows_type_size(self):
        md = self._markdown(_lines(
            (1, 1, 24.0, "THE PARTY KNOWS BEST"), (1, 2),
            (2, 3, HEAD_H, "1. A new era"), (2, 4)))
        self.assertIn("## THE PARTY KNOWS BEST", md)
        self.assertIn("### 1. A new era", md)


class TestGuards(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.chapter = db.upsert_publication(self.conn, {
            **BASE, "slug": "italy", "url": "https://merics.org/en/report/italy",
            "title": "Italy chapter", "pdf_url": SHARED_PDF})
        self.other = db.upsert_publication(self.conn, {
            **BASE, "slug": "ireland", "url": "https://merics.org/en/report/ireland",
            "title": "Ireland chapter", "pdf_url": SHARED_PDF})
        self.solo = db.upsert_publication(self.conn, {
            **BASE, "slug": "monitor", "url": "https://merics.org/en/report/monitor",
            "title": "China Monitor 4",
            "pdf_url": "https://merics.org/files/china-monitor-4.pdf"})
        db.upsert_text(self.conn, self.chapter, "abstract " * 40, 40)
        db.upsert_text(self.conn, self.solo, "abstract " * 37, 37)

    def test_a_shared_pdf_is_refused(self):
        """The ETNC case: the Italy chapter briefly held all 75,890 words of
        the whole report, Lithuania mentioned 90 times."""
        self.assertEqual(pdf.is_shared(self.conn, self.chapter), 2)
        out = pdf.import_text(self.conn, self.chapter, "italy")
        self.assertIn("shared", out["skipped"])
        row = self.conn.execute(
            "SELECT word_count, source FROM publication_text WHERE publication_id = ?",
            (self.chapter,)).fetchone()
        self.assertEqual(row["word_count"], 40)
        self.assertEqual(row["source"], "html")

    def test_allow_shared_overrides_it_for_a_citation(self):
        """A Brief that merely cites a report shares its PDF without being a
        chapter of anything (#42), and `COUNT(*)` cannot tell the two apart."""
        with mock.patch("pubbrain.pdf.pdf_path_for") as path_for, \
             mock.patch("pubbrain.pdf.to_markdown", return_value=""), \
             mock.patch("pubbrain.pdf.to_text", return_value="word " * 900):
            path_for.return_value = mock.Mock(exists=lambda: True)
            out = pdf.import_text(self.conn, self.chapter, "italy",
                                  allow_shared=True)
        self.assertEqual(out["words"], 900)
        self.assertEqual(db.text_source(self.conn, self.chapter), "pdf")

    def test_the_override_is_refused_without_only(self):
        """A bulk run with the guard off wholesale is how the ETNC chapters got
        the whole volume, so this is refused before a database is even opened —
        no run can reach the loop with the guard disabled for everything."""
        self.assertEqual(
            cli.main(["extract-pdf-text", "--allow-shared"]), 1)
        self.assertEqual(
            cli.main(["extract-pdf-text", "--allow-outsized"]), 1)

    def test_a_pdf_belonging_to_one_record_is_imported(self):
        with mock.patch("pubbrain.pdf.pdf_path_for") as path_for, \
             mock.patch("pubbrain.pdf.to_text", return_value="word " * 3747):
            path_for.return_value = mock.Mock(exists=lambda: True)
            out = pdf.import_text(self.conn, self.solo, "monitor")
        self.assertEqual(out["words"], 3747)
        self.assertEqual(out["before"], 37)
        self.assertEqual(db.text_source(self.conn, self.solo), "pdf")

    def test_a_shorter_extraction_never_replaces_good_text(self):
        db.upsert_text(self.conn, self.solo, "good html body " * 500, 1500)
        with mock.patch("pubbrain.pdf.pdf_path_for") as path_for, \
             mock.patch("pubbrain.pdf.to_text", return_value="word " * 20):
            path_for.return_value = mock.Mock(exists=lambda: True)
            out = pdf.import_text(self.conn, self.solo, "monitor")
        self.assertIn("against", out["skipped"])
        self.assertEqual(db.text_source(self.conn, self.solo), "html")

    def test_hand_entered_text_is_never_replaced_by_a_pdf(self):
        db.upsert_text(self.conn, self.solo, "typed by hand", 3, source="manual")
        with mock.patch("pubbrain.pdf.to_text", return_value="word " * 5000):
            out = pdf.import_text(self.conn, self.solo, "monitor")
        self.assertEqual(out["skipped"], "manual text")
        self.assertEqual(db.text_source(self.conn, self.solo), "manual")

    def test_downloads_are_newest_first(self):
        rows = pdf.pending_downloads(self.conn)
        self.assertTrue(rows)
        dates = [r["date_published"] for r in rows]
        self.assertEqual(dates, sorted(dates, reverse=True))


if __name__ == "__main__":
    unittest.main()


class TestForeignPdfs(unittest.TestCase):
    def test_a_pdf_hosted_elsewhere_is_never_this_records_text(self):
        """Three Comments link the European Commission's Strategic Outlook.
        Citing a document does not make it your body text."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        pid = db.upsert_publication(conn, {
            **BASE, "slug": "cite", "url": "https://merics.org/en/comment/cite",
            "title": "A comment citing the Commission", "pub_type": "Comment",
            "pdf_url": "https://ec.europa.eu/info/sites/communication-eu-china.pdf"})
        db.upsert_text(conn, pid, "the comment body " * 100, 300)
        with mock.patch("pubbrain.pdf.to_text", return_value="word " * 9000):
            out = pdf.import_text(conn, pid, "cite")
        self.assertIn("not published by MERICS", out["skipped"])
        self.assertEqual(db.text_source(conn, pid), "html")


class TestOutsizedPdfs(unittest.TestCase):
    """The ETNC parents whose chapters live at root-level URLs are linked by
    nothing else, so the sharing test cannot see them. Length is the backstop,
    and it flags rather than imports."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        for i in range(5):                       # give Reports a median
            pid = db.upsert_publication(self.conn, {
                **BASE, "slug": f"r{i}", "url": f"https://merics.org/en/report/r{i}",
                "title": f"Report {i}"})
            db.upsert_text(self.conn, pid, "word " * 3600, 3600)
        self.parent = db.upsert_publication(self.conn, {
            **BASE, "slug": "etnc", "url": "https://merics.org/en/report/etnc",
            "title": "Quest for strategic autonomy",
            "pdf_url": "https://merics.org/files/etnc2025.pdf"})
        db.upsert_text(self.conn, self.parent, "abstract " * 281, 281)

    def _import(self, words):
        with mock.patch("pubbrain.pdf.pdf_path_for") as path_for, \
             mock.patch("pubbrain.pdf.to_text", return_value="word " * words):
            path_for.return_value = mock.Mock(exists=lambda: True)
            return pdf.import_text(self.conn, self.parent, "etnc")

    def test_a_multi_partner_volume_is_flagged_not_imported(self):
        out = self._import(75890)
        self.assertTrue(out.get("outsized"))
        self.assertEqual(db.text_source(self.conn, self.parent), "html")

    def test_an_ordinary_long_report_still_imports(self):
        """Length alone must not block a genuinely substantial single report."""
        out = self._import(9000)
        self.assertIsNone(out.get("skipped"))
        self.assertEqual(out["words"], 9000)
        self.assertEqual(db.text_source(self.conn, self.parent), "pdf")
