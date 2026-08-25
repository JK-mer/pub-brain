"""Staff / affiliate / external and current / former."""

import unittest

from pubbrain.roster import classify, names_another_employer, parse_roster

ROSTER_HTML = """
<div class="views-row">
  <a href="/en/team/jane-analyst">Jane Analyst</a>
  <div class="field-name-field-job-title"><div>Senior Analyst</div></div>
</div>
<div class="views-row">
  <a href="/en/team/sam-fellow">Sam Fellow</a>
  <div class="field-name-field-job-title"><div>Senior Associate Fellow</div></div>
</div>
"""


class TestParseRoster(unittest.TestCase):
    def test_reads_slug_and_title_per_row(self):
        got = parse_roster(ROSTER_HTML, "experts")
        self.assertEqual(set(got), {"jane-analyst", "sam-fellow"})
        self.assertEqual(got["sam-fellow"], {"title": "Senior Associate Fellow", "source": "experts"})


class TestEmployerDetection(unittest.TestCase):
    def test_titles_naming_another_organisation(self):
        for title in [
            "Senior Fellow, German Marshall Fund (GMF)",
            "Professor of Sinology at the University of Freiburg",
            "Director, Rhodium Group",
            "Former Analyst, Stockholm Centre for Eastern European Studies (SCEEUS)",
        ]:
            self.assertTrue(names_another_employer(title), title)

    def test_merics_own_titles_are_not_another_organisation(self):
        for title in [
            "Senior Analyst", "Head of Program (Co-lead)", "Former Chief Economist",
            "Former Analyst (Brussels office)", "Head of Brussels Office/Senior Analyst",
            "Former German Chancellor Fellow at MERICS", "Founding president MERICS",
        ]:
            self.assertFalse(names_another_employer(title), title)


class TestClassify(unittest.TestCase):
    def on_roster(self, title):
        return classify(True, title, None, True, False)

    def off_roster(self, job_title, has_team_page=True, flagged_external=False):
        return classify(False, None, job_title, has_team_page, flagged_external)

    def test_roster_membership_decides_current(self):
        self.assertEqual(self.on_roster("Senior Analyst"), ("staff", True))
        self.assertEqual(self.on_roster("Senior Associate Fellow"), ("affiliate", True))

    def test_absence_from_the_roster_means_former(self):
        self.assertEqual(self.off_roster("Former Chief Economist"), ("staff", False))
        self.assertEqual(self.off_roster("Former Research Fellow"), ("affiliate", False))

    def test_a_former_prefix_naming_another_employer_is_external_not_former_staff(self):
        # The trap: their *previous* job was elsewhere; they were never MERICS staff.
        self.assertEqual(
            self.off_roster("Former Analyst, Stockholm Centre for Eastern European Studies"),
            ("external", False),
        )

    def test_podcast_guests_outside_guest_team_are_external(self):
        self.assertEqual(self.off_roster(None, flagged_external=True), ("external", False))

    def test_no_team_page_means_external(self):
        self.assertEqual(self.off_roster("Chairman of a committee", has_team_page=False),
                         ("external", False))

    def test_team_page_but_no_title_anywhere_is_left_unknown(self):
        self.assertEqual(self.off_roster(None), ("unknown", False))


if __name__ == "__main__":
    unittest.main()
