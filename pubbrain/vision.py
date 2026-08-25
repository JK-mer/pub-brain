"""Summaries for publications whose content is a picture (#35).

Enrichment reaches only what has body text, so an infographic, a timeline
graphic or a MERICS Data Insight is a title and nothing else — invisible to a
summary search and to every vector. These are not parser failures: the pages
carry `media-image` paragraphs and no prose at all (#6), so there is nothing to
extract and no fix short of looking at the picture.

Two sources, one path: images from the page's own content field, and pages
rendered out of a PDF for the Economic Indicators quarterlies, whose data
sections exist nowhere on merics.org.

Output is an ordinary enrichment row. `primary_enrichment`, search, the counts
and topic mapping then pick it up with no special-casing at all — the only
trace is `model`, which records that a vision model wrote it.
"""

import base64
import logging
import re
import subprocess
import tempfile
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from . import enrich, llm, paths, text

log = logging.getLogger("pubbrain")

# Named rather than hardcoded elsewhere so the picker (#39) and this pass
# cannot drift apart.
MODEL = "provider/large-vision"
PROVIDER = "default"

# Drupal serves resized derivatives under /styles/<preset>/public/. The
# original is the same path with that segment removed, and a chart is exactly
# the case where the full-resolution copy matters — axis labels and footnotes
# are what a summary needs and what a thumbnail loses.
_STYLE_PATH = re.compile(r"/styles/[^/]+/public/")

# A quarterly runs to ~25 pages of charts. Sent whole it is both slow and past
# what the model reads usefully, so the cap is explicit and reported rather
# than silently applied.
MAX_PAGES = 12
PDF_DPI = 110

# Same rule for page images. A "Key graphics" page carries 9 of them at full
# resolution — the originals run to ~1.7 MB each, so the whole set is a ~20 MB
# request, and the answer is one summary either way. Both caps are reported in
# the run log: a truncated read that says "12 of 22 pages" is honest, one that
# quietly stops at 12 is not.
MAX_IMAGES = 6

# `llm.chat` defaults to 600s, which is right for a long text completion and
# wrong here: a stalled multi-image request would hold a whole run for ten
# minutes per record. Measured, a single image answers in 11-18s and twelve
# rendered PDF pages in 24s — but two page images together were seen to hang
# past two minutes while each answered alone, which is provider behaviour this
# side cannot fix. Failing fast and moving on is the only thing that keeps a
# run finishing; the record simply stays on the worklist.
REQUEST_TIMEOUT = 150

# And the timeout alone bounds nothing: `chat_with_backoff` treats a timeout as
# transient and retries it 8 times, which turns a 150s ceiling into ~25 minutes
# per record. That is right for a rate limit and wrong here — the same payload
# stalls the same way, so retrying is pure waiting. One retry covers a genuine
# blip; past that the record stays on the worklist for a later run.
NETWORK_RETRIES = 1

SYSTEM = f"""You describe charts and infographics published by MERICS, a \
European think tank on China, for a personal recall tool.

Reply with JSON only, no code fence, matching exactly:
{{"summary_one_liner": "...", "summary_short": "...",
 "key_findings": ["...", "..."], "entities": {{"people": [], "organizations": [],
 "places": [], "policies": []}}}}

Rules:
- summary_one_liner: AT MOST {enrich.ONE_LINER_MAX_WORDS} words — what someone \
would say to remind themselves of this graphic a year later. Never start with \
"This graphic" or "The chart".
- summary_short: 3-5 sentences. What the graphic shows and what it is evidence \
for.
- key_findings: 3-5 concrete claims the graphic supports. **Read the numbers \
off the chart** — a direction of travel without magnitudes is not a finding. \
Give figures, units and years where they are printed.
- entities: only names actually printed in the images.
- Ignore logos, funder banners, page furniture and MERICS branding — they are \
not the content.
- Describe only what is shown. Do not supply background the images do not \
contain, and do not guess at a trend that continues past the plotted range."""


def _absolute(src, base="https://merics.org") -> str:
    if src.startswith("http"):
        return src
    return base + src if src.startswith("/") else f"{base}/{src}"


