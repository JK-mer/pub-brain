"""Plain-language blurbs: the mind-map layer (#38).

The one-liner is a *recall* unit — 30 words in the register of the piece,
carrying the vocabulary that makes it match a search. This is the opposite job:
explain the piece to someone who does not have that vocabulary yet.

Built from the primary summary rather than the body text (owner's call, and the
right one): the summary is already a distillation, so the prompt is short, the
run is cheap, and it cannot re-introduce detail the summary correctly dropped.

Not indexed for search. Plain-language wording would dilute a keyword index
built on the terms the documents actually use, which is the same reason the
one-liner is not written this way.
"""

import functools
import json
import re

from . import db, enrich, llm

PROMPT_VERSION = 1
MODEL = "provider/large-instruct"
PROVIDER = "default"

MIN_WORDS, MAX_WORDS = 30, 110

SYSTEM = f"""You rewrite think-tank summaries in plain language.

The reader works at MERICS in a non-analyst role. They know the institute \
publishes on China; they do not have the analytical vocabulary. Your job is to \
tell them what a piece is about so they could describe it to a friend.

Reply with JSON only, no code fence: {{"blurb": "..."}}

Rules:
- {MIN_WORDS}-{MAX_WORDS} words, two to four sentences.
- No jargon. If a term is unavoidable, say what it means in the same breath — \
"de-risking (cutting reliance on China for critical goods)".
- Say what the piece is ABOUT and why it matters. Not what it recommends, \
unless the recommendation is the point of it.
- Plain verbs and short sentences. Never "This publication" or "The report".
- Do not add facts. Everything you write must come from the summary given.
- Do not just shorten the words of the one-liner. If your blurb would tell the \
reader nothing the one-liner did not, you have not done the job."""


class NoSummary(RuntimeError):
    """Nothing to rewrite — a podcast or a paywalled record."""


def build_prompt(rec) -> str:
    """The primary summary, not the body. Key findings carry the substance."""
    parts = [f"Title: {rec['title']}",
             f"Type: {rec['pub_type']}  Date: {rec['date_published']}",
             f"\nOne-liner: {rec['summary_one_liner']}",
             f"\nSummary: {rec['summary_short']}"]
    findings = rec["key_findings"] or []
    if findings:
        parts.append("\nKey findings:\n" +
                     "\n".join(f"- {f}" for f in findings))
    return "\n".join(parts)


def _words(text):
    return len((text or "").split())


# Long enough to pass the 5-letter filter and present in most of the corpus, so
# counting them would make every honest blurb look derivative.
UBIQUITOUS = {"china", "chinese", "beijing", "europe", "european", "which",
              "their", "there", "would", "about", "these", "those", "other"}


def _shared_ratio(blurb, one_liner):
    """Fraction of the one-liner's distinctive words the blurb reuses.

    The failure this catches is the one the issue names: a blurb that is the
    one-liner with shorter words.
    """
    pick = lambda t: {w for w in re.findall(r"[a-z]{5,}", (t or "").lower())
                      } - UBIQUITOUS
    source = pick(one_liner)
    if not source:
        return 0.0
    return len(source & pick(blurb)) / len(source)


# Measured, not guessed: the first six blurbs reused 20-58% (median 33%), so
# this catches near-verbatim restatement and nothing else. Deliberately loose —
# a false positive costs a retry, and the register itself is what the prompt is
# for. It cannot tell a dull blurb from a good one; only reading can.
MAX_SHARED_RATIO = 0.8


def validate(data, one_liner=None) -> list:
    """Problems with a parsed response, phrased so the model can act on them."""
    if not isinstance(data, dict):
        return ["the response was not a JSON object"]
    blurb = data.get("blurb")
    if not isinstance(blurb, str) or not blurb.strip():
        return ["'blurb' must be a non-empty string"]
    problems = []
    n = _words(blurb)
    if n < MIN_WORDS:
        problems.append(f"the blurb is {n} words, at least {MIN_WORDS} are needed")
    if n > MAX_WORDS:
        problems.append(f"the blurb is {n} words, at most {MAX_WORDS} are allowed")
    if re.match(r"^(this (publication|report|piece)|the (report|paper))\b",
                blurb.strip(), re.I):
        problems.append("do not open with 'This publication' or 'The report'")
    if one_liner and _shared_ratio(blurb, one_liner) > MAX_SHARED_RATIO:
        problems.append("this repeats the one-liner rather than explaining it — "
                        "say what the piece is about in your own plain words")
    return problems


