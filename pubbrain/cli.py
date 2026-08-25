"""Two idempotent steps: sync-sitemap, then scrape."""

import argparse
import json
import logging
import sqlite3
import sys
from collections import Counter

import numpy
import requests

from . import (blurb, collect, db, embed, enrich, llm, paths, pdf, queries,
               roster, sections, sitemap, text, topicmap, topics, vision)
from .fetcher import Fetcher, same_page
from .parser import NotAPublication, parse_publication

log = logging.getLogger("pubbrain")

# Status codes that settle the question: the page is not coming back. A 403
# here survived three user-agents and a day (#13), so it is the server's
# answer, not a bot block.
DEFINITIVE_HTTP = {403, 404, 410}


class PageGone(Exception):
    """The URL is in the sitemap but no longer serves its publication."""


def cmd_sync_sitemap(args) -> int:
    fetcher = Fetcher(min_interval=args.delay)
    log.info("fetching %s", sitemap.SITEMAP_URL)
    xml = fetcher.get(sitemap.SITEMAP_URL).text
    counts = Counter()
    with db.connect() as conn:
        for url, lastmod in sitemap.parse(xml):
            scope, prefix = sitemap.classify(url)
            db.upsert_sitemap_url(conn, url, prefix, lastmod, scope)
            counts[scope] += 1
        conn.commit()
        pending = conn.execute(
            "SELECT COUNT(*) FROM sitemap_urls WHERE scope = 'publication' "
            "AND status IN ('pending', 'failed')"
        ).fetchone()[0]
    log.info(
        "sitemap: %d publication, %d root-level, %d excluded — %d pending scrape",
        counts["publication"], counts["root-level"], counts["excluded"], pending,
    )
    return 0


def cmd_scrape(args) -> int:
    fetcher = Fetcher(min_interval=args.delay)
    counts = Counter()
    with db.connect() as conn:
        todo = db.pending_urls(conn, limit=args.limit)
        log.info("%d pages to scrape", len(todo))
        for i, row in enumerate(todo, start=1):
            url, lastmod = row["url"], row["lastmod"]
            slug = sitemap.slug_for(url)
            try:
                html, final_url = fetcher.get_page(url, slug)
                if not same_page(url, final_url):
                    # The sitemap outlives the pages: merics.org still lists
                    # URLs it now redirects to a section landing page (#13).
                    # `gone`, not `skipped` — skipped means "not a publication",
                    # which reads as a parser judgement about a page that exists.
                    raise PageGone(f"redirected to {final_url}")
                record = parse_publication(html, url)
                db.upsert_publication(conn, record, sitemap_lastmod=lastmod)
                db.mark_url(conn, url, "done", lastmod=lastmod)
                counts["ok"] += 1
            except PageGone as exc:
                db.mark_url(conn, url, "gone", lastmod=lastmod, error=str(exc))
                counts["gone"] += 1
                log.warning("gone %s: %s", url, exc)
            except NotAPublication as exc:
                db.mark_url(conn, url, "skipped", lastmod=lastmod, error=str(exc))
                counts["skipped"] += 1
                log.warning("skipped %s: %s", url, exc)
            except requests.HTTPError as exc:
                # 403/404/410 are the server's settled answer, not a wobble. A
                # retryable `failed` would re-fetch them on every run forever.
                code = exc.response.status_code if exc.response is not None else None
                if code in DEFINITIVE_HTTP:
                    db.mark_url(conn, url, "gone", lastmod=lastmod,
                                error=f"HTTP {code}")
                    counts["gone"] += 1
                    log.warning("gone %s: HTTP %s", url, code)
                else:
                    db.mark_url(conn, url, "failed", error=f"HTTPError: {exc}")
                    counts["failed"] += 1
                    log.error("failed %s: %s", url, exc)
            except Exception as exc:  # a bad page must never end the run
                db.mark_url(conn, url, "failed", error=f"{type(exc).__name__}: {exc}")
                counts["failed"] += 1
                log.error("failed %s: %s", url, exc)
            conn.commit()
            if i % 25 == 0:
                log.info("%d/%d (%s)", i, len(todo), dict(counts))
    log.info("scrape done: %s", dict(counts))
    return 1 if counts["failed"] else 0


def cmd_sync_roster(args) -> int:
    """Refresh who is currently at MERICS, then re-classify everyone."""
    fetcher = Fetcher(min_interval=args.delay)
    current = {}
    for source, url in roster.ROSTER_URLS.items():
        current.update(roster.parse_roster(fetcher.get(url).text, source))
    log.info("roster: %d current people", len(current))

    counts = Counter()
    with db.connect() as conn:
        for person in conn.execute(
                "SELECT * FROM people WHERE merged_into IS NULL").fetchall():
            # A merged duplicate's slug may be the one the roster lists (#47);
            # its entry belongs to the surviving row, and tombstones are not
            # classified at all.
            slugs = [person["slug"]] + [r["slug"] for r in conn.execute(
                "SELECT slug FROM people WHERE merged_into = ? "
                "AND slug IS NOT NULL", (person["id"],))]
            entry = next((current[s] for s in slugs if s in current), None)
            affiliation, is_current = roster.classify(
                on_roster=entry is not None,
                roster_title=entry["title"] if entry else None,
                job_title=person["job_title"],
                has_team_page=bool(person["slug"]),
                flagged_external=not person["is_internal"],
            )
            conn.execute(
                "UPDATE people SET affiliation = ?, is_current = ?, roster_title = ?, "
                "roster_source = ? WHERE id = ?",
                (affiliation, 1 if is_current else 0,
                 entry["title"] if entry else None,
                 entry["source"] if entry else None, person["id"]),
            )
            counts[f"{affiliation}/{'current' if is_current else 'former'}"] += 1
        conn.commit()
    for key, n in counts.most_common():
        log.info("%-22s %d", key, n)
    return 0


def cmd_merge_person(args) -> int:
    """Merge a duplicate person row into the surviving one (#47)."""
    with db.connect() as conn:
        try:
            result = db.merge_person(conn, args.duplicate, args.survivor)
        except ValueError as e:
            log.error("%s", e)
            return 1
        conn.commit()
    log.info("merged %r (%d) into %r (%d): %d credits moved, %d already there",
             result["duplicate"], args.duplicate,
             result["survivor"], args.survivor,
             result["moved"], result["already_there"])
    return 0


def cmd_reparse(args) -> int:
    """Re-run the page parser over the cached HTML. Local only — no crawling."""
    counts = Counter()
    with db.connect() as conn:
        rows = conn.execute("SELECT id, slug, url FROM publications ORDER BY id").fetchall()
        for row in rows:
            path = paths.RAW_DIR / f"{row['slug']}.html"
            if not path.exists():
                counts["no-cache"] += 1
                continue
            try:
                record = parse_publication(path.read_text(encoding="utf-8"), row["url"])
            except NotAPublication as exc:
                counts["unparseable"] += 1
                log.warning("skipped %s: %s", row["slug"], exc)
                continue
            db.upsert_publication(conn, record)
            counts["ok"] += 1
        conn.commit()
        for r in conn.execute(
            "SELECT role, COUNT(*) n FROM publication_people GROUP BY role ORDER BY n DESC"
        ):
            log.info("role %-8s %d links", r["role"], r["n"])
    log.info("reparse: %s", dict(counts))
    return 0