def page_images(html: str) -> list:
    """Absolute URLs of the images in the page's own content field.

    Anchored on `text._fields(article, "content")` — the same accessor the body
    extractor uses — because a bare `select("img")` returns the site logo, the
    author portraits, the topic icons and every teaser's cover. On one Data
    Insight that is 9 images for a page carrying 1.
    """
    soup = BeautifulSoup(html, "html.parser")
    article = text._main_article(soup)
    if article is None:
        return []
    blocks = text._fields(article, "content")
    if not blocks:
        return []
    out = []
    for img in blocks[0].select("img"):
        src = img.get("src") or ""
        if not src:
            continue
        url = _absolute(_STYLE_PATH.sub("/", src).split("?")[0])
        if url not in out:
            out.append(url)
    return out


def pdf_page_images(pdf_path, dpi=PDF_DPI, max_pages=MAX_PAGES) -> tuple:
    """PDF pages as PNG bytes. Returns (images, total_pages) so a caller can
    report what it left out rather than implying it read the whole document."""
    total = _page_count(pdf_path)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), "-f", "1",
             "-l", str(min(max_pages, total or max_pages)),
             str(pdf_path), str(Path(tmp) / "p")],
            check=True, capture_output=True, timeout=600)
        return [p.read_bytes() for p in sorted(Path(tmp).glob("p*.png"))], total


def _page_count(pdf_path):
    try:
        out = subprocess.run(["pdfinfo", str(pdf_path)],
                             capture_output=True, timeout=60)
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("Pages:"):
                return int(line.split()[1])
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass
    return 0


