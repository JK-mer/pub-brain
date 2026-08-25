"""Hosting is not a credit (#27).

A recurring podcast host accumulates one link per episode, which made the
second-most-credited person in the catalog someone whose output is a role. The
`host` rows stay — podcast pages use them and #12 has not settled how
participants should be modelled — they simply stop counting as output.
"""

import argparse
import tempfile
import unittest
from pathlib import Path

from pubbrain import db, embed, queries
from pubbrain.web import create_app

BASE = {
    "slug": "s", "url": "u", "title": "T", "subtitle": None,
    "date_published": "2025-01-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None, "site_tags": [],
}
ANALYST = {"slug": "an-analyst", "name": "An Analyst", "is_internal": True,
           "job_title": "Analyst"}
HOST = {"slug": "a-host", "name": "A Host", "is_internal": True,
        "job_title": "Editor"}


class TestCredits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        # One authored report, then five episodes the host hosts and the
        # analyst guests on — the real shape, at small scale.
        db.upsert_publication(self.conn, {
            **BASE, "slug": "r1", "url": "u/r1", "title": "A written report",
            "people": [{**ANALYST, "role": "author"}]})
        for i in range(5):
            db.upsert_publication(self.conn, {
                **BASE, "slug": f"ep{i}", "url": f"u/ep{i}",
                "title": f"Episode {i}", "pub_type": "Podcast",
                "people": [{**HOST, "role": "host"},
                           {**ANALYST, "role": "guest"}]})
        self.conn.commit()
        self.ids = {r["name"]: r["id"] for r in
                    self.conn.execute("SELECT id, name FROM people")}

    def test_hosting_does_not_count_towards_output(self):
        people = {p["name"]: p for p in queries.list_people(self.conn, current_only=False)}
        self.assertEqual(people["A Host"]["n"], 0)
        self.assertEqual(people["A Host"]["hosted"], 5)

    def test_guesting_still_counts(self):
        people = {p["name"]: p for p in queries.list_people(self.conn, current_only=False)}
        self.assertEqual(people["An Analyst"]["n"], 6)     # 1 authored + 5 guest

    def test_the_analyst_now_outranks_the_host(self):
        names = [p["name"] for p in queries.list_people(self.conn, current_only=False)]
        self.assertLess(names.index("An Analyst"), names.index("A Host"))

    def test_a_host_only_person_is_still_listed(self):
        """Three real people have nothing but host links, two of them MERICS
        staff. Dropping them from the list of people would be a worse answer
        than ranking them last."""
        self.assertIn("A Host", [p["name"] for p in
                                 queries.list_people(self.conn, current_only=False)])

    def test_the_person_page_reports_hosting_as_a_number_not_as_rows(self):
        page = queries.person_page(self.conn, self.ids["A Host"])
        self.assertEqual(page["hosted"], 5)
        self.assertNotIn("host", page["by_role"])
        self.assertEqual(page["by_role"], {})

    def test_the_host_relation_is_not_deleted(self):
        """Reversible on purpose — #12 has not settled how podcast
        participants should be modelled."""
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM publication_people WHERE role = 'host'"
        ).fetchone()[0], 5)

    def test_the_workbench_shows_hosting_without_counting_it(self):
        app = create_app(self.db_path)
        app.testing = True
        client = app.test_client()
        page = client.get("/people?all=1").get_data(as_text=True)
        self.assertIn("A Host", page)
        self.assertIn("not counted", page)
        person = client.get(f"/person/{self.ids['A Host']}").get_data(as_text=True)
        self.assertIn("hosted 5 podcast episodes", person.lower())



