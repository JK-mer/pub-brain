"""The record page's edit controls (#60).

Reading the record is the common case and editing the exception, so every
hand-edit is a button whose fields live in a dialog. The tests worth having
are not about the markup: they are that each popup still submits exactly what
its route needs. The forms moved; what they do did not, and a popup that posts
half a form fails silently as "nothing happened".
"""

import re
import tempfile
import unittest
from pathlib import Path

from pubbrain import db
from pubbrain.web import create_app

REPORT = {
    "slug": "tariff-report", "url": "https://merics.org/en/report/tariff-report",
    "title": "Tariff pressure on Europe", "subtitle": None,
    "date_published": "2025-03-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [{"slug": "a-hmaidi", "name": "Antonia Hmaidi",
                "is_internal": True, "job_title": "Analyst", "role": "author"},
               {"slug": "j-heller", "name": "Johannes Heller-John",
                "is_internal": True, "job_title": "Editor", "role": "host"}],
    "site_tags": [],
}
ENRICHMENT = {
    "summary_one_liner": "Beijing's tariffs squeeze European carmakers.",
    "summary_short": "A short summary of the tariff piece.",
    "key_findings": ["Tariffs rose"], "entities": {"orgs": ["EU"]},
}
META = {"model": "test-model", "provider": "test", "prompt_version": 1,
        "words_sent": 100}


class RecordPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        self.pub_id = db.upsert_publication(conn, REPORT)
        db.upsert_text(conn, self.pub_id, "Beijing imposed tariffs.", 3)
        db.upsert_primary_enrichment(conn, self.pub_id, ENRICHMENT, META)
        db.upsert_collection(conn, "china-eu", "China-EU Dashboard")
        conn.commit()
        conn.close()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def page(self, **args):
        return self.client.get(f"/pub/{self.pub_id}",
                               query_string=args).get_data(as_text=True)

    def conn(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn


class TestLayout(RecordPageTest):
    def test_the_links_come_before_the_actions(self):
        """They are part of the record, not something to do to it."""
        page = self.page()
        self.assertLess(page.index('class="links"'),
                        page.index('class="record-actions"'))

    def test_every_button_opens_a_dialog_that_exists(self):
        """A `data-open` pointing at nothing is a button that does nothing, and
        nothing on the page says so."""
        page = self.page()
        wanted = set(re.findall(r'data-open="([\w-]+)"', page))
        self.assertTrue(wanted)
        for dialog_id in wanted:
            self.assertIn(f'<dialog id="{dialog_id}"', page)

    def test_the_project_button_is_red_until_it_belongs_to_one(self):
        self.assertIn("proj-btn loose", self.page())
        self.client.post(f"/pub/{self.pub_id}/collection",
                         data={"slug": "china-eu"})
        page = self.page()
        self.assertIn("proj-btn attached", page)
        self.assertIn("China-EU Dashboard", page)

    def test_the_star_is_filled_only_when_shortlisted(self):
        self.assertIn("☆", self.page())
        self.client.post(f"/pub/{self.pub_id}/shortlist", data={"note": "matters"})
        self.assertIn("star-btn on", self.page())

    def test_a_record_with_no_summary_still_offers_to_write_one(self):
        """The button lived inside the enrichment block; with none rendered it
        would have disappeared exactly where it is most useful."""
        conn = self.conn()
        conn.execute("DELETE FROM publication_enrichment")
        conn.commit()
        self.assertIn("dlg-write", self.page())


class TestThePopupsStillPost(RecordPageTest):
    def test_uncredit_lives_on_the_byline_and_carries_the_role(self):
        """A button of its own was space spent on the rare case (#61), so the ×
        sits on the name. The role has to travel with it: one person can hold
        two credits on one publication, and a name alone is ambiguous."""
        page = self.page()
        self.assertNotIn("dlg-uncredit", page)
        self.assertIn('name="role" value="host"', page)
        person_id = self.conn().execute(
            "SELECT id FROM people WHERE name = 'Antonia Hmaidi'").fetchone()["id"]
        self.client.post(f"/pub/{self.pub_id}/credit",
                         data={"person_id": person_id, "role": "author",
                               "action": "remove"})
        self.assertNotIn("Antonia Hmaidi", self.page())

    def test_the_byline_is_not_a_paragraph(self):
        """The parser closes an open <p> at a <form> start tag, which
        reparented the first credit's form out of its own span and left one ×
        of two unstyled. Nothing in the rendered page says so."""
        page = self.page()
        self.assertIn('<div class="byline credits">', page)
        byline = page[page.index('class="byline credits"'):]
        self.assertLess(byline.index("</div>"), byline.index("</p>"))

    def test_the_credit_popup_creates_a_person_from_a_typed_name(self):
        self.client.post(f"/pub/{self.pub_id}/credit",
                         data={"name": "Grzegorz Stec", "role": "author"})
        self.assertIn("Grzegorz Stec", self.page())

    def test_the_project_popup_starts_a_new_project(self):
        self.client.post(f"/pub/{self.pub_id}/collection",
                         data={"slug": "", "new_project": "Chip Watch"})
        self.assertIn("Chip Watch", self.page())

    def test_detaching_leaves_the_publication_in_the_catalog(self):
        self.client.post(f"/pub/{self.pub_id}/collection",
                         data={"slug": "china-eu"})
        self.client.post(f"/pub/{self.pub_id}/collection",
                         data={"slug": "china-eu", "action": "remove"})
        self.assertIn("proj-btn loose", self.page())
        self.assertEqual(self.conn().execute(
            "SELECT COUNT(*) c FROM publications").fetchone()["c"], 1)

    def test_the_write_summary_popup_saves_both_fields(self):
        self.client.post(f"/pub/{self.pub_id}/summary", data={
            "one_liner": "Europe's tools move in years, the surge in quarters.",
            "short": "A hand-written summary of the piece, long enough to pass "
                     "the validation gate and say what the argument is."})
        page = self.page()
        self.assertIn("the surge in quarters", page)

    def test_a_rejected_summary_reopens_its_own_dialog(self):
        """The error message is useless beside a closed popup."""
        r = self.client.post(f"/pub/{self.pub_id}/summary",
                             data={"one_liner": "word " * 40, "short": "a summary"})
        self.assertIn("open=dlg-write", r.headers["Location"])
        self.assertIn("the+limit+is", r.headers["Location"])

    def test_a_refused_chapter_attach_reopens_its_own_dialog(self):
        r = self.client.post(f"/pub/{self.pub_id}/chapter-of",
                             data={"parent_id": self.pub_id})
        self.assertIn("open=dlg-chapter", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
