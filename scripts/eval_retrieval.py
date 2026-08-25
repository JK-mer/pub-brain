"""Retrieval quality on a fixed question set (#17).

    python scripts/eval_retrieval.py

Two deliberately opposed question classes, because a single class would rig the
result: CASES avoids the words the documents use (where embeddings should win),
EXACT_CASES is named entities and coined terms (where bm25 should). Reporting
only the first would make hybrid look pointless; only the second, essential.

A case passes when a top-k result's title or one-liner matches `expect`. The
criterion is a *property of a correct answer*, not a specific publication id:
several MERICS pieces can legitimately answer one question, and pinning an id
would score a different-but-right result as a miss.
"""

import argparse
import pathlib
import re
import sqlite3
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pubbrain import db, embed, paths  # noqa: E402

CASES = [
    ("punishing a country economically for a political decision",
     r"coercion|economic pressure|sanction"),
    ("China's system for scoring the behaviour of citizens and companies",
     r"social credit"),
    ("forced labour allegations in the far west of China",
     r"xinjiang|uyghur|forced labor|forced labour"),
    ("stopping advanced chipmaking equipment reaching Chinese firms",
     r"semiconductor|chip|export control"),
    ("Beijing's overseas infrastructure lending programme",
     r"belt and road|bri\b|silk road|connectivity"),
    ("military intimidation of the island Beijing claims",
     r"taiwan|cross-strait"),
    ("Europe reducing dependency without cutting ties entirely",
     r"de-risk|derisk|dependen|decoupl"),
    ("security concerns about Chinese telecoms equipment in mobile networks",
     r"huawei|5g|telecom"),
    ("the collapse of a major property developer and its fallout",
     r"evergrande|property|real estate|housing"),
    ("how the Party asserts control over private entrepreneurs",
     r"private sector|entrepreneur|party control|state-owned|private compan"),
    ("selling monitoring and facial recognition systems to other governments",
     r"surveillance|facial recognition|tech authoritarian|digital"),
    ("erosion of civil liberties in the former British territory",
     r"hong kong"),
    ("Beijing's partnership with Moscow since the invasion of Ukraine",
     r"russia|ukraine|moscow"),
    ("dependence on Chinese critical minerals for green technology",
     r"rare earth|critical raw|mineral|supply chain"),
    ("rules Beijing is writing for artificial intelligence",
     r"artificial intelligence|\bai\b governance|algorithm"),
]

# The paraphrase set above is biased against bm25 by construction — it avoids the
# words the documents use, which is precisely where keyword search is weakest.
# Judging hybrid on it alone would be circular. These are the opposite case:
# named entities and coined terms, where the exact string is the whole signal and
# an embedding's nearest neighbour is often a merely-similar concept.
EXACT_CASES = [
    ("Made in China 2025", r"made in china 2025|industrial polic|self-relian"),
    ("dual circulation", r"dual circulation|domestic demand|self-relian"),
    ("Belt and Road Initiative", r"belt and road|\bbri\b|silk road"),
    ("Rebecca Arcesati", r"arcesati"),
    ("14th Five-Year Plan", r"five-year plan|\bfyp\b|15th|14th"),
    ("Anti-Foreign Sanctions Law", r"sanction|countermeasure|blocking"),
    ("Xi Jinping Thought", r"xi jinping|communist party|party control|ideolog"),
    ("Comprehensive Agreement on Investment", r"\bcai\b|investment agreement|comprehensive agreement"),
]


def load(conn, source_type, model):
    rows = db.load_embeddings(conn, source_type, model)
    if not rows:
        return None, None
    return rows, embed.normalise(np.stack([embed.unpack(r["vector"]) for r in rows]))


def vector_order(rows, matrix, query, model, depth):
    # The whole index, not a candidate window — mirrors `queries._vector_ranking`,
    # and for the same reason: chunking (#34) lets one long report fill a window.
    order, seen = [], set()
    for idx, _ in embed.rank(embed.embed_query(query, model=model), matrix,
                             limit=len(rows)):
        pub = rows[idx]["publication_id"]
        if pub not in seen:
            seen.add(pub)
            order.append(pub)
        if len(order) >= depth:
            break
    return order


def keyword_order(conn, query, depth):
    # Two exact-term questions are hyphenated. Without fts_safe they raise and
    # this returns [], scoring a syntax error as a ranking failure — which
    # understated keyword and overstated what hybrid adds.
    try:
        return [r["id"] for r in db.search(conn, db.fts_safe(query), limit=depth)]
    except sqlite3.OperationalError:
        return []


def evaluate(conn, mode, model, k, meta, indexes, cases):
    """Pure retrieval, deliberately not `queries.hybrid_find`.

    Going through the shared layer would fold in the shortlist boost (#25) and
    this script would then measure retrieval *plus* curation — the recorded
    baseline (87% recall@1, #17) would stop being comparable to anything
    measured after, with nothing to indicate why. If this is ever switched to
    `hybrid_find`, it must pass `boost_shortlist=False`.
    """
    hits, misses, depth = 0, [], max(k * 3, 20)
    for query, expect in cases:
        if mode == "keyword":
            order = keyword_order(conn, query, depth)
        elif mode == "hybrid":
            order = [p for p, _ in embed.fuse(
                [keyword_order(conn, query, depth)] +
                [vector_order(r, m, query, model, depth) for r, m in indexes.values()])]
        else:
            rows, matrix = indexes[mode]
            order = vector_order(rows, matrix, query, model, depth)

        pattern = re.compile(expect, re.I)
        found = any(pattern.search(meta.get(p, ("", ""))[0])
                    or pattern.search(meta.get(p, ("", ""))[1])
                    for p in order[:k])
        hits += found
        if not found:
            misses.append(query)
    return hits, misses


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", type=int, default=5, help="top-k publications considered")
    ap.add_argument("--model", default=embed.MODEL)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    meta = {r["id"]: (r["title"] or "", r["one"] or "") for r in conn.execute(
        "SELECT p.id, p.title, e.summary_one_liner AS one FROM publications p "
        "LEFT JOIN primary_enrichment e ON e.publication_id = p.id")}

    indexes = {}
    for source in ("section", "one_liner"):
        rows, matrix = load(conn, source, args.model)
        if rows is None:
            print(f"  {source}: no embeddings — run: pubbrain embed")
            return 2
        indexes[source] = (rows, matrix)

    print(f"top-{args.k} publications, model {args.model}\n")
    print(f"{'mode':<11}{'paraphrase':>12}{'exact term':>13}{'combined':>11}")
    for mode in ("keyword", "section", "one_liner", "hybrid"):
        a, _ = evaluate(conn, mode, args.model, args.k, meta, indexes, CASES)
        b, _ = evaluate(conn, mode, args.model, args.k, meta, indexes, EXACT_CASES)
        both = (a + b) / (len(CASES) + len(EXACT_CASES)) * 100
        print(f"  {mode:<11}{a}/{len(CASES)} ({a/len(CASES)*100:>3.0f}%)"
              f"{b:>5}/{len(EXACT_CASES)} ({b/len(EXACT_CASES)*100:>3.0f}%){both:>9.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
