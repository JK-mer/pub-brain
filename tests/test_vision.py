"""Vision summaries for publications that are a picture (#35).

The risk here is not crashes. It is a confident, well-formed summary of the
wrong picture — a site logo, an author portrait, or the cover of the report
rather than the report — which reads exactly like a good summary and is not
one.
"""

import sqlite3
import unittest
from unittest import mock

from pubbrain import db, enrich, vision

BASE = {
    "slug": "s", "url": "u", "title": "T", "subtitle": None,
    "date_published": "2016-05-20", "pub_type": "Comment", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [], "site_tags": [],
}

# The shape of a real Drupal page: the publication's own image sits inside the
# content field; everything else on the page is furniture.
PAGE = """
<html><body>
<img src="/themes/custom/merics/logo.svg" alt="Home">
<article>
 <div class="view-mode-full">
  <h1>China's nuclear industry goes global</h1>
  <div class="field-name-field-content">
    <div class="paragraph--type--media-container">
      <img src="/sites/default/files/styles/pt_media_default/public/2020-05/chart.jpg?itok=x">
    </div>
  </div>
  <img src="/sites/default/files/styles/ct_team_member/public/portrait.jpg" alt="An Author">
  <article class="node--type-publication">
    <div class="field-name-field-content"><img src="/teaser-cover.jpg"></div>
  </article>
 </div>
</article>
</body></html>
"""

GOOD = ('{"summary_one_liner": "Map of Chinese nuclear projects abroad, '
        'billions invested from Pakistan to Argentina.", '
        '"summary_short": "A world map. It plots projects. It shows shares. '
        'It marks status.", "key_findings": ["9.60 bn USD in Pakistan"], '
        '"entities": {"people": [], "organizations": [], "places": ["China"], '
        '"policies": []}}')


def stub_chat(*responses):
    calls = iter(responses)
    def _chat(messages, **kwargs):
        return {"content": next(calls), "prompt_tokens": 10,
                "completion_tokens": 5, "seconds": 1.0, "model": "model-b"}
    return _chat


class TestPageImages(unittest.TestCase):
    def test_only_the_publications_own_image_is_taken(self):
        """A bare select('img') returns the logo, the author portrait and every
        teaser cover — 9 images on a page carrying 1."""
        urls = vision.page_images(PAGE)
        self.assertEqual(len(urls), 1)
        self.assertIn("chart.jpg", urls[0])

    def test_the_resized_derivative_is_swapped_for_the_original(self):
        """Drupal serves a thumbnail under /styles/<preset>/public/. Axis
        labels and footnotes are exactly what a chart summary needs and what a
        thumbnail loses."""
        url = vision.page_images(PAGE)[0]
        self.assertNotIn("/styles/", url)
        self.assertTrue(url.startswith("https://merics.org/sites/default/files/"))

    def test_the_cache_busting_query_is_dropped(self):
        self.assertNotIn("?", vision.page_images(PAGE)[0])

    def test_a_page_with_no_content_field_yields_nothing(self):
        self.assertEqual(vision.page_images("<html><body><img src='/a.jpg'>"
                                            "</body></html>"), [])


