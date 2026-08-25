"""The coverage view (#55): a counted verdict, never a generated one.

The contract under test: the gradient's bands follow the measured tiers, a
missing adjacency signal is said rather than guessed around, and probing a
term never writes anything — `landscape_coords` holds publications only.
"""

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import numpy as np

from pubbrain import db, embed, landscape, queries
from pubbrain.queries import _coverage_verdict
from pubbrain.web import create_app

BASE = {
    "slug": "s", "url": "u", "title": "T", "subtitle": None,
    "date_published": "2025-01-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "site_tags": [], "people": [],
}

DIM = 8


def blob(center, n, rng):
    out = center + rng.normal(0, 0.05, size=(n, DIM))
    return out / np.linalg.norm(out, axis=1, keepdims=True)


class TestVerdict(unittest.TestCase):
    """Band cuts as calibrated on #55; pos always lands inside the scale."""

    def band(self, **kw):
        args = {"about": 0, "mentions": 0, "sim1": 0.0, "keyword_only": False}
        args.update(kw)
        return _coverage_verdict(**args)

    def test_the_gradient(self):
        self.assertEqual(self.band(about=14, mentions=296)["band"], 4)
        self.assertEqual(self.band(about=0, mentions=194)["band"], 4)
        self.assertEqual(self.band(about=3, mentions=68)["band"], 3)
        self.assertEqual(self.band(about=0, mentions=61)["band"], 2)
        self.assertEqual(self.band(about=0, mentions=1)["band"], 2)
        self.assertEqual(self.band(sim1=0.45)["band"], 1)
        self.assertEqual(self.band(sim1=0.2)["band"], 0)

    def test_mention_is_not_coverage(self):
        # 61 passing mentions with nothing titled for the term stay "touched";
        # two dedicated pieces outrank them.
        self.assertLess(self.band(about=0, mentions=61)["band"],
                        self.band(about=2, mentions=27)["band"])

    def test_pos_stays_inside_its_band(self):
        for kw in ({"about": 50, "mentions": 3000}, {"mentions": 1},
                   {"sim1": 0.99}, {"sim1": 0.0}):
            v = self.band(**kw)
            fifth = v["band"] / 5
            self.assertGreaterEqual(v["pos"], fifth)
            self.assertLessEqual(v["pos"], fifth + 0.2)

    def test_no_signal_is_said_not_guessed(self):
        v = self.band(keyword_only=True)
        self.assertIsNone(v["band"])
        self.assertIsNone(v["pos"])
        self.assertIn("ollama", v["measured"])
        # with matches, keyword-only degrades the sentence, not the band
        v = self.band(about=2, mentions=30, keyword_only=True)
        self.assertEqual(v["band"], 3)
        self.assertIn("adjacency scan is missing", v["measured"])


