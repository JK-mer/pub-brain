"""Body text from the cached HTML.

Reads `data/raw/`, never the network — the backfill already fetched every page.
Headings and lists are kept as light Markdown so chunking for embeddings can
split on structure instead of guessing.
"""

import logging

from bs4 import BeautifulSoup, NavigableString

from .parser import _fields, _main_article

log = logging.getLogger(__name__)

# Rendered inside the body but not part of the publication's prose.
_DROP_SELECTORS = (
    ".field-name-field-authors",
    ".field-name-field-copyright",
    ".field-name-field-download",
    ".field-name-field-media-file",
    ".field-name-field-tags",
    "figcaption",
    "script",
    "style",
)


class NoBodyText(Exception):
    """The page carries no prose — podcasts and paywalled items."""


def _block_text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split())


def extract(html: str) -> dict:
    """-> {'text': str, 'word_count': int}. Raises NoBodyText when there is none."""
    soup = BeautifulSoup(html, "html.parser")
    article = _main_article(soup)
    if article is None:
        raise NoBodyText("no main article")

    blocks = _fields(article, "content")
    if not blocks:
        raise NoBodyText("no content field")
    body = blocks[0]

    for selector in _DROP_SELECTORS:
        for junk in body.select(selector):
            junk.decompose()

    lines = to_markdown(body)
    if not lines:  # prose with no block markup at all
        text = _block_text(body)
        if not text:
            raise NoBodyText("empty content field")
        lines = [text]

    text = "\n\n".join(lines)
    return {"text": text, "word_count": len(text.split())}


def to_markdown(body, keep_links=False) -> list:
    """Block elements as light Markdown lines. Shared by the scraper and by
    pasted-HTML conversion (#31), so a hand-pasted digest sections exactly like
    a scraped one — `sections.split` keys on these `##` headings.
    """
    lines = []
    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote"]):
        # A <p> inside an <li> is already covered by the <li> line.
        if el.find_parent(["li", "blockquote"]) is not None:
            continue
        text = _linked_text(el) if keep_links else _block_text(el)
        if not text:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            # h1 is the page title when pasting a whole article; treat it as a
            # top-level heading rather than dropping the piece's own name.
            lines.append(f"{'#' * max(int(el.name[1]), 2)} {text}")
        elif el.name == "li":
            lines.append(f"- {text}")
        elif el.name == "blockquote":
            lines.append(f"> {text}")
        else:
            lines.append(text)
    return lines


def _linked_text(el) -> str:
    """Block text with anchors kept as Markdown links.

    Scraped text drops links, and that loss cost real information — dashboard
    membership had to be recovered from the raw HTML cache because no URL
    survives in `publication_text` (#32). Pasted text is often the only copy
    there will ever be, so its links are worth keeping.
    """
    parts = []
    for node in el.descendants:
        if isinstance(node, NavigableString):
            if node.find_parent("a") is None:
                parts.append(str(node))
        elif node.name == "a":
            label = " ".join(node.get_text(" ", strip=True).split())
            href = (node.get("href") or "").strip()
            if not label:
                continue
            parts.append(f"[{label}]({href})" if href.startswith("http") else label)
    return " ".join(" ".join(parts).split())


def from_fragment(html: str, keep_links=True) -> dict:
    """Convert a clipboard HTML fragment to the same Markdown shape (#31).

    A browser puts both `text/plain` and `text/html` on the clipboard; a
    textarea only ever receives the plain flavour, which is why pasting from
    the site arrives with headings and links already stripped. This takes the
    HTML flavour instead.
    """
    soup = BeautifulSoup(html, "html.parser")
    for selector in (*_DROP_SELECTORS, "nav", "header", "footer", "aside"):
        for junk in soup.select(selector):
            junk.decompose()
    lines = to_markdown(soup, keep_links=keep_links)
    if not lines:
        text = _block_text(soup)
        if not text:
            raise NoBodyText("nothing usable in the pasted HTML")
        lines = [text]
    text = "\n\n".join(lines)
    return {"text": text, "word_count": len(text.split())}
