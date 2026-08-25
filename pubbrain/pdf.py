"""PDF download and text extraction (#6).

Reports are the most substantial thing MERICS publishes and a third of them sit
in the catalog as their landing-page abstract — 37 words standing in for 3,747.
The analysis exists as a PDF whose URL is already stored; `pdf_url` is
populated on 499 records and `pdf_path` on none.

Nothing here costs API budget. Only re-enriching what gains text does.
"""

import collections
import html
import logging
import re
import subprocess

from . import db, paths, queries

log = logging.getLogger("pubbrain")

# poppler's pdftotext, with pdfminer as an in-process fallback. -layout is
# deliberately NOT used: it preserves column positions with runs of spaces,
# which reads well to a human and tokenizes badly for search.
PDFTOTEXT = ("pdftotext", "-enc", "UTF-8", "-nopgbrk")

# -bbox-layout adds per-line geometry, which is how headings are recovered
# (#34). A PDF carries no heading markup at all, but it does carry type size,
# and a line taller than the body text is a heading in every MERICS layout
# from the 2014 China Monitors to the 2026 reports.
PDFTOTEXT_BBOX = ("pdftotext", "-bbox-layout", "-enc", "UTF-8")


# A PDF extracting to more than this multiple of its type's median length is
# almost certainly a multi-chapter volume rather than one publication.
OUTSIZED_MULTIPLE = 4


class NoPdfText(Exception):
    """The PDF yielded nothing usable — scanned images, or a failed download."""


def pdf_path_for(slug: str):
    return paths.PDF_DIR / f"{slug}.pdf"


def pending_downloads(conn, limit=None, newest_first=True, needed_only=False,
                      only=None):
    """Records with a PDF URL and no local copy. Newest first by default: the
    owner's priority is 2025/2026 completeness, and the old China Monitor
    backlog can fill slowly.

    `needed_only` restricts to PDFs that would actually be imported — the
    record is thin, the PDF is single-owner, and it is hosted by MERICS.
    Downloading all 496 costs ~1.3 GB to gain text for about 48 of them.
    """
    where = "pdf_url IS NOT NULL AND pdf_path IS NULL"
    if only:
        where += f" AND id IN ({', '.join(str(int(i)) for i in only)})"
    if needed_only:
        where += """
          AND pdf_url LIKE '%merics.org%'
          AND (SELECT COUNT(*) FROM publications q
               WHERE q.pdf_url = publications.pdf_url) = 1
          AND id IN (
            SELECT t.publication_id FROM publication_text t
            JOIN publications pp ON pp.id = t.publication_id
            WHERE t.source <> 'manual' AND t.word_count < (
              SELECT CAST(AVG(w) * 0.25 AS INT) FROM (
                SELECT t2.word_count w FROM publication_text t2
                JOIN publications p2 ON p2.id = t2.publication_id
                WHERE p2.pub_type = pp.pub_type))
            UNION
            SELECT p3.id FROM publications p3 LEFT JOIN publication_text t3
              ON t3.publication_id = p3.id WHERE t3.publication_id IS NULL)"""
    sql = ("SELECT id, slug, pdf_url, date_published, title FROM publications "
           f"WHERE {where} "
           f"ORDER BY date_published {'DESC' if newest_first else 'ASC'}")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def download(fetcher, row) -> int:
    """Fetch one PDF to disk, returning its size. Raises on transport failure."""
    paths.ensure_dirs()
    target = pdf_path_for(row["slug"])
    resp = fetcher.get(row["pdf_url"])
    head = resp.content[:5]
    if not head.startswith(b"%PDF"):
        raise NoPdfText(f"not a PDF (starts {head!r})")
    target.write_bytes(resp.content)
    return len(resp.content)


def to_text(path) -> str:
    """Plain text from a PDF. `pdftotext` first; `pdfminer` if it is absent."""
    try:
        out = subprocess.run([*PDFTOTEXT, str(path), "-"],
                             capture_output=True, timeout=180)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.debug("pdftotext unavailable or slow (%s), trying pdfminer", exc)
    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(path)) or ""
    except Exception as exc:                       # noqa: BLE001 - last resort
        raise NoPdfText(f"no extractor succeeded: {type(exc).__name__}") from exc


def clean(raw: str) -> str:
    """Join hard-wrapped lines into paragraphs.

    A PDF has no paragraphs, only line breaks at the page's right margin. Left
    as-is, every line becomes its own block and `sections.split` sees hundreds
    of one-line fragments instead of prose.
    """
    paragraphs, buf = [], []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
            continue
        buf.append(line)
    if buf:
        paragraphs.append(" ".join(buf))
    return "\n\n".join(p for p in paragraphs if p)


