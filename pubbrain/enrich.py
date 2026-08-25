"""LLM enrichment: one-liner, short summary, key findings, entities (#5).

The one-liner is the recall unit the whole tool rests on, so a response that
fails validation is re-asked with the specific problem quoted back rather than
stored as-is. The prompt-and-validate half writes nothing — `cli.cmd_enrich`
owns the loop, the commit and the resume behaviour, which is what keeps the
retry logic testable without a database.

`regenerate` (#24) is the exception and is deliberately one function: replacing
a summary has three consequences besides the new row, and doing them apart is
how a stale vector survives.
"""

import functools
import json
import re

from . import db, embed, llm

# Bump when the prompt changes meaning: rows carry the version they were
# generated under, so a re-run can target the stale ones.
PROMPT_VERSION = 1

# The default model measured 26-30 words against a 25-word instruction on 4 of
# 12 samples (#14), and all 12 fit 30. The cap follows the model rather than
# fighting it.
ONE_LINER_MAX_WORDS = 30

# Long enough for the 99th percentile; the tail is China Essentials round-ups
# where the back half is a digest of items already covered.
BODY_WORD_CAP = 6000

# A heading reading "Executive Summary" is not proof of one. On the 56,033-word
# security report the section under it is **13 words** — a divider page whose
# heading the PDF geometry recovered correctly and whose content is elsewhere.
# Below this floor the opening is the better bet, so the cap wins. Measured
# against the real ones: median 333 words, shortest genuine 217.
MIN_EXEC_WORDS = 150

# The caveat on #5: a Chinese-hosted model summarizing a European think tank's
# criticism of China is where distortion would show, so this slice is enriched
# and reviewed first rather than being left to a random sample.
SENSITIVE_QUERY = (
    'Xinjiang OR Uyghur OR "Hong Kong" OR Taiwan OR Tibet OR repression '
    'OR surveillance OR "human rights" OR "party control" OR censorship'
)

SYSTEM = f"""You summarize think-tank publications for a personal recall tool.
The reader works at MERICS in a non-analyst role and wants to remember what \
the institute has published and who works on what.

Reply with JSON only, no code fence, matching exactly:
{{"summary_one_liner": "...", "summary_short": "...",
 "key_findings": ["...", "..."], "entities": {{"people": [], "organizations": [],
 "places": [], "policies": []}}}}

Rules:
- summary_one_liner: AT MOST {ONE_LINER_MAX_WORDS} words. This is the recall \
unit — it must be what someone would say to remind themselves of this piece a \
year later. Specific, not generic. Never start with "This publication" or \
"The report".
- summary_short: 3-5 sentences, the argument and the so-what.
- key_findings: 3-5 concrete claims the piece actually makes.
- entities: only names that literally appear in the text. Do not infer or add \
context. Empty lists are fine.
- Report what the publication argues, including where it is critical. Do not \
soften, balance or add caveats the authors did not write."""


class Invalid(RuntimeError):
    """The model never returned a usable object within the attempt budget."""


VERBATIM_MODEL = "executive summary (verbatim)"


def write_summary(conn, publication_id, one_liner, short):
    """Store a summary the owner wrote, and make it the one being served (#46).

    Returns (enrichment_id, problems). Nothing is written when `problems` is
    non-empty — the 30-word cap is the model's rule and applies here too, since
    it is the retrieval unit either way.

    Editing an existing hand-written summary updates that row rather than
    stacking a new one per keystroke-session; the first edit demotes whatever
    the model wrote, which stays readable underneath. Either way the one-liner
    vector is rebuilt and the topics are invalidated — see `resummarised`.

    Key findings and entities carry over from the summary being replaced: they
    are extracted claims, not prose, and asking someone to retype them to fix a
    sentence is how a correction stops being worth making.
    """
    one_liner, short = (one_liner or "").strip(), (short or "").strip()
    problems = []
    if not one_liner:
        problems.append("the one-liner cannot be empty — it is what search ranks on")
    elif len(one_liner.split()) > ONE_LINER_MAX_WORDS:
        problems.append(f"the one-liner is {len(one_liner.split())} words; the "
                        f"limit is {ONE_LINER_MAX_WORDS}")
    if not short:
        problems.append("the summary cannot be empty")
    if problems:
        return None, problems

    current = conn.execute(
        "SELECT * FROM primary_enrichment WHERE publication_id = ?",
        (publication_id,)).fetchone()
    data = {
        "summary_one_liner": one_liner, "summary_short": short,
        "key_findings": json.loads(current["key_findings"] or "[]") if current else [],
        "entities": json.loads(current["entities"] or "{}") if current else {},
    }
    meta = {"model": db.HAND_WRITTEN_MODEL, "provider": "none",
            "prompt_version": 0, "words_sent": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "attempts": 0, "seconds": 0.0}

    if current is not None and current["model"] == db.HAND_WRITTEN_MODEL:
        db.update_enrichment(conn, current["id"], data, meta)
        # There is no promotion to hang the consequences off — the row is
        # edited in place — so they are called directly. This branch previously
        # deleted the vector and never rebuilt it, leaving the record on
        # keyword-only ranking until someone happened to run `embed`.
        resummarised(conn, publication_id)
        return current["id"], []

    new_id = db.add_enrichment(conn, publication_id, data, meta,
                               is_primary=current is None)
    if current is not None:
        promote(conn, publication_id, new_id)
    return new_id, []