class TestDescribe(unittest.TestCase):
    def setUp(self):
        self.rec = dict(BASE, id=1, title="China's nuclear industry goes global")

    def test_a_valid_response_is_returned_with_its_meta(self):
        data, meta = vision.describe(self.rec, ["data:image/png;base64,AA"],
                                     chat=stub_chat(GOOD))
        self.assertIn("Pakistan", data["summary_one_liner"])
        self.assertEqual(meta["images_sent"], 1)
        self.assertEqual(meta["attempts"], 1)

    def test_the_same_validation_gate_as_the_text_pass(self):
        """A reader cannot tell which pass wrote a row, so neither may accept
        what the other would reject."""
        long_one_liner = GOOD.replace(
            "Map of Chinese nuclear projects abroad, billions invested from "
            "Pakistan to Argentina.",
            " ".join(["word"] * (enrich.ONE_LINER_MAX_WORDS + 10)))
        with self.assertRaises(enrich.Invalid):
            vision.describe(self.rec, ["data:image/png;base64,AA"], attempts=1,
                            chat=stub_chat(long_one_liner))

    def test_a_rejected_answer_is_re_asked_with_the_reason(self):
        bad = GOOD.replace('"key_findings": ["9.60 bn USD in Pakistan"]',
                           '"key_findings": []')
        data, meta = vision.describe(self.rec, ["data:image/png;base64,AA"],
                                     attempts=2, chat=stub_chat(bad, GOOD))
        self.assertEqual(meta["attempts"], 2)
        self.assertTrue(data["key_findings"])

    def test_no_images_is_refused_rather_than_summarized_from_the_title(self):
        """Nothing to look at must not become a summary invented from the
        metadata header, which would be indistinguishable from a real one."""
        with self.assertRaises(enrich.Invalid):
            vision.describe(self.rec, [], chat=stub_chat(GOOD))

    def test_a_stalled_request_is_bounded_rather_than_retried_for_an_hour(self):
        """`chat_with_backoff` treats a timeout as transient and retries it 8
        times. Right for a rate limit; here the same payload stalls the same
        way, so the default turns a 150s ceiling into ~25 minutes per record."""
        seen = {}
        def capture(messages, **kwargs):
            seen.update(kwargs)
            return {"content": GOOD, "prompt_tokens": 1, "completion_tokens": 1,
                    "seconds": 0.1, "model": "model-b"}
        vision.describe(self.rec, ["data:image/png;base64,AA"], chat=capture)
        self.assertEqual(seen["retries"], vision.NETWORK_RETRIES)
        self.assertEqual(seen["timeout"], vision.REQUEST_TIMEOUT)
        self.assertLess(vision.NETWORK_RETRIES, 8)

    def test_a_multi_image_timeout_falls_back_to_the_leading_image(self):
        """Measured against syn:large:vision: two page images together time out
        while each answers alone. The first image in a content field is the
        publication's own graphic, so a summary of it beats no summary."""
        import requests
        calls = []
        def flaky(messages, **kwargs):
            images = [p for p in messages[-1]["content"] if p["type"] == "image_url"]
            calls.append(len(images))
            if len(images) > 1:
                raise requests.Timeout("read timed out")
            return {"content": GOOD, "prompt_tokens": 1, "completion_tokens": 1,
                    "seconds": 1.0, "model": "model-b"}
        data, meta = vision.describe(
            self.rec, ["data:image/png;base64,AA", "data:image/png;base64,BB"],
            chat=flaky)
        self.assertEqual(calls, [2, 1])
        self.assertTrue(data["summary_one_liner"])

    def test_the_fallback_records_that_it_read_less(self):
        """A thin reading must not be indistinguishable from a full one."""
        import requests
        def flaky(messages, **kwargs):
            images = [p for p in messages[-1]["content"] if p["type"] == "image_url"]
            if len(images) > 1:
                raise requests.Timeout("read timed out")
            return {"content": GOOD, "prompt_tokens": 1, "completion_tokens": 1,
                    "seconds": 1.0, "model": "model-b"}
        _, meta = vision.describe(
            self.rec, ["data:image/png;base64,AA", "data:image/png;base64,BB"],
            chat=flaky)
        self.assertEqual(meta["images_sent"], 1)
        self.assertTrue(meta["degraded"])

    def test_a_single_image_timeout_is_not_swallowed(self):
        """With nothing to fall back to, the record must stay on the worklist
        rather than be marked done with no summary."""
        import requests
        def always_times_out(messages, **kwargs):
            raise requests.Timeout("read timed out")
        with self.assertRaises(requests.Timeout):
            vision.describe(self.rec, ["data:image/png;base64,AA"],
                            chat=always_times_out)

    def test_the_images_reach_the_model(self):
        sent = {}
        def capture(messages, **kwargs):
            sent["messages"] = messages
            return {"content": GOOD, "prompt_tokens": 1, "completion_tokens": 1,
                    "seconds": 0.1, "model": "model-b"}
        vision.describe(self.rec, ["data:image/png;base64,AA",
                                   "data:image/png;base64,BB"], chat=capture)
        parts = sent["messages"][-1]["content"]
        self.assertEqual(sum(p["type"] == "image_url" for p in parts), 2)
        # The title goes too: a chart states its axis, not its subject.
        self.assertIn("nuclear industry", parts[0]["text"])


class TestWorklist(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        db.migrate(self.conn)

    def _add(self, slug, **kw):
        return db.upsert_publication(self.conn, {**BASE, "slug": slug,
                                                 "url": f"https://x/{slug}", **kw})

    def test_a_record_with_no_text_and_no_summary_is_picked_up(self):
        pid = self._add("infographic")
        self.assertIn(pid, [r["id"] for r in vision.pending(self.conn)])

    def test_a_record_that_already_has_a_summary_is_not(self):
        pid = self._add("infographic")
        db.upsert_primary_enrichment(self.conn, pid, {
            "summary_one_liner": "x", "summary_short": "y",
            "key_findings": ["z"], "entities": {}},
            {"model": "m", "provider": "p", "prompt_version": 1, "words_sent": 1})
        self.assertNotIn(pid, [r["id"] for r in vision.pending(self.conn)])

    def test_podcasts_are_left_alone(self):
        """212 of them, audio with no picture — they would fail one by one."""
        pid = self._add("ep-1", pub_type="Podcast")
        self.assertNotIn(pid, [r["id"] for r in vision.pending(self.conn)])

    def test_a_report_with_a_downloaded_pdf_belongs_to_the_text_pass(self):
        """Its page image is the report's cover, and a faithful description of
        a cover reads like a summary of the report without being one."""
        pid = self._add("pdf-report", pdf_url="https://x/r.pdf")
        self.conn.execute("UPDATE publications SET pdf_path = ? WHERE id = ?",
                          ("/tmp/r.pdf", pid))
        self.assertNotIn(pid, [r["id"] for r in vision.pending(self.conn)])

    def test_but_a_parent_whose_pdf_is_charts_is_this_pass_s_job(self):
        """The Economic Indicators quarterlies: the volume's data sections are
        charts, so extract-pdf-text yields nothing useful and only vision can
        read them (#36)."""
        parent = self._add("quarterly", pdf_url="https://x/q.pdf")
        self.conn.execute("UPDATE publications SET pdf_path = ? WHERE id = ?",
                          ("/tmp/q.pdf", parent))
        child = self._add("article")
        db.attach_chapter(self.conn, child, parent)
        self.assertIn(parent, [r["id"] for r in vision.pending(self.conn)])


if __name__ == "__main__":
    unittest.main()