# Geometry, all measured against the document's own body height rather than in
# absolute points — the corpus spans a dozen layouts and two page sizes.
HEADING_RATIO = 1.20     # taller than this multiple of body type is a heading
ROTATED_RATIO = 3.0      # ...but a chart's rotated axis label gets a tall box
HEADING_MAX_WORDS = 20   # a long "tall" line is a pull-quote, not a heading

# The 2014-2015 China Monitors set their body copy above the modal height (the
# mode falls on footnotes), which turns every line into a heading. A document
# averaging fewer words than this between headings has no structure worth
# trusting, so it takes the flat path and the chunker.
MIN_WORDS_PER_HEADING = 80

# Furniture that survives every layout: the chart credit, page footers, exhibit
# labels, and bare numbers. All render at heading size and none is a heading.
FURNITURE = re.compile(
    r"^(©\s*MERICS|MERICS\s*\|.*|\d+\s*\|\s*MERICS.*|Exhibit\s+\d+|"
    r"Figure\s+\d+|Table\s+\d+|[\d\s|.,–-]+)$", re.I)

_LINE = re.compile(
    r'<line\b[^>]*yMin="([\d.]+)"[^>]*yMax="([\d.]+)"[^>]*>(.*?)</line>', re.S)
_WORD = re.compile(r"<word\b[^>]*>(.*?)</word>", re.S)
_BLOCK_OR_PAGE = re.compile(r"<(block|page)\b")


def _bbox_lines(path):
    """(page, block, height, text) per laid-out line, or [] if unavailable."""
    try:
        out = subprocess.run([*PDFTOTEXT_BBOX, str(path), "-"],
                             capture_output=True, timeout=300)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.debug("bbox extraction unavailable (%s)", exc)
        return []
    if out.returncode != 0:
        return []
    doc = out.stdout.decode("utf-8", errors="replace")
    # Position, not a parser: poppler's XHTML is not reliably well-formed, and
    # one stray byte would otherwise cost the whole document.
    marks = [(m.start(), m.group(1)) for m in _BLOCK_OR_PAGE.finditer(doc)]
    rows, page, block, mark = [], 0, 0, 0
    for m in _LINE.finditer(doc):
        while mark < len(marks) and marks[mark][0] < m.start():
            if marks[mark][1] == "page":
                page += 1
            else:
                block += 1
            mark += 1
        text = " ".join(" ".join(html.unescape(w).split())
                        for w in _WORD.findall(m.group(3)))
        text = " ".join(text.split())
        if text:
            rows.append((page, block,
                         round(float(m.group(2)) - float(m.group(1)), 1), text))
    return rows


def _running_furniture(rows) -> set:
    """Text repeating across a fifth of the pages — running heads and footers.

    Page numbers vary, so they are compared with digits stripped. These are
    noise in the body and, at heading size, false headings.
    """
    pages = len({r[0] for r in rows}) or 1
    seen = collections.defaultdict(set)
    for page, _, _, text in rows:
        if len(text.split()) <= 12:
            seen[re.sub(r"\d+", "", text).strip()].add(page)
    return {k for k, p in seen.items() if len(p) >= max(3, pages * 0.2)}


def to_markdown(path) -> str:
    """PDF text with its headings recovered as Markdown, so `sections.split`
    can bound sections in it exactly as it does for HTML (#34).

    Returns "" when the geometry yields no usable heading structure — a scanned
    or single-size document — leaving the caller on the flat path.
    """
    rows = _bbox_lines(path)
    if not rows:
        return ""
    body_h = collections.Counter(h for _, _, h, _ in rows).most_common(1)[0][0]
    running = _running_furniture(rows)

    def is_heading(h, text):
        if not body_h or h <= body_h * HEADING_RATIO or h >= body_h * ROTATED_RATIO:
            return False
        if len(text.split()) > HEADING_MAX_WORDS:
            return False
        if not re.search(r"\w", text):
            return False
        return not FURNITURE.match(text)

    out, para, last_block, last_h = [], [], None, None
    for page, block, h, text in rows:
        if re.sub(r"\d+", "", text).strip() in running:
            continue
        if is_heading(h, text):
            if para:
                out.append(["p", " ".join(para)])
                para = []
            # A heading wrapping onto a second line arrives as two lines of the
            # same height in the same block.
            if out and out[-1][0] == "h" and last_block == block and last_h == h:
                out[-1][1] += f" {text}"
            else:
                out.append(["h", text, h])
        else:
            if block != last_block and para:
                out.append(["p", " ".join(para)])
                para = []
            para.append(text)
        last_block, last_h = block, h
    if para:
        out.append(["p", " ".join(para)])

    out = _drop_repeats(out)
    headings = [i for i in out if i[0] == "h"]
    words = sum(len(i[1].split()) for i in out)
    if not headings or words < len(headings) * MIN_WORDS_PER_HEADING:
        return ""
    heights = {i[2] for i in headings}
    # Depth by type size: the largest surviving band is `##`, everything below
    # it `###`. Absolute sizes mean nothing across layouts, the ordering does.
    top = max(heights)
    return "\n\n".join(
        f"{'##' if i[2] == top else '###'} {i[1]}" if i[0] == "h" else i[1]
        for i in out)