def blurb_one(rec, model=MODEL, provider=PROVIDER, attempts=3, max_tokens=700,
              chat=None, network_retries=None):
    """Ask, validate, re-ask. Returns (blurb, meta); raises enrich.Invalid.

    Reuses `enrich.parse` and the retry-in-conversation shape: the model seeing
    its own bad answer fixes format far more reliably than re-asking cold.

    `network_retries=0` turns off waiting out a quota error (#45). Two distinct
    retries live here: this one is the transport's, `attempts` is the
    validator's, and only the first can burn a request without producing text.
    """
    chat = chat or llm.chat_with_backoff
    if network_retries is not None:
        chat = functools.partial(chat, retries=network_retries)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": build_prompt(rec)}]
    meta = {"model": model, "provider": provider,
            "prompt_version": PROMPT_VERSION,
            "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0}
    problems = []
    for attempt in range(1, attempts + 1):
        res = chat(messages, model=model, provider=provider,
                   max_tokens=max_tokens)
        meta["prompt_tokens"] += res["prompt_tokens"] or 0
        meta["completion_tokens"] += res["completion_tokens"] or 0
        meta["seconds"] = round(meta["seconds"] + (res["seconds"] or 0), 2)
        meta["model"] = res.get("model") or model

        data = enrich.parse(res["content"])
        problems = validate(data, rec["summary_one_liner"])
        if not problems:
            return data["blurb"].strip(), meta
        if attempt < attempts:
            messages += [
                {"role": "assistant", "content": res["content"] or ""},
                {"role": "user", "content":
                 "That response was not usable: " + "; ".join(problems) +
                 ". Reply again with the corrected JSON only."},
            ]
    raise enrich.Invalid("; ".join(problems) or "no response")


def pending(conn, only=None, limit=None, stale=False, year=None):
    """Records with a primary summary and no current blurb, newest first.

    `stale` also returns blurbs built from a summary that is no longer primary
    — promoting a candidate (#18) does not silently leave the old blurb
    standing beside the new summary.

    `year` is how the backfill is scoped: the owner wants recent years first,
    and "stop after N" cannot express "finish 2025" without arithmetic that
    goes wrong the moment a record is added.
    """
    where = ["e.publication_id IS NOT NULL"]
    params = []
    if year:
        where.append(f"substr(p.date_published, 1, 4) IN "
                     f"({', '.join('?' * len(year))})")
        params.extend(str(y) for y in year)
    if stale:
        where.append("(b.publication_id IS NULL "
                     "OR b.source_enrichment_id IS NOT e.id)")
    else:
        where.append("b.publication_id IS NULL")
    if only:
        where.append(f"p.id IN ({', '.join('?' * len(only))})")
        params.extend(only)
    sql = f"""
        SELECT p.id, p.title, p.pub_type, p.date_published,
               e.id AS enrichment_id, e.summary_one_liner, e.summary_short,
               e.key_findings
        FROM publications p
        JOIN primary_enrichment e ON e.publication_id = p.id
        LEFT JOIN publication_blurbs b ON b.publication_id = p.id
        WHERE {' AND '.join(where)}
        ORDER BY p.date_published DESC, p.id DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r, key_findings=json.loads(r["key_findings"] or "[]"))
            for r in conn.execute(sql, params)]


def save(conn, publication_id, blurb, enrichment_id, meta) -> None:
    conn.execute(
        """INSERT INTO publication_blurbs
             (publication_id, blurb, source_enrichment_id, model, provider,
              prompt_version, prompt_tokens, completion_tokens, seconds,
              created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(publication_id) DO UPDATE SET
             blurb = excluded.blurb,
             source_enrichment_id = excluded.source_enrichment_id,
             model = excluded.model, provider = excluded.provider,
             prompt_version = excluded.prompt_version,
             prompt_tokens = excluded.prompt_tokens,
             completion_tokens = excluded.completion_tokens,
             seconds = excluded.seconds, created_at = excluded.created_at""",
        (publication_id, blurb, enrichment_id, meta.get("model"),
         meta.get("provider"), meta.get("prompt_version"),
         meta.get("prompt_tokens"), meta.get("completion_tokens"),
         meta.get("seconds"), db.now()))


def for_publication(conn, publication_id):
    """The blurb and whether the summary it was built from is still primary."""
    return conn.execute(
        """SELECT b.*, (b.source_enrichment_id IS NOT e.id) AS stale
           FROM publication_blurbs b
           LEFT JOIN primary_enrichment e ON e.publication_id = b.publication_id
           WHERE b.publication_id = ?""", (publication_id,)).fetchone()


def blurbs_for(conn, publication_ids):
    """{publication_id: blurb} for a listing page."""
    if not publication_ids:
        return {}
    holes = ", ".join("?" * len(publication_ids))
    return {r["publication_id"]: r["blurb"] for r in conn.execute(
        f"SELECT publication_id, blurb FROM publication_blurbs "
        f"WHERE publication_id IN ({holes})", publication_ids)}
