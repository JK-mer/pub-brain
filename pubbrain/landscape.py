"""The embedding landscape (#49): the catalog laid out by meaning.

A 2D t-SNE projection of the one-liner vectors, computed once and cached in
`landscape_coords`. The layout has to be identical on every visit — "the
export-controls cluster sits upper right" is spatial memory, a learning
feature, and a layout that reshuffles forgets it for the owner. So:

- The fit has no randomness at all: PCA initialisation, deterministic
  gradient descent. Same vectors in, same picture out.
- New publications are *placed*, never refit — a new point lands at the
  similarity-weighted centroid of its nearest already-placed neighbours and
  nobody else moves. `refit=True` is the deliberate exception that redraws
  the whole picture.
- A publication whose vector disappears (a summary being regenerated, #24)
  keeps its place: stale-by-one-summary beats vanishing. Only leaving the
  catalog scope — becoming a chapter, deletion — removes a point.

Exact O(n²) t-SNE in numpy: at ~1,100 points the reference algorithm runs in
seconds, so a dependency (sklearn, openTSNE) would be complexity bought with
nothing — the same argument as `embed.py`'s brute-force scan (#17).
"""

import numpy as np

from . import db, embed

PERPLEXITY = 30.0
ITERATIONS = 750
EXAGGERATION = 12.0
EXAGGERATE_UNTIL = 250

# Neighbours consulted when slotting in a new point. Squared-similarity
# weights, so the closest few dominate and a broad tail cannot drag a
# specialised piece toward the middle of the map.
PLACE_NEIGHBOURS = 10


def _pairwise_sq(Y):
    s = (Y * Y).sum(axis=1)
    return np.maximum(s[:, None] + s[None, :] - 2.0 * (Y @ Y.T), 0.0)


def _joint_probabilities(D, perplexity):
    """Symmetric affinities from squared distances, one beta per point found
    by binary search — the standard perplexity calibration."""
    n = len(D)
    target = np.log(perplexity)
    P = np.zeros((n, n))
    mask = ~np.eye(n, dtype=bool)
    for i in range(n):
        Di = D[i][mask[i]]
        lo, hi, beta = 0.0, np.inf, 1.0
        for _ in range(50):
            Pi = np.exp(-Di * beta)
            s = Pi.sum()
            if s <= 0:
                H = 0.0
            else:
                H = np.log(s) + beta * (Di * Pi).sum() / s
            if abs(H - target) < 1e-5:
                break
            if H > target:
                lo, beta = beta, beta * 2 if hi == np.inf else (beta + hi) / 2
            else:
                hi, beta = beta, beta / 2 if lo == 0 else (beta + lo) / 2
        Pi = np.exp(-Di * beta)
        P[i][mask[i]] = Pi / max(Pi.sum(), 1e-12)
    P = (P + P.T) / (2.0 * n)
    return np.maximum(P, 1e-12)


def _pca2(X):
    """Two principal components, sign-fixed so the run is reproducible —
    SVD is unique up to sign, and an unfixed sign mirrors the whole map."""
    Xc = X - X.mean(axis=0)
    U, S, _ = np.linalg.svd(Xc, full_matrices=False)
    Y = U[:, :2] * S[:2]
    for j in range(2):
        col = Y[:, j]
        if col.size and col[np.abs(col).argmax()] < 0:
            Y[:, j] = -col
    # Small start, as the reference implementation wants: early exaggeration
    # needs room to pull clusters apart before the layout hardens.
    return Y / max(Y[:, 0].std(), 1e-12) * 1e-4


def tsne(X, perplexity=PERPLEXITY, iterations=ITERATIONS):
    """Deterministic exact t-SNE to 2D. No RNG anywhere — PCA init plus
    plain gradient descent, so identical input yields the identical map."""
    X = np.asarray(X, dtype=np.float64)
    n = len(X)
    perplexity = min(perplexity, max((n - 1) / 3.0, 1.0))
    P = _joint_probabilities(_pairwise_sq(X), perplexity) * EXAGGERATION
    Y = _pca2(X)
    inc = np.zeros_like(Y)
    gains = np.ones_like(Y)
    momentum, eta = 0.5, max(n / EXAGGERATION, 50.0)
    for it in range(iterations):
        num = 1.0 / (1.0 + _pairwise_sq(Y))
        np.fill_diagonal(num, 0.0)
        Q = np.maximum(num / num.sum(), 1e-12)
        W = (P - Q) * num
        grad = 4.0 * (W.sum(axis=1)[:, None] * Y - W @ Y)
        gains = np.maximum(
            np.where(np.sign(grad) != np.sign(inc), gains + 0.2, gains * 0.8),
            0.01)
        inc = momentum * inc - eta * gains * grad
        Y += inc
        Y -= Y.mean(axis=0)
        if it == EXAGGERATE_UNTIL:
            P /= EXAGGERATION
            momentum = 0.8
    return Y


def scope_rows(conn):
    """(publication_id, vector) for every point the landscape may hold:
    one-liner vectors, parents only, Insights exclusions applied. Ordered by
    id so a refit sees the same input in the same order every time."""
    from . import queries
    marks = ", ".join("?" * len(queries.INSIGHT_EXCLUDED_TYPES))
    return conn.execute(
        f"""SELECT e.source_id AS publication_id, e.vector
            FROM embeddings e JOIN publications p ON p.id = e.source_id
            WHERE e.source_type = 'one_liner' AND e.model = ?
              AND p.parent_id IS NULL AND p.pub_type NOT IN ({marks})
            ORDER BY e.source_id""",
        [embed.MODEL, *queries.INSIGHT_EXCLUDED_TYPES]).fetchall()


