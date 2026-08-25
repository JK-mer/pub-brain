"""Who is currently at MERICS, and in what capacity.

The site keeps two up-to-date listings; between them they are the authority on
"current". Job titles alone cannot answer it — "Former Analyst, Stockholm
Centre for Eastern European Studies" is an outsider whose *previous* job was
elsewhere, not a former MERICS analyst.
"""

import logging
import re

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

ROSTER_URLS = {
    "experts": "https://merics.org/en/experts",              # analysts + fellows
    "leadership": "https://merics.org/en/leadership-and-staff",  # management + operations
}

STAFF, AFFILIATE, EXTERNAL, UNKNOWN = "staff", "affiliate", "external", "unknown"

# A fellow or associate is attached to MERICS without being staff.
_AFFILIATE = re.compile(r"\b(fellow|associate)\b", re.I)
# "…, Some Org" or "… at the Some Org" names an employer that is not MERICS.
_OTHER_ORG = re.compile(r",\s+[A-Z]|\bat\s+(?:the\s+)?[A-Z]|\bof\s+the\s+[A-Z]")
_MERICS = re.compile(r"\bMERICS\b", re.I)


def parse_roster(html: str, source: str) -> dict:
    """-> {team slug: {'title': str|None, 'source': str}} for one listing page."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for row in soup.select(".views-row"):
        link = row.select_one('a[href*="/en/team/"]')
        if link is None:
            continue
        slug = link["href"].rstrip("/").rsplit("/", 1)[-1]
        title_el = row.select_one(".field-name-field-job-title")
        title = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else None
        out.setdefault(slug, {"title": title, "source": source})
    return out


def names_another_employer(job_title: str) -> bool:
    """True when the title credits an organisation other than MERICS."""
    return bool(_OTHER_ORG.search(_MERICS.sub("", job_title)))


def classify(on_roster: bool, roster_title, job_title, has_team_page: bool,
             flagged_external: bool):
    """-> (affiliation, is_current).

    The roster settles current-vs-former; the title settles staff-vs-affiliate.
    """
    if on_roster:
        title = roster_title or job_title or ""
        return (AFFILIATE if _AFFILIATE.search(title) else STAFF), True
    if flagged_external or not has_team_page:
        return EXTERNAL, False
    if not job_title:
        return UNKNOWN, False
    if names_another_employer(job_title):
        return EXTERNAL, False
    return (AFFILIATE if _AFFILIATE.search(job_title) else STAFF), False
