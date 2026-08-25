"""Vectors fail silently: a wrong prefix or an unnormalised row still returns
ten confident results. These pin the arithmetic and the worklist, which are the
parts that can be checked without a model.
"""

import sqlite3
import unittest

import numpy as np

from pubbrain import db, embed, sections

PUB = {
    "slug": "b1", "url": "https://merics.org/en/merics-briefs/b1",
    "title": "Export controls + Hukou reform", "subtitle": None,
    "date_published": "2026-06-12", "pub_type": "MERICS Briefs", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [], "site_tags": [],
}
BODY = ("Opening text that has to run past twenty words in order to survive the "
        "minimum-length filter which drops bare labels from the section list "
        "before anything downstream ever sees them.\n\n"
        "## A real story about export controls\n\n" + "word " * 40 + "\n\n"
        "## METRIX\n\n" + "number " * 40 + "\n")


class TestVectorMath(unittest.TestCase):
    def test_normalising_makes_the_dot_product_a_cosine(self):
        m = embed.normalise(np.array([[3.0, 4.0], [1.0, 0.0]], dtype=np.float32))
        np.testing.assert_allclose(np.linalg.norm(m, axis=1), [1.0, 1.0], atol=1e-6)
        self.assertAlmostEqual(float(m[0] @ m[0]), 1.0, places=5)

    def test_a_zero_vector_does_not_divide_by_zero(self):
        m = embed.normalise(np.array([[0.0, 0.0]], dtype=np.float32))
        self.assertFalse(np.isnan(m).any())

    def test_pack_and_unpack_round_trip(self):
        v = np.arange(embed.DIM, dtype=np.float32) / embed.DIM
        np.testing.assert_allclose(embed.unpack(embed.pack(v)), v, atol=1e-6)

    def test_rank_returns_closest_first(self):
        matrix = embed.normalise(np.array([[1, 0], [0.7, 0.7], [0, 1]], dtype=np.float32))
        out = embed.rank(np.array([1, 0], dtype=np.float32), matrix, limit=3)
        self.assertEqual([i for i, _ in out], [0, 1, 2])
        self.assertAlmostEqual(out[0][1], 1.0, places=5)

    def test_rank_on_an_empty_index_returns_nothing(self):
        self.assertEqual(embed.rank(np.zeros(2, dtype=np.float32), np.empty((0, 2))), [])

    def test_documents_and_queries_use_different_prefixes(self):
        """embeddinggemma is asymmetric — encoding a query as a document
        measurably degrades retrieval, and nothing downstream would show it."""
        self.assertNotEqual(embed.DOC_TEMPLATE, embed.QUERY_TEMPLATE)
        self.assertIn("title:", embed.DOC_TEMPLATE)
        self.assertIn("query:", embed.QUERY_TEMPLATE)


class TestWorklist(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        self.pub_id = db.upsert_publication(self.conn, PUB)
        db.upsert_text(self.conn, self.pub_id, BODY, len(BODY.split()))
        found = sections.split(BODY)
        for s in found:
            s["is_boilerplate"] = sections.is_boilerplate(s["heading"])
        db.replace_sections(self.conn, self.pub_id, found)

    def test_boilerplate_sections_are_never_embedded(self):
        """A link list or a lone statistic retrieves on its format, not its
        subject — it pollutes every result set it survives into."""
        todo = db.pending_embeddings(self.conn, "section", "m")
        self.assertNotIn("METRIX", [r["title"] for r in todo])
        self.assertEqual(len(todo), 2)      # preamble + the real story

    def test_an_embedded_section_drops_out_of_the_worklist(self):
        todo = db.pending_embeddings(self.conn, "section", "m")
        vectors = embed.normalise(np.ones((len(todo), 4), dtype=np.float32))
        db.store_embeddings(self.conn, "section", "m", todo, vectors, embed.pack)
        self.assertEqual(db.pending_embeddings(self.conn, "section", "m"), [])

    def test_a_different_model_is_a_separate_index(self):
        """Changing model must not silently mix two vector spaces."""
        todo = db.pending_embeddings(self.conn, "section", "m")
        vectors = embed.normalise(np.ones((len(todo), 4), dtype=np.float32))
        db.store_embeddings(self.conn, "section", "m", todo, vectors, embed.pack)
        self.assertEqual(len(db.pending_embeddings(self.conn, "section", "other")), 2)

    def test_re_embedding_replaces_rather_than_duplicates(self):
        todo = db.pending_embeddings(self.conn, "section", "m")
        v = embed.normalise(np.ones((len(todo), 4), dtype=np.float32))
        db.store_embeddings(self.conn, "section", "m", todo, v, embed.pack)
        db.store_embeddings(self.conn, "section", "m", todo, v * 2, embed.pack)
        self.assertEqual(len(db.load_embeddings(self.conn, "section", "m")), 2)

    def test_deleting_a_publication_takes_its_vectors_with_it(self):
        todo = db.pending_embeddings(self.conn, "section", "m")
        v = embed.normalise(np.ones((len(todo), 4), dtype=np.float32))
        db.store_embeddings(self.conn, "section", "m", todo, v, embed.pack)
        self.conn.execute("DELETE FROM publications WHERE id = ?", (self.pub_id,))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 0)


