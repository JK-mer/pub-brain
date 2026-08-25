"""Shared query layer: the CLI and the web workbench answer through these.

Ranking and filtering logic lives here so the two surfaces cannot drift, and
so a future MCP server has its functions ready-made (#19).
"""

import datetime
import json
import sqlite3

import numpy

from . import db, embed, topics

PAGE_SIZE = 50

# What counts as a credit (#27). Hosting is a recurring role, not a body of
# work: one link per episode made the podcast host the second-most-credited
# person in the catalog. The `host` rows are kept — the podcast pages use them
# and #12 has not settled how participants should be modelled — they simply do
# not count towards anyone's output.
CREDIT_ROLES = ("author", "guest")
CREDIT_SQL = ", ".join(f"'{r}'" for r in CREDIT_ROLES)


def _vector_ranking(conn, query, source, model, depth):
    """Publication ids by vector similarity, best first, deduped.

    Ranks the whole index rather than a candidate window. A window truncates
    on *vectors*, and since #34 one long report can hold a hundred of them —
    for "Xi Jinping Thought" a single report took five of the top seven, and
    the publications behind it fell off the end before dedupe ever saw them.
    `embed.rank` argsorts the full array whatever limit it is given, so the
    window was never buying anything either.
    """
    stored = db.load_embeddings(conn, source, model)
    if not stored:
        return []
    matrix = embed.normalise(numpy.stack([embed.unpack(r["vector"]) for r in stored]))
    order, seen = [], set()
    for idx, _ in embed.rank(embed.embed_query(query, model=model), matrix,
                             limit=len(stored)):
        pub = stored[idx]["publication_id"]
        if pub not in seen:
            seen.add(pub)
            order.append(pub)
        if len(order) >= depth:
            break
    return order


# How much a shortlist mark is worth in the fused ranking (#25): two positions.
#
# Sized against the *worst* case, not the typical one. With several rankers
# fused, being found by a second ranker is worth ~0.016 against ~0.0003 between
# adjacent ranks, so almost any boost is a tiebreaker. But when one ranker
# supplies the whole list — keyword-only, which is what a search degrades to
# when ollama is down — the gaps are uniform and the boost moves a result
# exactly this many places. Five positions was tried first and took the fifth
# result to the top, which is not "wins a near-tie".
SHORTLIST_POSITIONS = 2
SHORTLIST_BOOST = (1 / (embed.FUSION_K + 1)
                   - 1 / (embed.FUSION_K + 1 + SHORTLIST_POSITIONS))


def hybrid_find(conn, query, limit=10, sources=("one_liner", "section"),
                model=embed.MODEL, with_vectors=True, boost_shortlist=True):
    """Hybrid keyword + vector search, fused by rank (scores do not compare).

    Returns (hits, notes). Each hit is a dict with the publication row, the
    fusion score, which rankers surfaced it, the one-liner, the best-matching
    section, and whether it is shortlisted. Raises embed.OllamaUnreachable when
    the vector half is wanted but ollama is down — callers decide whether to
    degrade (`with_vectors=False` reruns keyword-only) or abort.

    `boost_shortlist=False` returns pure retrieval. `scripts/eval_retrieval.py`
    must use it: with the boost on, the script measures retrieval *plus*
    curation, and the recorded baseline (87% recall@1, #17) silently stops
    being comparable to anything measured later.
    """
    depth = max(limit * 3, 20)
    notes = []
    try:
        # fts_safe, not the raw query: a hyphenated policy term would
        # otherwise raise and drop the keyword half unnoticed.
        keyword = [r["id"] for r in db.search(conn, db.fts_safe(query), limit=depth)]
    except sqlite3.OperationalError as exc:
        notes.append(f"keyword half skipped ({exc})")
        keyword = []
    vectors = ([_vector_ranking(conn, query, s, model, depth) for s in sources]
               if with_vectors else [])
    if not with_vectors:
        notes.append("vector half disabled — keyword ranking only")
    elif sources and not any(vectors):
        # An empty vector index degrades silently otherwise — a fresh database
        # would serve keyword-only results while claiming to be hybrid.
        notes.append("no stored vectors — keyword ranking only (run: pubbrain embed)")
    if not keyword and not any(vectors):
        return [], notes

    fused = embed.fuse([keyword, *vectors])
    starred = db.shortlisted_ids(conn) if boost_shortlist else set()
    if starred:
        # Re-sort after boosting, not before: applying it to an already-sliced
        # list would only reorder the page rather than change what is on it.
        fused = sorted(((i, s + (SHORTLIST_BOOST if i in starred else 0.0))
                        for i, s in fused), key=lambda kv: -kv[1])
    fused = fused[:limit]
    ids = [i for i, _ in fused]
    pubs = publications_by_id(conn, ids)
    where = db.matching_sections(conn, db.fts_safe(query), ids) if keyword else {}
    # Badge every hit, boosted or not: a result that ranks higher for a reason
    # the page does not show reads as the ranking being wrong.
    shortlisted = starred or db.shortlisted_ids(conn)

    # Search does the opposite of the listing (#36): a chapter is findable in
    # its own right, but reads as an orphan without the report it argues in.
    parents = parents_of(conn, ids)

    hits = []
    for pub_id, score in fused:
        rankers = [name for name, ranking in
                   [("K", keyword)] + [(f"V{i + 1}", v) for i, v in enumerate(vectors)]
                   if pub_id in ranking]
        hits.append({
            "publication": pubs[pub_id],
            "score": score,
            "rankers": "".join(rankers),
            "one_liner": pubs[pub_id]["summary_one_liner"],
            "section": where.get(pub_id),
            "shortlisted": pub_id in shortlisted,
            "parent": parents.get(pub_id),
        })
    return hits, notes


def parents_of(conn, ids):
    """{child_id: parent row} for whichever of `ids` are chapters (#36)."""
    if not ids:
        return {}
    holes = ", ".join("?" * len(ids))
    return {r["child_id"]: r for r in conn.execute(
        f"""SELECT c.id AS child_id, c.parent_position, p.id, p.title, p.url
            FROM publications c JOIN publications p ON p.id = c.parent_id
            WHERE c.id IN ({holes})""", list(ids)).fetchall()}


def publications_by_id(conn, ids):
    if not ids:
        return {}
    holes = ", ".join("?" * len(ids))
    return {r["id"]: r for r in conn.execute(
        f"""SELECT p.*, e.summary_one_liner
            FROM publications p
            LEFT JOIN primary_enrichment e ON e.publication_id = p.id
            WHERE p.id IN ({holes})""", ids).fetchall()}


def _as_list(value):
    """Accept a scalar or a list, and drop empties — the filters are
    multi-valued (#26) but the CLI and MCP still pass single values."""
    if value is None or value == "":
        return []
    if isinstance(value, (str, int)):
        return [str(value)]
    return [str(v) for v in value if v not in (None, "")]