def cmd_extract_text(args) -> int:
    """Re-parse the cached HTML for body text. Local only — no crawling."""
    counts = Counter()
    with db.connect() as conn:
        sql = "SELECT id, slug, pub_type FROM publications"
        params = []
        if args.only:
            sql += f" WHERE id IN ({', '.join('?' * len(args.only))})"
            params = args.only
        rows = conn.execute(sql + " ORDER BY id", params).fetchall()
        for row in rows:
            if db.is_authored(conn, row["id"]):
                # Hand-entered text is the only copy (#31); the cache cannot
                # regenerate it, so re-extraction must never reach it. Checked
                # before the cache lookup, so it holds even for records that
                # have no cached page at all — which is most of them.
                counts["manual-kept"] += 1
                continue
            path = paths.RAW_DIR / f"{row['slug']}.html"
            if not path.exists():
                counts["no-cache"] += 1
                continue
            try:
                result = text.extract(path.read_text(encoding="utf-8"))
            except text.NoBodyText:
                counts[f"no-text: {row['pub_type']}"] += 1
                continue
            # The landing page of a PDF-backed report is a 37-word abstract
            # standing in for 3,747 words (#6). This step runs after every
            # re-scrape, so without the guard the documented pipeline order
            # silently undoes the whole PDF import.
            stored = conn.execute(
                "SELECT word_count, source FROM publication_text "
                "WHERE publication_id = ?", (row["id"],)).fetchone()
            if (stored and stored["source"] == "pdf" and not args.force
                    and result["word_count"] <= stored["word_count"]):
                counts["pdf-kept"] += 1
                continue
            db.upsert_text(conn, row["id"], result["text"], result["word_count"])
            counts["ok"] += 1
        conn.commit()
        total = conn.execute("SELECT SUM(word_count) FROM publication_text").fetchone()[0]
    for key, n in counts.most_common():
        log.info("%-28s %d", key, n)
    log.info("total body words: %s", f"{total or 0:,}")
    return 0


def cmd_index_fts(args) -> int:
    """Rebuild the keyword index from the catalog. Local only — no crawling."""
    with db.connect() as conn:
        indexed = db.rebuild_fts(conn)
        conn.commit()
        with_body = conn.execute("SELECT COUNT(*) FROM publication_text").fetchone()[0]
    log.info("indexed %d publications (%d with body text)", indexed, with_body)
    return 0


def cmd_extract_sections(args) -> int:
    """Split body text at its headings. Local only — no crawling, no model."""
    counts = Counter()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT p.id, p.title, p.pub_type, t.body FROM publications p "
            "JOIN publication_text t ON t.publication_id = p.id ORDER BY p.id"
        ).fetchall()
        for row in rows:
            found = sections.split(row["body"])
            if not found:
                counts["no-sections"] += 1
                continue
            independent = sections.has_independent_topics(
                row["pub_type"], row["title"], found)
            for s in found:
                s["is_boilerplate"] = sections.is_boilerplate(s["heading"])
                s["independent"] = independent and not s["is_boilerplate"]
                counts["boilerplate"] += s["is_boilerplate"]
            db.replace_sections(conn, row["id"], found)
            counts["ok"] += 1
            counts[f"sections: {row['pub_type']}"] += len(found)
            if independent:
                counts["with independent topics"] += 1
        conn.commit()
        total = db.rebuild_section_fts(conn)
        orphaned = db.purge_orphan_embeddings(conn)
        conn.commit()
    if orphaned:
        log.info("dropped %d vectors for sections that no longer exist "
                 "— re-run embed", orphaned)
    for key, n in sorted(counts.items()):
        log.info("%-34s %d", key, n)
    log.info("indexed %d sections", total)
    return 0


def cmd_search(args) -> int:
    """A smoke test over the index, not a UI — the real interface is SQL."""
    with db.connect() as conn:
        try:
            rows = db.search(conn, args.query, limit=args.limit)
            # Which part of a digest matched. Section text is a subset of the
            # body, so this never adds or removes a publication.
            where = db.matching_sections(conn, args.query, [r["id"] for r in rows])
        except sqlite3.OperationalError as exc:
            log.error("bad FTS query: %s", exc)
            if "-" in args.query:
                # The tokenizer splits on the hyphen and the parser then reads
                # it as a column filter — 'de-risking' is the common casualty.
                log.error('hyphens need quoting: try \'"%s"\'', args.query.replace("-", " "))
            return 2
    if not rows:
        print("no matches")
        return 0
    for r in rows:
        print(f"{r['date_published']}  {r['pub_type']:<21} {r['title']}")
        print(f"  {r['url']}")
        section = where.get(r["id"])
        if section and section["heading"]:
            print(f"  § {section['heading']}")
            print(f"  {' '.join((section['snippet'] or '').split())}")
        elif r["snippet"]:
            print(f"  {' '.join(r['snippet'].split())}")
        print()
    return 0


def _quota_stop(exc, args) -> bool:
    """True when a run should end here rather than wait out a quota error (#45).

    The backoff is right for the unattended weekly job and wrong when someone
    is watching a nearly-empty window: each retry is another request, so it
    eats the budget as it recovers. Every pass is resumable, so stopping costs
    the current record and nothing else.
    """
    if not getattr(args, "no_wait", False):
        return False
    code = exc.response.status_code if exc.response is not None else None
    if code not in llm.QUOTA_STATUS:
        return False
    log.error("HTTP %s — out of quota, stopping. Re-run to continue.", code)
    return True


def cmd_enrich(args) -> int:
    """Generate summaries for publications that have none. Resumable: the
    worklist is derived from what is missing, so re-running continues."""
    provider = args.provider
    model = args.model or llm.PROVIDERS[provider]["default_model"]
    match = enrich.SENSITIVE_QUERY if args.sensitive else None
    counts, consecutive_errors = Counter(), 0

    with db.connect() as conn:
        todo = db.pending_enrichment(conn, limit=args.limit, match=match)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM publication_text t LEFT JOIN primary_enrichment e "
            "ON e.publication_id = t.publication_id WHERE e.publication_id IS NULL"
        ).fetchone()[0]
        log.info("%d to enrich now, %d un-enriched in total | %s via %s%s",
                 len(todo), remaining, model, provider,
                 " | sensitive slice first" if match else "")
        if args.dry_run:
            for rec in todo:
                print(f"{rec['id']:>5}  {rec['word_count']:>6,}w  {rec['title']}")
            return 0

        for i, rec in enumerate(todo, start=1):
            try:
                data, meta = enrich.enrich_one(
                    rec, model, provider, max_tokens=args.max_tokens,
                    attempts=args.attempts, reasoning_effort=args.reasoning_effort,
                    network_retries=0 if args.no_wait else None,
                )
            except requests.HTTPError as exc:
                if _quota_stop(exc, args):
                    break
                raise
            except enrich.Invalid as exc:
                # The model answered but never usably. Leaving no row means a
                # later run retries it; it is not silently marked done.
                counts["invalid"] += 1
                consecutive_errors = 0
                log.error("id=%s unusable after %d attempts: %s",
                          rec["id"], args.attempts, exc)
                continue
            except Exception as exc:
                counts["error"] += 1
                consecutive_errors += 1
                log.error("id=%s %s: %s", rec["id"], type(exc).__name__, str(exc)[:200])
                if consecutive_errors >= 3:
                    log.error("3 failures in a row — stopping. Re-run to resume.")
                    break
                continue

            consecutive_errors = 0
            db.upsert_primary_enrichment(conn, rec["id"], data, meta)
            conn.commit()          # per row: an interrupted run keeps its work
            counts["ok"] += 1
            counts["retried"] += meta["attempts"] > 1
            words = len(data["summary_one_liner"].split())
            log.info("%d/%d id=%s %s %2dw  %.0fs  %s",
                     i, len(todo), rec["id"],
                     "ok " if meta["attempts"] == 1 else f"x{meta['attempts']}",
                     words, meta["seconds"], rec["title"][:60])

        totals = conn.execute(
            "SELECT COUNT(*) n, SUM(prompt_tokens) pt, SUM(completion_tokens) ct, "
            "SUM(seconds) s FROM primary_enrichment"
        ).fetchone()
    log.info("enrich: %s", dict(counts))
    log.info("catalog now: %d enriched, %s prompt + %s completion tokens, %.0f min",
             totals["n"], f"{totals['pt'] or 0:,}", f"{totals['ct'] or 0:,}",
             (totals["s"] or 0) / 60)
    return 1 if counts["error"] or counts["invalid"] else 0


