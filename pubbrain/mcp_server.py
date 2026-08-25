"""MCP server (#23): the catalog as tools for Claude conversations.

A third surface over `queries.py`, beside the CLI and the workbench. No SQL and
no ranking live here — only the shaping a model needs, since it cannot see the
database and cannot check what a number leaves out.

**Transport is chosen at startup**, not baked in: stdio today (Claude Code,
Claude Desktop), streamable HTTP once the Cloudflare Tunnel exists. Nothing
below may assume either.

Two rules make this layer worth having over raw SQL access:

- **An absence claim carries its limits.** `coverage_check` measures them per
  call rather than reciting a paragraph that goes stale.
- **A failed query is never a negative.** FTS5 raises on syntax the caller did
  not know was syntax; that is reported as an error, not as "nothing found".

The `mcp` SDK import is deferred into `create_server` so the tool functions
stay importable — and testable — without it, as `cli.cmd_web` does for Flask.
"""

import sqlite3
from typing import Any

from . import db, embed, paths, queries, remote, topics

NAME = "pub-brain"
HOST = "127.0.0.1"
PORT = 8902
BODY_WORD_CAP = 2000

SCOPE_NOTE = (
    "Enrichment — one-liners, summaries, key findings, entities — exists only "
    "for publications with body text. Podcasts and the paywalled handful are in "
    "the catalog but carry a title and nothing generated, so any answer drawn "
    "from summaries silently omits them."
)


def _conn():
    """A fresh connection per call: SQLite connections are not shareable across
    threads, and streamable HTTP will serve calls on more than one."""
    return db.connect()


def _rows(rows, *keys):
    """sqlite3.Row is not JSON-serialisable; naming the keys also stops a new
    column leaking into every tool response by accident."""
    return [{k: r[k] for k in keys} for r in rows]


def find(query: str, limit: int = 10) -> dict[str, Any]:
    """Search the catalog by meaning and keyword together. Start here.

    Fuses keyword ranking with vector similarity, which beats either alone —
    use it for questions ("what has MERICS said about export controls") and for
    named entities alike. Each hit names the section that matched, so a
    multi-story digest points at its relevant part.

    Degrades to keyword-only, and says so in `notes`, when the local embedding
    model is not running.
    """
    conn = _conn()
    try:
        try:
            hits, notes = queries.hybrid_find(conn, query, limit=limit)
        except embed.OllamaUnreachable:
            hits, notes = queries.hybrid_find(conn, query, limit=limit,
                                              with_vectors=False)
            notes.append("ollama is not running, so meaning-based matching is "
                         "off — these are keyword results only")
        return {
            "query": query,
            "hits": [{
                "id": h["publication"]["id"],
                "title": h["publication"]["title"],
                "pub_type": h["publication"]["pub_type"],
                "date_published": h["publication"]["date_published"],
                "url": h["publication"]["url"],
                "one_liner": h["one_liner"],
                "section": h["section"]["heading"] if h["section"] else None,
                "matched_by": h["rankers"],
            } for h in hits],
            "notes": notes,
            "scope": SCOPE_NOTE,
        }
    finally:
        conn.close()


def search(query: str, limit: int = 10) -> dict[str, Any]:
    """Exact keyword search (FTS5), for when the precise string is known.

    Prefer `find` unless you want literal matching. Accepts FTS5 syntax:
    `"rare earth"` for a phrase, `AND`/`OR`/`NOT`, `chip*` for a prefix.
    Hyphenated terms are quoted automatically — unquoted, FTS5 reads the hyphen
    as a column filter and raises.
    """
    conn = _conn()
    try:
        try:
            rows = db.search(conn, db.fts_safe(query), limit=limit)
        except sqlite3.OperationalError as exc:
            # Not a negative result: the query never ran.
            return {"query": query, "error": f"FTS5 rejected this query: {exc}",
                    "hits": []}
        return {
            "query": query,
            "hits": _rows(rows, "id", "title", "pub_type", "date_published",
                          "url", "snippet"),
            "scope": SCOPE_NOTE,
        }
    finally:
        conn.close()


def publication(publication_id: int) -> dict[str, Any]:
    """Everything held about one publication: metadata, people, the generated
    summary and findings, its sections, and its body text.

    Body text is truncated; `text_truncated` says whether anything was cut.
    """
    conn = _conn()
    try:
        detail = queries.publication_detail(conn, publication_id)
        if not detail:
            return {"error": f"no publication with id {publication_id}"}
        pub, text = detail["publication"], detail["text"]
        words = (text["body"] or "").split() if text else []
        return {
            "id": pub["id"],
            "title": pub["title"],
            "subtitle": pub["subtitle"],
            "pub_type": pub["pub_type"],
            "date_published": pub["date_published"],
            "url": pub["url"],
            "access": pub["access"],
            "people": _rows(detail["people"], "id", "name", "role",
                            "affiliation", "is_current"),
            "site_tags": detail["site_tags"],
            "enrichment": [{
                "one_liner": e["summary_one_liner"],
                "summary": e["summary_short"],
                "key_findings": e["key_findings"],
                "entities": e["entities"],
                "model": e["model"],
            } for e in detail["enrichments"]] or None,
            "sections": _rows(detail["sections"], "heading", "level",
                              "word_count", "is_boilerplate"),
            "body_text": " ".join(words[:BODY_WORD_CAP]) or None,
            "text_truncated": len(words) > BODY_WORD_CAP,
            "flagged": bool(detail["review"]
                            and detail["review"]["verdict"] == "flagged"),
            "note": None if detail["enrichments"] else
                    "No summary exists for this record — it has no body text "
                    "(podcast or paywalled). Its title and metadata are all "
                    "the catalog holds.",
        }
    finally:
        conn.close()


