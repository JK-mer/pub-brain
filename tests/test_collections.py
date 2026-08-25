"""Standing projects and their members (#32).

The two rules that matter: detection must read the page's own article, not the
site-wide promo block, and a hand-curated membership must survive detection
re-running.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pubbrain import collect, db
from pubbrain.web import create_app

BASE = {
    "slug": "s", "url": "u", "title": "T", "subtitle": None,
    "date_published": "2025-01-01", "pub_type": "Comment", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [], "site_tags": [],
}
DASH = "/en/china-russia-dashboard"

IN_ARTICLE = f"""<html><body><article><div class="view-mode-full">
  <p>Analysis citing the <a href="{DASH}">China-Russia Dashboard</a>.</p>
</div></article></body></html>"""

PROMO_ONLY = f"""<html><body>
  <article><div class="view-mode-full"><p>Unrelated analysis.</p></div></article>
  <aside><article><a href="{DASH}">China-Russia Dashboard</a></article></aside>
</body></html>"""


class TestDetection(unittest.TestCase):
    def test_a_link_in_the_article_counts(self):
        self.assertTrue(collect.links_to(IN_ARTICLE, DASH))

    def test_a_link_only_in_a_promo_block_does_not(self):
        """A naive scan returned 66 pages for the Tech Observatory; 60 were
        this. Teasers reuse the article markup — the trap Phase-1-Plan names."""
        self.assertFalse(collect.links_to(PROMO_ONLY, DASH))

    def test_a_page_that_never_mentions_it_is_cheap_to_reject(self):
        self.assertFalse(collect.links_to("<html><body>nothing</body></html>", DASH))


class TestMembership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        db.upsert_collection(self.conn, "crd", "China-Russia Dashboard",
                             "https://merics.org/en/china-russia-dashboard")
        self.pid = db.upsert_publication(self.conn, {
            **BASE, "slug": "a", "url": "https://merics.org/en/comment/a",
            "title": "A member"})
        self.other = db.upsert_publication(self.conn, {
            **BASE, "slug": "b", "url": "https://merics.org/en/comment/b",
            "title": "Not a member"})
        self.conn.commit()

    def test_auto_never_overwrites_manual(self):
        """A curated membership has to survive every re-scrape; detection runs
        constantly and would otherwise quietly undo hand corrections."""
        db.add_to_collection(self.conn, self.pid, "crd", source="manual")
        self.assertFalse(db.add_to_collection(self.conn, self.pid, "crd", source="auto"))
        self.assertEqual(db.collections_for(self.conn, self.pid)[0]["source"], "manual")

    def test_manual_upgrades_an_auto_membership(self):
        db.add_to_collection(self.conn, self.pid, "crd", source="auto")
        self.assertTrue(db.add_to_collection(self.conn, self.pid, "crd", source="manual"))
        self.assertEqual(db.collections_for(self.conn, self.pid)[0]["source"], "manual")

    def test_a_publication_can_be_in_a_collection_and_keep_its_series(self):
        """Why this is not the `series` column: two China-Russia members are
        MERICS China Essentials Briefs and one is a Security and Risk Tracker."""
        pid = db.upsert_publication(self.conn, {
            **BASE, "slug": "c", "url": "https://merics.org/en/merics-briefs/c",
            "title": "A brief", "pub_type": "MERICS Briefs",
            "series": "MERICS China Essentials"})
        db.add_to_collection(self.conn, pid, "crd", source="auto")
        row = db.collection_members(self.conn, "crd")[0]
        self.assertEqual(row["series"], "MERICS China Essentials")

    def test_detect_all_reads_the_cache_and_respects_the_article_rule(self):
        cache = Path(self.tmp.name) / "raw"
        cache.mkdir()
        (cache / "a.html").write_text(IN_ARTICLE)
        (cache / "b.html").write_text(PROMO_ONLY)
        with mock.patch("pubbrain.paths.RAW_DIR", cache):
            added = collect.detect_all(self.conn)
        self.assertEqual(added, {"crd": 1})
        self.assertEqual([r["id"] for r in db.collection_members(self.conn, "crd")],
                         [self.pid])

    def test_from_page_takes_the_teaser_card_and_leaves_the_promo(self):
        """#41: a listing page's members sit *outside* its own article, so this
        harvest cannot use the article rule — only the promo block is dropped.
        Delete `latest_newsletter` from `PROMO_VIEWS` and this fails."""
        page = f"""<html><body>
          <article><div class="view-mode-full"><p>The series.</p></div></article>
          <div class="view-content">
            <div class="views-row"><a href="/en/comment/a">A member</a></div>
          </div>
          <section class="views-element-container">
            <div class="view view-latest-newsletter view-id-latest_newsletter">
              <div class="views-row"><a href="/en/comment/b">Not a member</a></div>
            </div>
          </section>
        </body></html>"""
        self.assertEqual(collect.from_page(self.conn, "crd", page), 1)
        self.assertEqual([r["id"] for r in db.collection_members(self.conn, "crd")],
                         [self.pid])

    def test_detaching_by_hand_works(self):
        db.add_to_collection(self.conn, self.pid, "crd", source="auto")
        db.remove_from_collection(self.conn, self.pid, "crd")
        self.assertEqual(db.collection_members(self.conn, "crd"), [])

    def test_deleting_a_publication_takes_its_memberships(self):
        db.add_to_collection(self.conn, self.pid, "crd", source="auto")
        self.conn.execute("DELETE FROM publications WHERE id = ?", (self.pid,))
        self.assertEqual(db.collection_members(self.conn, "crd"), [])


class TestTheWorkbench(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        db.upsert_collection(conn, "crd", "China-Russia Dashboard",
                             "https://merics.org/en/china-russia-dashboard")
        self.pid = db.upsert_publication(conn, {
            **BASE, "slug": "a", "url": "https://merics.org/en/comment/a",
            "title": "A candidate member"})
        conn.commit()
        conn.close()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def test_attaching_from_the_record_page_is_marked_by_hand(self):
        self.client.post(f"/pub/{self.pid}/collection", data={"slug": "crd"})
        page = self.client.get("/collection/crd").get_data(as_text=True)
        self.assertIn("A candidate member", page)
        self.assertIn("by hand", page)

    def test_detaching_works(self):
        self.client.post(f"/pub/{self.pid}/collection", data={"slug": "crd"})
        self.client.post(f"/pub/{self.pid}/collection",
                         data={"slug": "crd", "action": "remove"})
        self.assertNotIn("A candidate member",
                         self.client.get("/collection/crd").get_data(as_text=True))

    def test_an_unknown_collection_is_rejected(self):
        self.assertEqual(self.client.post(
            f"/pub/{self.pid}/collection", data={"slug": "nope"}).status_code, 404)
        self.assertEqual(self.client.get("/collection/nope").status_code, 404)

    def test_the_projects_page_lists_counts(self):
        self.client.post(f"/pub/{self.pid}/collection", data={"slug": "crd"})
        page = self.client.get("/collections").get_data(as_text=True)
        self.assertIn("China-Russia Dashboard", page)


if __name__ == "__main__":
    unittest.main()


class TestProjectsFromTheWorkbench(unittest.TestCase):
    """Creating and curating a project without a terminal (#37).

    The invariant that matters: `slug` is the identity `publication_collections`
    stores, so a rename must never touch it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.pid = db.upsert_publication(self.conn, {
            **BASE, "slug": "a", "url": "https://merics.org/en/comment/a",
            "title": "A member"})
        self.conn.commit()
        app = create_app(self.db_path)
        app.testing = True
        self.client = app.test_client()

    def test_a_project_can_be_created_with_no_page_url(self):
        """A project the owner invents has no merics.org page, and detection has
        nothing to read for it — so the URL cannot be required."""
        self.client.post("/collections", data={"name": "Rare earths watch"})
        rows = db.collections(self.conn)
        self.assertEqual([(r["slug"], r["name"], r["url"]) for r in rows],
                         [("rare-earths-watch", "Rare earths watch", None)])

    def test_the_page_says_which_projects_have_no_detection(self):
        self.client.post("/collections", data={"name": "Mine"})
        page = self.client.get("/collections").get_data(as_text=True)
        self.assertIn("curated", page)

    def test_a_duplicate_slug_is_refused_rather_than_merged(self):
        self.client.post("/collections", data={"name": "Rare earths watch"})
        self.client.post("/collections", data={"name": "Rare Earths  Watch"})
        self.assertEqual(len(db.collections(self.conn)), 1)

    def test_a_nameless_project_is_refused(self):
        self.client.post("/collections", data={"name": "   "})
        self.client.post("/collections", data={"name": "!!!"})
        self.assertEqual(db.collections(self.conn), [])

    def test_renaming_keeps_the_slug_and_the_members(self):
        """The slug is what membership rows point at. Renaming through it would
        orphan every member — the same rule the topic vocabulary follows."""
        self.client.post("/collections", data={"name": "Old name"})
        self.client.post(f"/pub/{self.pid}/collection", data={"slug": "old-name"})
        self.client.post("/collection/old-name/edit", data={"name": "New name"})
        rows = db.collections(self.conn)
        self.assertEqual((rows[0]["slug"], rows[0]["name"]), ("old-name", "New name"))
        self.assertEqual([r["id"] for r in db.collection_members(self.conn, "old-name")],
                         [self.pid])

    def test_deleting_takes_the_memberships_and_leaves_the_publications(self):
        self.client.post("/collections", data={"name": "Doomed"})
        self.client.post(f"/pub/{self.pid}/collection", data={"slug": "doomed"})
        self.client.post("/collection/doomed/edit", data={"action": "delete"})
        self.assertEqual(db.collections(self.conn), [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM publication_collections"
                              ).fetchone()["c"], 0)
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM publications WHERE id = ?", (self.pid,)).fetchone())

    def test_the_delete_redirect_says_how_many_memberships_went(self):
        self.client.post("/collections", data={"name": "Doomed"})
        self.client.post(f"/pub/{self.pid}/collection", data={"slug": "doomed"})
        r = self.client.post("/collection/doomed/edit", data={"action": "delete"})
        self.assertIn("1+membership", r.headers["Location"])

    def test_a_project_can_be_started_from_a_record_page(self):
        """Starting one must not mean leaving the page you are reading."""
        self.client.post(f"/pub/{self.pid}/collection",
                         data={"slug": "", "new_project": "Invented here"})
        self.assertEqual([r["slug"] for r in db.collections(self.conn)],
                         ["invented-here"])
        self.assertEqual(
            db.collections_for(self.conn, self.pid)[0]["source"], "manual")

    def test_editing_an_unknown_project_is_404(self):
        self.assertEqual(self.client.post(
            "/collection/nope/edit", data={"name": "x"}).status_code, 404)

    def test_naming_an_existing_project_attaches_without_blanking_it(self):
        """`upsert_collection` sets url and blurb from its arguments, so
        re-registering with neither blanks both — one typo onto "ETNC" cost that
        project its page URL. Found in adversarial review. Remove the existence
        check in `collection_toggle` and this fails."""
        db.upsert_collection(self.conn, "etnc", "ETNC",
                             "https://merics.org/en/european-think-tank-network-china",
                             "twelve annual volumes")
        self.conn.commit()
        self.client.post(f"/pub/{self.pid}/collection",
                         data={"slug": "", "new_project": "ETNC"})
        row = self.conn.execute(
            "SELECT name, url, blurb FROM collections WHERE slug = 'etnc'").fetchone()
        self.assertEqual(row["url"],
                         "https://merics.org/en/european-think-tank-network-china")
        self.assertEqual(row["blurb"], "twelve annual volumes")
        self.assertEqual([r["id"] for r in db.collection_members(self.conn, "etnc")],
                         [self.pid], "and it still attaches")