class TestMultiValueFilters(unittest.TestCase):
    """The query layer, where several years can actually be exercised (#26)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        self.addCleanup(self.conn.close)
        for year in (2023, 2024, 2025, 2026):
            for kind in ("Report", "Podcast"):
                db.upsert_publication(self.conn, {
                    **BASE, "slug": f"{kind}{year}", "url": f"u/{kind}{year}",
                    "title": f"{kind} {year}", "pub_type": kind,
                    "date_published": f"{year}-05-01", "people": []})
        self.conn.commit()

    def titles(self, **kw):
        rows, total, _ = queries.list_publications(self.conn, **kw)
        self.assertEqual(total, len(rows))
        return sorted(r["title"] for r in rows)

    def test_a_year_range_is_expressible(self):
        self.assertEqual(self.titles(year=[2024, 2025, 2026], pub_type=["Report"]),
                         ["Report 2024", "Report 2025", "Report 2026"])

    def test_years_need_not_be_contiguous(self):
        """Why multi-select rather than from/to."""
        self.assertEqual(self.titles(year=[2023, 2026], pub_type=["Report"]),
                         ["Report 2023", "Report 2026"])

    def test_excluding_one_type_is_the_motivating_case(self):
        self.assertEqual(self.titles(pub_type=["Report"], year=[2023]),
                         ["Report 2023"])

    def test_an_empty_selection_means_everything(self):
        self.assertEqual(len(self.titles(pub_type=[], year=[])), 8)
        self.assertEqual(len(self.titles(pub_type=None, year=None)), 8)

    def test_a_scalar_still_works_for_the_cli_and_mcp(self):
        """The shared layer serves four surfaces; only the web sends lists."""
        self.assertEqual(self.titles(pub_type="Report", year="2025"),
                         ["Report 2025"])



class TestPeopleDefaultsToCurrent(unittest.TestCase):
    """#28. A default that hides 212 of 249 people must say what it hides —
    the third most-credited person in the real catalog is former staff."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        for i, (name, current) in enumerate(
                [("Still Here", 1), ("Long Gone", 0)]):
            db.upsert_publication(self.conn, {
                **BASE, "slug": f"p{i}", "url": f"u/p{i}", "title": f"Piece {i}",
                "people": [{"slug": f"s{i}", "name": name, "is_internal": True,
                            "job_title": "Analyst", "role": "author"}]})
            self.conn.execute("UPDATE people SET is_current = ?, affiliation = 'staff'"
                              " WHERE name = ?", (current, name))
        self.conn.commit()

    def test_the_default_is_current_people_only(self):
        names = [p["name"] for p in queries.list_people(self.conn)]
        self.assertEqual(names, ["Still Here"])

    def test_show_all_returns_everyone(self):
        names = {p["name"] for p in queries.list_people(self.conn, current_only=False)}
        self.assertEqual(names, {"Still Here", "Long Gone"})

    def test_the_page_states_how_many_it_is_hiding(self):
        app = create_app(self.db_path)
        app.testing = True
        client = app.test_client()
        page = client.get("/people").get_data(as_text=True)
        self.assertNotIn("Long Gone", page)
        self.assertIn("show all 2", page)
        self.assertIn("1 former or external", page)
        every = client.get("/people?all=1").get_data(as_text=True)
        self.assertIn("Long Gone", every)
        self.assertIn("Still Here", every)


if __name__ == "__main__":
    unittest.main()


