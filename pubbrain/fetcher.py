"""Polite HTTP client. It is the owner's employer's site — when in doubt, slower."""

import logging
import re
import time
from urllib.parse import urlsplit

import requests

from . import paths

USER_AGENT = "pub-brain/0.1 (personal MERICS publications catalog)"
MIN_INTERVAL = 2.0  # seconds between requests
MAX_RETRIES = 4

log = logging.getLogger(__name__)


class Fetcher:
    def __init__(self, min_interval=MIN_INTERVAL, cache_dir=None):
        self.min_interval = min_interval
        self.cache_dir = cache_dir if cache_dir is not None else paths.RAW_DIR
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def _wait(self) -> None:
        gap = time.monotonic() - self._last_request
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)

    def get(self, url: str) -> requests.Response:
        """GET with rate limiting and backoff. Raises on final failure."""
        delay = 5.0
        for attempt in range(1, MAX_RETRIES + 1):
            self._wait()
            try:
                resp = self.session.get(url, timeout=30)
                self._last_request = time.monotonic()
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                resp.raise_for_status()
                resp.encoding = resp.encoding or "utf-8"
                return resp
            except requests.RequestException as exc:
                self._last_request = time.monotonic()
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and 400 <= status < 500 and status != 429:
                    raise
                if attempt == MAX_RETRIES:
                    raise
                log.warning("%s on %s — retry %d/%d in %.0fs", exc, url, attempt, MAX_RETRIES, delay)
                time.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    def get_page(self, url: str, slug: str):
        """Fetch and cache to data/raw/<slug>.html so Phase 2 need not re-crawl.

        Returns (html, final_url). Stale sitemap URLs soft-404 by redirecting to
        a listing instead of returning 404, so a page that landed somewhere else
        is not cached — it would file another node's HTML under this slug.
        """
        resp = self.get(url)
        html = resp.text
        if same_page(url, resp.url) and self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{slug}.html").write_text(html, encoding="utf-8")
        return html, resp.url


def same_page(requested: str, final: str) -> bool:
    """Ignore scheme, trailing slash and the /index.php path variant."""
    def key(u):
        parts = urlsplit(u)
        path = re.sub(r"^/index(%2e|\.)php", "", parts.path, flags=re.IGNORECASE)
        return (parts.netloc.removeprefix("www."), path.rstrip("/"))

    return key(requested) == key(final)