def copy_executive_summary(conn, publication_id):
    """Put the report's own executive summary in the summary field. No model.

    Owner decision, 2026-08-09: where the authors already wrote a summary, a
    model rewriting it adds a step and a risk and nothing else. Returns the new
    enrichment id, or None if there is no usable executive summary.

    Written as a new row and promoted, so the generated summary is demoted
    rather than destroyed and both are readable on the record page.

    **The one-liner is carried over, not copied.** It is capped at 30 words and
    is what search ranks on, so it cannot hold 600 words of executive summary;
    `rewrite_one_liner` is the separate, cheap step that brings it into line.
    """
    rec = db.enrichment_row(conn, publication_id)
    if rec is None:
        return None
    exec_words = (rec["exec_body"] or "").split()
    if len(exec_words) < MIN_EXEC_WORDS:
        return None
    current = conn.execute(
        "SELECT * FROM primary_enrichment WHERE publication_id = ?",
        (publication_id,)).fetchone()
    if current is None:
        return None
    new_id = db.add_enrichment(conn, publication_id, {
        "summary_one_liner": current["summary_one_liner"],
        "summary_short": " ".join(exec_words),
        "key_findings": json.loads(current["key_findings"] or "[]"),
        "entities": json.loads(current["entities"] or "{}"),
    }, {"model": VERBATIM_MODEL, "provider": "none", "prompt_version": 0,
        "words_sent": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "attempts": 0, "seconds": 0.0})
    promote(conn, publication_id, new_id)
    return new_id


def pending_executive_summary(conn, min_document=BODY_WORD_CAP):
    """Records holding a usable executive summary that is not yet the summary.

    Two filters, and both matter. The **length floor** applies the same test
    `copy_executive_summary` does, so the dry run cannot promise records the
    apply would skip — the 56,033-word security report has a heading reading
    "Executive Summary" with 13 words under it.

    `min_document` is why this is scoped to long reports: under the cap the
    model already reads the whole document, so its summary is written from
    everything the executive summary was written from, and swapping adds
    nothing.
    """
    words = ("(length(x.exec_body) - length(replace(x.exec_body, ' ', '')) + 1)")
    return [r["id"] for r in conn.execute(
        f"""SELECT x.id FROM (
              SELECT p.id, t.word_count, {db.EXEC_SUMMARY_SQL} AS exec_body
              FROM publications p
              JOIN publication_text t ON t.publication_id = p.id
              JOIN primary_enrichment e ON e.publication_id = p.id
              WHERE e.model != ?
            ) x
            WHERE x.exec_body IS NOT NULL AND {words} >= ?
              AND x.word_count >= ?
            ORDER BY x.word_count DESC""",
        (VERBATIM_MODEL, MIN_EXEC_WORDS, min_document))]


def _field(rec, key):
    """`rec` is a sqlite Row in production and a plain dict in tests, and the
    two disagree about a missing key."""
    try:
        return rec[key]
    except (IndexError, KeyError):
        return None


def build_prompt(rec, cap_words=BODY_WORD_CAP) -> str:
    """The text to summarise from — the executive summary where there is one.

    Owner, 2026-08-09: a 300-650 word executive summary copied in verbatim is
    accurate but *"not digestible"*. So the authors' summary is what the model
    reads, rather than the first 6,000 words of a document that opens with a
    cover and a contents page. Summarising a summary is the point, not a
    redundancy: the input is right and the output is short.

    Documents under the cap go whole — nothing is being chosen there.
    """
    words = (rec["body"] or "").split()
    head = f"Title: {rec['title']}\nType: {rec['pub_type']}\nDate: {rec['date_published']}"
    if rec["subtitle"]:
        head += f"\nSubtitle: {rec['subtitle']}"

    exec_words = (_field(rec, "exec_body") or "").split()
    if len(words) > cap_words and len(exec_words) >= MIN_EXEC_WORDS:
        outline = (_field(rec, "outline") or "").strip()
        parts = [f"This is a {len(words):,}-word document. Below is the "
                 f"authors' own executive summary and the contents.",
                 f"\nExecutive summary:\n{' '.join(exec_words[:cap_words])}"]
        if outline:
            parts.append("\nContents:\n" + "\n".join(
                f"- {line}" for line in outline.splitlines() if line.strip()))
        return f"{head}\n\n" + "\n".join(parts)

    body = " ".join(words[:cap_words])
    # og:description is the whole article again (#15) — only worth sending when
    # there is no body to send instead.
    if rec["og_description"] and not body:
        head += f"\nDescription: {rec['og_description'][:8000]}"
    return f"{head}\n\nBody:\n{body}"