class TestManualText(unittest.TestCase):
    """#31. Hand-entered text is the only copy — the cache cannot regenerate
    it, so the pipeline must leave it alone and search must reach it at once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.pid = db.upsert_publication(self.conn, {
            **BASE, "slug": "paywalled", "url": "https://merics.org/en/report/paywalled",
            "title": "A member-only brief", "people": []})
        self.conn.commit()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def tearDown(self):
        self.conn.close()

    def save(self, body):
        return self.client.post(f"/pub/{self.pid}/text", data={"body": body})

    def test_saved_text_is_marked_manual(self):
        self.save("The full text of a paywalled brief. " * 20)
        self.assertEqual(db.text_source(self.conn, self.pid), "manual")
        self.assertTrue(db.is_authored(self.conn, self.pid))

    def test_the_guard_that_stops_extract_text_overwriting_it(self):
        """`cmd_extract_text` skips a row when `is_authored` is true; this pins
        the predicate, which is the part that can silently regress. Scraped
        text stays replaceable — only authored text is protected."""
        self.save("Hand entered content that exists nowhere else. " * 10)
        self.assertTrue(db.is_authored(self.conn, self.pid))
        other = db.upsert_publication(self.conn, {
            **BASE, "slug": "scraped", "url": "https://merics.org/en/report/scraped",
            "title": "An ordinary scraped record", "people": []})
        db.upsert_text(self.conn, other, "scraped body", 2)
        self.assertFalse(db.is_authored(self.conn, other))

    def test_extract_text_leaves_a_manual_row_alone(self):
        """The guard at its real call site. A cached page must exist, or
        extraction is skipped for being uncached and the test proves nothing —
        verified by disabling the guard and watching this fail."""
        from unittest import mock
        from pubbrain import cli
        self.save("Hand entered content that exists nowhere else. " * 10)
        self.conn.commit()
        cache = Path(self.tmp.name) / "raw"
        cache.mkdir()
        (cache / "paywalled.html").write_text("<html><body>anything</body></html>")
        before = self.conn.execute(
            "SELECT body, source FROM publication_text WHERE publication_id = ?",
            (self.pid,)).fetchone()["body"]
        with mock.patch("pubbrain.paths.RAW_DIR", cache), \
             mock.patch("pubbrain.db.connect", return_value=db.connect(self.db_path)), \
             mock.patch("pubbrain.text.extract",
                        return_value={"text": "REPLACED", "word_count": 1}):
            cli.cmd_extract_text(argparse.Namespace(only=None, force=False))
        after = self.conn.execute(
            "SELECT body, source FROM publication_text WHERE publication_id = ?",
            (self.pid,)).fetchone()
        self.assertEqual(after["body"], before)
        self.assertEqual(after["source"], "manual")

    def _extract(self, force=False):
        from unittest import mock
        from pubbrain import cli
        cache = Path(self.tmp.name) / "raw"
        cache.mkdir(exist_ok=True)
        (cache / "pdf-backed.html").write_text("<html><body>x</body></html>")
        with mock.patch("pubbrain.paths.RAW_DIR", cache), \
             mock.patch("pubbrain.db.connect", return_value=db.connect(self.db_path)), \
             mock.patch("pubbrain.text.extract",
                        return_value={"text": "a 37 word abstract", "word_count": 37}):
            cli.cmd_extract_text(argparse.Namespace(only=None, force=force))

    def test_extract_text_does_not_undo_the_pdf_import(self):
        """`extract-text` runs after every re-scrape, per the documented order.
        Without this guard that step silently replaces 3,747 words of imported
        report with the 37-word landing-page abstract it stands in for (#6)."""
        pdf_backed = db.upsert_publication(self.conn, {
            **BASE, "slug": "pdf-backed",
            "url": "https://merics.org/en/report/pdf-backed",
            "title": "A report whose body came from its PDF", "people": []})
        db.upsert_text(self.conn, pdf_backed, "the full report " * 1000, 3747,
                       source="pdf")
        self.conn.commit()
        self._extract()
        row = self.conn.execute(
            "SELECT word_count, source FROM publication_text WHERE publication_id = ?",
            (pdf_backed,)).fetchone()
        self.assertEqual(row["word_count"], 3747)
        self.assertEqual(row["source"], "pdf")

    def test_force_reverts_a_parent_to_its_landing_page(self):
        """The deliberate case: a report whose chapters now carry its body must
        stop holding the whole PDF, or it duplicates them (#36)."""
        pdf_backed = db.upsert_publication(self.conn, {
            **BASE, "slug": "pdf-backed",
            "url": "https://merics.org/en/report/pdf-backed",
            "title": "A report whose chapters have landed", "people": []})
        db.upsert_text(self.conn, pdf_backed, "the full report " * 1000, 3747,
                       source="pdf")
        self.conn.commit()
        self._extract(force=True)
        row = self.conn.execute(
            "SELECT word_count, source FROM publication_text WHERE publication_id = ?",
            (pdf_backed,)).fetchone()
        self.assertEqual(row["word_count"], 37)
        self.assertEqual(row["source"], "html")

    def test_the_text_is_searchable_immediately(self):
        """Saved but unfindable looks exactly like not saved at all."""
        self.save("Kazakhstan pipeline diplomacy is the subject here. " * 5)
        hits = db.search(self.conn, "Kazakhstan", limit=5)
        self.assertIn(self.pid, [h["id"] for h in hits])

    def test_markdown_headings_become_sections(self):
        self.save("## First story\n\n" + "word " * 60 +
                  "\n\n## Second story\n\n" + "word " * 60)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM publication_sections WHERE publication_id = ?",
            (self.pid,)).fetchone()[0]
        self.assertGreaterEqual(n, 2)

    def test_discarding_removes_the_text_and_its_sections(self):
        self.save("## A heading\n\n" + "word " * 80)
        self.client.post(f"/pub/{self.pid}/text", data={"action": "remove"})
        self.assertIsNone(db.text_source(self.conn, self.pid))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM publication_sections WHERE publication_id = ?",
            (self.pid,)).fetchone()[0], 0)

    def test_stale_vectors_are_dropped_when_the_text_changes(self):
        """A vector describing replaced text is a confident wrong match (#24)."""
        db.store_embeddings(self.conn, "one_liner", embed.MODEL,
                            [{"source_id": self.pid, "publication_id": self.pid}],
                            [[0.0] * embed.DIM], embed.pack)
        self.conn.commit()
        self.save("Entirely different content now. " * 20)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE publication_id = ?",
            (self.pid,)).fetchone()[0], 0)

    def test_the_page_says_where_the_text_came_from(self):
        self.save("Some hand entered text. " * 20)
        page = self.client.get(f"/pub/{self.pid}").get_data(as_text=True)
        self.assertIn("manual", page)

    def test_an_unknown_publication_cannot_be_edited(self):
        self.assertEqual(
            self.client.post("/pub/9999/text", data={"body": "x"}).status_code, 404)


