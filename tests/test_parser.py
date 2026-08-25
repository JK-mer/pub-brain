"""Regression tests for the page-parsing traps. `python -m unittest discover tests`"""

import unittest
from pathlib import Path

from pubbrain.fetcher import same_page
from pubbrain.parser import NotAPublication, parse_publication
from pubbrain.sitemap import classify, slug_for

FIXTURES = Path(__file__).parent / "fixtures"


def parse(name, url):
    return parse_publication((FIXTURES / name).read_text(encoding="utf-8"), url)


class TestReportPage(unittest.TestCase):
    """A report page also renders three related-content teasers and a footer promo."""

    @classmethod
    def setUpClass(cls):
        cls.rec = parse(
            "report.html",
            "https://merics.org/en/report/towards-extreme-competition-mapping-contours"
            "-us-china-relations-under-biden-administration",
        )

    def test_type_is_the_pages_own_not_a_teasers(self):
        self.assertEqual(self.rec["pub_type"], "Report")
        self.assertIsNone(self.rec["series"])

    def test_date_is_the_pages_own(self):
        self.assertEqual(self.rec["date_published"], "2021-02-10")

    def test_authors_deduped_across_the_two_layout_copies(self):
        self.assertEqual(
            [(p["name"], p["slug"], p["role"]) for p in self.rec["people"]],
            [("Matt Ferchen", "matt-ferchen", "author")],
        )
        self.assertEqual(self.rec["people"][0]["job_title"], "Former Head of Global China Research")

    def test_pdf_is_the_publications_own_not_a_footnote_link(self):
        self.assertIn("/sites/default/files/", self.rec["pdf_url"])
        self.assertIn("ChinaMonitor%2068", self.rec["pdf_url"])

    def test_footer_promo_subtitle_is_not_picked_up(self):
        self.assertIsNone(self.rec["subtitle"])

    def test_public_access(self):
        self.assertEqual(self.rec["access"], "public")


class TestSeriesSplit(unittest.TestCase):
    def test_type_and_series_are_separate_taxonomy_terms(self):
        rec = parse("tracker-with-series.html", "https://merics.org/en/tracker/x")
        self.assertEqual(rec["pub_type"], "Tracker")
        self.assertEqual(rec["series"], "China Economic Indicators")
        self.assertEqual(rec["subtitle"], "MERICS Economic Indicators Q4/2020")

    def test_index_php_author_links_yield_the_same_slug(self):
        rec = parse("tracker-with-series.html", "https://merics.org/en/tracker/x")
        self.assertEqual(
            [a["slug"] for a in rec["people"]],
            ["max-j-zenglein", "maximilian-karnfelt", "francois-chimits"],
        )


class TestPaywalledPage(unittest.TestCase):
    """Member-only items render view-mode-paywall and carry no body."""

    def test_member_access_and_metadata_still_extracted(self):
        rec = parse("paywalled-executive-memo.html", "https://merics.org/en/executive-memo/x")
        self.assertEqual(rec["access"], "member")
        self.assertEqual(rec["pub_type"], "Executive Memo")
        self.assertEqual(rec["date_published"], "2021-12-20")
        self.assertEqual(len(rec["people"]), 6)


class TestScope(unittest.TestCase):
    def test_publication_prefixes(self):
        self.assertEqual(classify("https://merics.org/en/report/foo"), ("publication", "report"))
        self.assertEqual(classify("https://merics.org/en/team/jane-doe"), ("excluded", "team"))
        self.assertEqual(classify("https://merics.org/en/legacy-piece"), ("root-level", ""))
        self.assertEqual(classify("https://example.com/en/report/foo"), ("excluded", ""))

    def test_slug_ignores_the_index_php_url_variant(self):
        self.assertEqual(slug_for("/index%2ephp/en/team/max-j-zenglein"), "max-j-zenglein")


class TestNonPublication(unittest.TestCase):
    def test_page_without_a_publication_type_field_is_rejected(self):
        with self.assertRaises(NotAPublication):
            parse_publication("<html><body><h1>Hello</h1></body></html>", "https://merics.org/en/x")


class TestRedirectDetection(unittest.TestCase):
    """Stale sitemap URLs soft-404 by redirecting to a listing, not by returning 404."""

    def test_benign_url_variants_are_the_same_page(self):
        self.assertTrue(same_page("https://merics.org/en/report/x", "https://merics.org/en/report/x/"))
        self.assertTrue(same_page("https://merics.org/en/report/x", "https://www.merics.org/en/report/x"))
        self.assertTrue(same_page("https://merics.org/en/team/x", "https://merics.org/index%2ephp/en/team/x"))

    def test_landing_elsewhere_is_not_the_same_page(self):
        self.assertFalse(same_page("https://merics.org/en/podcast/x", "https://merics.org/en/analysis"))