def parse(content):
    """The model is asked for bare JSON; accept a fenced block too."""
    if not content:
        return None
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def validate(data) -> list:
    """Problems with a parsed response, in words the model can act on — the
    list is fed straight back to it as the retry instruction."""
    if not isinstance(data, dict):
        return ["the response was not a JSON object"]
    problems = []

    one = data.get("summary_one_liner")
    if not isinstance(one, str) or not one.strip():
        problems.append("summary_one_liner is missing or empty")
    elif len(one.split()) > ONE_LINER_MAX_WORDS:
        problems.append(
            f"summary_one_liner is {len(one.split())} words; the limit is "
            f"{ONE_LINER_MAX_WORDS}. Rewrite it shorter without dropping the specifics"
        )

    short = data.get("summary_short")
    if not isinstance(short, str) or not short.strip():
        problems.append("summary_short is missing or empty")

    findings = data.get("key_findings")
    if not isinstance(findings, list) or not all(
        isinstance(f, str) and f.strip() for f in findings
    ):
        problems.append("key_findings must be a list of non-empty strings")
    elif not findings:
        problems.append("key_findings is empty; give 3 to 5 concrete claims")
    elif len(findings) > 8:
        problems.append(f"key_findings has {len(findings)} items; give 3 to 5")

    entities = data.get("entities")
    if not isinstance(entities, dict) or not all(
        isinstance(v, list) and all(isinstance(n, str) for n in v)
        for v in entities.values()
    ):
        problems.append(
            'entities must be an object whose values are lists of names, e.g. '
            '{"people": ["..."], "organizations": []}'
        )
    elif any(not n.strip() for v in entities.values() for n in v):
        # A blank name counts as grounded ('' is in every text), which would
        # quietly inflate the one quality metric this pipeline is judged on.
        problems.append("entities contains a blank name; omit it instead")
    return problems


def grounding(entities, body):
    """Share of extracted entity names that literally occur in the source. The
    model was told to extract, not infer, so anything absent is invented.

    Blank names are dropped rather than counted: `'' in text` is always True,
    so including them would inflate the score.
    """
    low = (body or "").lower()
    names = [n for group in (entities or {}).values() if isinstance(group, list)
             for n in group if isinstance(n, str) and n.strip()]
    if not names:
        return None
    return round(sum(n.lower() in low for n in names) / len(names), 3)


def enrich_one(rec, model, provider, max_tokens=4000, attempts=3,
               reasoning_effort="none", chat=None, network_retries=None):
    """Ask, validate, re-ask on failure. Returns (data, meta); raises Invalid
    if no attempt produced a usable object. Transport errors propagate.

    `chat` resolves at call time, not as a default: bound at import it cannot be
    patched, and a test that thinks it stubbed the model would call the real one.
    """
    chat = chat or llm.chat_with_backoff
    if network_retries is not None:
        # 0 stops on a quota error instead of waiting it out (#45).
        chat = functools.partial(chat, retries=network_retries)
    prompt = build_prompt(rec)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]
    meta = {
        "model": model, "provider": provider, "prompt_version": PROMPT_VERSION,
        # Measured, not inferred. `min(word_count, cap)` was right only while
        # the prompt was always the opening; the executive-summary path (#18)
        # sends a tenth of that, and this column is how anyone later finds a
        # summary written from too little. It must not overstate what was seen.
        "words_sent": len(prompt.split()),
        "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0, "attempts": 0,
    }
    problems = []
    for attempt in range(1, attempts + 1):
        res = chat(messages, model=model, provider=provider,
                   max_tokens=max_tokens, reasoning_effort=reasoning_effort)
        meta["attempts"] = attempt
        meta["prompt_tokens"] += res["prompt_tokens"] or 0
        meta["completion_tokens"] += res["completion_tokens"] or 0
        meta["seconds"] = round(meta["seconds"] + (res["seconds"] or 0), 2)
        meta["model"] = res.get("model") or model

        data = parse(res["content"])
        problems = validate(data)
        if not problems:
            return data, meta

        if attempt < attempts:
            # Feed the failure back in-conversation: the model sees its own bad
            # answer, which fixes formatting far more reliably than re-asking cold.
            messages += [
                {"role": "assistant", "content": res["content"] or ""},
                {"role": "user", "content":
                 "That response was not usable: " + "; ".join(problems) +
                 ". Reply again with the corrected JSON only."},
            ]
    raise Invalid("; ".join(problems) or "no response")


