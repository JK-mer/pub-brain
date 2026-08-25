"""Ask the catalog (#24): a question answered from retrieved excerpts, with
citations back to the records the answer used.

Retrieval is `queries.hybrid_find` — the same ranking the workbench and the CLI
use, so an answer can never be better or worse grounded than a search for the
same words.

The load-bearing part is not the prompt but what happens after it: **a citation
is only rendered as a link if it names a publication that was actually
retrieved.** A model asked to cite ids will occasionally cite one it was never
shown, and a fabricated citation that renders identically to a real one is
worse than no citation at all — so they are separated and counted.

The answer is returned as segments rather than HTML. Model output is untrusted
text; letting a template escape it normally is what keeps it that way.
"""

import re

from . import llm, queries

MAX_HITS = 8
SECTION_WORDS = 120
MAX_TOKENS = 1200

# Models group citations as [8, 443] as readily as [8] [443]. Matching only the
# single form left grouped ones as plain text — not linked, and not counted as
# invalid either, so they escaped the check entirely.
CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

SYSTEM = """You answer questions about a catalog of MERICS publications
(Mercator Institute for China Studies, a European think tank on China) for one
person, who works there in a non-analyst role and uses this to recall what has
been published.

You are given excerpts retrieved from that catalog. Rules:

- Answer ONLY from the excerpts. You have no other knowledge of what MERICS has
  published, and your background knowledge about China is not evidence about
  their output.
- Cite every claim with the publication id in square brackets, like [412].
  Cite only ids that appear in the excerpts below.
- If the excerpts do not answer the question, say so plainly and describe what
  they do cover. Do not fill the gap.
- Never say MERICS has not covered something. The excerpts are the top matches
  for one search, not the whole catalog, and cannot show absence.
- Be brief and concrete: name the publications, their argument, and their date.
  Prefer three tight sentences over a page. No preamble, no restating the
  question, no closing offer of further help."""


def _plain(text, words=None):
    """Excerpt text with its square brackets neutralised.

    Article prose carries footnote markers, and a model copying `[12]` out of a
    source would mint a citation pointing at whatever publication 12 happens to
    be. Only the id lines this function does not touch may use brackets.
    """
    kept = (text or "").split()
    if words:
        kept = kept[:words]
    return " ".join(kept).replace("[", "(").replace("]", ")")


def _excerpt(hit):
    """One retrieved record as the model sees it. The section is included when
    the match was inside a multi-story digest — that is the whole reason
    sections exist, and without it the model summarises the wrong item."""
    pub = hit["publication"]
    lines = [f"[{pub['id']}] {_plain(pub['title'])} — {pub['pub_type']}, "
             f"{pub['date_published'] or 'undated'}"]
    if hit["one_liner"]:
        lines.append(f"  summary: {_plain(hit['one_liner'])}")
    else:
        lines.append("  summary: none — this record has no body text, so only "
                     "its title is known")
    section = hit.get("section")
    if section and section["heading"]:
        lines.append(f"  matching section “{_plain(section['heading'])}”: "
                     f"{_plain(section['body'], SECTION_WORDS)}")
    return "\n".join(lines)


def build_prompt(question, hits) -> str:
    return (f"Question: {question}\n\nExcerpts:\n\n"
            + "\n\n".join(_excerpt(h) for h in hits))


def segments(text, by_id):
    """Split an answer into plain text and citations, resolving each id against
    what was actually retrieved.

    A citation the retrieval never produced is kept in the text and reported —
    silently dropping it would hide the one failure mode worth watching.
    """
    out, invalid, cited, last = [], [], [], 0
    for m in CITATION.finditer(text):
        if m.start() > last:
            out.append({"text": text[last:m.start()]})
        for ident in (int(n) for n in m.group(1).split(",")):
            pub = by_id.get(ident)
            if pub is not None:
                out.append({"cite": pub})
                if pub["id"] not in [c["id"] for c in cited]:
                    cited.append(pub)
            else:
                out.append({"text": f"[{ident}]"})
                invalid.append(ident)
        last = m.end()
    if last < len(text):
        out.append({"text": text[last:]})
    return out, cited, invalid


def answer(conn, question, provider=llm.DEFAULT_PROVIDER, model=None,
           limit=MAX_HITS, with_vectors=True, chat=None) -> dict:
    """Retrieve, ask, resolve citations. Never raises on an empty catalog hit —
    "nothing was retrieved" is an answer and must be shown as one.

    `chat` resolves at call time so it can actually be stubbed; as a default
    argument it binds at import and a test believing it patched the model would
    quietly call the real one.
    """
    chat = chat or llm.chat_with_backoff
    hits, notes = queries.hybrid_find(conn, question, limit=limit,
                                      with_vectors=with_vectors)
    if not hits:
        return {"question": question, "hits": [], "notes": notes,
                "segments": [{"text": "Nothing in the catalog matched that "
                                      "search, so there is nothing to answer "
                                      "from. Try different words — this is a "
                                      "failed search, not an absence of "
                                      "coverage."}],
                "cited": [], "invalid": [], "usage": None}

    res = chat([{"role": "system", "content": SYSTEM},
                {"role": "user", "content": build_prompt(question, hits)}],
               model=model, provider=provider, max_tokens=MAX_TOKENS,
               reasoning_effort="none")
    by_id = {h["publication"]["id"]: h["publication"] for h in hits}
    parts, cited, invalid = segments(res["content"].strip(), by_id)
    return {
        "question": question,
        "hits": hits,
        "notes": notes,
        "segments": parts,
        "cited": cited,
        "invalid": invalid,
        "usage": {"model": res["model"], "seconds": res["seconds"],
                  "prompt_tokens": res["prompt_tokens"],
                  "completion_tokens": res["completion_tokens"]},
    }