if __name__ == "__main__":
    unittest.main()


class TestOwnPdfDetection(unittest.TestCase):
    """Drupal wraps every attached media file in its own
    `<article class="media media--type-file">`, so the nearest article ancestor
    of a download link is that wrapper — and the strict teaser guard rejected
    the publication's own PDF. 17 records were missing one because of it."""

    def _page(self, inner):
        from bs4 import BeautifulSoup
        from pubbrain.parser import _main_article
        html = f'<html><body><article><div class="view-mode-full">{inner}</div></article></body></html>'
        return _main_article(BeautifulSoup(html, "html.parser"))

    def test_a_pdf_inside_a_media_wrapper_is_the_publications_own(self):
        from pubbrain.parser import _parse_pdf
        art = self._page(
            '<article class="media media--type-file">'
            '<a href="/sites/default/files/2025-05/report.pdf">Download</a></article>')
        self.assertEqual(_parse_pdf(art), "/sites/default/files/2025-05/report.pdf")

    def test_a_pdf_inside_a_teaser_still_belongs_to_the_other_publication(self):
        from pubbrain.parser import _parse_pdf
        art = self._page(
            '<article class="node node--type-publication teaser">'
            '<a href="/sites/default/files/2020-01/someone-elses.pdf">Other</a></article>')
        self.assertIsNone(_parse_pdf(art))

    def test_a_directly_attached_pdf_still_works(self):
        from pubbrain.parser import _parse_pdf
        art = self._page('<a href="/sites/default/files/2024-02/plain.pdf">PDF</a>')
        self.assertEqual(_parse_pdf(art), "/sites/default/files/2024-02/plain.pdf")


class TestEncodedAbsoluteHrefs(unittest.TestCase):
    """Some hrefs wrap an absolute URL inside a relative path with a
    percent-encoded scheme. Three PDFs were stored as
    `https://merics.org/https%3A//merics.org/...` and 404'd on download."""

    def test_an_index_php_wrapped_absolute_url_is_unwrapped(self):
        from pubbrain.parser import _absolute
        self.assertEqual(
            _absolute("/index%2Ephp/https%3A//merics.org/sites/x.pdf"),
            "https://merics.org/sites/x.pdf")

    def test_a_bare_encoded_scheme_is_unwrapped(self):
        from pubbrain.parser import _absolute
        self.assertEqual(_absolute("/https%3A//merics.org/sites/a.pdf"),
                         "https://merics.org/sites/a.pdf")

    def test_ordinary_relative_paths_are_unaffected(self):
        from pubbrain.parser import _absolute
        self.assertEqual(_absolute("/sites/default/files/y%20z.pdf"),
                         "https://merics.org/sites/default/files/y%20z.pdf")
        self.assertEqual(_absolute("/index.php/en/report/foo"),
                         "https://merics.org/en/report/foo")

    def test_an_already_absolute_url_is_left_alone(self):
        from pubbrain.parser import _absolute
        self.assertEqual(_absolute("https://merics.org/sites/b.pdf"),
                         "https://merics.org/sites/b.pdf")


class TestTypeOverride(unittest.TestCase):
    """Root-level pages state no publication type (#10). An override exists so
    they can be brought in by hand — never so the scraper can guess one."""

    HTML = ('<html><body><article><div class="view-mode-full">'
            '<h1>Executive Summary: exploring European approaches</h1>'
            '<div class="field-name-field-content"><p>Prose.</p></div>'
            '</div></article></body></html>')

    def test_an_explicit_type_lets_an_untyped_page_parse(self):
        from pubbrain.parser import parse_publication
        rec = parse_publication(self.HTML, "https://merics.org/en/exec-summary",
                                pub_type="Report", series="ETNC")
        self.assertEqual(rec["pub_type"], "Report")
        self.assertEqual(rec["series"], "ETNC")

    def test_without_one_an_untyped_page_is_still_refused(self):
        from pubbrain.parser import NotAPublication, parse_publication
        with self.assertRaises(NotAPublication):
            parse_publication(self.HTML, "https://merics.org/en/exec-summary")

    def test_the_pages_own_type_still_wins_where_it_has_one(self):
        from pubbrain.parser import parse_publication
        html = self.HTML.replace(
            '<h1>', '<div class="field-name-field-publication-type">Comment</div><h1>')
        rec = parse_publication(html, "https://merics.org/en/x")
        self.assertEqual(rec["pub_type"], "Comment")
