"""The workbench must browse without enrichment, review with it, and persist
verdicts — and podcasts must stay visible despite having no enrichment."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pubbrain import db, embed
from pubbrain.web import create_app

REPORT = {
    "slug": "tariff-report", "url": "https://merics.org/en/report/tariff-report",
    "title": "Tariff pressure on Europe", "subtitle": None,
    "date_published": "2025-03-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [{"slug": "a-hmaidi", "name": "Antonia Hmaidi",
                "is_internal": True, "job_title": "Analyst", "role": "author"}],
    "site_tags": [],
}
PODCAST = {
    **REPORT, "slug": "ep-12", "url": "https://merics.org/en/podcast/ep-12",
    "title": "Episode 12: chips", "pub_type": "Podcast", "people": [],
    "og_description": "A conversation about semiconductors.",
}
ENRICHMENT = {
    "summary_one_liner": "Beijing's tariffs squeeze European carmakers.",
    "summary_short": "A short summary of the tariff piece.",
    "key_findings": ["Tariffs rose", "Carmakers suffered"],
    "entities": {"orgs": ["EU"]},
}
META = {"model": "test-model", "provider": "test", "prompt_version": 1,
        "words_sent": 100}


class TestWeb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(db_path)
        self.report_id = db.upsert_publication(conn, REPORT)
        self.podcast_id = db.upsert_publication(conn, PODCAST)
        db.upsert_text(conn, self.report_id, "Beijing imposed tariffs.", 3)
        db.upsert_primary_enrichment(conn, self.report_id, ENRICHMENT, META)
        db.rebuild_fts(conn)
        conn.commit()
        conn.close()
        app = create_app(db_path)
        app.testing = True
        self.client = app.test_client()

    def test_list_shows_both_with_and_without_enrichment(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Tariff pressure on Europe", page)
        self.assertIn("squeeze European carmakers", page)
        self.assertIn("Episode 12: chips", page)          # no enrichment row
        self.assertIn("no enrichment", page)

    def test_keyword_search_finds_the_podcast(self):
        """Search must reach records that have no body text (via description),
        and an empty vector index must be stated, not silently absorbed."""
        page = self.client.get("/?q=semiconductors").get_data(as_text=True)
        self.assertIn("Episode 12: chips", page)
        self.assertIn("no stored vectors", page)

    def test_search_degrades_visibly_when_ollama_is_down(self):
        conn = db.connect(Path(self.tmp.name) / "test.db")
        row = {"source_id": self.report_id, "publication_id": self.report_id}
        db.store_embeddings(conn, "one_liner", embed.MODEL, [row],
                            [[0.0] * embed.DIM], embed.pack)
        conn.commit()
        conn.close()
        with mock.patch("pubbrain.embed.embed_query",
                        side_effect=embed.OllamaUnreachable("down")):
            page = self.client.get("/?q=semiconductors").get_data(as_text=True)
        self.assertIn("Episode 12: chips", page)
        self.assertIn("paraphrase matching is off", page)

    def test_detail_page_carries_enrichment_and_source_link(self):
        page = self.client.get(f"/pub/{self.report_id}").get_data(as_text=True)
        self.assertIn("squeeze European carmakers", page)
        self.assertIn("Tariffs rose", page)
        self.assertIn("https://merics.org/en/report/tariff-report", page)
        self.assertIn("Antonia Hmaidi", page)

    def test_person_page_states_the_credit_caveat(self):
        # ?all=1: the fixture has no roster sync, so nobody is "current" (#28)
        page = self.client.get("/people?all=1").get_data(as_text=True)
        self.assertIn("Antonia Hmaidi", page)
        person_id = page.split('/person/')[1].split('"')[0]
        page = self.client.get(f"/person/{person_id}").get_data(as_text=True)
        self.assertIn("under-report", page)               # the #12 caveat
        self.assertIn("Tariff pressure on Europe", page)

    def test_flag_persists_updates_and_clears(self):
        resp = self.client.post(f"/pub/{self.report_id}/flag",
                                data={"note": "too bullish on Europe"})
        self.assertEqual(resp.status_code, 302)
        flags = self.client.get("/flags").get_data(as_text=True)
        self.assertIn("Tariff pressure on Europe", flags)
        self.assertIn("too bullish on Europe", flags)
        # re-flagging replaces the note rather than stacking rows
        self.client.post(f"/pub/{self.report_id}/flag", data={"note": "wrong emphasis"})
        conn = db.connect(Path(self.tmp.name) / "test.db")
        rows = conn.execute("SELECT * FROM reviews").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "flagged")
        self.assertEqual(rows[0]["note"], "wrong emphasis")
        # clearing removes the flag entirely
        self.client.post(f"/pub/{self.report_id}/flag", data={"action": "clear"})
        conn = db.connect(Path(self.tmp.name) / "test.db")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 0)
        conn.close()

    def _add_candidate(self, one_liner="A second opinion on the tariffs."):
        conn = db.connect(Path(self.tmp.name) / "test.db")
        cid = db.add_enrichment(conn, self.report_id, {
            "summary_one_liner": one_liner, "summary_short": "Other reading.",
            "key_findings": ["x"], "entities": {}},
            {**META, "model": "model-b"})
        conn.commit()
        conn.close()
        return cid

    def test_a_candidate_is_shown_but_marked_as_used_by_nothing(self):
        self._add_candidate()
        page = self.client.get(f"/pub/{self.report_id}").get_data(as_text=True)
        self.assertIn("A second opinion on the tariffs.", page)
        self.assertIn("Candidate", page)
        self.assertIn("Primary", page)

    def test_a_candidate_does_not_duplicate_the_record_in_the_catalog(self):
        """The silent-doubling failure this schema invites: no error, just the
        publication listed twice and every count inflated."""
        self._add_candidate()
        page = self.client.get("/").get_data(as_text=True)
        self.assertEqual(page.count("Tariff pressure on Europe"), 1)

    def test_promoting_swaps_which_summary_the_catalog_shows(self):
        cid = self._add_candidate()
        resp = self.client.post(f"/pub/{self.report_id}/promote/{cid}")
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("A second opinion on the tariffs.", page)
        self.assertEqual(page.count("Tariff pressure on Europe"), 1)

    def test_dismissing_removes_the_candidate_and_keeps_the_primary(self):
        cid = self._add_candidate()
        self.client.post(f"/pub/{self.report_id}/dismiss/{cid}")
        page = self.client.get(f"/pub/{self.report_id}").get_data(as_text=True)
        self.assertNotIn("A second opinion on the tariffs.", page)
        self.assertIn("tariffs squeeze European carmakers", page)

    def test_the_primary_cannot_be_dismissed_through_the_route(self):
        """No button offers this, but a hand-typed URL must not be able to
        leave a publication with no summary at all."""
        conn = db.connect(Path(self.tmp.name) / "test.db")
        primary = db.primary_enrichment_id(conn, self.report_id)
        conn.close()
        resp = self.client.post(f"/pub/{self.report_id}/dismiss/{primary}")
        self.assertEqual(resp.status_code, 404)
        page = self.client.get(f"/pub/{self.report_id}").get_data(as_text=True)
        self.assertIn("tariffs squeeze European carmakers", page)

    def test_another_publications_summary_cannot_be_promoted_here(self):
        cid = self._add_candidate()
        resp = self.client.post(f"/pub/{self.podcast_id}/promote/{cid}")
        self.assertEqual(resp.status_code, 404)

    def test_several_types_widen_the_selection(self):
        page = self.client.get("/?type=Report&type=Podcast").get_data(as_text=True)
        self.assertIn("Tariff pressure on Europe", page)
        self.assertIn("Episode 12: chips", page)

    def test_one_type_excludes_the_other(self):
        """The motivating case (#26): drop podcasts, keep everything else."""
        page = self.client.get("/?type=Report").get_data(as_text=True)
        self.assertIn("Tariff pressure on Europe", page)
        self.assertNotIn("Episode 12: chips", page)

    def test_no_selection_means_all_not_none(self):
        page = self.client.get("/?type=").get_data(as_text=True)
        self.assertIn("Tariff pressure on Europe", page)
        self.assertIn("Episode 12: chips", page)

    def test_a_year_selection_comes_back_ticked(self):
        """A filter whose state is not reflected back cannot be adjusted —
        you would have to rebuild it from scratch on every change."""
        page = self.client.get("/?year=2025").get_data(as_text=True)
        self.assertIn("Tariff pressure on Europe", page)
        self.assertIn('value="2025"\n        checked', page)

    def test_a_selection_survives_into_the_paging_links(self):
        """A filter that silently dies on page 2 is worse than no filter."""
        page = self.client.get("/?type=Report&type=Podcast&page=1").get_data(as_text=True)
        self.assertIn('value="Report"\n        checked', page)
        self.assertIn('value="Podcast"\n        checked', page)

    def test_filters_narrow_search_results_too(self):
        page = self.client.get("/?q=semiconductors&type=Report").get_data(as_text=True)
        self.assertNotIn("Episode 12: chips", page)

    def test_flags_and_settings_sit_in_the_header_not_the_nav(self):
        """They are maintenance and configuration, not destinations, so they
        must not compete with Catalog/People/Ask in the nav."""
        page = self.client.get("/").get_data(as_text=True)
        header = page.split("<header>")[1].split("</header>")[0]
        nav = page.split('<nav class="top">')[1].split("</nav>")[0]
        self.assertIn("/flags", header)
        self.assertIn("/settings", header)
        self.assertNotIn("/flags", nav)
        self.assertNotIn("/settings", nav)
        self.assertIn("/glossary", nav)

    def test_settings_reports_config_without_leaking_the_key(self):
        with mock.patch("pubbrain.llm.has_api_key", return_value=True):
            page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("default", page)
        self.assertIn("in keyring", page)
        self.assertIn("provider/large-instruct", page)

    def test_settings_says_so_when_no_key_is_reachable(self):
        with mock.patch("pubbrain.llm.has_api_key", return_value=False):
            page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("not found", page)

    def test_flags_page_reports_spot_check_stats(self):
        conn = db.connect(Path(self.tmp.name) / "test.db")
        db.upsert_review(conn, "enrichment", self.report_id, "confirmed",
                         "fable-sample: checked ok")
        conn.commit()
        conn.close()
        page = self.client.get("/flags").get_data(as_text=True)
        self.assertIn("1</span> checked ok", page)
        self.assertIn("Nothing flagged", page)

    def test_glossary_renders_the_vocabulary(self):
        page = self.client.get("/glossary").get_data(as_text=True)
        self.assertIn("China-Russia", page)
        self.assertIn("conference-panel", page)

    def test_flagging_needs_an_enrichment_row(self):
        resp = self.client.post(f"/pub/{self.podcast_id}/flag", data={"note": "x"})
        self.assertEqual(resp.status_code, 404)


class TestRegeneratePicker(unittest.TestCase):
    """#39. The button existed only on a flagged summary and the picker chose a
    provider rather than a model, so a configured model could be unreachable at
    the same time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(db_path)
        self.report_id = db.upsert_publication(conn, REPORT)
        db.upsert_text(conn, self.report_id, "Beijing imposed tariffs.", 3)
        db.upsert_primary_enrichment(conn, self.report_id, ENRICHMENT, META)
        db.rebuild_fts(conn)
        conn.commit()
        conn.close()
        app = create_app(db_path)
        app.testing = True
        self.client = app.test_client()

    def _page(self):
        return self.client.get(f"/pub/{self.report_id}").get_data(as_text=True)

    def test_an_unflagged_summary_can_still_be_regenerated(self):
        """Nothing is flagged in this fixture — that is the point."""
        self.assertIn("dlg-regen", self._page())

    def test_every_model_the_provider_offers_is_offered(self):
        page = self._page()
        self.assertIn("provider/small-instruct", page)
        self.assertIn("provider/large-vision", page)

    def test_the_form_carries_provider_and_model_together(self):
        """One control, because the pair has to agree — asking one provider for
        another's model fails as an opaque upstream 400."""
        self.assertIn("default:provider/small-instruct", self._page())


class TestPickedModel(unittest.TestCase):
    def test_a_model_id_containing_a_colon_survives_the_split(self):
        """`hf:some-org/some-model` has its own colon, so only the first one
        separates provider from model."""
        from pubbrain.web import _picked_model
        self.assertEqual(
            _picked_model({"model": "default:hf:some-org/some-model"}),
            ("default", "hf:some-org/some-model"))

    def test_an_unknown_provider_falls_back_rather_than_calling_it(self):
        from pubbrain import llm
        from pubbrain.web import _picked_model
        self.assertEqual(_picked_model({"model": "madeup:some-model"}),
                         (llm.DEFAULT_PROVIDER, None))

    def test_no_selection_means_the_default_provider_and_its_default_model(self):
        from pubbrain import llm
        from pubbrain.web import _picked_model
        self.assertEqual(_picked_model({}), (llm.DEFAULT_PROVIDER, None))


class TestTopicFilter(unittest.TestCase):
    """The glossary's way into the catalog (#22). The counts it prints and the
    list it links to are two different queries, and they have to agree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(db_path)
        self.about = db.upsert_publication(conn, REPORT)
        self.touching = db.upsert_publication(conn, {
            **REPORT, "slug": "other", "url": "https://merics.org/en/report/other",
            "title": "A piece that only touches it"})
        for pid in (self.about, self.touching):
            db.upsert_text(conn, pid, "Beijing imposed tariffs.", 3)
            db.upsert_primary_enrichment(conn, pid, ENRICHMENT, META)
        # position 1 is the model's "what this is about"; 2 is a topic touched.
        conn.executemany(
            "INSERT INTO publication_topics (publication_id, topic_slug, position,"
            " model, prompt_version, mapped_at) VALUES (?, ?, ?, 'm', 1, 'now')",
            [(self.about, "xi-party-rule", 1), (self.touching, "xi-party-rule", 2)])
        db.rebuild_fts(conn)
        conn.commit()
        conn.close()
        app = create_app(db_path)
        app.testing = True
        self.client = app.test_client()

    def _titles(self, url):
        page = self.client.get(url).get_data(as_text=True)
        return ("Tariff pressure on Europe" in page,
                "A piece that only touches it" in page)

    def test_primary_narrows_to_what_the_piece_is_about(self):
        self.assertEqual(self._titles("/?topic=xi-party-rule&primary=1"),
                         (True, False))

    def test_without_primary_every_carrier_is_listed(self):
        self.assertEqual(self._titles("/?topic=xi-party-rule"), (True, True))

    def test_an_unmapped_topic_lists_nothing_rather_than_everything(self):
        """A filter that silently matches all is the dangerous failure — it
        reads as 'MERICS covers this heavily'."""
        self.assertEqual(self._titles("/?topic=climate-cooperation"), (False, False))

    def test_the_glossary_count_matches_the_page_it_links_to(self):
        page = self.client.get("/glossary").get_data(as_text=True)
        self.assertIn("1 about this", page)
        self.assertIn("2 touching it", page)

    def test_the_filter_survives_paging(self):
        """Dropped from the paging links, page 2 quietly shows the whole
        catalog under the topic's heading."""
        page = self.client.get("/?topic=xi-party-rule&page=1").get_data(as_text=True)
        self.assertIn("topic=xi-party-rule", page)

    def test_the_record_page_links_its_topics_back_to_the_catalog(self):
        page = self.client.get(f"/pub/{self.about}").get_data(as_text=True)
        self.assertIn("topic=xi-party-rule", page)
        self.assertIn("Xi Jinping &amp; Party Rule", page)


class TestSeriesFilter(unittest.TestCase):
    """#32's last item: series is populated on ~350 records and filterable
    nowhere. The facet, the record-page link and the query must agree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(db_path)
        self.essentials = db.upsert_publication(conn, {
            **REPORT, "series": "MERICS China Essentials"})
        self.tracker = db.upsert_publication(conn, {
            **REPORT, "slug": "t1", "url": "https://merics.org/en/tracker/t1",
            "title": "Security and Risk Tracker 01",
            "series": "MERICS China Security and Risk Tracker"})
        self.loose = db.upsert_publication(conn, {
            **REPORT, "slug": "loose", "url": "https://merics.org/en/report/loose",
            "title": "A report in no series", "series": None})
        db.rebuild_fts(conn)
        conn.commit()
        conn.close()
        app = create_app(db_path)
        app.testing = True
        self.client = app.test_client()

    def _titles(self, url):
        page = self.client.get(url).get_data(as_text=True)
        return ("Tariff pressure on Europe" in page,
                "Security and Risk Tracker 01" in page,
                "A report in no series" in page)

    def test_one_series_narrows_to_its_run(self):
        self.assertEqual(
            self._titles("/?series=MERICS+China+Essentials&filtered=1"),
            (True, False, False))

    def test_several_series_widen_like_types(self):
        self.assertEqual(
            self._titles("/?series=MERICS+China+Essentials"
                         "&series=MERICS+China+Security+and+Risk+Tracker"
                         "&filtered=1"),
            (True, True, False))

    def test_the_facet_lists_runs_with_counts(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("all series", page)
        self.assertIn("MERICS China Essentials", page)

    def test_search_results_narrow_too(self):
        page = self.client.get(
            "/?q=Tariff&series=MERICS+China+Security+and+Risk+Tracker"
            "&filtered=1").get_data(as_text=True)
        self.assertNotIn("Tariff pressure on Europe", page)

    def test_the_selection_survives_paging_links(self):
        page = self.client.get(
            "/?series=MERICS+China+Essentials&filtered=1").get_data(as_text=True)
        self.assertIn("MERICS+China+Essentials", page)

    def test_the_record_page_links_its_series_into_the_catalog(self):
        page = self.client.get(f"/pub/{self.essentials}").get_data(as_text=True)
        self.assertIn("series=MERICS+China+Essentials", page)


if __name__ == "__main__":
    unittest.main()


class TestFacetSummaries(unittest.TestCase):
    """#26 follow-up: the summary label listed every selected value, so eight
    ticked types made the control wider than the search box."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(db_path)
        base = {"slug": "s", "url": "u", "title": "T", "subtitle": None,
                "date_published": "2025-01-01", "pub_type": "Report",
                "series": None, "access": "public", "pdf_url": None,
                "og_description": None, "people": [], "site_tags": []}
        for i, (kind, yr) in enumerate(
                [("Report", 2023), ("Comment", 2024), ("Podcast", 2025),
                 ("Tracker", 2026)]):
            db.upsert_publication(conn, {**base, "slug": f"p{i}", "url": f"u/{i}",
                                         "title": f"P{i}", "pub_type": kind,
                                         "date_published": f"{yr}-01-01"})
        conn.commit()
        conn.close()
        app = create_app(db_path)
        app.testing = True
        self.client = app.test_client()

    def summary(self, query, which=0):
        page = self.client.get(query).get_data(as_text=True)
        return page.split("<summary>")[which + 1].split("</summary>")[0].strip()

    def test_nothing_selected_reads_as_all(self):
        self.assertEqual(self.summary("/"), "all types")
        self.assertEqual(self.summary("/", 1), "all years")

    def test_one_selected_shows_its_name(self):
        self.assertEqual(self.summary("/?type=Report"), "Report")

    def test_several_selected_collapse_to_a_count(self):
        got = self.summary("/?type=Report&type=Comment&type=Podcast")
        self.assertEqual(got, "3 of 4 types")
        self.assertNotIn("Comment", got)

    def test_contiguous_years_read_as_a_range(self):
        self.assertEqual(self.summary("/?year=2024&year=2025&year=2026", 1),
                         "2024–2026")

    def test_a_gappy_year_selection_is_not_faked_as_a_range(self):
        """2023 and 2026 is not 2023-2026 — the label would claim four years."""
        self.assertEqual(self.summary("/?year=2023&year=2026", 1), "2 years")

    def test_the_dropdown_offers_bulk_toggles(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("facetSet(this, true)", page)
        self.assertIn("facetSet(this, false)", page)


class TestDefaultView(unittest.TestCase):
    """#30. A default that hides most of the catalog must be overridable and
    must announce itself — the same rule as the People default (#28)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        base = {"slug": "s", "url": "u", "title": "T", "subtitle": None,
                "date_published": "2025-01-01", "pub_type": "Report",
                "series": None, "access": "public", "pdf_url": None,
                "og_description": None, "people": [], "site_tags": []}
        for i, kind in enumerate(["Report", "MERICS Briefs", "Podcast",
                                  "Comment", "Interview"]):
            db.upsert_publication(conn, {**base, "slug": f"p{i}", "url": f"u/{i}",
                                         "title": f"A {kind} record",
                                         "pub_type": kind})
        conn.commit()
        conn.close()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def save(self, *types):
        return self.client.post("/settings/default-view",
                                data={"type": list(types)})

    def test_with_no_default_the_catalog_opens_on_everything(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("A Podcast record", page)
        self.assertIn("all types", page)

    def test_a_saved_default_applies_on_a_bare_visit(self):
        self.save("Report", "MERICS Briefs")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("A Report record", page)
        self.assertIn("A MERICS Briefs record", page)
        self.assertNotIn("A Podcast record", page)

    def test_the_default_announces_itself_rather_than_filtering_silently(self):
        self.save("Report")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Showing your default view", page)
        self.assertIn("show all types", page)

    def test_an_explicit_selection_beats_the_default(self):
        self.save("Report")
        page = self.client.get("/?type=Podcast&filtered=1").get_data(as_text=True)
        self.assertIn("A Podcast record", page)
        self.assertNotIn("A Report record", page)

    def test_explicitly_asking_for_all_types_beats_the_default(self):
        """The case the `filtered` marker exists for: an empty selection from
        the form means all, and must not silently fall back to the default."""
        self.save("Report")
        page = self.client.get("/?filtered=1").get_data(as_text=True)
        self.assertIn("A Podcast record", page)
        self.assertNotIn("Showing your default view", page)

    def test_saving_none_clears_the_default(self):
        self.save("Report")
        self.client.post("/settings/default-view", data={})
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("A Podcast record", page)
        self.assertNotIn("Showing your default view", page)

    def test_the_choice_survives_a_restart(self):
        """It is a preference, not session state — a new app must see it."""
        self.save("Report", "Tracker")
        app = create_app(self.db_path)
        app.testing = True
        page = app.test_client().get("/").get_data(as_text=True)
        self.assertNotIn("A Podcast record", page)

    def test_the_settings_form_shows_what_is_saved(self):
        self.save("Report")
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('value="Report"\n      checked', page)


class TestBacklog(unittest.TestCase):
    """#33. The list is only worth reading if it excludes what is complete —
    a flat threshold flagged short Comments that were fine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        base = {"slug": "s", "url": "u", "title": "T", "subtitle": None,
                "date_published": "2025-01-01", "pub_type": "Report",
                "series": None, "access": "public", "pdf_url": None,
                "og_description": None, "people": [], "site_tags": []}
        # Reports run long; Comments run short. Five of each so a median exists.
        for i in range(5):
            pid = db.upsert_publication(conn, {
                **base, "slug": f"r{i}", "url": f"https://merics.org/en/report/r{i}",
                "title": f"Full report {i}"})
            db.upsert_text(conn, pid, "word " * 4000, 4000)
            pid = db.upsert_publication(conn, {
                **base, "slug": f"c{i}", "url": f"https://merics.org/en/comment/c{i}",
                "title": f"Short comment {i}", "pub_type": "Comment"})
            db.upsert_text(conn, pid, "word " * 900, 900)
        # a stub Report, and a Comment that is short but normal for its type
        self.stub = db.upsert_publication(conn, {
            **base, "slug": "stub", "url": "https://merics.org/en/report/stub",
            "title": "Landing page abstract only", "pdf_url": "https://x/y.pdf"})
        db.upsert_text(conn, self.stub, "word " * 226, 226)
        self.ok_comment = db.upsert_publication(conn, {
            **base, "slug": "tech", "url": "https://merics.org/en/comment/tech",
            "title": "A complete short tech note", "pub_type": "Comment"})
        db.upsert_text(conn, self.ok_comment, "word " * 380, 380)
        conn.commit()
        conn.close()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def test_a_stub_report_is_listed(self):
        page = self.client.get("/backlog").get_data(as_text=True)
        self.assertIn("Landing page abstract only", page)

    def test_a_short_but_normal_comment_is_not(self):
        """380 words is a complete tech note. Listing it would train the eye to
        skip the page."""
        page = self.client.get("/backlog").get_data(as_text=True)
        self.assertNotIn("A complete short tech note", page)

    def test_thresholds_are_per_type(self):
        from pubbrain import queries
        conn = db.connect(self.db_path)
        norms = queries.type_length_norms(conn)
        conn.close()
        self.assertGreater(norms["Report"][1], norms["Comment"][1])

    def test_podcasts_are_never_listed_as_thin(self):
        """No prose by design, so a podcast is not unfinished work."""
        from pubbrain import queries
        conn = db.connect(self.db_path)
        self.assertNotIn("Podcast", queries.type_length_norms(conn))
        conn.close()

    def test_every_row_carries_a_source_link(self):
        """The owner resolves these by looking at the page."""
        page = self.client.get("/backlog").get_data(as_text=True)
        self.assertIn("https://merics.org/en/report/stub", page)

    def test_the_pdf_is_offered_where_one_exists(self):
        page = self.client.get("/backlog").get_data(as_text=True)
        self.assertIn("https://x/y.pdf", page)

    def test_homeless_and_gone_urls_appear(self):
        conn = db.connect(self.db_path)
        db.upsert_sitemap_url(conn, "https://merics.org/en/mystery-page", "/en/",
                              "2025-01-01", "root-level")
        db.upsert_sitemap_url(conn, "https://merics.org/en/podcast/dead", "/en/podcast/",
                              "2025-01-01", "publication")
        db.mark_url(conn, "https://merics.org/en/podcast/dead", "gone")
        conn.commit()
        conn.close()
        page = self.client.get("/backlog").get_data(as_text=True)
        self.assertIn("mystery-page", page)
        self.assertIn("podcast/dead", page)

    def test_backlog_is_a_utility_link_beside_flags(self):
        page = self.client.get("/").get_data(as_text=True)
        header = page.split("<header>")[1].split("</header>")[0]
        self.assertIn("/backlog", header)

    def test_parking_takes_a_record_off_the_list_without_changing_it(self):
        """A graphics-only piece is *meant* to have 34 words. Parking records
        that a human looked and decided, rather than editing the record."""
        page = self.client.get("/backlog").get_data(as_text=True)
        self.assertIn("Landing page abstract only", page)
        self.client.post(f"/pub/{self.stub}/park",
                         data={"note": "infographic, no prose exists"})
        page = self.client.get("/backlog").get_data(as_text=True)
        self.assertNotIn("Landing page abstract only", page)
        self.assertIn("1 record", page)
        conn = db.connect(self.db_path)
        row = conn.execute("SELECT word_count FROM publication_text "
                           "WHERE publication_id = ?", (self.stub,)).fetchone()
        conn.close()
        self.assertEqual(row["word_count"], 226)     # untouched

    def test_parked_records_can_be_shown_and_unparked(self):
        self.client.post(f"/pub/{self.stub}/park", data={"note": "fine as is"})
        page = self.client.get("/backlog?parked=1").get_data(as_text=True)
        self.assertIn("fine as is", page)
        self.client.post(f"/pub/{self.stub}/park", data={"action": "unpark"})
        self.assertIn("Landing page abstract only",
                      self.client.get("/backlog").get_data(as_text=True))


class TestHomelessDisposition(unittest.TestCase):
    """#10/#33. Resolving 170 URLs one button at a time is not a workflow, so
    the proposals are measured, pre-labelled and confirmed in bulk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        for url, words, has_date, title in (
                ("https://merics.org/en/event-a", 64, False, "Registration Form"),
                ("https://merics.org/en/event-b", 55, False, "Anmeldung"),
                ("https://merics.org/en/real-piece", 4037, True,
                 "Executive Summary: exploring European approaches"),
                ("https://merics.org/en/policy-piece", 3000, True,
                 "Registration and accreditation policy")):
            db.upsert_sitemap_url(conn, url, "/en/", "2025-01-01", "root-level")
            db.set_url_probe(conn, url, words, has_date, title)
        conn.commit()
        conn.close()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)

    def proposed(self, url):
        return self.conn.execute(
            "SELECT disposition FROM sitemap_urls WHERE url = ?", (url,)).fetchone()[0]

    def test_event_pages_are_proposed_for_exclusion(self):
        self.assertEqual(self.proposed("https://merics.org/en/event-a"), "exclude")
        self.assertEqual(self.proposed("https://merics.org/en/event-b"), "exclude")

    def test_a_real_publication_is_proposed_for_ingest(self):
        self.assertEqual(self.proposed("https://merics.org/en/real-piece"), "ingest")

    def test_the_title_rule_does_not_swallow_a_real_publication(self):
        """"Registration and accreditation policy" is a publication. Matching
        the event title as a prefix excluded it — the same over-broad matching
        that swept two real reports into "site furniture" earlier."""
        self.assertEqual(self.proposed("https://merics.org/en/policy-piece"), "ingest")

    def test_bulk_confirm_applies_to_every_selected_url(self):
        r = self.client.post("/url-dispositions", data={
            "disposition": "exclude", "bulk_note": "registration form",
            "url": ["https://merics.org/en/event-a", "https://merics.org/en/event-b"]})
        self.assertEqual(r.status_code, 302)
        for u in ("https://merics.org/en/event-a", "https://merics.org/en/event-b"):
            row = self.conn.execute(
                "SELECT disposition, note FROM sitemap_urls WHERE url = ?", (u,)).fetchone()
            self.assertEqual(row["disposition"], "exclude")
            self.assertEqual(row["note"], "registration form")

    def test_handing_a_url_over_puts_it_in_the_work_queue(self):
        self.client.post("/url-disposition", data={
            "url": "https://merics.org/en/real-piece", "disposition": "todo",
            "note": "ETNC 2023 exec summary — ingest as Report, series ETNC"})
        page = self.client.get("/backlog").get_data(as_text=True)
        self.assertIn("Handed over", page)
        self.assertIn("ETNC 2023 exec summary", page)

    def test_a_publication_can_be_handed_over_with_instructions(self):
        pid = db.upsert_publication(self.conn, {
            "slug": "x", "url": "https://merics.org/en/report/x", "title": "A stub",
            "subtitle": None, "date_published": "2025-01-01", "pub_type": "Report",
            "series": None, "access": "public", "pdf_url": None,
            "og_description": None, "people": [], "site_tags": []})
        self.conn.commit()
        self.client.post(f"/pub/{pid}/park",
                         data={"action": "todo", "note": "attach the root-level chapters"})
        rows = db.backlog_todo(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "attach the root-level chapters")

    def test_a_confirmed_url_leaves_the_list(self):
        """Working in bulk is pointless if the list never shortens."""
        before = len(db.homeless_urls(self.conn))
        self.client.post("/url-dispositions", data={
            "disposition": "exclude", "url": ["https://merics.org/en/event-a"]})
        self.assertEqual(len(db.homeless_urls(self.conn)), before - 1)
        self.assertEqual(len(db.homeless_urls(self.conn, include_settled=True)), before)

    def test_a_hand_over_also_leaves_the_working_list(self):
        """It is not finished work, but it is off the owner's desk and still
        listed under "Handed over". The list he works through must empty."""
        before = len(db.homeless_urls(self.conn))
        url = "https://merics.org/en/real-piece"
        self.client.post("/url-disposition", data={
            "url": url, "disposition": "todo", "note": "ingest as Report"})
        self.assertEqual(len(db.homeless_urls(self.conn)), before - 1)
        page = self.client.get("/backlog").get_data(as_text=True)
        self.assertIn("ingest as Report", page)      # still visible above

    def test_the_per_row_exclude_button_settles_one_url(self):
        r = self.client.post("/url-row",
                             data={"exclude": "https://merics.org/en/event-b"})
        self.assertEqual(r.status_code, 302)
        row = self.conn.execute(
            "SELECT disposition, settled_at FROM sitemap_urls WHERE url = ?",
            ("https://merics.org/en/event-b",)).fetchone()
        self.assertEqual(row["disposition"], "exclude")
        self.assertIsNotNone(row["settled_at"])

    def test_shift_range_selection_is_wired_up(self):
        page = self.client.get("/backlog").get_data(as_text=True)
        self.assertIn("e.shiftKey", page)
        self.assertIn('class="pick"', page)

    def test_a_row_can_be_handed_over_with_its_own_note(self):
        """The two verbs the owner actually uses. Rows sit inside the bulk
        form — nested forms are invalid — so each note is named for its URL."""
        url = "https://merics.org/en/real-piece"
        r = self.client.post("/url-row", data={
            "claude": url, f"note__{url}": "ETNC 2023 exec summary, ingest as Report"})
        self.assertEqual(r.status_code, 302)
        row = self.conn.execute(
            "SELECT disposition, note, settled_at FROM sitemap_urls WHERE url = ?",
            (url,)).fetchone()
        self.assertEqual(row["disposition"], "todo")
        self.assertEqual(row["note"], "ETNC 2023 exec summary, ingest as Report")
        self.assertIsNotNone(row["settled_at"])       # off his desk, onto mine

    def test_a_row_note_does_not_leak_onto_another_row(self):
        a, b = "https://merics.org/en/event-a", "https://merics.org/en/event-b"
        self.client.post("/url-row", data={
            "exclude": a, f"note__{a}": "for A", f"note__{b}": "for B"})
        self.assertEqual(self.conn.execute(
            "SELECT note FROM sitemap_urls WHERE url = ?", (a,)).fetchone()["note"],
            "for A")
        self.assertIsNone(self.conn.execute(
            "SELECT note FROM sitemap_urls WHERE url = ?", (b,)).fetchone()["note"])

    def test_pressing_neither_button_is_rejected(self):
        self.assertEqual(self.client.post("/url-row", data={}).status_code, 400)


class TestSubtitleSeries(unittest.TestCase):
    """#10. The pages that predate typed URLs name their own run in the
    subtitle — "China Update 1/2019" is what became MERICS China Essentials in
    June 2020. That supplies the type *and* the series, so ingesting them is
    reading the page rather than inferring anything."""

    def test_a_china_update_subtitle_names_its_series(self):
        self.assertEqual(db.series_from_subtitle("China Update 1/2019"),
                         ("MERICS Briefs", "MERICS China Essentials"))

    def test_spacing_variants_are_tolerated(self):
        self.assertEqual(db.series_from_subtitle("China Update 12 / 2018")[1],
                         "MERICS China Essentials")

    def test_an_unrelated_subtitle_names_nothing(self):
        self.assertIsNone(db.series_from_subtitle("A subtitle about China"))
        self.assertIsNone(db.series_from_subtitle(None))

    def test_a_named_series_overrides_the_word_count_proposal(self):
        """A short China Update would otherwise be proposed for exclusion."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            url = "https://merics.org/en/an-old-update"
            db.upsert_sitemap_url(conn, url, "/en/", "2019-01-10", "root-level")
            db.set_url_probe(conn, url, 120, False, "A headline",
                             "China Update 1/2019")
            row = conn.execute(
                "SELECT disposition, proposed_type, proposed_series "
                "FROM sitemap_urls WHERE url = ?", (url,)).fetchone()
            self.assertEqual(row["disposition"], "ingest")
            self.assertEqual(row["proposed_type"], "MERICS Briefs")
            self.assertEqual(row["proposed_series"], "MERICS China Essentials")
            conn.close()


class TestPersonFilter(unittest.TestCase):
    """One person's credits, filtered by keyword and type (#61).

    A filter, not the catalog's hybrid retrieval: over a few dozen credits
    ranking buys nothing, and a rank cut would drop exactly the records that
    have no summary — which is where credits are thinnest to begin with.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        person = [{"slug": "a-hmaidi", "name": "Antonia Hmaidi",
                   "is_internal": True, "job_title": "Analyst", "role": "author"}]
        for slug, title, pub_type, one_liner in [
                ("chips", "Chip self-sufficiency stalls", "Report",
                 "Beijing's fab buildout misses its own targets."),
                ("rare-earths", "Rare earth controls", "Comment",
                 "Export licensing becomes the lever of choice."),
                ("podcast-1", "Episode 40: the plenum", "Podcast", None)]:
            pub_id = db.upsert_publication(conn, {
                "slug": slug, "url": f"https://merics.org/en/x/{slug}",
                "title": title, "subtitle": None, "date_published": "2026-01-01",
                "pub_type": pub_type, "series": None, "access": "public",
                "pdf_url": None, "og_description": None, "people": person,
                "site_tags": []})
            if one_liner:
                db.upsert_primary_enrichment(conn, pub_id, {
                    "summary_one_liner": one_liner, "summary_short": "Short.",
                    "key_findings": [], "entities": {}}, META)
        conn.commit()
        conn.close()
        self.person_id = 1
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def page(self, **args):
        return self.client.get(f"/person/{self.person_id}",
                               query_string=args).get_data(as_text=True)

    def test_a_keyword_narrows_the_list(self):
        page = self.page(q="rare earth")
        self.assertIn("Rare earth controls", page)
        self.assertNotIn("Chip self-sufficiency", page)

    def test_the_keyword_reaches_the_summary_not_only_the_title(self):
        """The one-liner is where the subject usually is — "fab" appears in no
        title here."""
        self.assertIn("Chip self-sufficiency", self.page(q="fab buildout"))

    def test_every_word_has_to_appear_somewhere(self):
        """Same AND-ing as the keyword index, without its match syntax: one
        word from the title and one from the summary still finds it."""
        self.assertIn("Chip self-sufficiency", self.page(q="chip targets"))
        self.assertNotIn("Chip self-sufficiency", self.page(q="chip lever"))

    def test_a_record_with_no_summary_is_filterable_by_its_title(self):
        """Podcasts carry no enrichment (#50) — a filter that only read
        summaries would make them unreachable here."""
        self.assertIn("Episode 40", self.page(q="plenum"))

    def test_the_type_facet_narrows_and_keeps_its_unfiltered_counts(self):
        page = self.page(type="Report")
        self.assertIn("Chip self-sufficiency", page)
        self.assertNotIn("Rare earth controls", page)
        # The facet still offers Comment and Podcast, or narrowing once would
        # be a one-way door.
        self.assertIn("Podcast", page)

    def test_the_page_says_how_much_it_is_hiding(self):
        self.assertIn("1 of 3 credits shown", self.page(q="rare earth"))

    def test_an_empty_result_says_what_the_filter_does_not_read(self):
        page = self.page(q="submarine cables")
        self.assertIn("not the body text", page)

    def test_no_filter_shows_everything_and_no_count_line(self):
        page = self.page()
        for title in ("Chip self-sufficiency", "Rare earth controls", "Episode 40"):
            self.assertIn(title, page)
        self.assertNotIn("credits shown", page)