def person(name_or_id: str) -> dict[str, Any]:
    """A person's publications, grouped by role (author, guest).

    Accepts an id or part of a name; an ambiguous name returns the candidates
    rather than guessing. Always read the `caveats` — person credits are
    incomplete in three known ways and no join fixes the third, so this
    under-reports rather than being wrong.

    Podcast hosting is reported as a count (`hosted`), not as publications: a
    recurring host accumulates one link per episode, which describes a role
    rather than a body of work (#27).
    """
    conn = _conn()
    try:
        if str(name_or_id).strip().isdigit():
            person_id = int(name_or_id)
        else:
            matches = queries.people_matching(conn, str(name_or_id).strip())
            if not matches:
                return {"query": name_or_id, "matches": [],
                        "error": f"nobody in the catalog matches {name_or_id!r}"}
            if len(matches) > 1:
                return {"query": name_or_id,
                        "matches": _rows(matches, "id", "name", "affiliation",
                                         "is_current", "n"),
                        "note": "several people match — call again with an id"}
            person_id = matches[0]["id"]

        page = queries.person_page(conn, person_id)
        if not page:
            return {"error": f"no person with id {person_id}"}
        p = page["person"]
        return {
            "id": p["id"],
            "name": p["name"],
            "affiliation": p["affiliation"],
            "is_current": bool(p["is_current"]),
            "roster_title": p["roster_title"],
            "by_role": {role: _rows(rows, "id", "title", "pub_type",
                                    "date_published", "summary_one_liner")
                        for role, rows in page["by_role"].items()},
            "hosted": page["hosted"],
            "caveats": [
                "Credits come from each publication's own page. Records that "
                "credit nobody are not attributed here (#12).",
                "Hosting a podcast is counted separately as `hosted` and is "
                "not among the publications above — it is a recurring role, "
                "not output (#27).",
                "Some publications name a person in their title — an "
                "interviewee, typically — without crediting them, and those "
                "never appear on this list.",
                "Whether someone is currently at MERICS comes from the site's "
                "staff listings, not from their job title.",
            ],
        }
    finally:
        conn.close()


def list_publications(pub_type: str = "", year: str = "", person_id: int = 0,
                      series: str = "", limit: int = 50,
                      offset: int = 0) -> dict[str, Any]:
    """Browse the catalog newest first, optionally filtered by type, year,
    person or series (a recurring MERICS format, e.g. "MERICS China
    Essentials" — exact name). Use `find` to search; use this to enumerate.

    Records without a summary are included on purpose — dropping them would
    quietly hide 16% of the catalog.
    """
    conn = _conn()
    try:
        rows, total, people = queries.list_publications(
            conn, pub_type=pub_type or None, year=year or None,
            person_id=person_id or None, series=series or None,
            limit=limit, offset=offset)
        return {
            "total": total,
            "offset": offset,
            "publications": [{
                **{k: r[k] for k in ("id", "title", "pub_type",
                                     "date_published", "access")},
                "one_liner": r["summary_one_liner"],
                "people": [p["name"] for p in people.get(r["id"], [])],
            } for r in rows],
            "scope": SCOPE_NOTE,
        }
    finally:
        conn.close()


def coverage_check(topic: str) -> dict[str, Any]:
    """Has MERICS covered this? Counted probes and a graded verdict (#55).

    The verdict runs on two tiers — publications *about* the term (title or
    standfirst) vs publications merely *mentioning* it — plus an embedding
    scan for ground covered under different words (works across languages).
    Bands: not covered here / adjacent only / touched / covered / covered
    extensively. `verdict.measured` states exactly what was counted; repeat
    it rather than paraphrasing it stronger.

    **Never report a bare "no".** A low band means "within these `caveats`" —
    they are measured per call and shrink as the catalog is completed. A
    non-null `error` means the query failed and says nothing about coverage.

    Site tags cover a small minority of the catalog and are not a topic
    field — treat an empty tag probe as near-meaningless.
    """
    conn = _conn()
    try:
        r = queries.coverage_view(conn, topic)
        if r["error"]:
            return {"topic": topic, "error": r["error"],
                    "note": "The search itself failed — this is not evidence "
                            "of absence. Retry with a simpler query."}
        found = bool(r["mentions"] or r["site_tags"] or r["people"])
        return {
            "topic": topic,
            "found": found,
            "verdict": r["verdict"],
            "about_matches": r["about"],
            "full_text_matches": r["mentions"],
            "top_hits": _rows(r["hits"], "id", "title", "pub_type",
                              "date_published", "url", "snippet"),
            "nearest_by_meaning": [
                {k: n[k] for k in ("id", "title", "type", "date", "sim")}
                for n in r["neighbours"][:6]],
            "site_tags": _rows(r["site_tags"], "tag", "id", "title"),
            "people_matching_the_term": _rows(r["people"], "id", "name",
                                              "affiliation", "is_current", "n"),
            "notes": r["notes"],
            "caveats": r["caveats"],
            "how_to_report": (
                "Report the counts as a floor, not a total, and carry the "
                "caveats verbatim." if found else
                "Give the verdict band and its measured sentence, then the "
                "caveats. Do not flatten it to 'MERICS has not covered it'."),
        }
    finally:
        conn.close()