def cmd_exec_summary(args) -> int:
    """Copy each report's own executive summary into its summary field (#18).

    No model, no network. Where the authors wrote a summary, rewriting it adds
    a step and a risk and nothing else.
    """
    counts = Counter()
    with db.connect() as conn:
        todo = enrich.pending_executive_summary(conn)
        if args.only:
            todo = [p for p in todo if p in set(args.only)]
        log.info("%d record(s) hold an executive summary that is not the summary",
                 len(todo))
        for pid in todo:
            rec = conn.execute("SELECT title FROM publications WHERE id = ?",
                               (pid,)).fetchone()
            if args.dry_run:
                print(f"{pid:>5}  {rec['title'][:60]}")
                continue
            if enrich.copy_executive_summary(conn, pid) is None:
                # A heading is not proof of a section: the 56,033-word security
                # report has 13 words under "Executive Summary" (#18).
                counts["too short to use"] += 1
                continue
            conn.commit()
            counts["copied"] += 1
            log.info("%s  %s", pid, rec["title"][:60])
    log.info("exec-summary: %s", dict(counts))
    if counts["copied"]:
        log.info("the one-liner still comes from the old summary — it is capped "
                 "at 30 words and cannot hold one; run index-fts and embed")
    return 0


def cmd_blurb(args) -> int:
    """Plain-language blurbs from the existing summaries (#38). Resumable.

    Paced by `--limit` rather than run in one go (owner: "just do a year per
    day or something") — nothing here needs the whole corpus at once, and a
    small run can be read before the next one is started.
    """
    counts = Counter()
    with db.connect() as conn:
        todo = blurb.pending(conn, only=args.only, limit=args.limit,
                             stale=args.stale, year=args.year)
        log.info("%d to write | %s via %s", len(todo), args.model, args.provider)
        for i, rec in enumerate(todo, start=1):
            if args.dry_run:
                print(f"{rec['id']:>5}  {rec['title'][:56]}")
                continue
            try:
                text_out, meta = blurb.blurb_one(
                    rec, model=args.model, provider=args.provider,
                    attempts=args.attempts,
                    network_retries=0 if args.no_wait else None)
            except requests.HTTPError as exc:
                if _quota_stop(exc, args):
                    log.error("%d written, %d still to do", counts["ok"],
                              len(todo) - i + 1)
                    break
                raise
            except enrich.Invalid as exc:
                counts["invalid"] += 1
                log.error("id=%s unusable: %s", rec["id"], exc)
                continue
            except Exception as exc:               # noqa: BLE001 — one bad record
                counts["error"] += 1
                log.error("id=%s %s: %s", rec["id"], type(exc).__name__,
                          str(exc)[:200])
                continue
            blurb.save(conn, rec["id"], text_out, rec["enrichment_id"], meta)
            conn.commit()          # per row: an interrupted run keeps its work
            counts["ok"] += 1
            log.info("%d/%d id=%s %dw %.0fs  %s", i, len(todo), rec["id"],
                     len(text_out.split()), meta["seconds"], rec["title"][:44])
    log.info("blurb: %s", dict(counts))
    if counts["ok"]:
        log.info("read a few before running the rest — a blurb that only "
                 "shortens the one-liner is a failure this cannot measure")
    return 1 if counts["error"] or counts["invalid"] else 0


def cmd_vision(args) -> int:
    """Summarize publications whose content is a picture (#35). Resumable."""
    fetcher = Fetcher(min_interval=args.delay)
    counts = Counter()
    with db.connect() as conn:
        todo = vision.pending(conn, limit=args.limit)
        if args.only:
            todo = [r for r in todo if r["id"] in set(args.only)] or conn.execute(
                f"""SELECT id, slug, title, subtitle, pub_type, series,
                           date_published, pdf_url, pdf_path FROM publications
                    WHERE id IN ({", ".join("?" * len(args.only))})""",
                args.only).fetchall()
        log.info("%d to describe | %s via %s", len(todo), args.model, args.provider)
        for i, rec in enumerate(todo, start=1):
            try:
                images, note = vision.images_for(conn, rec, fetcher)
            except Exception as exc:               # noqa: BLE001 — one bad record
                counts["no images"] += 1
                log.error("id=%s could not gather images: %s", rec["id"], exc)
                continue
            if args.dry_run:
                print(f"{rec['id']:>5}  {note:<22} {rec['title'][:56]}")
                continue
            if not images:
                counts["no images"] += 1
                log.warning("id=%s %s — nothing to look at", rec["id"], note)
                continue
            try:
                data, meta = vision.describe(
                    rec, images, model=args.model, provider=args.provider,
                    attempts=args.attempts,
                    network_retries=0 if args.no_wait else vision.NETWORK_RETRIES)
            except requests.HTTPError as exc:
                if _quota_stop(exc, args):
                    break
                raise
            except enrich.Invalid as exc:
                counts["invalid"] += 1
                log.error("id=%s unusable: %s", rec["id"], exc)
                continue
            except Exception as exc:               # noqa: BLE001
                counts["error"] += 1
                log.error("id=%s %s: %s", rec["id"], type(exc).__name__, str(exc)[:200])
                continue
            # A record with no summary gets one; a record that already has one
            # gets a *candidate* beside it (#18). The Data Insights are the
            # case: their summary was written from a one-line caption and is
            # thin rather than absent, so overwriting it would destroy the only
            # reading there is if the vision pass turns out worse.
            existing = conn.execute(
                "SELECT 1 FROM primary_enrichment WHERE publication_id = ?",
                (rec["id"],)).fetchone()
            if existing:
                db.add_enrichment(conn, rec["id"], data, meta)
                counts["candidate"] += 1
            else:
                db.upsert_primary_enrichment(conn, rec["id"], data, meta)
                counts["ok"] += 1
            conn.commit()          # per row: an interrupted run keeps its work
            # `note` says what was gathered; `meta` says what was actually
            # read, and the two differ when a multi-image request timed out and
            # fell back to the leading image. Reporting the first alone would
            # claim a fuller reading than the row holds.
            log.info("%d/%d id=%s %s%s%s  %.0fs  %s", i, len(todo), rec["id"],
                     note,
                     f" -> {meta['images_sent']} after timeout"
                     if meta.get("degraded") else "",
                     " (candidate)" if existing else "",
                     meta["seconds"], rec["title"][:44])
    log.info("vision: %s", dict(counts))
    if counts["candidate"]:
        log.info("candidates written beside existing summaries — promote or dismiss "
                 "them on the record page (#18)")
    if counts["ok"] or counts["candidate"]:
        log.info("now run: index-fts, embed, map-topics")
    return 1 if counts["error"] or counts["invalid"] else 0


