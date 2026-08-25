"""Publication page -> record.

The pages are server-rendered Drupal with stable `field-name-field-*` divs.
The trap: the same field classes appear again in related-content teasers and
in footer promo blocks, so every field is read only from the *main* article —
an element counts only when its nearest ancestor <article> is that one.
"""

import logging
import re
from datetime import datetime

from bs4 import BeautifulSoup

from .sitemap import normalize_path, slug_for

log = logging.getLogger(__name__)


class NotAPublication(Exception):
    """Page has no publication-type field — see Phase-1-Plan scope rules."""


def _main_article(soup):
    """The article rendering this page's own node.

    `view-mode-full` marks a normally rendered node; member-only items render
    `view-mode-paywall` instead and carry no body.
    """
    for view_mode in ("div.view-mode-full", "div.view-mode-paywall"):
        el = soup.select_one(view_mode)
        if el is not None:
            article = el.find_parent("article")
            if article is not None:
                return article
    h1 = soup.find("h1")
    return h1.find_parent("article") if h1 else None


def _fields(article, name):
    """Field divs belonging to the main article itself, not to nested teasers."""
    return [
        el for el in article.select(f".field-name-field-{name}")
        if el.find_parent("article") is article
    ]


def _text(el):
    if el is None:
        return None
    return " ".join(el.get_text(" ", strip=True).split()) or None


