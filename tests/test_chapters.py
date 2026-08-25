"""Reports and their chapters (#36).

The owner's requirement is about behaviour, not about a type string: the report
appears once, the chapters are searchable in their own right, and no count
silently changes meaning. The last of those is the part most likely to go wrong
quietly, so most of these tests are about counts.
"""

import sqlite3
import unittest

from pubbrain import db, queries

BASE = {
    "slug": "s", "url": "u", "title": "T", "subtitle": None,
    "date_published": "2023-10-12", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [], "site_tags": [],
}


class TestChapters(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        # As `db.connect` does. Without it `ON DELETE SET NULL` never fires and
        # a deleted parent leaves its chapters pointing at nothing — which the
        # listing filter reads as "still a chapter", so they vanish from the
        # catalog with no error. The test would then pass against a database
        # that behaves differently from the real one.
        self.conn.execute("PRAGMA foreign_keys = ON")
        db.migrate(self.conn)
        self.parent = db.upsert_publication(self.conn, {
            **BASE, "slug": "party-knows-best", "url": "https://merics.org/en/report/pkb",
            "title": "The party knows best"})
        self.chapters = [
            db.upsert_publication(self.conn, {
                **BASE, "slug": f"ch{i}", "url": f"https://merics.org/en/ch{i}",
                "title": f"Chapter {i}"})
            for i in range(1, 4)]
        self.loner = db.upsert_publication(self.conn, {
            **BASE, "slug": "comment", "url": "https://merics.org/en/comment/x",
            "title": "An ordinary comment", "pub_type": "Comment"})

    def _attach_all(self):
        for i, c in enumerate(self.chapters, start=1):
            db.attach_chapter(self.conn, c, self.parent, i)

    def test_chapters_come_back_in_reading_order(self):
        db.attach_chapter(self.conn, self.chapters[2], self.parent, 3)
        db.attach_chapter(self.conn, self.chapters[0], self.parent, 1)
        db.attach_chapter(self.conn, self.chapters[1], self.parent, 2)
        self.assertEqual([r["title"] for r in db.chapters_of(self.conn, self.parent)],
                         ["Chapter 1", "Chapter 2", "Chapter 3"])

    def test_position_appends_when_not_given(self):
        for c in self.chapters:
            db.attach_chapter(self.conn, c, self.parent)
        self.assertEqual([r["parent_position"]
                          for r in db.chapters_of(self.conn, self.parent)], [1, 2, 3])

    def test_the_listing_shows_the_report_once_and_hides_its_chapters(self):
        """The whole point of the issue: a report appears once."""
        self._attach_all()
        rows, total, _ = queries.list_publications(self.conn)
        titles = [r["title"] for r in rows]
        self.assertIn("The party knows best", titles)
        self.assertNotIn("Chapter 1", titles)
        self.assertEqual(total, 2)          # the report and the comment

    def test_include_chapters_is_available_but_not_the_default(self):
        self._attach_all()
        _, total, _ = queries.list_publications(self.conn, include_chapters=True)
        self.assertEqual(total, 5)

    def test_the_type_count_stays_reports_not_reports_plus_chapters(self):
        """"184 Reports" must keep meaning reports. Chapters inherit the
        parent's type, so this is exactly where the count would drift."""
        before = dict((r["pub_type"], r["n"])
                      for r in queries.filter_options(self.conn)[0])
        self._attach_all()
        after = dict((r["pub_type"], r["n"])
                     for r in queries.filter_options(self.conn)[0])
        self.assertEqual(before["Report"], 4)
        self.assertEqual(after["Report"], 1)

    def test_status_reports_chapters_separately_from_publications(self):
        self._attach_all()
        s = queries.status_report(self.conn)
        self.assertEqual(s["publications"], 2)
        self.assertEqual(s["chapters"], 3)
        self.assertEqual(dict((r["pub_type"], r["n"]) for r in s["by_type"]),
                         {"Report": 1, "Comment": 1})

    def test_a_chapter_still_carries_its_parent_for_search_to_label(self):
        self._attach_all()
        parents = queries.parents_of(self.conn, self.chapters)
        self.assertEqual(parents[self.chapters[1]]["title"], "The party knows best")
        self.assertEqual(parents[self.chapters[1]]["parent_position"], 2)

    def test_the_record_page_carries_the_contents_or_the_parent_never_both(self):
        self._attach_all()
        parent = queries.publication_detail(self.conn, self.parent)
        child = queries.publication_detail(self.conn, self.chapters[0])
        self.assertEqual(len(parent["chapters"]), 3)
        self.assertIsNone(parent["parent"])
        self.assertEqual(child["chapters"], [])
        self.assertEqual(child["parent"]["title"], "The party knows best")

    def test_detaching_returns_a_chapter_to_the_listing(self):
        self._attach_all()
        db.detach_chapter(self.conn, self.chapters[0])
        _, total, _ = queries.list_publications(self.conn)
        self.assertEqual(total, 3)

    def test_a_publication_cannot_be_its_own_parent(self):
        with self.assertRaises(ValueError):
            db.attach_chapter(self.conn, self.parent, self.parent)

    def test_the_hierarchy_stays_one_level_deep(self):
        """A cycle would make chapters_of recurse forever, and a chapter of a
        chapter has no meaning the listing filter could express."""
        db.attach_chapter(self.conn, self.chapters[0], self.parent)
        with self.assertRaises(ValueError):
            db.attach_chapter(self.conn, self.chapters[1], self.chapters[0])
        with self.assertRaises(ValueError):
            db.attach_chapter(self.conn, self.parent, self.chapters[0])

    def test_deleting_a_parent_frees_its_chapters_rather_than_deleting_them(self):
        """ON DELETE SET NULL, not CASCADE: a chapter is a real publication
        with its own text and vectors, and losing the parent must not lose it."""
        self._attach_all()
        self.conn.execute("DELETE FROM publications WHERE id = ?", (self.parent,))
        rows, total, _ = queries.list_publications(self.conn)
        self.assertEqual(total, 4)
        self.assertIn("Chapter 1", [r["title"] for r in rows])

    def test_parent_picker_offers_no_chapter_and_ranks_prefix_first(self):
        """The attach form searches by title (#36) — offering a chapter would
        only manufacture the attach-time refusal."""
        self._attach_all()
        self.conn.execute("UPDATE publications SET title = "
                          "'The party in the provinces' WHERE id = ?",
                          (self.chapters[0],))
        db.upsert_publication(self.conn, {
            **BASE, "slug": "party-congress",
            "url": "https://merics.org/en/report/party-congress",
            "title": "Party congress decoded"})
        got = db.find_parents(self.conn, "party")
        titles = [r["title"] for r in got]
        self.assertEqual(titles[0], "Party congress decoded")  # prefix first
        self.assertIn("The party knows best", titles)
        self.assertNotIn("The party in the provinces", titles)  # a chapter
        self.assertEqual(
            next(r for r in got if r["title"] == "The party knows best")
            ["chapters"], 3)


if __name__ == "__main__":
    unittest.main()