def cmd_enrichment(args) -> int:
    """Print stored enrichment for reading — the review step before a full run."""
    with db.connect() as conn:
        sql = """
            SELECT p.id, p.title, p.url, p.pub_type, p.date_published,
                   e.summary_one_liner, e.summary_short, e.key_findings,
                   e.entities, e.model, e.attempts, t.body
            FROM primary_enrichment e
            JOIN publications p     ON p.id = e.publication_id
            JOIN publication_text t ON t.publication_id = p.id
        """
        params = []
        if args.sensitive:
            sql += (" WHERE p.id IN (SELECT rowid FROM publication_fts "
                    "WHERE publication_fts MATCH ?)")
            params.append(enrich.SENSITIVE_QUERY)
        sql += " ORDER BY p.date_published DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()

    for r in rows:
        entities = json.loads(r["entities"])
        ground = enrich.grounding(entities, r["body"])
        print(f"\n{'-' * 78}\n{r['date_published']}  {r['pub_type']}  (id {r['id']})")
        print(f"{r['title']}\n{r['url']}")
        print(f"\n  ONE-LINER ({len(r['summary_one_liner'].split())}w): "
              f"{r['summary_one_liner']}")
        if args.full:
            print(f"\n  {r['summary_short']}")
            for f in json.loads(r["key_findings"]):
                print(f"   - {f}")
            named = ", ".join(n for v in entities.values() for n in v)
            print(f"\n  entities ({ground} grounded): {named}")
    print(f"\n{len(rows)} shown")
    return 0