class TestPastedHtml(unittest.TestCase):
    """#31. A textarea only receives the clipboard's text/plain flavour, so
    pasting from the site arrives with headings and links already stripped.
    The HTML flavour is converted with the same rules as scraped pages."""

    # Each section must clear sections.MIN_SECTION_WORDS (20), which exists to
    # drop bare labels like "Analysis" — so the fixture has to be realistic.
    FRAGMENT = ('<div><h2>Beijing and Moscow</h2>'
                '<p>Prose citing the <a href="https://merics.org/en/china-russia-dashboard">'
                'China-Russia Dashboard</a> here, running on at enough length that '
                'this section clears the twenty word minimum applied downstream.</p>'
                '<ul><li>first point</li><li>second point</li></ul>'
                '<h2>A second story</h2><p>More prose, again long enough that the '
                'section survives the minimum word count rule that drops bare '
                'labels from the section list.</p>'
                '<nav><a href="/en/about">About</a></nav></div>')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.pid = db.upsert_publication(self.conn, {
            **BASE, "slug": "paste", "url": "https://merics.org/en/report/paste",
            "title": "Needs manual text", "people": []})
        self.conn.commit()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def tearDown(self):
        self.conn.close()

    def convert(self, html=None):
        return self.client.post("/convert-html",
                                data=(html or self.FRAGMENT).encode()).get_json()

    def test_headings_survive_as_markdown(self):
        self.assertIn("## Beijing and Moscow", self.convert()["text"])

    def test_links_survive_as_markdown(self):
        """Scraped text drops links, and that loss cost real information —
        dashboard membership had to be rebuilt from the raw HTML cache (#32)."""
        self.assertIn("[China-Russia Dashboard](https://merics.org/en/china-russia-dashboard)",
                      self.convert()["text"])

    def test_lists_survive(self):
        text = self.convert()["text"]
        self.assertIn("- first point", text)
        self.assertIn("- second point", text)

    def test_site_furniture_in_the_paste_is_dropped(self):
        self.assertNotIn("About", self.convert()["text"])

    def test_an_unusable_paste_is_an_error_not_an_empty_save(self):
        r = self.client.post("/convert-html", data=b"<div><img src='x'></div>")
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_converted_text_sections_the_way_scraped_text_does(self):
        """The point of matching the scraper's Markdown: `sections.split` keys
        on these `##` headings, so a pasted digest behaves like a scraped one."""
        body = self.convert()["text"]
        self.client.post(f"/pub/{self.pid}/text", data={"body": body})
        headings = [r["heading"] for r in self.conn.execute(
            "SELECT heading FROM publication_sections WHERE publication_id = ? "
            "ORDER BY position", (self.pid,))]
        self.assertIn("Beijing and Moscow", headings)
        self.assertIn("A second story", headings)

    def test_pasted_sections_match_what_the_scraper_would_produce(self):
        """A pasted Brief must be sectioned like a scraped one. Hardcoding
        `independent = False` made a ten-story digest look like one argument,
        which is exactly what the section layer exists to distinguish (#16)."""
        from pubbrain import sections
        brief = db.upsert_publication(self.conn, {
            **BASE, "slug": "brief", "url": "https://merics.org/en/merics-briefs/b",
            "title": "Story one + Story two + Story three",
            "pub_type": "MERICS Briefs", "people": []})
        self.conn.commit()
        body = "\n\n".join(
            f"## Story {n}\n\n" + "word " * 60 for n in ("one", "two", "three"))
        self.client.post(f"/pub/{brief}/text", data={"body": body})
        rows = self.conn.execute(
            "SELECT heading, independent FROM publication_sections "
            "WHERE publication_id = ? ORDER BY position", (brief,)).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["independent"] for r in rows),
                        "a multi-story Brief's sections must be independent")

    def test_a_standing_feature_is_flagged_and_never_independent(self):
        brief = db.upsert_publication(self.conn, {
            **BASE, "slug": "brief2", "url": "https://merics.org/en/merics-briefs/b2",
            "title": "Story one + Story two", "pub_type": "MERICS Briefs", "people": []})
        self.conn.commit()
        body = ("## Story one\n\n" + "word " * 60 +
                "\n\n## Story two\n\n" + "word " * 60 +
                "\n\n## Short takes\n\n" + "word " * 60)
        self.client.post(f"/pub/{brief}/text", data={"body": body})
        rows = {r["heading"]: r for r in self.conn.execute(
            "SELECT heading, independent, is_boilerplate FROM publication_sections "
            "WHERE publication_id = ?", (brief,))}
        self.assertTrue(rows["Short takes"]["is_boilerplate"])
        self.assertFalse(rows["Short takes"]["independent"])
        self.assertTrue(rows["Story one"]["independent"])