def _drop_repeats(out: list) -> list:
    """Remove a heading the layout printed twice — the cover repeated inside,
    and the divider page that announces a chapter the next page then opens
    with. The two are separated by a little furniture rather than by prose, so
    proximity in the item list is what distinguishes them from a real reprise.
    """
    kept, recent = [], []
    for item in out:
        if item[0] == "h":
            if item[1] in recent:
                continue
            recent = [item[1], *recent][:4]
        kept.append(item)
    return kept


def is_shared(conn, publication_id) -> int:
    """How many publications link the same PDF.

    More than one means it is a **compilation**, not this record's text: the
    ETNC reports, the Papers on China volume and the Economic Indicators
    quarterlies are each linked from every chapter that appears in them. 13
    PDFs are shared by 37 publications; the other 462 belong to one record.

    Importing a shared PDF into each sharer copies the whole document N times —
    the Italy chapter briefly held all 75,890 words of the 2026 ETNC report,
    with Lithuania mentioned 90 times in a record about Italy.
    """
    row = conn.execute(
        """SELECT COUNT(*) n FROM publications WHERE pdf_url = (
             SELECT pdf_url FROM publications WHERE id = ?)""",
        (publication_id,)).fetchone()
    return row["n"] if row else 0


def import_text(conn, publication_id, slug, allow_outsized=False,
                allow_shared=False) -> dict:
    """Extract a downloaded PDF into `publication_text`.

    Three refusals, all because wrong text is worse than missing text:

    - **never touch hand-entered text** — it is the only copy (#31);
    - **never import a shared PDF** — it belongs to the parent publication,
      not to the chapter that links it. `allow_shared` is the override for the
      case the count cannot see: a Brief that merely *cites* a report shares
      its PDF without being a chapter of anything (#42). Per-record, after
      looking at the sharers;
    - **never shorten a record** — a PDF that extracts to less than the stored
      HTML is a failed extraction, not an improvement. Re-extracting over text
      this step itself wrote is exempt: it is the same source read again, and
      heading recovery (#34) legitimately drops running heads and page numbers.
    """
    if db.is_authored(conn, publication_id):
        return {"skipped": "manual text"}
    url = conn.execute("SELECT pdf_url FROM publications WHERE id = ?",
                       (publication_id,)).fetchone()["pdf_url"]
    if url and "merics.org" not in url:
        # Three Comments link the European Commission's own Strategic Outlook.
        # A cited document is not the citing publication's text.
        return {"skipped": "PDF is not published by MERICS"}
    shared = is_shared(conn, publication_id)
    if shared > 1 and not allow_shared:
        return {"skipped": f"PDF shared by {shared} publications", "shared": shared}
    path = pdf_path_for(slug)
    if not path.exists():
        return {"skipped": "not downloaded"}
    text = to_markdown(path) or clean(to_text(path))
    words = len(text.split())
    if not words:
        raise NoPdfText("extracted nothing")
    # A safety net, not the main test. Sharing catches most compilations, but
    # an ETNC parent whose chapters live at root-level URLs is linked by
    # nothing else and slips through. A single institute's report does not run
    # to four times its type's median; a multi-partner volume does. Flagged for
    # review rather than imported — the owner can look and decide.
    norms = queries.type_length_norms(conn)
    kind = conn.execute("SELECT pub_type FROM publications WHERE id = ?",
                        (publication_id,)).fetchone()["pub_type"]
    ceiling = 0 if allow_outsized else norms.get(kind, (0, 0))[0] * OUTSIZED_MULTIPLE
    if ceiling and words > ceiling:
        return {"skipped": f"{words} words is over {int(ceiling)} for a {kind} "
                           f"— probably a compilation, review it",
                "words": words, "outsized": True}
    row = conn.execute(
        "SELECT word_count, source FROM publication_text WHERE publication_id = ?",
        (publication_id,)).fetchone()
    before = row["word_count"] if row else 0
    if words <= before and (row is None or row["source"] != "pdf"):
        return {"skipped": f"PDF has {words} words against {before} stored",
                "words": words, "before": before}
    db.upsert_text(conn, publication_id, text, words, source="pdf")
    return {"words": words, "before": before}