class TestFusion(unittest.TestCase):
    """RRF is what lets two incomparable rankers vote without inventing a
    conversion between bm25 and cosine."""

    def test_agreement_beats_a_single_strong_hit(self):
        merged = [i for i, _ in embed.fuse([["a", "b", "c"], ["b", "a", "c"]])]
        self.assertEqual(merged[0], "a")          # first in one, second in the other
        self.assertEqual(merged[-1], "c")

    def test_an_item_only_one_ranker_saw_still_places(self):
        merged = dict(embed.fuse([["a"], ["b"]]))
        self.assertAlmostEqual(merged["a"], merged["b"])

    def test_an_empty_ranker_is_harmless(self):
        self.assertEqual([i for i, _ in embed.fuse([[], ["a", "b"]])], ["a", "b"])
        self.assertEqual(embed.fuse([[], []]), [])

    def test_two_rankers_agreeing_beat_one_ranker_alone(self):
        """The point of fusing: a result two rankers surfaced outranks one that
        only a single ranker put first, which is how the keyword half rescues
        named-entity queries without overruling the vectors elsewhere."""
        merged = [i for i, _ in embed.fuse([["x"], ["y", "a"], ["y", "b"]])]
        self.assertEqual(merged[0], "y")


class TestShortResponse(unittest.TestCase):
    def test_a_short_batch_raises_instead_of_mis_pairing(self):
        """zip() downstream would truncate silently, writing vectors against the
        wrong section ids — every affected row would then retrieve as a
        different document with nothing to reveal it."""
        import requests

        class FakeResponse:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"embeddings": [[0.0] * 4]}   # 1 back, 2 asked

        real = requests.post
        requests.post = lambda *a, **k: FakeResponse()
        try:
            with self.assertRaises(embed.ShortResponse):
                embed.embed_documents(["one", "two"])
        finally:
            requests.post = real


class TestOrphanedVectors(unittest.TestCase):
    """extract-sections deletes and reinserts, minting new ids. Without a purge
    the old vectors survive and their publication ranks twice for one text."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        self.pub_id = db.upsert_publication(self.conn, PUB)
        db.upsert_text(self.conn, self.pub_id, BODY, len(BODY.split()))
        self.other = db.upsert_publication(
            self.conn, {**PUB, "slug": "b2",
                        "url": "https://merics.org/en/merics-briefs/b2"})
        db.upsert_text(self.conn, self.other, BODY, len(BODY.split()))

    def _extract_and_embed(self):
        found = sections.split(BODY)
        for s in found:
            s["is_boilerplate"] = sections.is_boilerplate(s["heading"])
        db.replace_sections(self.conn, self.pub_id, found)
        todo = db.pending_embeddings(self.conn, "section", "m")
        v = embed.normalise(np.ones((len(todo), 4), dtype=np.float32))
        db.store_embeddings(self.conn, "section", "m", todo, v, embed.pack)

    def test_re_extracting_leaves_orphans_that_the_purge_removes(self):
        self._extract_and_embed()
        # Another publication must hold the *higher* ids, or SQLite reuses the
        # freed rowids and nothing is orphaned. extract-sections walks the
        # catalog in id order, so every publication but the last is in this
        # position — a one-publication fixture cannot reproduce the bug.
        db.replace_sections(self.conn, self.other, sections.split(BODY))
        todo = db.pending_embeddings(self.conn, "section", "m")
        v = embed.normalise(np.ones((len(todo), 4), dtype=np.float32))
        db.store_embeddings(self.conn, "section", "m", todo, v, embed.pack)

        before = len(db.load_embeddings(self.conn, "section", "m"))
        mine = self.conn.execute(
            "SELECT COUNT(*) FROM publication_sections "
            "WHERE publication_id = ? AND is_boilerplate = 0", (self.pub_id,)
        ).fetchone()[0]
        self.assertTrue(mine)

        self._extract_and_embed()                      # ids change underneath
        # the rewritten publication's old vectors are still there, unreferenced
        self.assertEqual(len(db.load_embeddings(self.conn, "section", "m")),
                         before + mine)
        self.assertEqual(db.purge_orphan_embeddings(self.conn), mine)
        self.assertEqual(len(db.load_embeddings(self.conn, "section", "m")), before)

    def test_purging_leaves_one_liner_vectors_alone(self):
        db.upsert_primary_enrichment(self.conn, self.pub_id, {
            "summary_one_liner": "x", "summary_short": "y",
            "key_findings": ["a"], "entities": {}}, {
            "model": "m", "provider": "p", "prompt_version": 1,
            "words_sent": 1, "attempts": 1})
        todo = db.pending_embeddings(self.conn, "one_liner", "m")
        v = embed.normalise(np.ones((len(todo), 4), dtype=np.float32))
        db.store_embeddings(self.conn, "one_liner", "m", todo, v, embed.pack)
        db.purge_orphan_embeddings(self.conn)
        self.assertEqual(len(db.load_embeddings(self.conn, "one_liner", "m")), 1)


if __name__ == "__main__":
    unittest.main()