def regenerate(conn, publication_id, model=None, provider=llm.DEFAULT_PROVIDER,
               attempts=3, **kwargs) -> dict:
    """Generate an *additional* summary as a candidate (#18). Commits nothing.

    It does not replace anything: the existing primary keeps serving search,
    embeddings and every list until someone promotes the candidate. That is the
    whole point of the change — a rewrite is "add one and choose", not
    "overwrite and hope", so the two can be read side by side first.

    Nothing here touches the flag or the one-liner vector, because nothing here
    changes which summary is primary. `promote` owns both consequences.

    Raises Invalid if no attempt produced a usable summary, leaving the
    publication exactly as it was.
    """
    rec = db.enrichment_row(conn, publication_id)
    if rec is None:
        raise ValueError(f"publication {publication_id} has no body text to "
                         f"summarize")
    current = conn.execute(
        "SELECT id, summary_one_liner, summary_short, model, enriched_at "
        "FROM primary_enrichment WHERE publication_id = ?",
        (publication_id,)).fetchone()

    model = model or llm.PROVIDERS[provider]["default_model"]
    data, meta = enrich_one(rec, model, provider, attempts=attempts, **kwargs)
    # First summary for this publication? Then it is the primary, not a
    # candidate — a publication with only candidates has no summary at all.
    candidate_id = db.add_enrichment(conn, publication_id, data, meta,
                                     is_primary=current is None)
    return {"before": current, "after": data, "meta": meta,
            "candidate_id": candidate_id, "is_primary": current is None}


def promote(conn, publication_id, enrichment_id) -> dict:
    """Make a candidate the primary summary, and clean up what that
    invalidates. Commits nothing — the caller owns that.

    1. The previous primary is demoted, not deleted: it stays available to
       promote back, and dismissing is a separate deliberate act.
    2. **The one-liner vector is deleted, always.** It describes the summary
       that was primary, and nothing downstream would reveal that — a stale
       vector does not look stale, it looks like a confident wrong match.
       Re-embedding is attempted immediately; if ollama is down the vector
       stays missing, which degrades to keyword ranking and `embed` repairs it.
    3. **The topic mapping is deleted too** (#43). Same reasoning as the
       vector: topics are read off the primary summary, so they describe the
       demoted one. `map-topics` re-derives them on its next run.
    4. The flag goes with the demoted row: a verdict describes the summary it
       reviewed, and that summary is no longer the one being served. It is not
       deleted — `reviews.subject_id` now names the enrichment row, so the
       verdict stays attached to the text it was actually about.
    """
    moved = db.promote_enrichment(conn, enrichment_id)
    if moved["publication_id"] != publication_id:
        raise ValueError(f"summary {enrichment_id} does not belong to "
                         f"publication {publication_id}")
    return {**moved, "note": resummarised(conn, publication_id)}


def resummarised(conn, publication_id):
    """Invalidate everything derived from a summary that just changed.

    Returns a note when the vector could not be rebuilt, else None. Both paths
    that change which text is served come here — promoting a candidate and
    writing one by hand (#46) — because they have identical consequences and
    keeping two copies is how one of them silently stopped re-embedding.

    - **The one-liner vector is deleted and rebuilt.** It describes the text
      that was being served; a stale vector does not look stale, it looks like a
      confident wrong match. If ollama is down the vector stays *missing*, which
      degrades to keyword ranking and `status` reports.
    - **The topic mapping is deleted, not re-derived.** Mapping costs a model
      call, and `map-topics` owns that — its worklist is "has a summary and no
      topics", which this puts the record back on.
    """
    conn.execute("DELETE FROM embeddings WHERE source_type = 'one_liner' "
                 "AND source_id = ?", (publication_id,))
    conn.execute("DELETE FROM publication_topics WHERE publication_id = ?",
                 (publication_id,))
    try:
        todo = [r for r in db.pending_embeddings(conn, "one_liner", embed.MODEL)
                if r["source_id"] == publication_id]
        if todo:
            vectors = embed.embed_documents([r["text"] for r in todo],
                                            [r["title"] for r in todo])
            db.store_embeddings(conn, "one_liner", embed.MODEL, todo, vectors,
                                embed.pack)
    except embed.OllamaUnreachable as exc:
        return (f"the summary vector could not be rebuilt ({exc}) — it is "
                f"missing rather than stale; run `pubbrain embed` when ollama "
                f"is back")
    return None