def list_publications(conn, pub_type=None, year=None, person_id=None,
                      shortlisted=False, topic=None, topic_primary=False,
                      series=None, include_chapters=False,
                      limit=PAGE_SIZE, offset=0):
    """Browse rows, newest first, each with its one-liner and its people.

    `pub_type`, `year` and `series` take a list or a single value; several of
    any widens the selection (#26). The motivating case is exclusion — picking
    every type except Podcast — so "none selected" means all, never none.
    `series` matches the recurring MERICS formats exactly (China Essentials,
    Economic Indicators, …) — for the standing projects use collections (#32),
    which are a different relation on purpose.

    `topic` filters on the controlled vocabulary (#4); `topic_primary` narrows
    to the model's first-ranked topic, which is the honest "what is this
    about" rather than "this was mentioned".

    **Chapters are hidden by default** (#36): a report appears once, and its
    chapters are reached through it. `include_chapters=True` is for the
    handful of places that genuinely mean every row — never for a count
    described as "publications".

    Publications without enrichment (podcasts, the paywalled handful) are
    included on purpose — silently dropping 16% of the catalog is the exact
    failure coverage_check documents.
    """
    where, params = [], []
    types, years = _as_list(pub_type), _as_list(year)
    runs = _as_list(series)
    if not include_chapters:
        where.append("p.parent_id IS NULL")
    if runs:
        where.append(f"p.series IN ({', '.join('?' * len(runs))})")
        params.extend(runs)
    if topic:
        where.append("p.id IN (SELECT publication_id FROM publication_topics "
                     f"WHERE topic_slug = ?{' AND position = 1' if topic_primary else ''})")
        params.append(topic)
    if types:
        where.append(f"p.pub_type IN ({', '.join('?' * len(types))})")
        params.extend(types)
    if years:
        where.append(f"substr(p.date_published, 1, 4) IN "
                     f"({', '.join('?' * len(years))})")
        params.extend(years)
    if person_id:
        where.append("p.id IN (SELECT publication_id FROM publication_people "
                     "WHERE person_id = ?)")
        params.append(person_id)
    if shortlisted:
        where.append("p.id IN (SELECT publication_id FROM shortlist)")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM publications p {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT p.id, p.title, p.pub_type, p.date_published, p.access,
               e.summary_one_liner
        FROM publications p
        LEFT JOIN primary_enrichment e ON e.publication_id = p.id
        {clause}
        ORDER BY p.date_published DESC, p.id DESC
        LIMIT ? OFFSET ?
        """, [*params, limit, offset]).fetchall()
    return rows, total, people_for(conn, [r["id"] for r in rows])


def people_for(conn, publication_ids):
    """{publication_id: [person rows in byline order]} for the ids given."""
    if not publication_ids:
        return {}
    holes = ", ".join("?" * len(publication_ids))
    out = {}
    for r in conn.execute(
        f"""SELECT pp.publication_id, pp.role, pp.source, pe.id, pe.name,
                   pe.affiliation, pe.is_current
            FROM publication_people pp JOIN people pe ON pe.id = pp.person_id
            WHERE pp.publication_id IN ({holes})
            ORDER BY pp.publication_id, pp.role, pp.position""", publication_ids):
        out.setdefault(r["publication_id"], []).append(r)
    return out


def filter_options(conn):
    """Distinct types, years and series for the browse filters, with counts.

    Chapters are excluded, because these numbers label the checkboxes that
    filter the listing and the listing hides chapters (#36). A count beside a
    filter that does not match what ticking it produces is worse than no count.
    """
    types = conn.execute(
        "SELECT pub_type, COUNT(*) n FROM publications WHERE parent_id IS NULL "
        "GROUP BY pub_type ORDER BY n DESC").fetchall()
    years = conn.execute(
        "SELECT substr(date_published, 1, 4) y, COUNT(*) n FROM publications "
        "WHERE date_published IS NOT NULL AND parent_id IS NULL "
        "GROUP BY y ORDER BY y DESC").fetchall()
    series = conn.execute(
        "SELECT series, COUNT(*) n FROM publications "
        "WHERE series IS NOT NULL AND parent_id IS NULL "
        "GROUP BY series ORDER BY n DESC").fetchall()
    return types, years, series


def publication_detail(conn, pub_id):
    """Everything one page needs, or None. Enrichment JSON comes back parsed."""
    pub = conn.execute("SELECT * FROM publications WHERE id = ?", (pub_id,)).fetchone()
    if not pub:
        return None
    # Every summary the publication holds, primary first (#18). Candidates are
    # shown for comparison and are used by nothing else — search, embeddings
    # and every count read the primary alone.
    verdicts = {r["subject_id"]: r for r in conn.execute(
        "SELECT * FROM reviews WHERE scope = 'enrichment'")}
    enrichments = [dict(r, key_findings=json.loads(r["key_findings"]),
                        entities=json.loads(r["entities"]),
                        review=verdicts.get(r["id"]))
                   for r in db.enrichments_for(conn, pub_id)]
    sections = conn.execute(
        "SELECT * FROM publication_sections WHERE publication_id = ? "
        "ORDER BY position", (pub_id,)).fetchall()
    labels = topics.labels()
    return {
        "publication": pub,
        "people": people_for(conn, [pub_id]).get(pub_id, []),
        "enrichments": enrichments,
        "text": conn.execute("SELECT * FROM publication_text WHERE publication_id = ?",
                             (pub_id,)).fetchone(),
        # Ranked, so the first is what the piece is about (#4). Labels come
        # from the YAML and a retired slug simply falls back to itself.
        "topics": [{"slug": s, "name": labels.get(s, s), "primary": i == 0}
                   for i, s in enumerate(
                       db.topics_for_publications(conn, [pub_id]).get(pub_id, []))],
        # The report's table of contents, or the report a chapter belongs to
        # — exactly one of these is ever non-empty (#36).
        "chapters": db.chapters_of(conn, pub_id),
        "parent": db.parent_of(conn, pub_id),
        "sections": sections,
        # Two different counts, and conflating them overstates the structure:
        # a section too long to embed is stored as several windows (#34), so
        # the rows are retrieval units, not divisions the author made.
        "section_count": sum(1 for s in sections if not s["chunk_index"]),
        "site_tags": [r["name"] for r in conn.execute(
            "SELECT t.name FROM publication_site_tags pt "
            "JOIN site_tags t ON t.id = pt.tag_id WHERE pt.publication_id = ? "
            "ORDER BY t.name", (pub_id,))],
        # The flag on the summary actually being served, which is what the
        # page's flag form acts on.
        "review": next((e["review"] for e in enrichments if e["is_primary"]), None),
    }


def person_page(conn, person_id, q=None, pub_type=None):
    """A person and their publications grouped by role, or None.

    This is the embryo of the author cheat sheet (#9). Credits under-report in
    three known ways (#12) — every consumer must say so, not imply completeness.

    `q` and `pub_type` narrow the list (#61). A *filter*, not the catalog's
    hybrid retrieval: over one person's few dozen credits, ranking buys
    nothing, and a rank cut would drop the records with no summary — which is
    where credits are thinnest in the first place. `types` comes back with the
    unfiltered counts, so the facet does not shrink as it is used.
    """
    person = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if not person:
        return None
    if person["merged_into"]:
        # A merged duplicate (#47) — old links and stale ids land on the
        # survivor rather than on an empty tombstone.
        return person_page(conn, person["merged_into"], q=q, pub_type=pub_type)
    wanted = _as_list(pub_type)
    term = (q or "").strip()
    by_role, types, total = {}, {}, 0
    for r in conn.execute(
        f"""
        SELECT pp.role, p.id, p.title, p.pub_type, p.date_published,
               e.summary_one_liner, e.summary_short
        FROM publication_people pp
        JOIN publications p ON p.id = pp.publication_id
        LEFT JOIN primary_enrichment e ON e.publication_id = p.id
        WHERE pp.person_id = ? AND pp.role IN ({CREDIT_SQL})
        ORDER BY pp.role, p.date_published DESC
        """, (person_id,)):
        total += 1
        types[r["pub_type"]] = types.get(r["pub_type"], 0) + 1
        if wanted and r["pub_type"] not in wanted:
            continue
        if term and not _mentions(term, r["title"], r["summary_one_liner"],
                                  r["summary_short"]):
            continue
        by_role.setdefault(r["role"], []).append(r)
    # Hosting is reported as a number, not 92 rows of episodes (#27) — it says
    # what the person does without burying what they wrote.
    hosted = conn.execute(
        "SELECT COUNT(*) FROM publication_people WHERE person_id = ? "
        "AND role = 'host'", (person_id,)).fetchone()[0]
    return {"person": person, "by_role": by_role, "hosted": hosted,
            "q": term, "pub_type": wanted, "total": total,
            "shown": sum(len(v) for v in by_role.values()),
            "types": sorted(({"pub_type": t, "n": n} for t, n in types.items()),
                            key=lambda t: -t["n"])}


def _mentions(term, *fields):
    """Case-insensitive substring match over a record's own words.

    Every word of the term has to appear somewhere, so "chip export" finds a
    piece whose title says chips and whose summary says export controls —
    the same AND-ing the keyword index does, without its match syntax.
    """
    haystack = " ".join(f for f in fields if f).lower()
    return all(word in haystack for word in term.lower().split())


def list_people(conn, current_only=True):
    """People with at least one link, most credited first.

    Defaults to who is at MERICS today (#28) — 37 of 249. `is_current` is a
    snapshot of the site's roster, not a verdict on whether the work matters:
    the third most-credited person in the catalog is former staff. Callers must
    show the hidden count, never filter silently.

    **Hosting is not a credit (#27).** A recurring podcast host accumulates a
    link per episode, which put the second-most-credited slot in this list on
    someone whose output is a role rather than a body of work, and pushed the
    analysts down. `n` therefore counts `author` and `guest` only.

    People whose links are *all* hosting are still listed, with `n = 0` and
    their `hosted` count — three of them are MERICS staff, and dropping them
    from the list of people would be a worse answer than ranking them last.
    """
    rows = conn.execute(
        f"""
        SELECT pe.id, pe.name, pe.affiliation, pe.is_current, pe.roster_title,
               SUM(pp.role IN ({CREDIT_SQL})) n,
               SUM(pp.role = 'host') hosted
        FROM people pe JOIN publication_people pp ON pp.person_id = pe.id
        WHERE pe.merged_into IS NULL
        GROUP BY pe.id ORDER BY n DESC, pe.name
        """).fetchall()
    if not current_only:
        return rows
    return [r for r in rows if r["is_current"]]


def flagged(conn):
    """Summaries flagged as misleading, newest flag first.

    Trust is the default (#21): a summary needs no approval, only a flag when
    it misleads. `confirmed` rows come from spot checks and feed the stats,
    not this list.
    """
    return conn.execute(
        """
        SELECT p.id, p.title, p.pub_type, p.date_published,
               e.summary_one_liner, e.model, e.is_primary, r.note,
               r.created_at AS flagged_at
        FROM reviews r
        JOIN publication_enrichment e ON e.id = r.subject_id
        JOIN publications p ON p.id = e.publication_id
        WHERE r.scope = 'enrichment' AND r.verdict = 'flagged'
        ORDER BY r.created_at DESC
        """).fetchall()


def people_matching(conn, fragment):
    """People whose name contains `fragment`, most credited first.

    `n` counts credits, not links — hosting is excluded (#27), so the
    disambiguation list ranks the same way the People page does.
    """
    return conn.execute(
        f"""
        SELECT pe.id, pe.name, pe.affiliation, pe.is_current,
               SUM(pp.role IN ({CREDIT_SQL})) n
        FROM people pe LEFT JOIN publication_people pp ON pp.person_id = pe.id
        WHERE pe.name LIKE ? AND pe.merged_into IS NULL
        GROUP BY pe.id ORDER BY n DESC, pe.name
        """, (f"%{fragment}%",)).fetchall()


# Publication weight by type for the Insights views (#49): a Report is a
# bigger institutional bet than a Comment, and marks and aggregates size by
# that, not by row count. Owner-set ordering; the values are tuning, the
# order is not. Podcasts are absent on purpose — Insights excludes them
# uniformly (owner, 2026-08-09): no text, no topics, and one stated
# exclusion beats a footnote per view (#50 is the later answer).
INSIGHT_TYPE_WEIGHTS = {
    "Report": 8,
    "Tracker": 4, "Executive Memo": 4,
    "MERICS Briefs": 2, "External publication": 2, "Interview": 2,
    "Comment": 1,
}
INSIGHT_EXCLUDED_TYPES = ("Podcast",)


def _insight_scope():
    """WHERE fragment every Insights query shares: parents only, no podcasts."""
    marks = ", ".join("?" * len(INSIGHT_EXCLUDED_TYPES))
    return (f"p.parent_id IS NULL AND p.pub_type NOT IN ({marks})",
            list(INSIGHT_EXCLUDED_TYPES))


def topic_graph(conn):
    """Nodes and edges for the topic co-occurrence map (#49).

    Edges count publications where both topics sit at `position <= 2` — the
    positions the mapping trusts; the tail of a multi-story Brief is stretched
    from single clauses, and counting it gave 307 of 325 possible pairs, a
    hairball by construction. The client thresholds further; nothing below 2
    shared publications leaves here.
    """
    scope, params = _insight_scope()
    nodes = {slug: {"slug": slug, "label": label,
                    "pubs": 0, "weighted": 0, "about": 0}
             for slug, label in topics.labels().items()}
    for r in conn.execute(
        f"""SELECT pt.topic_slug AS s, p.pub_type AS t,
                   MIN(pt.position) AS pos
            FROM publication_topics pt
            JOIN publications p ON p.id = pt.publication_id
            WHERE {scope}
            GROUP BY pt.publication_id, pt.topic_slug""", params):
        node = nodes.get(r["s"])
        if node is None:      # a renamed slug lingering in an old mapping
            continue
        node["pubs"] += 1
        node["weighted"] += INSIGHT_TYPE_WEIGHTS.get(r["t"], 1)
        if r["pos"] == 1:
            node["about"] += 1
    edges = [dict(r) for r in conn.execute(
        f"""SELECT a.topic_slug AS s1, b.topic_slug AS s2, COUNT(*) AS n
            FROM publication_topics a
            JOIN publication_topics b ON b.publication_id = a.publication_id
             AND a.topic_slug < b.topic_slug
            JOIN publications p ON p.id = a.publication_id
            WHERE a.position <= 2 AND b.position <= 2 AND {scope}
            GROUP BY s1, s2 HAVING n >= 2""", params)]
    return {"nodes": [n for n in nodes.values() if n["pubs"]], "edges": edges}


def topic_time(conn):
    """Quarterly attention per topic (#49), publications counted once each
    under their primary topic — the honest "about" signal; counting touches
    would count a four-story Brief four times.

    Two measures ship and the chart toggles between them: type weight, and
    word count — a 40,000-word flagship and a 6,000-word report are the same
    type weight while being very different bets. Word counts read the stored
    text, so they are 0 for the handful with none.
    """
    scope, params = _insight_scope()
    per = {}
    quarters = set()
    for r in conn.execute(
        f"""SELECT pt.topic_slug AS slug,
                   substr(p.date_published, 1, 4) || '-Q' ||
                     ((CAST(substr(p.date_published, 6, 2) AS INTEGER) + 2) / 3)
                     AS q,
                   p.pub_type AS t, COALESCE(x.word_count, 0) AS words
            FROM publication_topics pt
            JOIN publications p ON p.id = pt.publication_id
            LEFT JOIN publication_text x ON x.publication_id = p.id
            WHERE pt.position = 1 AND p.date_published IS NOT NULL
              AND {scope}""", params):
        quarters.add(r["q"])
        cell = per.setdefault(r["slug"], {}).setdefault(
            r["q"], {"w": 0, "words": 0, "n": 0})
        cell["w"] += INSIGHT_TYPE_WEIGHTS.get(r["t"], 1)
        cell["words"] += r["words"]
        cell["n"] += 1
    if not quarters:
        return {"quarters": [], "topics": []}
    axis = _quarters_between(min(quarters), max(quarters))
    labels = topics.labels()
    out = []
    for slug, cells in per.items():
        if slug not in labels:
            continue
        out.append({
            "slug": slug, "label": labels[slug],
            "w": [cells.get(q, {}).get("w", 0) for q in axis],
            "words": [cells.get(q, {}).get("words", 0) for q in axis],
            "n": [cells.get(q, {}).get("n", 0) for q in axis],
        })
    out.sort(key=lambda t: sum(t["w"]), reverse=True)
    return {"quarters": axis, "topics": out}


def who_knows_what(conn, current_only=True):
    """Analyst × topic (#49): who at MERICS knows about Y, one screen.

    A cell counts credited publications whose *primary* topic is the column —
    "wrote something about Y", the same honesty rule as the time view.
    Credits mean author or guest (#27: hosting is not output). Chapters count:
    they carry their own bylines and their own topics, and this measures
    evidence of expertise, not catalog size. Podcasts fall out naturally —
    no enrichment, no topics (#50).

    Every row carries `credits_total` — all credits, mapped or not — because
    a cell reading 0 means something different for someone with 40 credits
    than for someone with 3 (#12): the caller must show the difference, not
    imply a gap that is a recording artifact.
    """
    cells = {}
    for r in conn.execute(
        f"""SELECT pp.person_id AS pid, pt.topic_slug AS slug,
                   COUNT(DISTINCT pp.publication_id) AS n
            FROM publication_people pp
            JOIN publication_topics pt
              ON pt.publication_id = pp.publication_id AND pt.position = 1
            WHERE pp.role IN ({CREDIT_SQL})
            GROUP BY pid, slug"""):
        cells.setdefault(r["pid"], {})[r["slug"]] = r["n"]
    labels = topics.labels()
    people = []
    for r in conn.execute(
        f"""SELECT pe.id, pe.name, pe.affiliation, pe.is_current,
                   COUNT(*) AS credits_total
            FROM people pe
            JOIN publication_people pp ON pp.person_id = pe.id
             AND pp.role IN ({CREDIT_SQL})
            WHERE pe.merged_into IS NULL
            GROUP BY pe.id"""):
        own = {s: n for s, n in cells.get(r["id"], {}).items() if s in labels}
        if not own:
            continue
        if current_only and not r["is_current"]:
            continue
        people.append({
            "id": r["id"], "name": r["name"], "affiliation": r["affiliation"],
            "is_current": r["is_current"], "credits_total": r["credits_total"],
            "mapped_total": sum(own.values()), "cells": own,
        })
    people.sort(key=lambda p: p["mapped_total"], reverse=True)
    col_totals = {}
    for p in people:
        for s, n in p["cells"].items():
            col_totals[s] = col_totals.get(s, 0) + n
    cols = sorted(col_totals, key=col_totals.get, reverse=True)
    return {"topics": [{"slug": s, "label": labels[s]} for s in cols],
            "people": people}


def _quarters_between(lo, hi):
    """Contiguous quarter labels lo..hi — a quarter nothing landed in is a
    real zero, not a skipped tick."""
    axis = []
    y, q = int(lo[:4]), int(lo[-1])
    while True:
        axis.append(f"{y}-Q{q}")
        if f"{y}-Q{q}" == hi:
            break
        q += 1
        if q == 5:
            y, q = y + 1, 1
    return axis


QUARTER_SQL = ("substr(date_published, 1, 4) || '-Q' || "
               "((CAST(substr(date_published, 6, 2) AS INTEGER) + 2) / 3)")


def keyword_time(conn, query, deep=False):
    """Quarterly attention for an arbitrary term (#49) — the drill-down freed
    from the 26-topic vocabulary: type "Beidaihe" and see its curve.

    Two scopes, and they answer different questions. Default is *about*: the
    headline fields plus the LLM summaries (the FTS description column is
    empty wherever a body exists, by the #15 fix, so summaries carry the
    "about" signal there). `deep` matches anywhere in the indexed text — a
    mention, not a subject. A chapter hit counts as its parent, dated by the
    parent, so a report is one hit however many chapters repeat the term.
    """
    match = db.fts_safe(query)
    if not match:
        return {"query": query, "deep": deep, "quarters": [],
                "w": [], "words": [], "n": [], "total": 0}
    if deep:
        hits = """SELECT f.rowid AS id FROM publication_fts f
                  WHERE publication_fts MATCH :m"""
    else:
        hits = """SELECT f.rowid AS id FROM publication_fts f
                  WHERE publication_fts MATCH '{title subtitle description} : ' || :m
                  UNION
                  SELECT e.publication_id FROM primary_enrichment e
                  WHERE e.summary_short LIKE :l OR e.summary_one_liner LIKE :l"""
    # The excluded types are code constants, safe to inline — and inlining
    # keeps the named-parameter dict clean.
    types = ", ".join(f"'{t}'" for t in INSIGHT_EXCLUDED_TYPES)
    scope = f"p.parent_id IS NULL AND p.pub_type NOT IN ({types})"
    q_sql = QUARTER_SQL.replace("date_published", "p.date_published")
    try:
        rows = conn.execute(
            f"""SELECT {q_sql} AS q, p.pub_type AS t,
                       COALESCE(x.word_count, 0) AS words
                FROM (SELECT DISTINCT COALESCE(pub.parent_id, pub.id) AS root_id
                      FROM ({hits}) h JOIN publications pub ON pub.id = h.id) r
                JOIN publications p ON p.id = r.root_id
                LEFT JOIN publication_text x ON x.publication_id = p.id
                WHERE p.date_published IS NOT NULL AND {scope}""",
            {"m": match, "l": f"%{query.strip()}%"}).fetchall()
    except sqlite3.OperationalError:
        # An unbalanced quote survives fts_safe as a "quoted phrase". Say so —
        # a silent zero would read as "MERICS never wrote about this".
        return {"query": query, "deep": deep, "quarters": [], "w": [],
                "words": [], "n": [], "total": 0,
                "error": "unparseable query — try plain words"}
    # The axis spans the whole catalog, not just the hits, so a keyword curve
    # lines up under the streamgraph and sparse terms read as sparse.
    span = conn.execute(
        f"""SELECT MIN({q_sql}) lo, MAX({q_sql}) hi
            FROM publications p
            WHERE p.date_published IS NOT NULL AND {scope}""").fetchone()
    if not span["lo"]:
        return {"query": query, "deep": deep, "quarters": [],
                "w": [], "words": [], "n": [], "total": 0}
    axis = _quarters_between(span["lo"], span["hi"])
    idx = {q: i for i, q in enumerate(axis)}
    w = [0] * len(axis)
    words = [0] * len(axis)
    n = [0] * len(axis)
    for r in rows:
        i = idx.get(r["q"])
        if i is None:
            continue
        w[i] += INSIGHT_TYPE_WEIGHTS.get(r["t"], 1)
        words[i] += r["words"]
        n[i] += 1
    return {"query": query, "deep": deep, "quarters": axis,
            "w": w, "words": words, "n": n, "total": sum(n)}


def topic_spotlight(conn, slug, limit=6):
    """A topic's registry card for the map panel: what it is *about*, latest
    first — position 1 only, matching the glossary's "about" count."""
    scope, params = _insight_scope()
    return conn.execute(
        f"""SELECT p.id, p.title, p.pub_type, p.date_published,
                   e.summary_one_liner
            FROM publications p
            JOIN publication_topics pt ON pt.publication_id = p.id
             AND pt.topic_slug = ? AND pt.position = 1
            LEFT JOIN primary_enrichment e ON e.publication_id = p.id
            WHERE {scope}
            ORDER BY p.date_published DESC LIMIT ?""",
        (slug, *params, limit)).fetchall()


def topic_pair(conn, slug_a, slug_b, limit=30):
    """The publications an edge stands for: both topics at position <= 2."""
    scope, params = _insight_scope()
    return conn.execute(
        f"""SELECT p.id, p.title, p.pub_type, p.date_published,
                   e.summary_one_liner
            FROM publications p
            JOIN publication_topics ta ON ta.publication_id = p.id
             AND ta.topic_slug = ? AND ta.position <= 2
            JOIN publication_topics tb ON tb.publication_id = p.id
             AND tb.topic_slug = ? AND tb.position <= 2
            LEFT JOIN primary_enrichment e ON e.publication_id = p.id
            WHERE {scope}
            ORDER BY p.date_published DESC LIMIT ?""",
        (slug_a, slug_b, *params, limit)).fetchall()


def landscape(conn):
    """Points for the embedding landscape (#49), coordinates from the cache.

    Color identity is the *cluster* of the primary topic, not the topic: 26
    hues cannot be told apart and the validated palette carries 8, so the six
    vocabulary clusters are the honest coloring — the tooltip names the exact
    topic. A point whose summary lost its topics (regeneration, #24) stays on
    the map in neutral rather than vanishing.

    `missing` counts in-scope vectors with no coordinate: non-zero only before
    the first fit, since `place_new` runs on every data request after that.
    """
    from . import landscape as _landscape
    cluster_of, clusters = {}, []
    for c in topics.load():
        for t in c["topics"]:
            cluster_of[t["slug"]] = len(clusters)
        clusters.append(c["cluster"])
    labels = topics.labels()
    points = []
    for r in conn.execute(
        f"""SELECT lc.publication_id AS id, lc.x, lc.y, lc.placed,
                   p.title, p.pub_type, p.date_published,
                   e.summary_one_liner, pt.topic_slug
            FROM landscape_coords lc
            JOIN publications p ON p.id = lc.publication_id
            LEFT JOIN primary_enrichment e ON e.publication_id = lc.publication_id
            LEFT JOIN publication_topics pt
              ON pt.publication_id = lc.publication_id AND pt.position = 1
            WHERE p.parent_id IS NULL
              AND p.pub_type NOT IN
                  ({", ".join("?" * len(INSIGHT_EXCLUDED_TYPES))})""",
        list(INSIGHT_EXCLUDED_TYPES)):
        slug = r["topic_slug"]
        points.append({
            "id": r["id"], "x": r["x"], "y": r["y"],
            "type": r["pub_type"],
            "w": INSIGHT_TYPE_WEIGHTS.get(r["pub_type"], 1),
            "date": (r["date_published"] or "")[:7],
            "title": r["title"],
            "one_liner": r["summary_one_liner"],
            "topic": labels.get(slug),
            "cluster": cluster_of.get(slug, -1),
        })
    fitted = bool(points)
    missing = (len(_landscape.scope_rows(conn)) - len(points)
               if not fitted else 0)
    return {"points": points, "clusters": clusters,
            "fitted": fitted, "missing": max(missing, 0)}


# A record is "thin" below this share of its own type's median length. Per
# type, because the types differ by an order of magnitude: a 350-word Comment
# is a complete tech note, a 350-word Report is a landing-page abstract. A flat
# threshold flagged ~55 recent Comments that were perfectly fine (#6).
THIN_SHARE = 0.25

# Types where a short record means nothing is missing.
THIN_EXEMPT = ("Podcast", "External publication")


def type_length_norms(conn) -> dict:
    """pub_type -> (median words, thin threshold), measured from the catalog
    rather than written down, so it stays true as the corpus grows."""
    norms = {}
    for row in conn.execute(
        """SELECT p.pub_type, t.word_count FROM publications p
           JOIN publication_text t ON t.publication_id = p.id
           ORDER BY p.pub_type, t.word_count"""):
        norms.setdefault(row["pub_type"], []).append(row["word_count"])
    out = {}
    for kind, words in norms.items():
        if kind in THIN_EXEMPT or len(words) < 5:
            continue
        median = words[len(words) // 2]
        out[kind] = (median, int(median * THIN_SHARE))
    return out


def thin_records(conn, limit=200):
    """Records far shorter than their type normally runs to (#33).

    These are the landing-page-abstract cases: a Report holding 226 words when
    Reports median 3,563. Paywalled records are excluded — their text is behind
    the paywall by definition, and they have their own backlog section.
    """
    norms = type_length_norms(conn)
    if not norms:
        return []
    clauses, params = [], []
    for kind, (_median, floor) in norms.items():
        clauses.append("(p.pub_type = ? AND t.word_count < ?)")
        params += [kind, floor]
    rows = conn.execute(
        f"""
        SELECT p.id, p.title, p.url, p.pub_type, p.date_published, p.pdf_url,
               t.word_count, t.source
        FROM publications p JOIN publication_text t ON t.publication_id = p.id
        WHERE p.access = 'public' AND ({' OR '.join(clauses)})
        ORDER BY p.date_published DESC LIMIT ?
        """, [*params, limit]).fetchall()
    return [dict(r, expected=norms[r["pub_type"]][0]) for r in rows]


def backlog(conn, show_parked=False) -> dict:
    """Everything unfinished, grouped, each row carrying a link (#33).

    Deliberately excludes what is incomplete *by design* — a podcast without a
    summary is not a task, and listing it would teach the eye to skip the page.
    """
    parked = db.parked_ids(conn)
    thin = thin_records(conn)
    if not show_parked:
        thin = [r for r in thin if r["id"] not in parked]
    return {
        "thin": thin,
        "parked": parked,
        "parked_rows": conn.execute(
            """SELECT p.id, p.title, p.url, p.pub_type, p.date_published, r.note,
                      r.verdict
               FROM reviews r JOIN publications p ON p.id = r.subject_id
               WHERE r.scope = 'backlog' AND r.verdict = 'parked'
               ORDER BY p.date_published DESC""").fetchall(),
        "todo_rows": db.backlog_todo(conn),
        "url_todo": conn.execute(
            """SELECT url, note, probe_title FROM sitemap_urls
               WHERE disposition = 'todo' ORDER BY url""").fetchall(),
        "thin_with_pdf": [r for r in thin if r["pdf_url"]],
        "homeless": db.homeless_urls(conn),
        "gone": conn.execute(
            """SELECT url, last_error FROM sitemap_urls WHERE status = 'gone'
               ORDER BY url""").fetchall(),
        "paywalled": conn.execute(
            """SELECT p.id, p.title, p.url, p.pub_type, p.date_published
               FROM publications p LEFT JOIN publication_text t
               ON t.publication_id = p.id
               WHERE p.access <> 'public' AND COALESCE(t.word_count, 0) = 0
               ORDER BY p.date_published DESC""").fetchall(),
        "undownloaded_pdfs": conn.execute(
            """SELECT COUNT(*) FROM publications
               WHERE pdf_url IS NOT NULL AND pdf_path IS NULL""").fetchone()[0],
        "unmapped": conn.execute(
            """SELECT COUNT(*) FROM primary_enrichment e
               LEFT JOIN publication_topics pt ON pt.publication_id = e.publication_id
               WHERE pt.publication_id IS NULL""").fetchone()[0],
        "norms": type_length_norms(conn),
    }


def coverage_caveats(conn) -> list:
    """The limits any absence claim has to carry, measured rather than written.

    Every clause is a known gap with an issue behind it, and each disappears on
    its own once that issue closes — a hand-maintained paragraph goes on
    claiming gaps that no longer exist. `docs/schema.md` points here instead of
    keeping a second copy.
    """
    row = conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM sitemap_urls
                WHERE scope = 'root-level' AND status = 'pending'
                  -- Undecided only. A settled exclusion is reported below as
                  -- what it is, rather than counted as a gap (#10).
                  AND COALESCE(disposition, '') != 'exclude')           AS legacy,
               (SELECT COUNT(*) FROM sitemap_urls
                WHERE scope = 'root-level' AND disposition = 'exclude')  AS declined,
               (SELECT COUNT(*) FROM sitemap_urls
                WHERE scope = 'publication'
                  AND status IN ('failed', 'skipped'))                  AS unscraped,
               (SELECT COUNT(*) FROM sitemap_urls
                WHERE scope = 'publication' AND status = 'gone')        AS gone,
               -- Chapters are excluded: a chapter of a report is reached
               -- through its parent, which carries the byline (#36). Counting
               -- them inflated this from 263 to 298 the day chapters landed.
               (SELECT COUNT(*) FROM publications p
                WHERE p.parent_id IS NULL AND NOT EXISTS
                (SELECT 1 FROM publication_people pp
                 WHERE pp.publication_id = p.id))                       AS uncredited,
               (SELECT COUNT(*) FROM publication_people
                WHERE source = 'manual')                                AS by_hand,
               (SELECT COUNT(*) FROM publications)                      AS pubs,
               (SELECT COUNT(*) FROM publication_text)                  AS texts,
               (SELECT COUNT(*) FROM publications p
                LEFT JOIN publication_text t ON t.publication_id = p.id
                WHERE t.publication_id IS NULL
                  AND (p.og_description IS NULL OR p.og_description = ''))
                                                                        AS title_only
        """).fetchone()
    out = []
    if row["legacy"]:
        out.append(f"the catalog covers merics.org publications with typed URLs "
                   f"only — {row['legacy']} root-level legacy pages are not "
                   f"ingested (#10)")
    if row["declined"]:
        # Not a gap, and reported for the same reason `by_hand` is: a reader
        # comparing this catalog against the sitemap has to be able to account
        # for the difference. Most were confirmed in bulk against a
        # no-date-or-under-250-words rule rather than read one by one, so
        # "reviewed" would claim more than happened (#10).
        out.append(f"{row['declined']} root-level pages were assessed and "
                   f"deliberately excluded — site furniture, event pages and "
                   f"landing pages for other records (#10)")
    if row["unscraped"]:
        out.append(f"{row['unscraped']} pages failed or were skipped during "
                   f"scraping (#13)")
    if row["gone"]:
        # Not a scraping gap: the sitemap outlives the pages. These were real
        # publications and are invisible here, which an absence claim must own.
        out.append(f"{row['gone']} publications are listed in the sitemap but "
                   f"no longer resolve on merics.org, so they are absent from "
                   f"the catalog (#13)")
    if row["uncredited"]:
        out.append(f"{row['uncredited']} records credit no person, so "
                   f"author-based answers under-report (#12)")
    split = db.duplicate_people(conn)
    if split:
        # The one credit gap where the record looks complete (#47): each row
        # of the pair collects its own credits, so a per-person count is
        # silently short rather than visibly empty.
        out.append(f"{len(split)} people exist as more than one row "
                   f"({', '.join(r['name'] for r in split)}), splitting "
                   f"their credits until merged (#47)")
    if row["by_hand"]:
        # Not a gap — the opposite. But a reader comparing a count against
        # merics.org deserves to know part of it was entered rather than read.
        out.append(f"{row['by_hand']} credits were assigned by hand from the "
                   f"page's own text rather than parsed from a byline field (#40)")
    if row["texts"] < row["pubs"]:
        out.append(f"full text is indexed for {row['texts']} of {row['pubs']} "
                   f"records, so the rest are matchable by title alone"
                   + (f" — {row['title_only']} carry no description either"
                      if row["title_only"] else ""))
    # The one caveat with no number, and the reason it is written rather than
    # measured: MERICS work published only on a partner's site leaves no trace
    # on merics.org, so the sitemap the rest of this function counts against
    # never saw it (#12). Every other clause disappears when its issue closes;
    # this one cannot, because nothing here could ever detect it.
    out.append("MERICS work published only on a partner institution's site has "
               "no merics.org page and is therefore absent from this catalog "
               "entirely. Its extent is unknown (#12)")
    return out


def coverage_check(conn, term, limit=10):
    """Has MERICS covered X? Three probes, then a verdict that states its scope.

    Never returns a bare negative: `caveats` is what bounds the answer, and
    `error` means the query itself failed — which is not evidence of absence
    and must never be reported as one.
    """
    result = {"term": term, "caveats": coverage_caveats(conn), "error": None,
              "full_text": [], "matches": 0, "site_tags": [], "people": []}
    try:
        result["full_text"] = db.search(conn, db.fts_safe(term), limit=limit)
        result["matches"] = conn.execute(
            "SELECT COUNT(*) FROM publication_fts WHERE publication_fts MATCH ?",
            (db.fts_safe(term),)).fetchone()[0]
    except sqlite3.OperationalError as exc:
        result["error"] = str(exc)
        return result
    result["site_tags"] = conn.execute(
        """
        SELECT st.name AS tag, p.id, p.title, p.url
        FROM publications p
        JOIN publication_site_tags pst ON pst.publication_id = p.id
        JOIN site_tags st              ON st.id = pst.tag_id
        WHERE st.name LIKE ? ORDER BY st.name, p.date_published DESC
        """, (f"%{term}%",)).fetchall()
    result["people"] = people_matching(conn, term)
    return result


# The coverage view (#55). Two tiers of direct evidence, because a mention is
# not coverage: `about` matches in title/subtitle/standfirst, `mentions`
# matches anywhere — one FTS pass with a column filter apart. Both deduped to
# parent level first: a term found in six chapters of one report is one
# report's worth of coverage. Thresholds calibrated against the live catalog
# (probe table on #55), not chosen in the abstract.
COVERAGE_NEIGHBOURS = 12
COVERAGE_BANDS = ("not covered here", "adjacent only", "touched",
                  "covered", "covered extensively")
ABOUT_COLUMNS = "{title subtitle description}"
SIM_ADJACENT = 0.40     # top-1 cosine above which zero keyword matches reads
                        # as "covered under different words" — nonsense terms
                        # peak ~0.38, German synonyms of covered ground 0.41+
SIM_FLOOR = 0.15


def _coverage_verdict(about, mentions, sim1, keyword_only):
    """Band + needle position from the measured counts, nothing generated.

    `pos` places the needle on a 0-1 scale whose fifths are the bands —
    presentational; the `measured` sentence carries the actual numbers. The
    band cuts are a reading aid: the neighbours and hits are the evidence.
    """
    def out(band, t, measured):
        pos = (band + min(max(t, 0.0), 1.0)) / len(COVERAGE_BANDS)
        return {"band": band, "label": COVERAGE_BANDS[band],
                "measured": measured, "pos": round(pos, 4)}

    if mentions:
        if about:
            measured = (f"{about} publication{'s are' if about != 1 else ' is'} "
                        f"about it (title or standfirst) and {mentions} mention "
                        f"it in full text")
        else:
            measured = (f"nothing is titled for it, but {mentions} "
                        f"publication{'s' if mentions != 1 else ''} mention it "
                        f"in full text")
        if keyword_only:
            measured += "; the adjacency scan is missing (ollama is not running)"
        mass = mentions + 10 * about
        if about >= 8 or (about >= 3 and mentions >= 80) or mentions >= 150:
            return out(4, (numpy.log1p(mass) - numpy.log1p(150))
                       / (numpy.log1p(1500) - numpy.log1p(150)), measured)
        if about >= 2 or (about >= 1 and mentions >= 20):
            return out(3, (about + mentions / 50 - 1) / 7, measured)
        return out(2, mass / 20, measured)
    if keyword_only:
        # No matches and no adjacency signal: the two lowest bands cannot be
        # told apart, and saying so beats silently picking one.
        return {"band": None, "label": "no keyword match — adjacency unknown",
                "measured": "no full-text match, and with ollama down the "
                            "embedding scan that separates “not covered” "
                            "from “covered under different words” could "
                            "not run", "pos": None}
    if sim1 >= SIM_ADJACENT:
        return out(1, (sim1 - SIM_ADJACENT) / (0.65 - SIM_ADJACENT),
                   f"no full-text match, but the closest publication sits at "
                   f"{sim1:.0%} similarity — possibly covered under different "
                   f"words; read the neighbours below")
    return out(0, (sim1 - SIM_FLOOR) / (SIM_ADJACENT - SIM_FLOOR),
               f"no full-text match, and the closest publication is "
               f"semantically distant ({sim1:.0%} similarity) — within the "
               f"caveats below, this is not covered")


def coverage_view(conn, term, neighbours=COVERAGE_NEIGHBOURS):
    """The coverage view (#55): `coverage_check`'s probes plus the adjacency
    scan and a measured verdict. No LLM anywhere — the `ask` model is
    forbidden from claiming absence, and this view exists to claim it
    honestly, so the verdict is computed from counts, never generated.
    """
    from . import landscape as _landscape
    base = coverage_check(conn, term)
    out = {"term": term, "caveats": base["caveats"], "error": base["error"],
           "notes": [], "about": 0, "mentions": 0, "weighted": 0,
           "hits": [dict(r) for r in base["full_text"]],
           "site_tags": [dict(r) for r in base["site_tags"]],
           "people": [dict(r) for r in base["people"]],
           "neighbours": [], "adjacent_topics": [], "adjacent_people": [],
           "term_xy": None, "verdict": None}
    if base["error"]:
        return out          # a failed search says nothing about coverage

    # Direct evidence in two tiers, deduped to parent level, whole catalog
    # (podcasts count here at weight 1 — a podcast on the term is still
    # coverage; only the visual layers below share the Insights scope).
    safe = db.fts_safe(term)

    def tier(match):
        return conn.execute(
            """SELECT p2.pub_type AS t, COUNT(*) AS n FROM (
                 SELECT DISTINCT COALESCE(p.parent_id, p.id) AS pid
                 FROM publication_fts JOIN publications p
                   ON p.id = publication_fts.rowid
                 WHERE publication_fts MATCH ?
               ) q JOIN publications p2 ON p2.id = q.pid GROUP BY t""",
            (match,)).fetchall()

    direct = tier(safe)
    out["mentions"] = sum(r["n"] for r in direct)
    out["weighted"] = sum(INSIGHT_TYPE_WEIGHTS.get(r["t"], 1) * r["n"]
                          for r in direct)
    out["about"] = sum(r["n"] for r in tier(f"{ABOUT_COLUMNS} : ({safe})"))

    sim1, keyword_only = 0.0, False
    try:
        qv = embed.embed_query(term).astype(numpy.float64)
    except embed.OllamaUnreachable:
        keyword_only = True
        out["notes"].append("ollama is not running, so the adjacency scan is "
                            "missing — this result rests on keywords alone")
        qv = None
    if qv is not None:
        rows = _landscape.scope_rows(conn)
        if not rows:
            # An empty vector index is not evidence of distance (#17's rule).
            keyword_only = True
            out["notes"].append("no stored vectors — the adjacency scan could "
                                "not run (run: pubbrain embed)")
        else:
            sims = _landscape._vectors(rows) @ qv
            order = numpy.argsort(-sims)[:neighbours]
            ids = [rows[int(i)]["publication_id"] for i in order]
            by_sim = {pid: float(sims[int(i)]) for pid, i in zip(ids, order)}
            sim1 = float(sims.max())
            marks = ", ".join("?" * len(ids))
            labels = topics.labels()
            for r in conn.execute(
                f"""SELECT p.id, p.title, p.pub_type, p.date_published,
                           e.summary_one_liner, pt.topic_slug,
                           lc.x, lc.y
                    FROM publications p
                    LEFT JOIN primary_enrichment e ON e.publication_id = p.id
                    LEFT JOIN publication_topics pt
                      ON pt.publication_id = p.id AND pt.position = 1
                    LEFT JOIN landscape_coords lc ON lc.publication_id = p.id
                    WHERE p.id IN ({marks})""", ids):
                out["neighbours"].append({
                    "id": r["id"], "title": r["title"], "type": r["pub_type"],
                    "date": (r["date_published"] or "")[:7],
                    "one_liner": r["summary_one_liner"],
                    "topic": labels.get(r["topic_slug"]),
                    "sim": round(by_sim[r["id"]], 3),
                    "x": r["x"], "y": r["y"],
                })
            out["neighbours"].sort(key=lambda n: -n["sim"])
            topic_counts, people_counts = {}, {}
            for r in conn.execute(
                f"""SELECT pt.topic_slug AS s, COUNT(DISTINCT pt.publication_id) n
                    FROM publication_topics pt
                    WHERE pt.publication_id IN ({marks}) AND pt.position <= 2
                    GROUP BY s ORDER BY n DESC""", ids):
                if labels.get(r["s"]):
                    topic_counts[labels[r["s"]]] = r["n"]
            for r in conn.execute(
                f"""SELECT pe.id, pe.name, pe.is_current, COUNT(*) n
                    FROM publication_people pp
                    JOIN people pe ON pe.id = pp.person_id
                    WHERE pp.publication_id IN ({marks})
                      AND pp.role IN ({CREDIT_SQL}) AND pe.merged_into IS NULL
                    GROUP BY pe.id ORDER BY n DESC, pe.name""", ids):
                people_counts[r["id"]] = {"id": r["id"], "name": r["name"],
                                          "current": bool(r["is_current"]),
                                          "n": r["n"]}
            out["adjacent_topics"] = [{"label": k, "n": v}
                                      for k, v in topic_counts.items()]
            out["adjacent_people"] = list(people_counts.values())
        out["term_xy"] = _landscape.place_term(conn, qv)

    out["verdict"] = _coverage_verdict(out["about"], out["mentions"],
                                       sim1, keyword_only)
    return out


def status_report(conn) -> dict:
    """Counts plus the staleness warnings. The CLI prints this and the MCP
    `status` tool returns it — neither recomputes the numbers itself.

    No derived index maintains itself, so every count here exists to be
    compared against another one.
    """
    row = conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM publications WHERE parent_id IS NULL) AS publications,
               (SELECT COUNT(*) FROM publications
                WHERE parent_id IS NOT NULL)                            AS chapters,
               (SELECT COUNT(*) FROM people
                WHERE merged_into IS NULL)                              AS people,
               (SELECT COUNT(*) FROM site_tags)                         AS site_tags,
               (SELECT COUNT(*) FROM publication_text)                  AS texts,
               (SELECT COALESCE(SUM(word_count), 0) FROM publication_text) AS words,
               (SELECT COUNT(*) FROM publication_fts)                   AS fts,
               (SELECT COUNT(*) FROM primary_enrichment)            AS enriched,
               (SELECT COUNT(DISTINCT model) FROM primary_enrichment) AS models,
               (SELECT COUNT(*) FROM publication_sections)              AS sections,
               (SELECT COUNT(*) FROM publication_text t LEFT JOIN publication_sections s
                       ON s.publication_id = t.publication_id
                WHERE s.id IS NULL)                                     AS no_sections,
               (SELECT COUNT(*) FROM publication_sections s LEFT JOIN embeddings e
                       ON e.source_id = s.id AND e.source_type = 'section'
                WHERE e.id IS NULL AND s.is_boilerplate = 0)            AS no_vector,
               (SELECT COUNT(*) FROM primary_enrichment n LEFT JOIN embeddings e
                       ON e.source_id = n.publication_id
                      AND e.source_type = 'one_liner'
                WHERE e.id IS NULL)                                     AS no_one_liner,
               (SELECT COUNT(*) FROM publication_text t
                LEFT JOIN primary_enrichment e ON e.publication_id = t.publication_id
                WHERE e.publication_id IS NULL
                  AND t.word_count > 0)                                 AS unenriched,
               (SELECT COUNT(DISTINCT publication_id) FROM publication_topics) AS mapped,
               (SELECT COUNT(*) FROM primary_enrichment n LEFT JOIN publication_topics pt
                       ON pt.publication_id = n.publication_id
                WHERE pt.publication_id IS NULL)                        AS no_topics
        """).fetchone()
    report = dict(row)
    report["vectors"] = {r["source_type"]: r["n"] for r in conn.execute(
        "SELECT source_type, COUNT(*) n FROM embeddings GROUP BY source_type")}
    # Parents only, matching the headline count. A chapter inherits its
    # parent's type, so counting both would turn "184 Reports" into
    # reports-plus-their-chapters with nothing saying so (#36).
    report["by_type"] = conn.execute(
        "SELECT pub_type, COUNT(*) n FROM publications WHERE parent_id IS NULL "
        "GROUP BY pub_type ORDER BY n DESC").fetchall()
    report["sitemap"] = conn.execute(
        "SELECT scope, status, COUNT(*) n FROM sitemap_urls GROUP BY scope, status "
        "ORDER BY scope, status").fetchall()
    report["roles"] = conn.execute(
        "SELECT role, COUNT(*) n FROM publication_people GROUP BY role "
        "ORDER BY n DESC").fetchall()
    report["affiliations"] = conn.execute(
        "SELECT affiliation, SUM(is_current) cur, COUNT(*) n FROM people "
        "WHERE merged_into IS NULL "
        "GROUP BY affiliation ORDER BY n DESC").fetchall()

    warnings = []
    # The site can mint a duplicate team node for someone at any time (#47);
    # nothing else would report it — the record looks complete either way.
    split = db.duplicate_people(conn)
    if split:
        names = ", ".join(r["name"] for r in split)
        warnings.append(f"{len(split)} names are carried by more than one "
                        f"person row ({names}) — their credits are split, "
                        f"run merge-person (#47)")
    # Against every row, chapters included: search reaches them on purpose, so
    # comparing the index to the chapter-free headline count would report the
    # index as stale for as long as any report has chapters (#36).
    indexable = row["publications"] + row["chapters"]
    if row["fts"] != indexable:
        warnings.append(f"fts index holds {row['fts']} rows against "
                        f"{indexable} publications and chapters — run index-fts")
    if row["no_sections"]:
        warnings.append(f"{row['no_sections']} publications with body text have "
                        f"no sections — run extract-sections")
    if row["no_vector"]:
        warnings.append(f"{row['no_vector']} sections have no vector — run embed")
    if row["no_one_liner"]:
        # A regenerated summary drops its vector on purpose (#24); nothing else
        # would report the gap, since the section counts still match.
        warnings.append(f"{row['no_one_liner']} summaries have no vector — "
                        f"run embed")
    if row["no_topics"]:
        warnings.append(f"{row['no_topics']} summaries carry no topics — "
                        f"run map-topics")
    if not report["vectors"]:
        warnings.append("no stored vectors — hybrid search degrades to keyword "
                        "only until embed runs")
    report["warnings"] = warnings
    return report


def review_stats(conn):
    """How much of the corpus has been looked at, and with what outcome."""
    row = conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM primary_enrichment) AS enriched,
               SUM(verdict = 'confirmed') AS confirmed,
               SUM(verdict = 'flagged') AS flagged
        FROM reviews WHERE scope = 'enrichment'
        """).fetchone()
    return {"enriched": row["enriched"], "confirmed": row["confirmed"] or 0,
            "flagged": row["flagged"] or 0}


# --- the upcoming layer (#56) ------------------------------------------------
#
# Read side of the hand-entered notes. Nothing above this line reaches these
# tables, and nothing here reaches an LLM or the MCP server: the notes are
# internal knowledge, unlike the published record the rest of this module
# describes.

# Below this, "planned vs actual" is arithmetic on a handful of points and
# would read as a measurement (#56).
UPCOMING_STATS_MIN = 3


def _days_between(iso_a, iso_b):
    """Whole days from an ISO timestamp to a 'YYYY-MM-DD' date, or None."""
    if not iso_a or not iso_b:
        return None
    try:
        a = datetime.date.fromisoformat(iso_a[:10])
        b = datetime.date.fromisoformat(iso_b[:10])
    except ValueError:
        return None
    return (b - a).days


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def upcoming_notes(conn, today=None):
    """Every note with its derived status, topics, people and — once landed —
    what it landed as.

    Status is computed here rather than stored: a status column would be a
    second copy of `landed_publication_id` and `shelved_at`, free to disagree
    with them.
    """
    rows = conn.execute(
        """SELECT n.*, p.title AS pub_title, p.pub_type AS pub_type,
                  p.date_published AS pub_date
           FROM upcoming_notes n
           LEFT JOIN publications p ON p.id = n.landed_publication_id""").fetchall()
    ids = [r["id"] for r in rows]
    by_topic = db.upcoming_note_topics(conn, ids)
    by_person = db.upcoming_note_people(conn, ids)
    labels = topics.labels()
    today = today or db.now()[:10]
    notes = []
    for r in rows:
        note = dict(r)
        note["status"] = ("landed" if r["landed_publication_id"]
                          else "shelved" if r["shelved_at"] else "expected")
        note["topics"] = [{"slug": s, "label": labels.get(s, s)}
                          for s in by_topic.get(r["id"], [])]
        note["people"] = by_person.get(r["id"], [])
        note["age_days"] = _days_between(r["created_at"], today)
        note["actual"] = db.quarter_of(r["pub_date"]) or None
        note["lead_days"] = _days_between(r["created_at"], r["pub_date"])
        # None rather than False where there was nothing to be right about:
        # an undated note cannot miss its quarter.
        note["on_time"] = (None if not (note["expected"] and note["actual"])
                           else note["expected"] == note["actual"])
        notes.append(note)
    # Open first, by the quarter each is expected in — a note with no quarter
    # is not urgent, it is unplaced, so it sorts last rather than first.
    open_notes = sorted((n for n in notes if n["status"] == "expected"),
                        key=lambda n: (n["expected"] is None, n["expected"] or "",
                                       n["created_at"]))
    closed = sorted((n for n in notes if n["status"] != "expected"),
                    key=lambda n: n["landed_at"] or n["shelved_at"] or "",
                    reverse=True)
    return {"open": open_notes, "closed": closed,
            "stats": _upcoming_stats(notes)}


def _upcoming_stats(notes):
    """Planned vs actual, or None while there is nothing to measure.

    Two numbers only, both about the owner's own forecasting: how far ahead a
    piece is known about, and how often the quarter is right. Held back below
    `UPCOMING_STATS_MIN` landings — a hit rate over two notes is a coin toss
    wearing a percentage.
    """
    landed = [n for n in notes if n["status"] == "landed"]
    if len(landed) < UPCOMING_STATS_MIN:
        return None
    # A note written after the piece appeared is a backfill, not a forecast:
    # its negative lead is real but it is not evidence about seeing things
    # coming, and a handful of them drag the median below zero.
    leads = [n["lead_days"] for n in landed
             if n["lead_days"] is not None and n["lead_days"] >= 0]
    judged = [n for n in landed if n["on_time"] is not None]
    return {
        "landed": len(landed),
        "shelved": sum(1 for n in notes if n["status"] == "shelved"),
        "median_lead_days": _median(leads),
        "lead_sample": len(leads),
        "on_time": sum(1 for n in judged if n["on_time"]),
        "judged": len(judged),
    }


def _next_quarter(quarter, n=1):
    year, q = int(quarter[:4]), int(quarter[-1]) - 1 + n
    return f"{year + q // 4}-Q{q % 4 + 1}"


# How far past today the leading edge may reach. A single note filed years out
# would otherwise squash the whole streamgraph into the left third; anything
# beyond is counted and said out loud rather than dropped quietly.
UPCOMING_HORIZON = 8


def upcoming_edge(conn, today=None):
    """Open notes as the Insights views want them: the quarters an axis has to
    grow by, the notes, and a count per topic.

    Its own function, and its own endpoint, precisely because `topic_time` and
    `topic_graph` describe the *published* record and stay identical whether
    or not notes exist. A view that wants the leading edge asks for it by name
    — nothing inherits it (#56).

    Open notes only: a landed note is the published record's business and a
    shelved one never happened.
    """
    today = today or db.now()[:10]
    current = db.quarter_of(today)
    notes = [{"id": n["id"], "working_title": n["working_title"],
              "expected": n["expected"],
              "topics": [t["slug"] for t in n["topics"]],
              "people": [p["name"] for p in n["people"]]}
             for n in upcoming_notes(conn, today=today)["open"]]
    by_topic = {}
    for note in notes:
        for slug in note["topics"]:
            by_topic[slug] = by_topic.get(slug, 0) + 1
    # The axis has to reach the furthest note, or that note is drawn nowhere
    # and the view quietly under-reports what is coming.
    horizon = _next_quarter(current, UPCOMING_HORIZON)
    dated = [n["expected"] for n in notes if n["expected"]]
    end = max([_next_quarter(current)] + [q for q in dated if q <= horizon])
    return {"current_quarter": current, "edge": [current, end],
            "beyond": sum(1 for q in dated if q > horizon),
            "notes": notes, "by_topic": by_topic}
