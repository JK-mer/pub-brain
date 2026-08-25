"""The embedding landscape (#49): deterministic layout, cached coordinates.

The contract under test is stability: the same vectors give the same picture,
new points slot in without moving anyone, and only leaving the catalog scope
removes a point — a missing vector alone does not.
"""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from pubbrain import db, embed, landscape, queries, topics
from pubbrain.web import create_app

BASE = {
    "slug": "s", "url": "u", "title": "T", "subtitle": None,
    "date_published": "2025-01-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "site_tags": [], "people": [],
}

DIM = 8


def blob(center, n, rng):
    """n unit vectors scattered tightly around one direction."""
    out = center + rng.normal(0, 0.05, size=(n, DIM))
    return out / np.linalg.norm(out, axis=1, keepdims=True)


class TestTSNE(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        a = np.zeros(DIM); a[0] = 1
        b = np.zeros(DIM); b[1] = 1
        self.X = np.vstack([blob(a, 15, rng), blob(b, 15, rng)])

    def test_deterministic_without_any_seed(self):
        one = landscape.tsne(self.X, iterations=300)
        two = landscape.tsne(self.X, iterations=300)
        self.assertTrue(np.array_equal(one, two))

    def test_separates_what_the_vectors_separate(self):
        # Default iterations: most of a short run is still early exaggeration,
        # and judging separation mid-exaggeration judges the wrong thing.
        Y = landscape.tsne(self.X)
        a, b = Y[:15], Y[15:]
        intra = (np.linalg.norm(a - a.mean(0), axis=1).mean()
                 + np.linalg.norm(b - b.mean(0), axis=1).mean()) / 2
        inter = np.linalg.norm(a.mean(0) - b.mean(0))
        self.assertGreater(inter, intra * 2)


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.rng = np.random.default_rng(11)
        self.a = np.zeros(DIM); self.a[0] = 1
        self.b = np.zeros(DIM); self.b[1] = 1

    def add(self, slug, pub_type="Comment", vector=None, parent_id=None):
        pid = db.upsert_publication(self.conn, {
            **BASE, "slug": slug, "url": f"u/{slug}", "pub_type": pub_type})
        if parent_id:
            self.conn.execute(
                "UPDATE publications SET parent_id = ? WHERE id = ?",
                (parent_id, pid))
        if vector is not None:
            db.store_embeddings(
                self.conn, "one_liner", embed.MODEL,
                [{"source_id": pid, "publication_id": pid}],
                [np.asarray(vector, dtype=np.float32)], embed.pack)
        return pid

    def seed(self, n_per_side=4):
        ids = []
        for i in range(n_per_side):
            ids.append(self.add(f"a{i}", vector=blob(self.a, 1, self.rng)[0]))
        for i in range(n_per_side):
            ids.append(self.add(f"b{i}", vector=blob(self.b, 1, self.rng)[0]))
        return ids

    def coords(self):
        return {r["publication_id"]: (r["x"], r["y"], r["placed"])
                for r in self.conn.execute("SELECT * FROM landscape_coords")}

    def test_fit_covers_scope_and_only_scope(self):
        self.seed()
        pod = self.add("pod", "Podcast", vector=blob(self.a, 1, self.rng)[0])
        parent = self.add("rep", "Report", vector=blob(self.a, 1, self.rng)[0])
        chap = self.add("ch", "Report", vector=blob(self.a, 1, self.rng)[0],
                        parent_id=parent)
        out = landscape.refresh(self.conn)
        self.assertEqual(out["mode"], "fit")
        got = self.coords()
        self.assertEqual(len(got), 9)          # 8 seeded + the parent report
        self.assertNotIn(pod, got)
        self.assertNotIn(chap, got)
        self.assertTrue(all(p == "fit" for _, _, p in got.values()))

    def test_incremental_moves_nobody(self):
        self.seed()
        landscape.refresh(self.conn)
        before = self.coords()
        new = self.add("new", vector=blob(self.a, 1, self.rng)[0])
        out = landscape.refresh(self.conn)
        self.assertEqual(out["mode"], "incremental")
        after = self.coords()
        self.assertEqual(after[new][2], "incremental")
        for pid, row in before.items():
            self.assertEqual(after[pid], row)   # bit-identical, nobody moved

    def test_new_point_lands_with_its_neighbours(self):
        ids = self.seed()
        landscape.refresh(self.conn)
        got = self.coords()
        a_pts = np.array([got[p][:2] for p in ids[:4]])
        b_pts = np.array([got[p][:2] for p in ids[4:]])
        new = self.add("new", vector=blob(self.a, 1, self.rng)[0])
        landscape.refresh(self.conn)
        nx, ny, _ = self.coords()[new]
        to_a = np.linalg.norm(a_pts.mean(0) - (nx, ny))
        to_b = np.linalg.norm(b_pts.mean(0) - (nx, ny))
        self.assertLess(to_a, to_b)

    def test_leaving_scope_prunes_but_a_lost_vector_does_not(self):
        ids = self.seed()
        landscape.refresh(self.conn)
        # became a chapter -> out; vector deleted (re-summarised) -> stays
        self.conn.execute("UPDATE publications SET parent_id = ? WHERE id = ?",
                          (ids[0], ids[1]))
        self.conn.execute("DELETE FROM embeddings WHERE publication_id = ?",
                          (ids[2],))
        landscape.refresh(self.conn)
        got = self.coords()
        self.assertNotIn(ids[1], got)
        self.assertIn(ids[2], got)

    def test_place_new_never_fits_an_empty_table(self):
        self.seed()
        self.assertEqual(landscape.place_new(self.conn), 0)
        self.assertEqual(self.coords(), {})

    def test_query_carries_cluster_weight_and_missing(self):
        pid = self.seed(1)[0]
        slug = topics.slugs()[0]
        self.conn.execute(
            "INSERT INTO publication_topics (publication_id, topic_slug, "
            "position, model, prompt_version, mapped_at) "
            "VALUES (?, ?, 1, 'test', 1, '2025')", (pid, slug))
        out = queries.landscape(self.conn)
        self.assertFalse(out["fitted"])
        self.assertEqual(out["missing"], 2)
        landscape.refresh(self.conn)
        out = queries.landscape(self.conn)
        self.assertTrue(out["fitted"])
        point = next(p for p in out["points"] if p["id"] == pid)
        self.assertEqual(point["cluster"], 0)   # first cluster in the YAML
        self.assertEqual(point["w"], 1)         # a Comment weighs 1
        other = next(p for p in out["points"] if p["id"] != pid)
        self.assertEqual(other["cluster"], -1)  # unmapped -> neutral, not gone

    def test_hidden_empty_note_stays_hidden(self):
        """#53: `.scape-empty { display:flex }` overrode the UA's
        `[hidden] { display:none }` — author styles beat UA styles whatever
        the specificity — so the note rendered over the stage and ate every
        click. The explicit [hidden] rule is the guard; this pins it."""
        css = (Path(__file__).parents[1] / "pubbrain" / "static"
               / "style.css").read_text(encoding="utf-8")
        self.assertIn(".scape-empty[hidden]", css)

    def test_routes_serve_and_auto_place(self):
        self.seed()
        landscape.refresh(self.conn)
        self.conn.commit()
        new = self.add("late", vector=blob(self.b, 1, self.rng)[0])
        self.conn.commit()
        app = create_app(self.db_path)
        with app.test_client() as client:
            self.assertEqual(
                client.get("/insights/landscape").status_code, 200)
            d = client.get("/insights/data/landscape.json").get_json()
            self.assertTrue(d["fitted"])
            self.assertIn(new, [p["id"] for p in d["points"]])
            self.assertEqual(len(d["clusters"]), len(topics.load()))


if __name__ == "__main__":
    unittest.main()