def cmd_map_topics(args) -> int:
    """Map publications onto the frozen topic vocabulary (#4). Resumable: the
    worklist is what has a summary and no topics, so re-running continues."""
    provider = args.provider
    model = args.model or llm.PROVIDERS[provider]["default_model"]
    labels = topics.labels()
    counts, consecutive_errors = Counter(), 0

    with db.connect() as conn:
        todo = db.pending_topic_mapping(
            conn, limit=args.limit, remap=args.remap,
            match=enrich.SENSITIVE_QUERY if args.sensitive else None)
        mapped = conn.execute(
            "SELECT COUNT(DISTINCT publication_id) FROM publication_topics"
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM primary_enrichment").fetchone()[0]
        log.info("%d to map now, %d of %d summaries already mapped | %s via %s%s",
                 len(todo), mapped, total, model, provider,
                 " | remapping" if args.remap else "")
        if args.dry_run:
            for rec in todo:
                print(f"{rec['id']:>5}  {rec['pub_type']:<22} {rec['title'][:70]}")
            return 0

        for i, rec in enumerate(todo, start=1):
            try:
                slugs, meta = topicmap.map_one(
                    rec, model, provider, max_tokens=args.max_tokens,
                    attempts=args.attempts, reasoning_effort=args.reasoning_effort,
                    network_retries=0 if args.no_wait else None,
                )
            except requests.HTTPError as exc:
                if _quota_stop(exc, args):
                    break
                raise
            except topicmap.Invalid as exc:
                # No row means a later run retries it — never silently done.
                counts["invalid"] += 1
                consecutive_errors = 0
                log.error("id=%s unusable after %d attempts: %s",
                          rec["id"], args.attempts, exc)
                continue
            except Exception as exc:
                counts["error"] += 1
                consecutive_errors += 1
                log.error("id=%s %s: %s", rec["id"], type(exc).__name__, str(exc)[:200])
                if consecutive_errors >= 3:
                    log.error("3 failures in a row — stopping. Re-run to resume.")
                    break
                continue

            consecutive_errors = 0
            db.replace_topics(conn, rec["id"], slugs, meta)
            conn.commit()          # per row: an interrupted run keeps its work
            counts["ok"] += 1
            counts["retried"] += meta["attempts"] > 1
            counts[f"n{len(slugs)}"] += 1
            log.info("%d/%d id=%s %s  %s  | %s", i, len(todo), rec["id"],
                     "ok " if meta["attempts"] == 1 else f"x{meta['attempts']}",
                     ", ".join(labels.get(s, s) for s in slugs), rec["title"][:50])

    log.info("map-topics: %s", dict(counts))
    return 1 if counts["error"] or counts["invalid"] else 0


def cmd_topics(args) -> int:
    """Print the vocabulary with its publication counts — the review surface
    for a mapping run, and the fastest way to spot a topic nobody landed in."""
    with db.connect() as conn:
        counts = db.topic_counts(conn)
        primary = db.topic_counts(conn, primary_only=True)
        mapped = conn.execute(
            "SELECT COUNT(DISTINCT publication_id) FROM publication_topics"
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM primary_enrichment").fetchone()[0]
        samples = {}
        if args.samples:
            for slug in topics.slugs():
                samples[slug] = [r["title"] for r in conn.execute(
                    "SELECT p.title FROM publication_topics pt "
                    "JOIN publications p ON p.id = pt.publication_id "
                    "WHERE pt.topic_slug = ? AND pt.position = 1 "
                    "ORDER BY p.date_published DESC LIMIT ?",
                    (slug, args.samples)).fetchall()]

    print(f"{mapped} of {total} summarized publications mapped\n")
    cluster = None
    for t in topics.flat():
        if t["cluster"] != cluster:
            cluster = t["cluster"]
            print(f"\n{cluster}")
        n, p = counts.get(t["slug"], 0), primary.get(t["slug"], 0)
        flag = "  <- empty" if not n else ""
        print(f"  {n:>4} ({p:>4} primary)  {t['name']}{flag}")
        for title in samples.get(t["slug"], []):
            print(f"                      · {title[:66]}")
    return 0


def cmd_todo(args) -> int:
    """What the owner handed over from the Backlog, with his instructions."""
    with db.connect() as conn:
        rows = db.backlog_todo(conn)
        urls = conn.execute(
            "SELECT url, note, probe_title FROM sitemap_urls "
            "WHERE disposition = 'todo' ORDER BY url").fetchall()
        ingest = conn.execute(
            "SELECT COUNT(*) FROM sitemap_urls WHERE scope = 'root-level' "
            "AND status = 'pending' AND disposition = 'ingest'").fetchone()[0]
    for r in rows:
        print(f"\n  pub {r['id']}  {r['title'][:58]}\n      {r['url']}"
              f"\n      -> {r['note'] or '(no note)'}")
    for r in urls:
        print(f"\n  url  {(r['probe_title'] or '')[:58]}\n      {r['url']}"
              f"\n      -> {r['note'] or '(no note)'}")
    print(f"\n{len(rows) + len(urls)} handed over · "
          f"{ingest} root URLs marked for ingest")
    return 0


def _ingest_one(conn, fetcher, url, pub_type, series=None, note=None):
    """Bring one root-level URL in. Returns (publication_id, record, words)."""
    slug = sitemap.slug_for(url)
    html, final_url = fetcher.get_page(url, slug)
    if not same_page(url, final_url):
        raise NotAPublication(f"redirects to {final_url}")
    record = parse_publication(html, url, pub_type=pub_type, series=series)
    pub_id = db.upsert_publication(conn, record)
    result = text.extract(html)
    db.upsert_text(conn, pub_id, result["text"], result["word_count"])
    found = sections.split(result["text"])
    independent = sections.has_independent_topics(
        record["pub_type"], record["title"], found)
    for sec in found:
        sec["is_boilerplate"] = sections.is_boilerplate(sec["heading"])
        sec["independent"] = independent and not sec["is_boilerplate"]
    db.replace_sections(conn, pub_id, found)
    db.reindex_one(conn, pub_id)
    db.mark_url(conn, url, "done")          # a publication now, not homeless
    db.set_url_disposition(conn, url, "ingest", note)
    return pub_id, record, result["word_count"]


def cmd_ingest_url(args) -> int:
    """Bring one root-level URL into the catalog with a type given by hand (#10).

    These pages state no publication type, and the standing rule is that type
    is read from the page and never inferred. So this exists as a deliberate,
    one-at-a-time act with the type named on the command line.
    """
    fetcher = Fetcher(min_interval=args.delay)
    with db.connect() as conn:
        try:
            pub_id, record, words = _ingest_one(
                conn, fetcher, args.url, args.type, args.series, args.note)
        except NotAPublication as exc:
            log.error("%s", exc)
            return 1
        db.rebuild_section_fts(conn)
        conn.commit()
    log.info("ingested %s as %s%s — %d words", pub_id, record["pub_type"],
             f" / {record['series']}" if record.get("series") else "", words)
    log.info("still to do: enrich, embed, map-topics")
    return 0


def _resolve_publication(conn, ref):
    """A publication id, a URL or a slug — whichever the owner has to hand."""
    if str(ref).isdigit():
        row = conn.execute("SELECT id FROM publications WHERE id = ?",
                           (int(ref),)).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM publications WHERE url = ? OR slug = ?",
            (ref, sitemap.slug_for(ref))).fetchone()
    if row is None:
        raise NotAPublication(f"no publication matches {ref!r}")
    return row["id"]


def cmd_add_pdf_record(args) -> int:
    """Create a record for a publication that exists only as a PDF (#36).

    The MERICS China Economic Indicators quarterlies are the motivating case:
    the volume is a real publication with its own articles, but merics.org
    gives it no page of its own — the landing page is a standing dashboard
    listing several quarters at once, so it cannot be any one of them. The PDF
    URL is the identity, and it is unique per quarter.

    Deliberately not a scrape: there is no page to parse. Everything the
    catalog would normally read off a page is given on the command line.
    """
    with db.connect() as conn:
        pub_id = db.upsert_publication(conn, {
            "slug": sitemap.slug_for(args.url).removesuffix(".pdf"),
            "url": args.url,
            "title": args.title,
            "subtitle": args.subtitle,
            "date_published": args.date,
            "pub_type": args.type,
            "series": args.series,
            "access": "public",
            "pdf_url": args.url,
            "og_description": None,
            "people": [],
            "site_tags": [],
        })
        db.reindex_one(conn, pub_id)
        conn.commit()
    log.info("record %s — %s", pub_id, args.title)
    log.info("no body text: download-pdfs then extract-pdf-text, or enter it by hand")
    return 0


def cmd_chapters(args) -> int:
    """Attach chapters to a report (#36), ingesting the URL first if needed.

    A chapter published at its own URL is a publication in every respect —
    text, summary, vectors — it simply is not listed beside its parent. So this
    reuses the ordinary ingest and then sets `parent_id`; it is not a separate
    kind of record.
    """
    fetcher = Fetcher(min_interval=args.delay)
    with db.connect() as conn:
        try:
            parent = _resolve_publication(conn, args.parent)
        except NotAPublication as exc:
            log.error("%s", exc)
            return 1
        if args.list:
            for c in db.chapters_of(conn, parent):
                print(f"  {c['parent_position']:>3}. {c['id']:>5} "
                      f"{c['word_count']:>7}w  {c['title'][:64]}")
            return 0
        counts = Counter()
        for position, ref in enumerate(args.chapters, start=args.start):
            try:
                child = _resolve_publication(conn, ref)
                counts["already in the catalog"] += 1
            except NotAPublication:
                if not str(ref).startswith("http"):
                    log.error("not in the catalog and not a URL: %s", ref)
                    counts["failed"] += 1
                    continue
                try:
                    child, record, words = _ingest_one(
                        conn, fetcher, ref, args.type or _parent_type(conn, parent),
                        note=f"chapter of {parent}")
                except NotAPublication as exc:
                    log.error("%s — %s", ref, exc)
                    counts["failed"] += 1
                    continue
                log.info("ingested %s — %d words", record["title"][:56], words)
                counts["ingested"] += 1
            try:
                db.attach_chapter(conn, child, parent, position)
            except ValueError as exc:
                log.error("%s", exc)
                counts["failed"] += 1
                continue
            counts["attached"] += 1
        db.rebuild_section_fts(conn)
        conn.commit()
    log.info("chapters: %s", dict(counts))
    log.info("still to do: enrich, embed, map-topics")
    return 0


def _parent_type(conn, parent_id) -> str:
    """A chapter inherits its parent's type (#36). The listing filter does the
    work of hiding it, so inventing a `Chapter` type would only add a value
    every type filter then has to know about."""
    return conn.execute("SELECT pub_type FROM publications WHERE id = ?",
                        (parent_id,)).fetchone()["pub_type"]


def cmd_ingest_proposed(args) -> int:
    """Ingest every root-level URL whose subtitle named its own series (#10).

    Not a guess: these pages say "China Update 1/2019" in their subtitle, which
    is the run that became MERICS China Essentials. The type comes from the
    page's own words, so the rule against inferring type is intact.
    """
    fetcher = Fetcher(min_interval=args.delay)
    counts = Counter()
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT url, proposed_type, proposed_series FROM sitemap_urls
               WHERE scope = 'root-level' AND status = 'pending'
                 AND proposed_type IS NOT NULL ORDER BY url""").fetchall()
        log.info("%d URLs name their own series", len(rows))
        if args.dry_run:
            for r in rows:
                print(f"  {r['proposed_type']:<14} {r['proposed_series']:<26} "
                      f"{r['url'].replace('https://merics.org/en/', '')[:52]}")
            return 0
        for i, r in enumerate(rows, start=1):
            try:
                pub_id, record, words = _ingest_one(
                    conn, fetcher, r["url"], r["proposed_type"],
                    r["proposed_series"], "series named in its own subtitle")
            except Exception as exc:
                counts["failed"] += 1
                log.error("%s: %s", r["url"][-48:], str(exc)[:90])
                continue
            conn.commit()
            counts["ok"] += 1
            log.info("%d/%d %s %5dw  %s", i, len(rows), record["date_published"],
                     words, record["title"][:52])
        db.rebuild_section_fts(conn)
        conn.commit()
    log.info("ingest-proposed: %s", dict(counts))
    log.info("still to do: enrich, embed, map-topics")
    return 1 if counts["failed"] else 0


def cmd_classify_root(args) -> int:
    """Probe the root-level URLs and propose a disposition for each (#10).

    Uses the parser's own accessors: teasers reuse the `field-name-field-*`
    classes, and a plain descendant selector reported 57 pages carrying a
    publication-type that in fact only had one in a promo block.
    """
    from bs4 import BeautifulSoup
    from .parser import _fields, _main_article, _text
    fetcher = Fetcher(min_interval=args.delay)
    counts = Counter()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT url FROM sitemap_urls WHERE scope = 'root-level' "
            "AND status = 'pending' AND (probe_words IS NULL OR ?) ORDER BY url",
            (1 if args.refresh else 0,)).fetchall()
        log.info("%d root-level URLs to probe", len(rows))
        for i, row in enumerate(rows, start=1):
            try:
                resp = fetcher.get(row["url"])
            except Exception as exc:
                counts["failed"] += 1
                log.warning("%s: %s", row["url"][-44:], str(exc)[:60])
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            art = _main_article(soup)
            has_date = bool(art and _fields(art, "date-published"))
            content = _fields(art, "content") if art else []
            words = len(content[0].get_text(" ", strip=True).split()) if content else 0
            title = _text(soup.find("h1"))
            sub_fields = _fields(art, "subtitle") if art else []
            subtitle = _text(sub_fields[0]) if sub_fields else None
            db.set_url_probe(conn, row["url"], words, has_date, title, subtitle)
            conn.commit()
            named = db.series_from_subtitle(subtitle)
            counts[f"{named[1]}" if named
                   else db.propose_disposition(has_date, words, title)] += 1
            if i % 25 == 0:
                log.info("%d/%d %s", i, len(rows), dict(counts))
    log.info("classify-root: %s", dict(counts))
    return 0


def cmd_download_pdfs(args) -> int:
    """Fetch the PDFs whose URLs the catalog already holds (#6). Newest first;
    resumable on `pdf_path IS NULL`. No API budget — this is a crawl."""
    fetcher = Fetcher(min_interval=args.delay)
    counts = Counter()
    with db.connect() as conn:
        todo = pdf.pending_downloads(conn, limit=args.limit,
                                     newest_first=not args.oldest_first,
                                     needed_only=args.needed, only=args.only)
        log.info("%d PDFs to download", len(todo))
        for i, row in enumerate(todo, start=1):
            try:
                size = pdf.download(fetcher, row)
            except Exception as exc:
                counts["failed"] += 1
                log.error("%s: %s", row["slug"], str(exc)[:120])
                continue
            conn.execute("UPDATE publications SET pdf_path = ? WHERE id = ?",
                         (str(pdf.pdf_path_for(row["slug"])), row["id"]))
            conn.commit()
            counts["ok"] += 1
            log.info("%d/%d %s %6.1fkB  %s", i, len(todo), row["date_published"],
                     size / 1024, row["title"][:52])
    log.info("download-pdfs: %s", dict(counts))
    return 1 if counts["failed"] else 0


def cmd_extract_pdf_text(args) -> int:
    """Extract downloaded PDFs into publication_text. Local, no network."""
    # Both overrides are per-record judgements made after looking at a specific
    # PDF. Refused before a database is opened, so no run can reach the loop
    # with a guard disabled for everything — that is how the ETNC chapters got
    # the whole volume (#42).
    if (args.allow_shared or args.allow_outsized) and not args.only:
        log.error("--allow-shared and --allow-outsized require --only")
        return 1
    counts = Counter()
    with db.connect() as conn:
        sql = ("SELECT id, slug, title, pdf_path FROM publications "
               "WHERE pdf_path IS NOT NULL")
        params = []
        if args.only:
            sql += f" AND id IN ({', '.join('?' * len(args.only))})"
            params = args.only
        rows = conn.execute(sql + " ORDER BY date_published DESC", params).fetchall()
        for row in rows:
            try:
                out = pdf.import_text(conn, row["id"], row["slug"],
                                      allow_outsized=args.allow_outsized,
                                      allow_shared=args.allow_shared)
            except pdf.NoPdfText as exc:
                counts["unusable"] += 1
                log.warning("%s: %s", row["slug"], exc)
                continue
            if out.get("skipped"):
                # Group by reason, not by the numbers in it — truncating a
                # formatted message made a 14,380 ceiling read as 1,438.
                reason = ("outsized" if out.get("outsized")
                          else "shared PDF" if out.get("shared")
                          else "not longer than stored" if "against" in out["skipped"]
                          else out["skipped"])
                counts[f"skipped: {reason}"] += 1
                if out.get("outsized"):
                    log.warning("REVIEW %s: %s", row["slug"][:40], out["skipped"])
                continue
            counts["ok"] += 1
            log.info("%s  %d -> %d words  %s", row["slug"][:34],
                     out["before"], out["words"], row["title"][:40])
        conn.commit()
    log.info("extract-pdf-text: %s", dict(counts))
    log.info("now run: index-fts, extract-sections, embed (in that order)")
    return 0


def cmd_collections(args) -> int:
    """Register a standing project, or re-derive memberships from the cache."""
    with db.connect() as conn:
        if args.add:
            slug, name, url = args.add
            db.upsert_collection(conn, slug, name, url)
            conn.commit()
            log.info("registered %s (%s)", name, slug)
        if args.attach:
            slug, refs = args.attach[0], args.attach[1:]
            if not conn.execute("SELECT 1 FROM collections WHERE slug = ?",
                                (slug,)).fetchone():
                log.error("no collection %r — register it with --add first", slug)
                return 1
            for ref in refs:
                try:
                    pub_id = _resolve_publication(conn, ref)
                except NotAPublication as exc:
                    log.error("%s", exc)
                    continue
                db.add_to_collection(conn, pub_id, slug, source="manual")
                log.info("attached %s to %s", pub_id, slug)
            conn.commit()
        if args.detect:
            added = collect.detect_all(conn)
            conn.commit()
            log.info("auto-attached (inbound links): %s", added or "nothing new")
        if args.from_page:
            row = conn.execute("SELECT slug, url FROM collections WHERE slug = ?",
                               (args.from_page,)).fetchone()
            if row is None or not row["url"]:
                log.error("no collection %r with a page URL", args.from_page)
                return 1
            html = Fetcher(min_interval=args.delay).get(row["url"]).text
            n = collect.from_page(conn, row["slug"], html)
            conn.commit()
            log.info("attached %d publications linked from the page", n)
        for c in db.collections(conn):
            print(f"  {c['n']:>4}  {c['slug']:<32} {c['name']}")
    return 0


def cmd_embed(args) -> int:
    """Embed sections and one-liners locally. Resumable: the worklist is what
    has no vector yet, so re-running continues."""
    total = 0
    with db.connect() as conn:
        for source_type in args.sources:
            todo = db.pending_embeddings(conn, source_type, args.model, limit=args.limit)
            log.info("%s: %d to embed via %s", source_type, len(todo), args.model)
            for start in range(0, len(todo), args.batch):
                chunk = todo[start:start + args.batch]
                try:
                    vectors = embed.embed_documents(
                        [r["text"] for r in chunk], [r["title"] for r in chunk],
                        model=args.model,
                    )
                except embed.OllamaUnreachable as exc:
                    log.error("%s", exc)
                    return 2
                db.store_embeddings(conn, source_type, args.model, chunk,
                                    vectors, embed.pack)
                conn.commit()          # per batch: an interrupt keeps its work
                total += len(chunk)
                log.info("  %d/%d", min(start + args.batch, len(todo)), len(todo))
        counts = conn.execute(
            "SELECT source_type, COUNT(*) n FROM embeddings GROUP BY source_type"
        ).fetchall()
    log.info("embedded %d this run", total)
    for r in counts:
        log.info("  %-12s %d vectors", r["source_type"], r["n"])
    return 0


def cmd_landscape(args) -> int:
    """Compute or update the 2D landscape coordinates (#49). Incremental by
    default — new points slot in, nobody moves; --refit redraws the picture
    and deliberately spends the spatial memory."""
    from . import landscape
    with db.connect() as conn:
        out = landscape.refresh(conn, refit=args.refit)
        conn.commit()
    log.info("%s: %d placed, %d pruned — %d points on the map",
             out["mode"], out["placed"], out["pruned"], out["total"])
    return 0


def cmd_semantic_search(args) -> int:
    """Vector search over sections — finds paraphrase, where `search` needs the
    word itself."""
    with db.connect() as conn:
        stored = db.load_embeddings(conn, args.source, args.model)
        if not stored:
            log.error("no %s embeddings for %s — run: pubbrain embed",
                      args.source, args.model)
            return 2
        matrix = embed.normalise(
            numpy.stack([embed.unpack(r["vector"]) for r in stored]))
        try:
            query = embed.embed_query(args.query, model=args.model)
        except embed.OllamaUnreachable as exc:
            log.error("%s", exc)
            return 2

        seen, results = set(), []
        for idx, score in embed.rank(query, matrix, limit=args.limit * 6):
            row = stored[idx]
            # One publication can own many sections; the best one represents it.
            if row["publication_id"] in seen:
                continue
            seen.add(row["publication_id"])
            results.append((row, score))
            if len(results) >= args.limit:
                break

        detail = (db.sections_by_id(conn, [r["source_id"] for r, _ in results])
                  if args.source == "section" else {})
        pubs = {p["id"]: p for p in conn.execute(
            "SELECT id, title, url, pub_type, date_published FROM publications"
        ).fetchall()}

    for row, score in results:
        section = detail.get(row["source_id"])
        pub = pubs[row["publication_id"]]
        print(f"{score:.3f}  {pub['date_published']}  {pub['pub_type']:<21} {pub['title']}")
        print(f"       {pub['url']}")
        if section and section["heading"]:
            print(f"       § {section['heading']}")
        if args.verbose and section:
            print(f"       {' '.join(section['body'].split()[:40])} …")
        print()
    return 0


def cmd_find(args) -> int:
    """Hybrid search — the one to reach for. bm25 finds names and exact terms,
    vectors find paraphrase; merged by rank, since their scores do not compare."""
    with db.connect() as conn:
        try:
            hits, notes = queries.hybrid_find(conn, args.query, limit=args.limit,
                                              sources=args.sources, model=args.model)
        except embed.OllamaUnreachable as exc:
            log.error("%s", exc)
            return 2
    for note in notes:
        log.warning("%s", note)
    if not hits:
        print("no matches")
        return 0
    for hit in hits:
        p = hit["publication"]
        print(f"{hit['score']:.4f} [{hit['rankers']:<5}] {p['date_published']}  "
              f"{p['pub_type']:<21} {p['title']}")
        print(f"                {p['url']}")
        if hit["one_liner"]:
            print(f"                {hit['one_liner']}")
        if hit["section"] and hit["section"]["heading"]:
            print(f"                § {hit['section']['heading']}")
        print()
    return 0


def cmd_web(args) -> int:
    """Serve the local workbench. Import is deferred so every other command
    still works without Flask installed."""
    from . import web
    log.info("workbench on http://%s:%d — Ctrl-C to stop",
             args.host or web.HOST, args.port)
    web.run(port=args.port, host=args.host, debug=args.debug)
    return 0


def cmd_mcp(args) -> int:
    """Serve the MCP tools. Import is deferred so every other command still
    works without the mcp SDK installed.

    Under stdio the client owns stdout — logging goes to stderr, and any tool
    that printed would corrupt the protocol.
    """
    from . import mcp_server
    if args.transport == "stdio":
        # The client owns stdout, and its stderr is a log file it keeps: the
        # SDK writes a line per tool call there, which buys nothing.
        logging.getLogger().setLevel(logging.WARNING)
    else:
        log.info("MCP on http://%s:%d/mcp", args.host, args.port)
    try:
        mcp_server.run(transport=args.transport, host=args.host, port=args.port)
    except ImportError:
        raise SystemExit(
            "the mcp package is not installed — this is the only command that "
            "needs it (Arch: sudo pacman -S python-mcp)") from None
    return 0


def cmd_status(args) -> int:
    with db.connect() as conn:
        s = queries.status_report(conn)
        print(f"database: {paths.DB_PATH}")
        print("\nsitemap worklist")
        for r in s["sitemap"]:
            print(f"  {r['scope']:<12} {r['status']:<9} {r['n']:>5}")
        print(f"\npublications {s['publications']} | people {s['people']}"
              f" | site tags {s['site_tags']}")
        if s["chapters"]:
            print(f"  + {s['chapters']} report chapters, counted under their "
                  f"parent rather than as publications (#36)")
        print(f"body text: {s['texts']} records, {s['words']:,} words")
        print(f"fts index: {s['fts']} rows")
        # Not "of the records with body text" any more: a vision summary (#35)
        # is written from pictures, so summaries now legitimately outnumber
        # bodies and that comparison printed 1,173 of 1,169.
        print(f"enriched: {s['enriched']} summaries ({s['models']} model(s))"
              + (f" — {s['unenriched']} records with body text have none"
                 if s["unenriched"] else ""))
        print(f"topics: {s['mapped']} of {s['enriched']} summaries mapped")
        print(f"sections: {s['sections']}")
        print("vectors: " + (", ".join(f"{k} {n}" for k, n in s["vectors"].items())
                             or "none"))
        print("people links: " + ", ".join(f"{r['role']} {r['n']}" for r in s["roles"]))
        for r in s["affiliations"]:
            print(f"  {r['affiliation'] or 'unclassified':<12} {r['n']:>4}"
                  f"  ({r['cur']} current)")
        for r in s["by_type"]:
            print(f"  {r['pub_type']:<26} {r['n']:>5}")
        for w in s["warnings"]:
            print(f"  <- {w}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pubbrain", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync-sitemap", help="refresh the URL worklist from the sitemap")
    p.add_argument("--delay", type=float, default=2.0)
    p.set_defaults(func=cmd_sync_sitemap)

    p = sub.add_parser("scrape", help="fetch and parse pending publication pages")
    p.add_argument("--limit", type=int, help="stop after N pages")
    p.add_argument("--delay", type=float, default=2.0, help="seconds between requests")
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("sync-roster", help="refresh current staff/fellows and re-classify people")
    p.add_argument("--delay", type=float, default=2.0)
    p.set_defaults(func=cmd_sync_roster)

    p = sub.add_parser("merge-person",
                       help="merge a duplicate person row into the survivor (#47)")
    p.add_argument("duplicate", type=int, help="person id that becomes the tombstone")
    p.add_argument("survivor", type=int, help="person id that keeps the credits")
    p.set_defaults(func=cmd_merge_person)

    p = sub.add_parser("reparse", help="re-run the page parser over the cached HTML")
    p.set_defaults(func=cmd_reparse)

    p = sub.add_parser("extract-text", help="parse body text out of the cached HTML")
    p.add_argument("--only", nargs="+", type=int, metavar="ID",
                   help="restrict to these publications")
    p.add_argument("--force", action="store_true",
                   help="replace imported PDF text with the landing page — for "
                        "a report whose chapters now carry its body (#36)")
    p.set_defaults(func=cmd_extract_text)

    p = sub.add_parser("extract-sections",
                       help="split body text at its headings and index the sections")
    p.set_defaults(func=cmd_extract_sections)

    p = sub.add_parser("index-fts", help="rebuild the keyword search index")
    p.set_defaults(func=cmd_index_fts)

    p = sub.add_parser("search", help="keyword search over the catalog")
    p.add_argument("query", help="FTS5 match expression")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("enrich", help="generate summaries for publications that have none")
    p.add_argument("--limit", type=int, help="stop after N publications")
    p.add_argument("--sensitive", action="store_true",
                   help="take the politically sensitive slice first (#5)")
    p.add_argument("--provider", default=llm.DEFAULT_PROVIDER, choices=sorted(llm.PROVIDERS))
    p.add_argument("--model", help="defaults to the provider's own default")
    p.add_argument("--reasoning-effort", default="none",
                   help="'none' turns a reasoning model's thinking off — 41%% fewer output tokens")
    p.add_argument("--max-tokens", type=int, default=4000)
    p.add_argument("--attempts", type=int, default=3, help="tries per publication")
    p.add_argument("--dry-run", action="store_true", help="print the worklist and stop")
    p.add_argument("--no-wait", action="store_true",
                   help="stop on a quota error instead of waiting it out (#45)")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("exec-summary",
                       help="copy a report's own executive summary in (#18, no model)")
    p.add_argument("--only", nargs="+", type=int, metavar="ID")
    p.add_argument("--dry-run", action="store_true", help="print the worklist")
    p.set_defaults(func=cmd_exec_summary)

    p = sub.add_parser("blurb", help="plain-language blurbs from the summaries (#38)")
    p.add_argument("--only", nargs="+", type=int, metavar="ID")
    p.add_argument("--limit", type=int, help="stop after N — pace the backfill")
    p.add_argument("--year", nargs="+", metavar="YYYY",
                   help="scope the backfill to these years; newest first anyway")
    p.add_argument("--include-stale", dest="stale", action="store_true",
                   help="add blurbs whose summary is no longer primary to the "
                        "worklist. Not 'stale only' — the missing ones come too")
    p.add_argument("--model", default=blurb.MODEL)
    p.add_argument("--provider", default=blurb.PROVIDER, choices=sorted(llm.PROVIDERS))
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--no-wait", action="store_true",
                   help="stop on a quota error instead of waiting it out — the "
                        "backoff otherwise eats the window as it recovers (#45)")
    p.add_argument("--dry-run", action="store_true", help="print the worklist")
    p.set_defaults(func=cmd_blurb)

    p = sub.add_parser("vision", help="summarize publications that are a picture (#35)")
    p.add_argument("--only", nargs="+", type=int, metavar="ID")
    p.add_argument("--limit", type=int, help="stop after N publications")
    p.add_argument("--model", default=vision.MODEL)
    p.add_argument("--provider", default=vision.PROVIDER, choices=sorted(llm.PROVIDERS))
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--dry-run", action="store_true",
                   help="print the worklist and what images it found")
    p.add_argument("--no-wait", action="store_true",
                   help="stop on a quota error instead of waiting it out (#45)")
    p.set_defaults(func=cmd_vision)

    p = sub.add_parser("enrichment", help="print stored summaries for review")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--sensitive", action="store_true")
    p.add_argument("--full", action="store_true", help="summary, findings and entities too")
    p.set_defaults(func=cmd_enrichment)

    p = sub.add_parser("map-topics",
                       help="assign controlled-vocabulary topics to summaries")
    p.add_argument("--limit", type=int, help="stop after N publications")
    p.add_argument("--remap", action="store_true",
                   help="re-map publications that already have topics")
    p.add_argument("--sensitive", action="store_true",
                   help="take the politically sensitive slice first (#5)")
    p.add_argument("--provider", default=llm.DEFAULT_PROVIDER, choices=sorted(llm.PROVIDERS))
    p.add_argument("--model", help="defaults to the provider's own default")
    p.add_argument("--reasoning-effort", default="none")
    p.add_argument("--max-tokens", type=int, default=300)
    p.add_argument("--attempts", type=int, default=3, help="tries per publication")
    p.add_argument("--dry-run", action="store_true", help="print the worklist and stop")
    p.add_argument("--no-wait", action="store_true",
                   help="stop on a quota error instead of waiting it out (#45)")
    p.set_defaults(func=cmd_map_topics)

    p = sub.add_parser("topics", help="the vocabulary with its publication counts")
    p.add_argument("--samples", type=int, default=0,
                   help="show N recent titles per topic")
    p.set_defaults(func=cmd_topics)

    p = sub.add_parser("todo", help="what the owner handed over from the Backlog")
    p.set_defaults(func=cmd_todo)

    p = sub.add_parser("ingest-url", help="bring one root-level URL in by hand")
    p.add_argument("url")
    p.add_argument("--type", required=True, help="pub_type — never inferred")
    p.add_argument("--series")
    p.add_argument("--note")
    p.add_argument("--delay", type=float, default=2.0)
    p.set_defaults(func=cmd_ingest_url)

    p = sub.add_parser("add-pdf-record",
                       help="a publication that exists only as a PDF (#36)")
    p.add_argument("url", help="the PDF URL — also the record's identity")
    p.add_argument("--title", required=True)
    p.add_argument("--type", required=True, help="pub_type — never inferred")
    p.add_argument("--date", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--series")
    p.add_argument("--subtitle")
    p.set_defaults(func=cmd_add_pdf_record)

    p = sub.add_parser("chapters", help="attach chapters to a report (#36)")
    p.add_argument("parent", help="parent publication id, URL or slug")
    p.add_argument("chapters", nargs="*",
                   help="chapter URLs, ids or slugs, in reading order")
    p.add_argument("--start", type=int, default=1, help="position of the first")
    p.add_argument("--type", help="defaults to the parent's own type")
    p.add_argument("--list", action="store_true", help="print the contents and stop")
    p.add_argument("--delay", type=float, default=2.0)
    p.set_defaults(func=cmd_chapters)

    p = sub.add_parser("ingest-proposed",
                       help="ingest root URLs whose subtitle names their series")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--delay", type=float, default=2.0)
    p.set_defaults(func=cmd_ingest_proposed)

    p = sub.add_parser("classify-root",
                       help="probe root-level URLs and propose ingest/exclude")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--refresh", action="store_true", help="re-probe everything")
    p.set_defaults(func=cmd_classify_root)

    p = sub.add_parser("download-pdfs", help="fetch PDFs the catalog links to")
    p.add_argument("--limit", type=int, help="stop after N")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--oldest-first", action="store_true",
                   help="default is newest first")
    p.add_argument("--needed", action="store_true",
                   help="only PDFs that would actually be imported — thin "
                        "record, single owner, hosted by MERICS")
    p.add_argument("--only", nargs="+", type=int, metavar="ID",
                   help="restrict to these publications")
    p.set_defaults(func=cmd_download_pdfs)

    p = sub.add_parser("extract-pdf-text", help="PDF text into the catalog (no network)")
    p.add_argument("--only", nargs="+", type=int, metavar="ID",
                   help="restrict to these publications")
    p.add_argument("--allow-outsized", action="store_true",
                   help="import even when the PDF dwarfs its type — for reports "
                        "confirmed by hand as genuinely long, not compilations")
    p.add_argument("--allow-shared", action="store_true",
                   help="import even when another publication links the same PDF "
                        "— for a report merely cited by a Brief (#42). Needs --only")
    p.set_defaults(func=cmd_extract_pdf_text)

    p = sub.add_parser("collections", help="standing projects and their members")
    p.add_argument("--add", nargs=3, metavar=("SLUG", "NAME", "URL"),
                   help="register a collection")
    p.add_argument("--detect", action="store_true",
                   help="re-derive auto memberships from the HTML cache")
    p.add_argument("--from-page", metavar="SLUG",
                   help="attach everything the collection's own page links to")
    p.add_argument("--attach", nargs="+", metavar=("SLUG", "REF"),
                   help="attach publications (id, URL or slug) by hand")
    p.add_argument("--delay", type=float, default=2.0)
    p.set_defaults(func=cmd_collections)

    p = sub.add_parser("embed", help="build local embeddings for semantic search")
    p.add_argument("--sources", nargs="+", default=["section", "one_liner"],
                   choices=["section", "one_liner"])
    p.add_argument("--model", default=embed.MODEL)
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--limit", type=int, help="stop after N per source")
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser("landscape",
                       help="compute/update the embedding landscape (#49)")
    p.add_argument("--refit", action="store_true",
                   help="redraw the whole layout — spends the spatial memory")
    p.set_defaults(func=cmd_landscape)

    p = sub.add_parser("semantic-search", help="vector search — finds paraphrase")
    p.add_argument("query")
    p.add_argument("--source", default="section", choices=["section", "one_liner"])
    p.add_argument("--model", default=embed.MODEL)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_semantic_search)

    p = sub.add_parser("find", help="hybrid keyword + vector search (start here)")
    p.add_argument("query")
    p.add_argument("--sources", nargs="+", default=["one_liner", "section"],
                   choices=["section", "one_liner"])
    p.add_argument("--model", default=embed.MODEL)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("web", help="local web workbench: browse, search, review")
    p.add_argument("--port", type=int, default=8901,
                   help="must stay outside 8780-8799 (exposed to LLM browsers)")
    p.add_argument("--host", default=None,
                   help="bind address; 0.0.0.0 makes the workbench reachable "
                        "from the home network (#51). Default: localhost only")
    p.add_argument("--debug", action="store_true", help="Flask auto-reload")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("mcp", help="serve the catalog as MCP tools")
    p.add_argument("--transport", default="stdio",
                   choices=["stdio", "streamable-http"])
    p.add_argument("--host", default="127.0.0.1", help="streamable-http only")
    p.add_argument("--port", type=int, default=8902, help="streamable-http only")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("status", help="show worklist and catalog counts")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
