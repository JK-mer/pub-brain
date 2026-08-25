"""Map publications onto the frozen topic vocabulary (#4).

Reads the generated summary rather than the body text — the one-liner, short
summary and key findings already say what a piece is about, so this run costs a
fraction of #5's and nothing here ever sees an article again.

Same shape as `enrich`: prompt, validate, re-ask with the problem quoted back.
The gate is stricter, because a topic outside the vocabulary is not a
formatting slip but a topic that does not exist — it would sit in the table
matching no glossary entry and appear in no filter.
"""

import functools
import json
import re

from . import llm, topics

# Bump when the prompt changes meaning: rows carry the version they were mapped
# under, so a re-run can target the stale ones.
PROMPT_VERSION = 1

# 1-4 per the frozen vocabulary's own spec. The floor matters more than the
# ceiling: an unmapped publication is invisible to every topic surface, so the
# model is never allowed to answer "none of these".
MIN_TOPICS, MAX_TOPICS = 1, 4


class Invalid(RuntimeError):
    """The model never returned a usable mapping within the attempt budget."""


def vocabulary_block() -> str:
    lines = []
    for cluster in topics.load():
        lines.append(f"\n{cluster['cluster']}:")
        for t in cluster["topics"]:
            lines.append(f"  {t['slug']} — {t['name']}: {t['entails'].strip()}")
    return "\n".join(lines)


def build_system() -> str:
    return f"""You assign controlled-vocabulary topics to think-tank \
publications from MERICS, a European research institute on China.

Choose between {MIN_TOPICS} and {MAX_TOPICS} topics from the list below, using \
the slug exactly as written. Order them by centrality: the first is what the \
piece is mainly about.

Reply with JSON only, no code fence, matching exactly:
{{"topics": ["slug", "slug"]}}

Rules:
- Only slugs from the list. Never invent one, never return an empty list.
- Assign a topic only if the publication is substantially about it. A passing \
mention is not a topic. Two accurate topics beat four padded ones.
- Judge the publication's subject, not its framing: almost everything here \
touches Europe and China somewhere, so those are topics only when the piece is \
actually about the relationship or the policy.
- A multi-story digest gets the topics of the stories it leads with.

The vocabulary:
{vocabulary_block()}"""


def build_prompt(rec) -> str:
    findings = rec["key_findings"]
    if isinstance(findings, str):
        try:
            findings = json.loads(findings)
        except json.JSONDecodeError:
            findings = []
    parts = [f"Title: {rec['title']}",
             f"Type: {rec['pub_type']}",
             f"Date: {rec['date_published']}"]
    if rec["subtitle"]:
        parts.append(f"Subtitle: {rec['subtitle']}")
    parts.append(f"\nOne-liner: {rec['summary_one_liner']}")
    parts.append(f"\nSummary: {rec['summary_short']}")
    if findings:
        parts.append("\nKey findings:\n" + "\n".join(f"- {f}" for f in findings))
    return "\n".join(parts)


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


def validate(data, valid=None) -> list:
    """Problems with a parsed response, in words the model can act on."""
    valid = set(valid if valid is not None else topics.slugs())
    if not isinstance(data, dict):
        return ["the response was not a JSON object"]
    chosen = data.get("topics")
    if not isinstance(chosen, list) or not all(
        isinstance(s, str) and s.strip() for s in chosen
    ):
        return ['"topics" must be a list of slug strings']

    problems = []
    if len(chosen) < MIN_TOPICS:
        problems.append(f"topics is empty; give {MIN_TOPICS} to {MAX_TOPICS} slugs "
                        f"— every publication belongs to at least one")
    elif len(chosen) > MAX_TOPICS:
        problems.append(f"topics has {len(chosen)} entries; the limit is "
                        f"{MAX_TOPICS}. Keep the most central ones")
    if len(set(chosen)) != len(chosen):
        problems.append("topics repeats a slug; each may appear once")
    unknown = [s for s in chosen if s not in valid]
    if unknown:
        problems.append(
            f"not in the vocabulary: {', '.join(repr(s) for s in unknown)}. "
            f"Use a slug exactly as listed, or drop it")
    return problems


def map_one(rec, model, provider, max_tokens=300, attempts=3,
            reasoning_effort="none", chat=None, network_retries=None):
    """Ask, validate, re-ask on failure. Returns (slugs, meta); raises Invalid
    if no attempt produced a usable mapping. Transport errors propagate.

    `chat` resolves at call time, not as a default: bound at import it cannot be
    patched, and a test that thinks it stubbed the model would call the real one.
    """
    chat = chat or llm.chat_with_backoff
    if network_retries is not None:
        # 0 stops on a quota error instead of waiting it out (#45).
        chat = functools.partial(chat, retries=network_retries)
    valid = topics.slugs()
    messages = [
        {"role": "system", "content": build_system()},
        {"role": "user", "content": build_prompt(rec)},
    ]
    meta = {
        "model": model, "provider": provider, "prompt_version": PROMPT_VERSION,
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
        problems = validate(data, valid)
        if not problems:
            return data["topics"], meta

        if attempt < attempts:
            messages += [
                {"role": "assistant", "content": res["content"] or ""},
                {"role": "user", "content":
                 "That response was not usable: " + "; ".join(problems) +
                 ". Reply again with the corrected JSON only."},
            ]
    raise Invalid("; ".join(problems) or "no response")
