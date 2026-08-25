"""Split extracted body text into sections at its markdown headings (#16).

Deterministic and local — no model. What a heading *means* varies by type: in a
MERICS Brief a `###` is an independent story, in a Report it is one step of a
single argument. This module only finds the boundaries; deciding which
publications have genuinely separate topics is `has_independent_topics`.
"""

import re

HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.M)

# A heading with almost nothing under it is a label ("Analysis", "Update"), not
# a section. Keeping them would put empty rows in the index and split a story
# away from its own headline.
MIN_SECTION_WORDS = 20


def split(body: str) -> list:
    """Sections in document order. Text before the first heading becomes an
    untitled section, so no prose is dropped."""
    if not body:
        return []
    out, marks = [], list(HEADING.finditer(body))

    preamble = body[: marks[0].start()] if marks else body
    if preamble.split():
        out.append({"heading": None, "level": None, "body": preamble.strip()})

    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append({
            "heading": m.group(2),
            "level": len(m.group(1)),
            "body": body[m.end():end].strip(),
        })

    kept, position = [], 0
    for s in out:
        s["word_count"] = len(s["body"].split())
        if s["word_count"] < MIN_SECTION_WORDS:
            continue
        for piece in windows(s):
            piece["position"] = position
            position += 1
            kept.append(piece)
    return kept


# The embedder truncates at 1,000 words (`embed.MAX_WORDS`), silently — a
# 56,000-word section is represented by its opening page and nothing says so.
# Anything above the ceiling is cut into windows near the size real
# heading-bounded sections run to (#16 measured a ~330-word median), which is
# also what retrieves best.
CHUNK_CEILING = 700
CHUNK_WORDS = 350
CHUNK_OVERLAP = 60


def windows(section: dict) -> list:
    """One section, or the overlapping windows it is too long to embed as.

    Splits on paragraph boundaries so a window is whole sentences, falling back
    to a word cut for a single paragraph longer than the target. Each window
    keeps its parent's heading — it is the only context it has, and the FTS
    index weights headings.
    """
    if section["word_count"] <= CHUNK_CEILING:
        return [section]

    paras = [p for p in re.split(r"\n\s*\n", section["body"]) if p.strip()]
    units = []
    for p in paras:
        words = p.split()
        # A wall of text with no blank lines still has to be cut somewhere.
        for i in range(0, len(words), CHUNK_WORDS):
            units.append(words[i:i + CHUNK_WORDS])

    chunks, buf = [], []
    for unit in units:
        if buf and len(buf) + len(unit) > CHUNK_WORDS:
            chunks.append(buf)
            buf = buf[-CHUNK_OVERLAP:] if CHUNK_OVERLAP else []
        buf.extend(unit)
    if buf:
        chunks.append(buf)

    return [{**section,
             "body": " ".join(c),
             "word_count": len(c),
             "chunk_index": i,
             "chunk_total": len(chunks)}
            for i, c in enumerate(chunks)]


# Recurring furniture in MERICS Briefs and Trackers, identified by heading text —
# they appear at both `##` and `###`, so heading depth does not find them. None
# is a topic: they are link collections, single statistics, or subsections of a
# chapter. In a vector index they retrieve on their format rather than on any
# subject, which is worse than useless.
STANDING_FEATURES = {
    "merics china digest",      # a collection of links
    "metrix",                   # one number and why it matters
    "short takes",              # links, under a later name
    "policy news",              # subsection of a Tracker chapter
    "corporate news",           # subsection of a Tracker chapter
    "buzzword of the week",
    "graphic of the week",
    "noteworthy",
    "worth noting",
    "europe-china diplomatic tracker",
    "acknowledgements",
    # PDF front and back matter (#34). A contents page is a list of the
    # headings that follow it, so in a vector index it retrieves for every
    # topic the report covers and answers none of them.
    "contents",
    "table of contents",
    "imprint",
    "references",
    "bibliography",
    "about merics",
    "about the authors",
    "about the author",
}

# The standing outlook slot appears under two names. Matched as prefixes, not
# as bare substrings: "what to watch" anywhere in a heading also catches real
# argument headlines like "What to watch: China's sanction regimes will combine
# manifold approaches", which is a story and belongs in the index.
STANDING_PATTERNS = (
    "what to watch in the months ahead",
    "looking forward: what to watch",
)


def is_boilerplate(heading) -> bool:
    """Whether a heading names a standing feature rather than a story."""
    if not heading:
        return False
    h = heading.strip().lower().rstrip(":").strip()
    return h in STANDING_FEATURES or any(h.startswith(p) for p in STANDING_PATTERNS)


def has_independent_topics(pub_type: str, title: str, sections: list) -> bool:
    """Whether a publication's sections are separate stories rather than parts
    of one argument — the difference between a digest and a report.

    In a Brief a `##` is a story and a `###` is a standing feature (METRIX,
    MERICS CHINA DIGEST, the Diplomatic Tracker), which is the opposite of what
    the nesting suggests. Verified against the `+`-separated title 80% of Briefs
    carry: 98% have at least as many `##` sections as the title names topics.
    Type alone decides nothing — Comments are almost all `##` and are a single
    argument.
    """
    if pub_type not in ("MERICS Briefs", "Tracker"):
        return False
    return len([s for s in sections
                if s["level"] == 2 and not is_boilerplate(s["heading"])]) >= 2


def topics_from_title(title: str) -> list:
    """The `+`-separated topic list MERICS Briefs put in their own titles.
    Used to sanity-check a split, not to produce sections."""
    if not title or " + " not in title:
        return []
    return [t.strip() for t in title.split(" + ") if t.strip()]