def as_data_uri(blob: bytes, mime="image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def fetch_images(fetcher, urls) -> list:
    """Download images as data URIs. Through the project's own rate-limited
    fetcher rather than handing merics.org URLs to the model provider: the
    politeness rule applies to images too, and a provider-side fetch would also
    fail silently on anything the provider cannot reach."""
    out = []
    for url in urls:
        resp = fetcher.get(url)
        mime = resp.headers.get("content-type", "image/png").split(";")[0]
        if not mime.startswith("image/"):
            continue
        out.append(as_data_uri(resp.content, mime))
    return out


def build_messages(rec, data_uris) -> list:
    """The publication's own metadata plus its pictures.

    Title and type are given because a chart alone rarely states its subject —
    "Chinese export prices" is printed on the axis, "this is a MERICS Data
    Insight from 2026" is not.
    """
    # dict(rec) rather than rec.get: callers pass sqlite3.Row, which indexes
    # like a mapping but has no .get, and the miss is an AttributeError at the
    # first record rather than anything the type system caught.
    rec = dict(rec)
    header = [f"Title: {rec['title']}"]
    if rec.get("subtitle"):
        header.append(f"Subtitle: {rec['subtitle']}")
    header.append(f"Type: {rec['pub_type']}  Published: {rec['date_published']}")
    if rec.get("series"):
        header.append(f"Series: {rec['series']}")
    content = [{"type": "text", "text": "\n".join(header)}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in data_uris]
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": content}]


def describe(rec, data_uris, model=MODEL, provider=PROVIDER, attempts=3,
             max_tokens=4000, timeout=REQUEST_TIMEOUT, chat=None,
             network_retries=NETWORK_RETRIES) -> tuple:
    """(data, meta) for one publication, validated by `enrich.validate` — the
    same gate the text pass uses, because the output has the same job and a
    reader cannot tell which one wrote a row.

    `chat` resolves at call time, not as a default: bound at import it cannot
    be patched, and a test that thinks it stubbed the model calls the real one.
    """
    if not data_uris:
        raise enrich.Invalid("no images to describe")
    chat = chat or llm.chat_with_backoff
    messages = build_messages(rec, data_uris)
    meta = {
        "model": model, "provider": provider,
        "prompt_version": enrich.PROMPT_VERSION,
        "words_sent": 0, "images_sent": len(data_uris),
        "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0, "attempts": 0,
    }
    problems = []
    for attempt in range(1, attempts + 1):
        try:
            res = chat(messages, model=model, provider=provider,
                       max_tokens=max_tokens, timeout=timeout,
                       retries=network_retries)
        except (requests.Timeout, requests.ConnectionError):
            # Measured against syn:large:vision: two page images together time
            # out at 150s while each answers alone in 11-18s — and twelve
            # rendered PDF pages go through in 24s, so it is neither size nor
            # count. Whatever it is, it is the provider's, and the leading
            # image in a page's content field is the publication's own graphic.
            # A summary of that beats no summary; `images_sent` records the
            # difference so a thin reading is never mistaken for a full one.
            if len(data_uris) == 1:
                raise
            log.warning("id=%s timed out on %d images — retrying with the first",
                        dict(rec).get("id"), len(data_uris))
            data_uris = data_uris[:1]
            meta["images_sent"] = 1
            meta["degraded"] = True
            messages = build_messages(rec, data_uris)
            res = chat(messages, model=model, provider=provider,
                       max_tokens=max_tokens, timeout=timeout,
                       retries=network_retries)
        meta["attempts"] = attempt
        meta["prompt_tokens"] += res["prompt_tokens"] or 0
        meta["completion_tokens"] += res["completion_tokens"] or 0
        meta["seconds"] = round(meta["seconds"] + (res["seconds"] or 0), 2)
        meta["model"] = res.get("model") or model

        data = enrich.parse(res["content"])
        problems = enrich.validate(data)
        if not problems:
            return data, meta
        messages = messages + [
            {"role": "assistant", "content": res["content"]},
            {"role": "user", "content": "That was rejected: "
                                        + "; ".join(problems) + ". Reply again."},
        ]
    raise enrich.Invalid("; ".join(problems) or "no usable response")


def pending(conn, limit=None):
    """Records with no summary and no body text to make one from — the ones
    only a picture can answer. Ordered newest first, resumable.

    **A record with a downloaded PDF and no text belongs to `extract-pdf-text`,
    not here**, and is excluded. Its page image is a report cover, and a
    faithful description of a cover ("a stylized map of Europe, countries
    shaded in yellow, orange and red") is worse than no summary: it reads like
    a summary of the report and is a summary of a picture of the report. The
    quarterlies are the case this pass does own — their PDF is charts, so
    `extract-pdf-text` yields nothing and `pdf_path` is set deliberately by
    `add-pdf-record` for exactly this.
    """
    sql = """
        SELECT p.id, p.slug, p.title, p.subtitle, p.pub_type, p.series,
               p.date_published, p.pdf_url, p.pdf_path
        FROM publications p
        LEFT JOIN primary_enrichment e ON e.publication_id = p.id
        LEFT JOIN publication_text t ON t.publication_id = p.id
        WHERE e.publication_id IS NULL
          AND COALESCE(t.word_count, 0) = 0
          AND p.pub_type <> 'Podcast'
          AND (p.pdf_path IS NULL
               OR EXISTS (SELECT 1 FROM publications c WHERE c.parent_id = p.id))
        ORDER BY p.date_published DESC, p.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def images_for(conn, rec, fetcher):
    """(data_uris, note) for one record — its PDF's pages where it has one,
    otherwise its page's pictures.

    **PDF first**, because a record that has both is a report whose page image
    is its cover, and describing a cover produces something that reads like a
    summary of the report while being a summary of a picture of it.
    """
    if rec["pdf_path"] and Path(rec["pdf_path"]).exists():
        pages, total = pdf_page_images(Path(rec["pdf_path"]))
        note = f"{len(pages)} of {total} PDF pages" if total else f"{len(pages)} PDF pages"
        return [as_data_uri(p) for p in pages], note
    cached = paths.RAW_DIR / f"{rec['slug']}.html"
    if cached.exists():
        urls = page_images(cached.read_text(encoding="utf-8"))
        if urls:
            kept = urls[:MAX_IMAGES]
            note = (f"{len(kept)} of {len(urls)} page images"
                    if len(kept) < len(urls) else f"{len(kept)} page image(s)")
            return fetch_images(fetcher, kept), note
    return [], "no images found"