class TestHandAssignedCredits(unittest.TestCase):
    """Crediting someone the page never names (#40).

    The byline is often in the prose, a PDF cover or the title, where no
    parser can reach it. The one rule that makes this usable: a hand-assigned
    credit must survive the next re-parse.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.rec = {**BASE, "slug": "r1", "url": "u/r1", "title": "An interview",
                    "people": [{**HOST, "role": "author"}]}
        self.pid = db.upsert_publication(self.conn, self.rec)
        self.conn.commit()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def _credits(self):
        return {(r["name"], r["role"]): r["source"] for r in self.conn.execute(
            """SELECT pe.name, pp.role, pp.source FROM publication_people pp
               JOIN people pe ON pe.id = pp.person_id
               WHERE pp.publication_id = ?""", (self.pid,))}

    def test_a_hand_assigned_credit_survives_reparsing(self):
        """The whole point. `upsert_publication` rewrites this table from the
        cached HTML on every re-scrape; without the `source` guard the credit
        is gone by the next routine run and nobody is told. Drop
        `AND source = 'parsed'` from the DELETE and this fails."""
        person = db.add_person(self.conn, "Named In The Prose")
        db.credit_person(self.conn, self.pid, person, "author")
        self.conn.commit()

        db.upsert_publication(self.conn, self.rec)      # the next re-parse
        self.conn.commit()

        self.assertEqual(self._credits(),
                         {("A Host", "author"): "parsed",
                          ("Named In The Prose", "author"): "manual"})

    def test_reparsing_still_replaces_what_the_page_carries(self):
        """The guard must not freeze parsed rows: a byline corrected on
        merics.org has to reach the catalog."""
        db.upsert_publication(self.conn, {
            **self.rec, "people": [{**ANALYST, "role": "author"}]})
        self.conn.commit()
        self.assertEqual(self._credits(), {("An Analyst", "author"): "parsed"})

    def test_a_credit_is_appended_after_the_printed_byline(self):
        person = db.add_person(self.conn, "Second")
        db.credit_person(self.conn, self.pid, person, "author")
        rows = self.conn.execute(
            "SELECT position FROM publication_people WHERE publication_id = ? "
            "AND person_id = ?", (self.pid, person)).fetchone()
        self.assertEqual(rows["position"], 1)

    def test_the_same_credit_twice_is_not_an_error_and_not_a_duplicate(self):
        person = db.add_person(self.conn, "Twice")
        self.assertTrue(db.credit_person(self.conn, self.pid, person, "author"))
        self.assertFalse(db.credit_person(self.conn, self.pid, person, "author"))

    def test_one_person_can_hold_two_roles(self):
        person = db.add_person(self.conn, "Both")
        db.credit_person(self.conn, self.pid, person, "host")
        db.credit_person(self.conn, self.pid, person, "guest")
        self.assertEqual(len(self._credits()), 3)

    def test_an_unknown_role_is_refused(self):
        person = db.add_person(self.conn, "Nobody")
        with self.assertRaises(ValueError):
            db.credit_person(self.conn, self.pid, person, "interviewee")

    def test_adding_a_person_twice_reuses_the_record(self):
        """The form can be submitted twice; that must not mint a second
        'Max J. Zenglein' keyed on nothing but a name."""
        self.assertEqual(db.add_person(self.conn, "Same Name"),
                         db.add_person(self.conn, "Same Name"))

    def test_crediting_from_the_record_page_marks_it_by_hand(self):
        self.client.post(f"/pub/{self.pid}/credit",
                         data={"name": "Prose Byline", "role": "author"})
        page = self.client.get(f"/pub/{self.pid}").get_data(as_text=True)
        self.assertIn("Prose Byline", page)
        self.assertIn("by hand", page)

    def test_picking_an_existing_person_does_not_create_another(self):
        before = self.conn.execute("SELECT COUNT(*) c FROM people").fetchone()["c"]
        pe = self.conn.execute("SELECT id FROM people WHERE name = 'A Host'").fetchone()
        self.client.post(f"/pub/{self.pid}/credit",
                         data={"person_id": pe["id"], "name": "A Host",
                               "role": "guest"})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM people").fetchone()["c"], before)
        self.assertIn(("A Host", "guest"), self._credits())

    def test_removing_a_credit_works_and_names_the_role(self):
        """One person can be host and guest on the same podcast, so the role
        is part of the identity — removing without it would take both."""
        person = db.add_person(self.conn, "Two Hats")
        db.credit_person(self.conn, self.pid, person, "host")
        db.credit_person(self.conn, self.pid, person, "guest")
        self.conn.commit()
        self.client.post(f"/pub/{self.pid}/credit",
                         data={"person_id": person, "role": "host",
                               "action": "remove"})
        self.assertEqual(list(self._credits()),
                         [("A Host", "author"), ("Two Hats", "guest")])

    def test_a_credit_with_neither_a_pick_nor_a_name_is_rejected(self):
        self.assertEqual(self.client.post(
            f"/pub/{self.pid}/credit", data={"role": "author"}).status_code, 400)

    def test_crediting_an_unknown_publication_is_404(self):
        self.assertEqual(self.client.post(
            "/pub/99999/credit", data={"name": "X"}).status_code, 404)

    def test_the_picker_searches_by_name(self):
        found = self.client.get("/people/search?q=Host").get_json()["people"]
        self.assertEqual([p["name"] for p in found], ["A Host"])

    def test_the_picker_stays_quiet_until_it_has_something_to_go_on(self):
        """One letter matches most of the roster; an unfiltered list is not a
        picker."""
        self.assertEqual(self.client.get("/people/search?q=A").get_json()["people"], [])

    def test_a_hand_assigned_credit_counts_as_output(self):
        """It must reach the person page, or the fix is cosmetic."""
        person = db.add_person(self.conn, "Counted")
        db.credit_person(self.conn, self.pid, person, "author")
        self.conn.commit()
        page = queries.person_page(self.conn, person)
        self.assertEqual([p["title"] for p in page["by_role"]["author"]],
                         ["An interview"])

    def test_typing_a_first_name_suggests_it_before_a_substring_match(self):
        """"Jac" means Jacob, not Bojacz. A plain LIKE ordered by name buries
        the obvious answer under an alphabetically earlier accident."""
        for name in ("Marcin Jacoby", "Jacob Gunter", "Jacob Mardell"):
            db.add_person(self.conn, name)
        found = [p["name"] for p in db.find_people(self.conn, "Jac")]
        self.assertEqual(found[:2], ["Jacob Gunter", "Jacob Mardell"])
        self.assertIn("Marcin Jacoby", found)

    def test_a_surname_typed_first_still_matches(self):
        db.add_person(self.conn, "Jacob Gunter")
        self.assertEqual([p["name"] for p in db.find_people(self.conn, "Gunter")],
                         ["Jacob Gunter"])

    def test_current_staff_outrank_former_on_an_equal_match(self):
        old = db.add_person(self.conn, "Jane Doe")
        self.conn.execute("UPDATE people SET is_current = 0 WHERE id = ?", (old,))
        new = db.add_person(self.conn, "Jane Doelan")
        self.conn.execute("UPDATE people SET is_current = 1 WHERE id = ?", (new,))
        self.assertEqual([p["name"] for p in db.find_people(self.conn, "Jane")],
                         ["Jane Doelan", "Jane Doe"])


class TestMergedPeople(unittest.TestCase):
    """A merged duplicate must stay merged (#47).

    merics.org carries two team nodes for some people. Deleting the duplicate
    row is not a fix: `_person_id` inserts on an unknown slug, so the next
    reparse re-creates it from the cached HTML and re-splits the credits.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        self.addCleanup(self.conn.close)
        one = {"slug": "nis-grunberg", "name": "Nis Grünberg",
               "is_internal": True, "job_title": "Lead Analyst"}
        two = {"slug": "nis-gruenberg", "name": "Nis Grünberg",
               "is_internal": True, "job_title": None}
        db.upsert_publication(self.conn, {
            **BASE, "slug": "p1", "url": "u/p1",
            "people": [{**one, "role": "author"}]})
        db.upsert_publication(self.conn, {
            **BASE, "slug": "p2", "url": "u/p2",
            "people": [{**two, "role": "author"}]})
        # One publication credits both spellings — the union must not double.
        db.upsert_publication(self.conn, {
            **BASE, "slug": "p3", "url": "u/p3",
            "people": [{**one, "role": "author"}, {**two, "role": "author"}]})
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT id, slug FROM people ORDER BY id").fetchall()
        self.by_slug = {r["slug"]: r["id"] for r in rows}
        self.survivor = self.by_slug["nis-grunberg"]
        self.duplicate = self.by_slug["nis-gruenberg"]

    def merge(self):
        return db.merge_person(self.conn, self.duplicate, self.survivor)

    def credits(self, person_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM publication_people WHERE person_id = ?",
            (person_id,)).fetchone()[0]

    def test_merge_moves_credits_without_doubling(self):
        result = self.merge()
        self.assertEqual(self.credits(self.survivor), 3)   # p1, p2, p3 — once each
        self.assertEqual(self.credits(self.duplicate), 0)
        self.assertEqual(result["moved"], 1)               # p2
        self.assertEqual(result["already_there"], 1)       # p3, both spellings

    def test_merge_survives_reparse(self):
        """The whole point. Re-upserting the page that credits the duplicate
        slug must land the credit on the survivor, not resurrect the row."""
        self.merge()
        db.upsert_publication(self.conn, {
            **BASE, "slug": "p2", "url": "u/p2",
            "people": [{"slug": "nis-gruenberg", "name": "Nis Grünberg",
                        "is_internal": True, "job_title": None,
                        "role": "author"}]})
        self.assertEqual(self.credits(self.survivor), 3)
        self.assertEqual(self.credits(self.duplicate), 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM people").fetchone()[0], 2)

    def test_reparse_does_not_rename_the_survivor(self):
        self.merge()
        db.upsert_publication(self.conn, {
            **BASE, "slug": "p2", "url": "u/p2",
            "people": [{"slug": "nis-gruenberg", "name": "Nis Gruenberg",
                        "is_internal": True, "job_title": None,
                        "role": "author"}]})
        name = self.conn.execute("SELECT name FROM people WHERE id = ?",
                                 (self.survivor,)).fetchone()[0]
        self.assertEqual(name, "Nis Grünberg")

    def test_tombstone_leaves_every_surface(self):
        self.merge()
        self.assertEqual(len(queries.list_people(self.conn, current_only=False)), 1)
        self.assertEqual(len(queries.people_matching(self.conn, "Grünberg")), 1)
        self.assertEqual(len(db.find_people(self.conn, "Nis")), 1)

    def test_a_stale_link_lands_on_the_survivor(self):
        self.merge()
        page = queries.person_page(self.conn, self.duplicate)
        self.assertEqual(page["person"]["id"], self.survivor)

    def test_self_merge_refused(self):
        with self.assertRaises(ValueError):
            db.merge_person(self.conn, self.survivor, self.survivor)

    def test_double_merge_refused(self):
        self.merge()
        other = db.add_person(self.conn, "Someone Else")
        with self.assertRaises(ValueError):
            db.merge_person(self.conn, self.duplicate, other)

    def test_merge_into_a_tombstone_refused(self):
        self.merge()
        other = db.add_person(self.conn, "Someone Else")
        with self.assertRaises(ValueError):
            db.merge_person(self.conn, other, self.duplicate)

    def test_chain_stays_one_level(self):
        """A merged into B, then B merged into C: A must point at C."""
        self.merge()
        third = db.add_person(self.conn, "Nis Grünberg III")
        db.merge_person(self.conn, self.survivor, third)
        self.assertEqual(self.conn.execute(
            "SELECT merged_into FROM people WHERE id = ?",
            (self.duplicate,)).fetchone()[0], third)

    def test_duplicate_names_are_reported_until_merged(self):
        self.assertTrue(any("#47" in c for c in
                            queries.coverage_caveats(self.conn)))
        self.assertTrue(any("merge-person" in w for w in
                            queries.status_report(self.conn)["warnings"]))
        self.merge()
        self.assertFalse(any("#47" in c for c in
                             queries.coverage_caveats(self.conn)))

    def test_the_duplicate_only_fills_gaps_on_the_survivor(self):
        self.conn.execute("UPDATE people SET job_title = 'Guest Author' "
                          "WHERE id = ?", (self.duplicate,))
        self.merge()
        title = self.conn.execute("SELECT job_title FROM people WHERE id = ?",
                                  (self.survivor,)).fetchone()[0]
        self.assertEqual(title, "Lead Analyst")