class TestCoverageView(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.rng = np.random.default_rng(23)
        self.a = np.zeros(DIM); self.a[0] = 1
        self.b = np.zeros(DIM); self.b[1] = 1

    def add(self, slug, title, vector=None, pub_type="Comment"):
        pid = db.upsert_publication(self.conn, {
            **BASE, "slug": slug, "url": f"u/{slug}", "title": title,
            "pub_type": pub_type})
        if vector is not None:
            db.store_embeddings(
                self.conn, "one_liner", embed.MODEL,
                [{"source_id": pid, "publication_id": pid}],
                [np.asarray(vector, dtype=np.float32)], embed.pack)
        return pid

    def seed(self):
        for i in range(4):
            self.add(f"a{i}", f"Semiconductors piece {i}",
                     vector=blob(self.a, 1, self.rng)[0])
        for i in range(4):
            self.add(f"b{i}", f"Climate piece {i}",
                     vector=blob(self.b, 1, self.rng)[0])
        db.rebuild_fts(self.conn)     # nothing keeps the index current on its own
        landscape.refresh(self.conn)

    def test_probing_a_term_writes_nothing(self):
        self.seed()
        before = self.conn.execute(
            "SELECT publication_id, x, y FROM landscape_coords").fetchall()
        with patch.object(embed, "embed_query",
                          return_value=blob(self.a, 1, self.rng)[0]):
            out = queries.coverage_view(self.conn, "semiconductors")
        after = self.conn.execute(
            "SELECT publication_id, x, y FROM landscape_coords").fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
        self.assertIsNotNone(out["term_xy"])

    def test_term_lands_with_its_meaning_not_its_spelling(self):
        self.seed()
        coords = {r["publication_id"]: (r["x"], r["y"]) for r in
                  self.conn.execute("SELECT * FROM landscape_coords")}
        a_ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM publications WHERE slug LIKE 'a%'")]
        a_pts = np.array([coords[i] for i in a_ids])
        b_pts = np.array([coords[i] for i in coords if i not in a_ids])
        with patch.object(embed, "embed_query",
                          return_value=blob(self.a, 1, self.rng)[0]):
            out = queries.coverage_view(self.conn, "Halbleiter")
        xy = np.array([out["term_xy"]["x"], out["term_xy"]["y"]])
        self.assertLess(np.linalg.norm(a_pts.mean(0) - xy),
                        np.linalg.norm(b_pts.mean(0) - xy))
        # no keyword match, close neighbours: adjacent only, neighbours shown
        self.assertEqual(out["verdict"]["band"], 1)
        self.assertTrue(out["neighbours"])

    def test_two_tiers_and_chapter_dedup(self):
        parent = self.add("rep", "Gallium export controls", pub_type="Report",
                          vector=blob(self.a, 1, self.rng)[0])
        chap = self.add("ch", "Gallium in detail", pub_type="Report")
        self.conn.execute("UPDATE publications SET parent_id = ? WHERE id = ?",
                          (parent, chap))
        db.rebuild_fts(self.conn)
        with patch.object(embed, "embed_query",
                          side_effect=embed.OllamaUnreachable("down")):
            out = queries.coverage_view(self.conn, "gallium")
        # two FTS rows, one publication's worth of coverage
        self.assertEqual(out["about"], 1)
        self.assertEqual(out["mentions"], 1)
        self.assertEqual(out["weighted"], 8)

    def test_ollama_down_degrades_loudly(self):
        self.seed()
        with patch.object(embed, "embed_query",
                          side_effect=embed.OllamaUnreachable("down")):
            out = queries.coverage_view(self.conn, "nowhere-term")
        self.assertIsNone(out["verdict"]["band"])
        self.assertTrue(any("ollama" in n for n in out["notes"]))
        self.assertEqual(out["neighbours"], [])

    def test_routes(self):
        self.seed()
        self.conn.commit()
        app = create_app(self.db_path)
        with app.test_client() as client:
            self.assertEqual(client.get("/insights/coverage").status_code, 200)
            self.assertEqual(
                client.get("/insights/data/coverage.json").status_code, 400)
            with patch.object(embed, "embed_query",
                              return_value=blob(self.b, 1, self.rng)[0]):
                d = client.get(
                    "/insights/data/coverage.json?term=climate").get_json()
        self.assertEqual(d["about"], 4)
        self.assertIsNotNone(d["verdict"]["band"])
        self.assertIn("caveats", d)


if __name__ == "__main__":
    unittest.main()


class TestThePartnerPublishedBlindSpot(unittest.TestCase):
    """The one caveat that cannot be measured (#12).

    Every other clause in `coverage_caveats` is computed and disappears when
    its issue closes. MERICS work published only on a partner's site leaves no
    trace on merics.org, so nothing here could ever detect it — which makes it
    the one clause that must be constant, and the one that must never be
    dropped for looking un-sourced.
    """

    def test_it_is_present_even_on_a_catalog_with_no_gaps(self):
        import tempfile
        from pathlib import Path
        from pubbrain import db, queries
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            caveats = queries.coverage_caveats(conn)
            joined = " ".join(caveats)
            self.assertIn("partner institution", joined)
            self.assertIn("extent is unknown", joined)
            conn.close()