def status() -> dict[str, Any]:
    """Catalog counts and whether any derived index is stale.

    `warnings` is the part that matters: no index maintains itself, so a stale
    one serves confident wrong results. If it is non-empty, say so before
    trusting a search.
    """
    conn = _conn()
    try:
        s = queries.status_report(conn)
        return {
            "database": str(paths.DB_PATH),
            "publications": s["publications"],
            "people": s["people"],
            "with_body_text": s["texts"],
            "words_of_body_text": s["words"],
            "with_summaries": s["enriched"],
            "sections": s["sections"],
            "vectors": s["vectors"],
            "fts_rows": s["fts"],
            "by_type": _rows(s["by_type"], "pub_type", "n"),
            "warnings": s["warnings"],
            "scope": SCOPE_NOTE,
        }
    finally:
        conn.close()


def glossary() -> dict[str, Any]:
    """The controlled topic vocabulary: what each topic entails and why it
    matters, grouped into clusters.

    This is the vocabulary the catalog is *meant* to be organised by, at the
    granularity of conference panels. **Publications are not mapped onto it
    yet** (#4), so it cannot be used to filter or count — use it to frame a
    question, then answer it with `find`.
    """
    return {
        "clusters": topics.load(),
        "note": "Not yet mapped onto publications — there is no way to list "
                "the publications for a topic. The site's own tags are a "
                "different, much sparser thing and are not this vocabulary.",
    }


def flag_summary(publication_id: int, note: str) -> dict[str, Any]:
    """Flag a generated summary as misleading, with a note saying why.

    The only tool here that writes. Summaries are trusted by default, so this
    records an exception, not a review; the same row the workbench writes shows
    up on its Flags page. Flagging again replaces the note.
    """
    conn = _conn()
    try:
        # The verdict names the summary row, not the publication (#18).
        subject = db.primary_enrichment_id(conn, publication_id)
        if subject is None:
            return {"error": f"publication {publication_id} has no summary to "
                             f"flag (no body text, or no such record)"}
        db.upsert_review(conn, "enrichment", subject, "flagged",
                         (note or "").strip())
        conn.commit()
        return {"publication_id": publication_id, "flagged": True,
                "note": (note or "").strip()}
    finally:
        conn.close()


TOOLS = [find, search, publication, person, list_publications,
         coverage_check, status, glossary, flag_summary]


def create_server(host=HOST, port=PORT):
    """Register the tools on a FastMCP instance. The SDK import is deferred so
    the rest of the package — and these tools' tests — do not need it."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(NAME, host=host, port=port)
    for tool in TOOLS:
        server.add_tool(tool)
    return server


def _authed_app(server):
    """The streamable-HTTP app behind a bearer check (#48).

    ASGI middleware rather than an SDK feature: the bundled FastMCP has no
    token verifier, and the check has to sit in front of the transport so an
    unauthenticated caller never opens a session. Cloudflare Access and
    connector OAuth are the front door — this is the second lock, so that one
    misconfiguration there is not an open surface.
    """
    from starlette.responses import PlainTextResponse

    app = server.streamable_http_app()
    expected = remote.mcp_token()
    if not expected:
        return app

    async def guard(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if remote.token_ok(remote.bearer_from(headers.get("authorization")), expected):
            return await app(scope, receive, send)
        await PlainTextResponse("not authorised", status_code=401)(scope, receive, send)

    return guard


def run(transport="stdio", host=HOST, port=PORT) -> None:
    """stdio for a local client, streamable-http behind the tunnel (#23).

    Over HTTP the tools are the same, and so is the rule that the upcoming
    layer (#56) is not among them — no tool reads `upcoming_*`, here or
    anywhere, which is what the tripwire tests hold.
    """
    problems = remote.check_config()
    if problems:
        raise SystemExit("; ".join(problems))
    server = create_server(host=host, port=port)
    if transport == "stdio":
        return server.run(transport="stdio")
    import uvicorn

    uvicorn.run(_authed_app(server), host=host, port=port)
