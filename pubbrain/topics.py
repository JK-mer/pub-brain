"""The controlled topic vocabulary (#4): loader for topics.yaml.

The YAML is the single source of truth — the glossary renders it, enrichment
maps publications onto it. Loaded fresh per call; at 26 entries caching would
buy nothing and cost an edit-restart trap.

`slug` is the identity and what `publication_topics` stores; `name` is only a
label, so renaming a topic never invalidates a mapping.
"""

from pathlib import Path

import yaml

PATH = Path(__file__).parent / "topics.yaml"


def load() -> list:
    """Clusters with their topics, in file order. Raises on a malformed file —
    a broken vocabulary should fail loudly, not render half a glossary."""
    clusters = yaml.safe_load(PATH.read_text(encoding="utf-8"))
    seen = set()
    for cluster in clusters:
        for topic in cluster["topics"]:
            missing = {"slug", "name", "entails", "why"} - topic.keys()
            if missing:
                raise ValueError(f"topic {topic.get('name', '?')!r} lacks {missing}")
            slug = topic["slug"]
            # A duplicate slug would silently merge two topics' publications.
            if slug in seen:
                raise ValueError(f"duplicate topic slug {slug!r}")
            seen.add(slug)
    return clusters


def flat() -> list:
    """Every topic as a dict, cluster order preserved, each carrying `cluster`."""
    return [dict(t, cluster=c["cluster"]) for c in load() for t in c["topics"]]


def names() -> list:
    """Flat topic names, for tests and display."""
    return [t["name"] for t in flat()]


def slugs() -> list:
    """Flat topic slugs — the values a mapping run is validated against (#4)."""
    return [t["slug"] for t in flat()]


def labels() -> dict:
    """slug -> display name, for rendering a stored mapping.

    Callers must tolerate a missing key: a slug retired from the YAML can still
    sit in `publication_topics` until that publication is re-mapped.
    """
    return {t["slug"]: t["name"] for t in flat()}
