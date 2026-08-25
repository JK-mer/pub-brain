"""Sitemap parsing and scope classification.

https://merics.org/en/sitemap.xml is one flat file covering the full archive;
the content type is encoded in the URL path prefix.
"""

import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

SITEMAP_URL = "https://merics.org/en/sitemap.xml"
_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# URL prefixes that are publications.
PUBLICATION_PREFIXES = {
    "report", "comment", "merics-briefs", "podcast", "tracker", "briefing",
    "interview", "external-publication", "analysis", "executive-memo",
    "short-analysis",
}


def normalize_path(url_or_path: str) -> str:
    """Path without the /index.php variant some in-page links carry."""
    path = urlsplit(url_or_path).path
    return re.sub(r"^/index(%2e|\.)php", "", path, flags=re.IGNORECASE)


def classify(url: str):
    """-> (scope, path_prefix). Scope is 'publication', 'root-level' or 'excluded'.

    Root-level legacy URLs carry no type prefix and — unlike what Phase-1-Plan
    assumed — no publication-type field either, so nothing on the page settles
    whether they are publications. They are recorded but not scraped; see #3.
    """
    if urlsplit(url).netloc not in ("merics.org", "www.merics.org"):
        return "excluded", ""
    path = normalize_path(url).strip("/")
    if not path.startswith("en/"):
        return "excluded", ""
    rest = path[len("en/"):]
    if not rest:
        return "excluded", ""
    head, _, tail = rest.partition("/")
    if not tail:
        return "root-level", ""
    if head in PUBLICATION_PREFIXES:
        return "publication", head
    return "excluded", head


def slug_for(url: str) -> str:
    return normalize_path(url).strip("/").rsplit("/", 1)[-1]


def parse(xml_text: str):
    """Yield (url, lastmod) for every entry in the sitemap."""
    root = ET.fromstring(xml_text)
    for url_el in root.findall("sm:url", _NS):
        loc = url_el.findtext("sm:loc", namespaces=_NS)
        if loc:
            yield loc.strip(), (url_el.findtext("sm:lastmod", namespaces=_NS) or None)