def _vectors(rows):
    return embed.normalise(np.stack(
        [np.frombuffer(r["vector"], dtype=np.float32) for r in rows]
    ).astype(np.float64))


def _prune(conn) -> int:
    """Remove points that left the catalog scope — became a chapter, or an
    excluded type. A missing vector alone does not remove a point (see the
    module docstring); deletion is handled by ON DELETE CASCADE."""
    from . import queries
    marks = ", ".join("?" * len(queries.INSIGHT_EXCLUDED_TYPES))
    return conn.execute(
        f"""DELETE FROM landscape_coords WHERE publication_id IN (
              SELECT lc.publication_id FROM landscape_coords lc
              JOIN publications p ON p.id = lc.publication_id
              WHERE p.parent_id IS NOT NULL OR p.pub_type IN ({marks}))""",
        list(queries.INSIGHT_EXCLUDED_TYPES)).rowcount


def _write(conn, ids, Y, placed):
    ts = db.now()
    conn.executemany(
        """INSERT INTO landscape_coords (publication_id, x, y, placed, computed_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(publication_id) DO UPDATE SET
             x = excluded.x, y = excluded.y, placed = excluded.placed,
             computed_at = excluded.computed_at""",
        [(pid, float(x), float(y), placed, ts) for pid, (x, y) in zip(ids, Y)])


def place_new(conn) -> int:
    """Slot new publications into an existing layout without moving anyone.

    Cheap enough that the web endpoint runs it per request, which is what
    keeps the view maintenance-free. A no-op while the table is empty — the
    first full fit is a deliberate CLI act, never a side effect of a page load.
    """
    coords = conn.execute(
        "SELECT publication_id, x, y FROM landscape_coords").fetchall()
    if not coords:
        return 0
    _prune(conn)
    rows = scope_rows(conn)
    have = {r["publication_id"] for r in coords}
    new = [r for r in rows if r["publication_id"] not in have]
    if not new:
        return 0
    anchors = [r for r in rows if r["publication_id"] in have]
    if not anchors:
        return 0
    A = _vectors(anchors)
    xy = {r["publication_id"]: (r["x"], r["y"]) for r in coords}
    AY = np.array([xy[r["publication_id"]] for r in anchors])
    span = max(float(AY.max(0).max() - AY.min(0).min()), 1.0)
    out_ids, out_xy = [], []
    for r in new:
        v = _vectors([r])[0]
        sims = A @ v
        top = np.argsort(-sims)[:PLACE_NEIGHBOURS]
        w = np.clip(sims[top], 0.05, None) ** 2
        pos = (AY[top] * w[:, None]).sum(axis=0) / w.sum()
        # Deterministic jitter keyed on the id: two near-identical pieces
        # must not stack into what reads as one point.
        rng = np.random.default_rng(r["publication_id"])
        angle = rng.uniform(0, 2 * np.pi)
        pos += np.array([np.cos(angle), np.sin(angle)]) * span * 0.008
        out_ids.append(r["publication_id"])
        out_xy.append(pos)
    _write(conn, out_ids, out_xy, "incremental")
    return len(out_ids)


def place_term(conn, vector):
    """Where a query vector would land on the existing layout (#55).

    The coverage view's probe: same weighted-centroid placement a new
    publication gets, computed per request and never written —
    `landscape_coords` holds publications only. None while there is no layout.
    """
    coords = conn.execute(
        "SELECT publication_id, x, y FROM landscape_coords").fetchall()
    if not coords:
        return None
    xy = {r["publication_id"]: (r["x"], r["y"]) for r in coords}
    anchors = [r for r in scope_rows(conn) if r["publication_id"] in xy]
    if not anchors:
        return None
    A = _vectors(anchors)
    sims = A @ np.asarray(vector, dtype=np.float64)
    top = np.argsort(-sims)[:PLACE_NEIGHBOURS]
    w = np.clip(sims[top], 0.05, None) ** 2
    AY = np.array([xy[anchors[int(i)]["publication_id"]] for i in top])
    pos = (AY * w[:, None]).sum(axis=0) / w.sum()
    return {"x": float(pos[0]), "y": float(pos[1])}


def refresh(conn, refit=False, iterations=ITERATIONS) -> dict:
    """Bring `landscape_coords` up to date. Incremental unless `refit` or the
    table is empty; a refit replaces every coordinate, provenance included."""
    pruned = _prune(conn)
    rows = scope_rows(conn)
    existing = conn.execute("SELECT COUNT(*) FROM landscape_coords").fetchone()[0]
    if not refit and existing:
        placed = place_new(conn)
        return {"mode": "incremental", "placed": placed, "pruned": pruned,
                "total": existing + placed}
    if not rows:
        return {"mode": "fit", "placed": 0, "pruned": pruned, "total": 0}
    ids = [r["publication_id"] for r in rows]
    X = _vectors(rows)
    if len(rows) < 5:
        # Too few points for perplexity to mean anything — a fixed ring.
        angles = np.linspace(0, 2 * np.pi, len(rows), endpoint=False)
        Y = np.stack([np.cos(angles), np.sin(angles)], axis=1)
    else:
        Y = tsne(X, iterations=iterations)
    conn.execute("DELETE FROM landscape_coords")
    _write(conn, ids, Y, "fit")
    return {"mode": "fit", "placed": len(ids), "pruned": pruned,
            "total": len(ids)}