def _parse_date(field):
    """ISO date. The <time datetime> attribute is authoritative; text is a fallback."""
    if field is None:
        return None
    time_el = field.find("time")
    if time_el and time_el.get("datetime"):
        raw = time_el["datetime"].replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw).date().isoformat()
        except ValueError:
            pass
    text = _text(field)
    for fmt in ("%b %d, %Y", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except (ValueError, TypeError):
            continue
    log.warning("unparseable date %r", text)
    return None


def _parse_type(field):
    """-> (pub_type, series).

    The field holds one or two taxonomy terms: the type ("Tracker") and,
    optionally, the series it belongs to ("China Economic Indicators").
    """
    terms = [_text(t) for t in field.select(".taxonomy-term")]
    terms = [t for t in terms if t]
    if not terms:
        whole = _text(field)
        return (whole, None) if whole else (None, None)
    return terms[0], (" / ".join(terms[1:]) or None)


def _parse_people(field, role):
    """Ordered people in one field. A linked /team/ page gives the slug.

    Each person renders as a nested team-member article carrying its own
    field-items, so only the field's own top-level items are one-per-person —
    iterating every descendant turns each job title into a spurious person.
    """
    container = field.find(class_="field-items")
    if container is None:
        return []
    people, seen = [], set()
    for item in container.find_all(class_="field-item", recursive=False):
        link = item.select_one('a[href*="/team/"]')
        job_title = _text(item.select_one(".field-name-field-job-title"))
        if link is not None:
            name = _text(link)
            slug = normalize_path(link["href"]).strip("/").rsplit("/", 1)[-1] or None
        else:
            title = item.select_one(".node-title")
            if title is None:
                title = item.__copy__()
                for junk in title.select(".field-name-field-job-title"):
                    junk.decompose()
            name, slug = _text(title), None
        if not name or (slug or name) in seen:
            continue
        seen.add(slug or name)
        people.append({"name": name, "slug": slug, "job_title": job_title, "role": role})
    return people


def _own_element(el, article) -> bool:
    """Whether `el` belongs to this page's own article rather than a teaser.

    Not simply `find_parent("article") is article`: Drupal wraps every attached
    media file in its own `<article class="media media--type-file">`, so the
    nearest article ancestor of a download link is that wrapper, and the strict
    test rejected the publication's own PDF. Media wrappers are part of this
    page; any other nested article is a teaser for a different publication.
    """
    for parent in el.parents:
        if parent is article:
            return True
        if parent.name == "article":
            classes = parent.get("class") or []
            if not any(c.startswith("media") for c in classes):
                return False
    return False


def _parse_pdf(article):
    """The publication's own PDF, not the ones its footnotes link to."""
    for download in _fields(article, "download") + _fields(article, "media-file"):
        for a in download.select("a[href]"):
            if ".pdf" in a["href"].lower():
                return a["href"]
    for a in article.select('a[href*="/sites/default/files/"]'):
        if _own_element(a, article) and ".pdf" in a["href"].lower():
            return a["href"]
    return None


def _absolute(href, base="https://merics.org"):
    """Absolute URL for a page-relative href.

    Some hrefs wrap an absolute URL with a percent-encoded scheme inside a
    relative path: `/index%2Ephp/https%3A//merics.org/sites/...`. Stripping the
    index.php leaves `/https%3A//...`, and prepending the base then produced
    `https://merics.org/https%3A//merics.org/...`, which 404s — three PDFs were
    stored that way. The check runs *after* normalisation, because that is when
    the encoded scheme surfaces. Only the scheme is decoded; unquoting the
    whole URL would turn %20 into spaces and break it.
    """
    if not href:
        return None
    if href.startswith(("http://", "https://")):
        return href
    path = normalize_path(href)
    embedded = re.match(r"/?(https?)%3[Aa](//.*)$", path)
    if embedded:
        return f"{embedded.group(1)}:{embedded.group(2)}"
    return base + path

def parse_publication(html: str, url: str, pub_type=None, series=None) -> dict:
    """Parse one publication page. Raises NotAPublication if it isn't one.

    `pub_type` overrides the page's own field, for the root-level pages that
    state no type at all (#10). **Only ever passed by hand**: type is read from
    the page and never inferred — the URL prefix does not imply it in either
    direction — so an override is a decision someone made, not a guess the
    scraper is entitled to.
    """
    soup = BeautifulSoup(html, "html.parser")
    article = _main_article(soup)
    if article is None:
        raise NotAPublication(f"no main article on {url}")

    type_fields = _fields(article, "publication-type")
    if type_fields:
        parsed_type, parsed_series = _parse_type(type_fields[0])
        pub_type = pub_type or parsed_type
        series = series or parsed_series
    if not pub_type:
        raise NotAPublication(f"no publication-type field on {url}")

    h1 = article.find("h1")
    og_title = soup.find("meta", property="og:title")
    title = _text(h1) or (og_title.get("content").strip() if og_title else None)
    if not title:
        raise NotAPublication(f"no title on {url}")

    date_fields = _fields(article, "date-published")
    subtitle_fields = _fields(article, "subtitle")
    tag_fields = _fields(article, "tags")

    # Podcasts credit nobody as an author; they name a host and guests instead.
    # `guest-team` is the MERICS-affiliated subset of `guests`, so it is the
    # site's own answer to who is internal.
    people, guest_team = [], set()
    for field_name, role in (("authors", "author"), ("host", "host"), ("guests", "guest")):
        fields = _fields(article, field_name)
        if fields:
            people.extend(_parse_people(fields[0], role))
    for field in _fields(article, "guest-team"):
        for person in _parse_people(field, "guest"):
            if person["slug"]:
                guest_team.add(person["slug"])
    for person in people:
        person["is_internal"] = bool(
            person["slug"] and (person["role"] != "guest" or person["slug"] in guest_team)
        )

    tags, seen = [], set()
    for field in tag_fields:
        for a in field.select("a"):
            name = _text(a)
            if name and name not in seen:
                seen.add(name)
                tags.append(name)

    og_desc = soup.find("meta", property="og:description")
    is_paywalled = (
        soup.select_one("div.view-mode-paywall") is not None
        or "lock-icon" in (article.get("class") or [])
    )

    return {
        "slug": slug_for(url),
        "url": url,
        "title": title,
        "subtitle": _text(subtitle_fields[0]) if subtitle_fields else None,
        "date_published": _parse_date(date_fields[0] if date_fields else None),
        "pub_type": pub_type,
        "series": series,
        "access": "member" if is_paywalled else "public",
        "pdf_url": _absolute(_parse_pdf(article)),
        "og_description": ((og_desc.get("content") or "").strip() or None) if og_desc else None,
        "people": people,
        "site_tags": tags,
    }
