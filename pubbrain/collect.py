"""Standing projects and the publications under them (#32).

A dashboard is a living resource — no publication date, updated rather than
published — and its members are ordinary publications that keep their own type
and series. Membership is therefore its own relation, not a column.

Detection reads the **cached HTML**, not `publication_text`: the extractor keeps
prose and drops links, so the very signal this needs does not survive into the
body text. Podcasts carry no body text at all and four of them are members.
"""

import logging

from bs4 import BeautifulSoup

from . import db, paths
from .parser import _main_article

log = logging.getLogger("pubbrain")


def links_to(html: str, path_fragment: str) -> bool:
    """Whether the page's **own article** links to `path_fragment`.

    The in-article test is the whole method. A naive scan for the same URL
    returns 66 pages for the Tech Observatory, of which 60 are a site-wide
    promo block; only 6 genuinely reference it. Teasers and promos reuse the
    article markup, which is the trap Phase-1-Plan documents.
    """
    if path_fragment not in html:
        return False
    article = _main_article(BeautifulSoup(html, "html.parser"))
    if article is None:
        return False
    return any(a.find_parent("article") is article
               for a in article.select(f'a[href*="{path_fragment}"]'))


def detect(conn, publication_id, slug, html) -> bool:
    """Attach one publication to one collection if its article links there."""
    row = conn.execute("SELECT url FROM collections WHERE slug = ?", (slug,)).fetchone()
    if row is None or not row["url"]:
        return False
    fragment = row["url"].replace("https://merics.org", "").rstrip("/")
    if not links_to(html, fragment):
        return False
    return db.add_to_collection(conn, publication_id, slug, source="auto")


PROMO_VIEWS = ("view-id-latest_newsletter",)


def _outside_promo(anchor) -> bool:
    """Whether a link is page content rather than a site-wide promo block (#41).

    The `latest_newsletter` view appears on five of the eight collection pages,
    contributing exactly one link each — which is how a 2026 Brief ended up in
    a 2020 mini-series.
    """
    return not any(cls in (p.get("class") or [])
                   for p in anchor.parents for cls in PROMO_VIEWS)


def from_page(conn, slug, html, source="auto") -> int:
    """Attach everything a collection's own page links to.

    The outbound direction. Inbound detection finds members that link *back*,
    which most do not: a MERICS Series page lists its four comments and one
    interview, and none of them mentions the series. Neither direction
    contains the other, so both are needed (#32).

    Unlike `links_to`, this deliberately does **not** restrict to the page's own
    `<article>`: a series landing page is a Drupal listing whose members are
    teaser cards outside it, and restricting takes all eight collections to
    zero members (#41).
    """
    known = {r["url"].replace("https://merics.org", "").rstrip("/"): r["id"]
             for r in conn.execute("SELECT id, url FROM publications")}
    hrefs = {a.get("href", "").split("?")[0].rstrip("/")
             for a in BeautifulSoup(html, "html.parser").select('a[href^="/en/"]')
             if _outside_promo(a)}
    added = 0
    for href in hrefs:
        pub_id = known.get(href)
        if pub_id and db.add_to_collection(conn, pub_id, slug, source=source):
            added += 1
    return added


def detect_all(conn, publication_ids=None) -> dict:
    """Re-derive auto memberships from the HTML cache. Local, no crawling.

    Runs over every registered collection, so a newly registered one picks up
    its back catalogue without a re-scrape.
    """
    registered = conn.execute(
        "SELECT slug, url FROM collections WHERE url IS NOT NULL").fetchall()
    if not registered:
        return {}
    sql = "SELECT id, slug FROM publications"
    params = []
    if publication_ids:
        sql += f" WHERE id IN ({', '.join('?' * len(publication_ids))})"
        params = list(publication_ids)
    added = {}
    for pub in conn.execute(sql, params).fetchall():
        path = paths.RAW_DIR / f"{pub['slug']}.html"
        if not path.exists():
            continue
        html = path.read_text(encoding="utf-8", errors="ignore")
        for coll in registered:
            fragment = coll["url"].replace("https://merics.org", "").rstrip("/")
            if links_to(html, fragment) and db.add_to_collection(
                    conn, pub["id"], coll["slug"], source="auto"):
                added[coll["slug"]] = added.get(coll["slug"], 0) + 1
    return added
