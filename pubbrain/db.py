"""SQLite catalog: schema, migrations, upserts.

Schema per wiki Phase-1-Plan. Enrichment (Layer 2) columns are deliberately
absent — they arrive in Phase 2 rather than being guessed now.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone

from . import paths, topics

MIGRATIONS = [
    # 1 — Phase 1 catalog
    """
    CREATE TABLE publications (
      id INTEGER PRIMARY KEY,
      slug TEXT UNIQUE NOT NULL,
      url TEXT UNIQUE NOT NULL,
      title TEXT NOT NULL,
      subtitle TEXT,
      date_published TEXT,
      pub_type TEXT NOT NULL,
      series TEXT,
      is_flagship INTEGER NOT NULL DEFAULT 0,
      access TEXT NOT NULL DEFAULT 'public',
      pdf_url TEXT,
      pdf_path TEXT,
      og_description TEXT,
      sitemap_lastmod TEXT,
      scraped_at TEXT NOT NULL
    );
    CREATE INDEX idx_publications_date ON publications(date_published);
    CREATE INDEX idx_publications_type ON publications(pub_type);

    CREATE TABLE authors (
      id INTEGER PRIMARY KEY,
      slug TEXT UNIQUE,
      name TEXT NOT NULL,
      is_internal INTEGER NOT NULL DEFAULT 1
    );
    CREATE UNIQUE INDEX idx_authors_external_name
      ON authors(name) WHERE slug IS NULL;

    CREATE TABLE publication_authors (
      publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
      author_id INTEGER NOT NULL REFERENCES authors(id),
      position INTEGER NOT NULL,
      PRIMARY KEY (publication_id, author_id)
    );

    CREATE TABLE site_tags (
      id INTEGER PRIMARY KEY,
      name TEXT UNIQUE NOT NULL
    );
    CREATE TABLE publication_site_tags (
      publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
      tag_id INTEGER NOT NULL REFERENCES site_tags(id),
      PRIMARY KEY (publication_id, tag_id)
    );

    -- Sitemap worklist. Drives both the backfill and, later, the weekly job:
    -- a changed lastmod is what marks a page for re-fetch.
    CREATE TABLE sitemap_urls (
      url TEXT PRIMARY KEY,
      path_prefix TEXT NOT NULL,
      lastmod TEXT,
      scope TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      scraped_lastmod TEXT,
      last_error TEXT,
      first_seen TEXT NOT NULL,
      last_seen TEXT NOT NULL
    );
    CREATE INDEX idx_sitemap_status ON sitemap_urls(scope, status);
    """,
    # 2 — Phase 2: body text, extracted from the cached HTML
    """
    CREATE TABLE publication_text (
      publication_id INTEGER PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE,
      body TEXT NOT NULL,
      word_count INTEGER NOT NULL,
      source TEXT NOT NULL,          -- 'html' now; 'pdf' once #6 downloads them
      extracted_at TEXT NOT NULL
    );
    """,
    # 3 — people, not just authors: podcasts credit hosts and guests in their
    # own fields, and job title is what actually separates staff from visitors.
    """
    ALTER TABLE authors RENAME TO people;
    ALTER TABLE people ADD COLUMN job_title TEXT;

    CREATE TABLE publication_people (
      publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
      person_id INTEGER NOT NULL REFERENCES people(id),
      role TEXT NOT NULL,               -- 'author' | 'host' | 'guest'
      position INTEGER NOT NULL,
      PRIMARY KEY (publication_id, person_id, role)
    );
    INSERT INTO publication_people (publication_id, person_id, role, position)
      SELECT publication_id, author_id, 'author', position FROM publication_authors;
    DROP TABLE publication_authors;
    CREATE INDEX idx_pubpeople_person ON publication_people(person_id, role);
    """,
    # 4 — staff/affiliate/external and current/former, settled by the site's own
    # Experts and Leadership listings rather than by reading "Former" in a title.
    """
    ALTER TABLE people ADD COLUMN affiliation TEXT;   -- staff|affiliate|external|unknown
    ALTER TABLE people ADD COLUMN is_current INTEGER; -- 1 = on a current roster
    ALTER TABLE people ADD COLUMN roster_title TEXT;  -- title as the roster lists it
    ALTER TABLE people ADD COLUMN roster_source TEXT; -- 'experts' | 'leadership'
    """,
    # 5 — FTS5 keyword index. An ordinary table, not external-content: the source
    # spans publications + publication_text, so one rebuild command is simpler
    # and safer than keeping four sets of triggers in sync. Populated here so a
    # migrated database is never silently unsearchable.
    """
    CREATE VIRTUAL TABLE publication_fts USING fts5(
      title, subtitle, description, body,
      tokenize = "porter unicode61 remove_diacritics 2"
    );
    INSERT INTO publication_fts (rowid, title, subtitle, description, body)
      SELECT p.id, p.title, p.subtitle, p.og_description, t.body
      FROM publications p LEFT JOIN publication_text t ON t.publication_id = p.id;
    """,
    # 6 — merics.org puts the whole article in og:description (#15), so the
    # index held a second copy of every body, weighted above the original.
    # Repopulate with the description only where there is no body to duplicate.
    """
    DELETE FROM publication_fts;
    INSERT INTO publication_fts (rowid, title, subtitle, description, body)
      SELECT p.id, p.title, p.subtitle,
             CASE WHEN t.body IS NULL THEN p.og_description END,
             t.body
      FROM publications p LEFT JOIN publication_text t ON t.publication_id = p.id;
    """,
    # 7 — LLM enrichment (#5). model/provider/prompt_version travel with the
    # output so a later run can replace rows selectively: the one-liner is the
    # recall unit the whole tool rests on, and which model wrote it matters.
    """
    CREATE TABLE publication_enrichment (
      publication_id INTEGER PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE,
      summary_one_liner TEXT NOT NULL,
      summary_short TEXT NOT NULL,
      key_findings TEXT NOT NULL,     -- JSON array of strings
      entities TEXT NOT NULL,         -- JSON object of name lists
      model TEXT NOT NULL,
      provider TEXT NOT NULL,
      prompt_version INTEGER NOT NULL,
      words_sent INTEGER NOT NULL,
      prompt_tokens INTEGER,
      completion_tokens INTEGER,
      attempts INTEGER NOT NULL DEFAULT 1,
      seconds REAL,
      enriched_at TEXT NOT NULL
    );
    CREATE INDEX idx_enrichment_model ON publication_enrichment(model, prompt_version);
    """,
    # 8 — sections (#16). A digest is several unrelated stories in one record,
    # so a publication-level hit cannot say which part matched. Its own FTS
    # table because publication_fts keys rowid to publications.id.
    """
    CREATE TABLE publication_sections (
      id INTEGER PRIMARY KEY,
      publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
      position INTEGER NOT NULL,
      heading TEXT,                  -- NULL for text before the first heading
      level INTEGER,                 -- markdown depth; NULL for that preamble
      body TEXT NOT NULL,
      word_count INTEGER NOT NULL,
      independent INTEGER NOT NULL DEFAULT 0,  -- a story of its own, not part of one argument
      UNIQUE (publication_id, position)
    );
    CREATE INDEX idx_sections_pub ON publication_sections(publication_id);

    CREATE VIRTUAL TABLE section_fts USING fts5(
      heading, body,
      tokenize = "porter unicode61 remove_diacritics 2"
    );
    """,
    # 9 — standing features (METRIX, MERICS CHINA DIGEST) are a recurring
    # furniture slot, not a story. Kept and searchable, but flagged so they can
    # be excluded from embeddings (#17), where a link list is pure noise.
    """
    ALTER TABLE publication_sections
      ADD COLUMN is_boilerplate INTEGER NOT NULL DEFAULT 0;
    """,
    # 10 — embeddings (#17). source_type keys what was embedded, so a section
    # index and a one-liner index coexist and can be compared rather than one
    # overwriting the other. Vectors are stored normalised, making cosine
    # similarity a plain dot product at query time.
    """
    CREATE TABLE embeddings (
      id INTEGER PRIMARY KEY,
      source_type TEXT NOT NULL,        -- 'section' | 'one_liner'
      source_id INTEGER NOT NULL,       -- section id, or publication id
      publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
      model TEXT NOT NULL,
      dim INTEGER NOT NULL,
      vector BLOB NOT NULL,             -- float32, L2-normalised
      embedded_at TEXT NOT NULL,
      UNIQUE (source_type, source_id, model)
    );
    CREATE INDEX idx_embeddings_index ON embeddings(source_type, model);
    """,
    # 11 — reviewer verdicts (#19). Generic on purpose: #18's summary review is
    # the first scope, not the only one. One verdict per subject — re-reviewing
    # replaces. A verdict describes the row as it was when reviewed; anything
    # that regenerates the subject invalidates it.
    """
    CREATE TABLE reviews (
      id INTEGER PRIMARY KEY,
      scope TEXT NOT NULL,
      subject_id INTEGER NOT NULL,
      verdict TEXT NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL,
      UNIQUE (scope, subject_id)
    );
    """,
    # 12 — controlled-vocabulary topics (#4). Stores the topic's *slug*, not its
    # display name: the label lives in topics.yaml and renaming it there must
    # not orphan a mapping. No foreign key — the vocabulary is a file, not a
    # table, so validation happens at write time against `topics.slugs()`.
    """
    CREATE TABLE publication_topics (
      publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
      topic_slug TEXT NOT NULL,
      position INTEGER NOT NULL,     -- the model's own ordering; 1 = most central
      model TEXT NOT NULL,
      prompt_version INTEGER NOT NULL,
      mapped_at TEXT NOT NULL,
      PRIMARY KEY (publication_id, topic_slug)
    );
    CREATE INDEX idx_pubtopics_topic ON publication_topics(topic_slug, position);
    """,
    # 13 — several summaries per publication, exactly one primary (#18). Only
    # the primary is used by search, embeddings, counts and every list; the
    # others exist to be compared against it and then promoted or dismissed.
    #
    # `publication_id` stops being unique, so every join that wants "the
    # summary" and forgets to say which one silently returns a row per
    # candidate — no error, just doubled counts and duplicated search hits.
    # The `primary_enrichment` view below exists so that the safe reading is
    # the default one: reads go through the view, and only code that
    # deliberately manages candidates touches the base table.
    """
    CREATE TABLE enrichment_new (
      id INTEGER PRIMARY KEY,
      publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
      is_primary INTEGER NOT NULL DEFAULT 0,
      summary_one_liner TEXT NOT NULL,
      summary_short TEXT NOT NULL,
      key_findings TEXT NOT NULL,
      entities TEXT NOT NULL,
      model TEXT NOT NULL,
      provider TEXT NOT NULL,
      prompt_version INTEGER NOT NULL,
      words_sent INTEGER NOT NULL,
      prompt_tokens INTEGER,
      completion_tokens INTEGER,
      attempts INTEGER NOT NULL DEFAULT 1,
      seconds REAL,
      enriched_at TEXT NOT NULL
    );
    -- Every existing row becomes its publication's primary. Without this the
    -- whole catalog loses its summaries at once: the view would be empty.
    INSERT INTO enrichment_new
      (publication_id, is_primary, summary_one_liner, summary_short, key_findings,
       entities, model, provider, prompt_version, words_sent, prompt_tokens,
       completion_tokens, attempts, seconds, enriched_at)
      SELECT publication_id, 1, summary_one_liner, summary_short, key_findings,
             entities, model, provider, prompt_version, words_sent, prompt_tokens,
             completion_tokens, attempts, seconds, enriched_at
      FROM publication_enrichment;

    -- A verdict describes one summary, so it has to point at one. Rebuilt
    -- rather than UPDATEd: remapping in place transiently collides with
    -- UNIQUE(scope, subject_id), because an enrichment id can equal a
    -- publication id that has not been remapped yet.
    CREATE TABLE reviews_new (
      id INTEGER PRIMARY KEY,
      scope TEXT NOT NULL,
      subject_id INTEGER NOT NULL,
      verdict TEXT NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL,
      UNIQUE (scope, subject_id)
    );
    INSERT INTO reviews_new (scope, subject_id, verdict, note, created_at)
      SELECT r.scope,
             CASE WHEN r.scope = 'enrichment' THEN e.id ELSE r.subject_id END,
             r.verdict, r.note, r.created_at
      FROM reviews r
      LEFT JOIN enrichment_new e
        ON r.scope = 'enrichment' AND e.publication_id = r.subject_id
      WHERE r.scope <> 'enrichment' OR e.id IS NOT NULL;

    DROP TABLE reviews;
    ALTER TABLE reviews_new RENAME TO reviews;
    DROP TABLE publication_enrichment;
    ALTER TABLE enrichment_new RENAME TO publication_enrichment;

    -- The invariant, enforced by SQLite rather than by discipline: a second
    -- primary is rejected with UNIQUE constraint failed.
    CREATE UNIQUE INDEX one_primary_per_publication
      ON publication_enrichment(publication_id) WHERE is_primary = 1;
    CREATE INDEX idx_enrichment_model ON publication_enrichment(model, prompt_version);
    CREATE INDEX idx_enrichment_pub ON publication_enrichment(publication_id);

    CREATE VIEW primary_enrichment AS
      SELECT * FROM publication_enrichment WHERE is_primary = 1;
    """,
    # 14 — the shortlist (#25): the first human signal in the catalog.
    # Everything else here is scraped or generated; this is the owner's own
    # judgement that a piece mattered, and it has to outlive any summary
    # rewrite — hence its own table rather than a `reviews` scope, since a
    # verdict describes a generated row and dies with it (#18).
    """
    CREATE TABLE shortlist (
      publication_id INTEGER PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE,
      note TEXT,
      added_at TEXT NOT NULL
    );
    """,
    # 15 — the first persisted preferences (#30). A key/value table rather than
    # columns: these are UI choices, not catalog data, and inventing a schema
    # for each one would mean a migration per preference.
    """
    CREATE TABLE settings (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,          -- JSON, so a list stays a list
      updated_at TEXT NOT NULL
    );
    """,
    # 16 — standing projects and the publications under them (#32). A dashboard
    # is a living resource with no publication date; its members are ordinary
    # publications that also belong to their own series, so this is a
    # many-to-many relation and NOT the `series` column: two of the
    # China-Russia members are MERICS China Essentials Briefs and one is a
    # Security and Risk Tracker.
    """
    CREATE TABLE collections (
      slug TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      url TEXT,                     -- its page on merics.org; drives detection
      blurb TEXT,
      created_at TEXT NOT NULL
    );

    CREATE TABLE publication_collections (
      publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
      collection_slug TEXT NOT NULL REFERENCES collections(slug) ON DELETE CASCADE,
      source TEXT NOT NULL,         -- 'auto' (detected) | 'manual' (curated)
      added_at TEXT NOT NULL,
      PRIMARY KEY (publication_id, collection_slug)
    );
    CREATE INDEX idx_pubcoll_collection ON publication_collections(collection_slug);
    """,
    # 17 — resolving the root-level URLs in the workbench rather than in chat
    # (#10, #33). `probe_*` are measured from the live page; `disposition` is
    # the owner's decision; `note` is an instruction to whoever executes it.
    """
    ALTER TABLE sitemap_urls ADD COLUMN probe_words INTEGER;
    ALTER TABLE sitemap_urls ADD COLUMN probe_has_date INTEGER;
    ALTER TABLE sitemap_urls ADD COLUMN probe_title TEXT;
    ALTER TABLE sitemap_urls ADD COLUMN disposition TEXT;  -- ingest|exclude|todo
    ALTER TABLE sitemap_urls ADD COLUMN note TEXT;
    """,
    # 18 — a confirmed disposition leaves the list; a proposal does not (#33).
    """
    ALTER TABLE sitemap_urls ADD COLUMN settled_at TEXT;
    """,
    # 19 — the subtitle names the series on the pages that predate the typed
    # URLs: "China Update 1/2019" is the run that became MERICS China
    # Essentials (#10). It gives both the type and the series, which is what
    # "ingest as what?" was missing.
    """
    ALTER TABLE sitemap_urls ADD COLUMN probe_subtitle TEXT;
    ALTER TABLE sitemap_urls ADD COLUMN proposed_type TEXT;
    ALTER TABLE sitemap_urls ADD COLUMN proposed_series TEXT;
    """,
    # 20 — windows of a section too long to embed whole (#34). NULL on an
    # authored section, so the workbench can tell a real heading-bounded
    # section from a slice the pipeline cut, and a reader is never shown a
    # fragment as if the author had written it that way.
    """
    ALTER TABLE publication_sections ADD COLUMN chunk_index INTEGER;
    ALTER TABLE publication_sections ADD COLUMN chunk_total INTEGER;
    """,
    # 21 — a report and its chapters (#36). Ordered and hierarchical, which is
    # what `publication_collections` (#32) deliberately is not: a dashboard has
    # members, a report has a table of contents. Owner decision 2026-08-09 —
    # the report appears once carrying its executive summary, the chapters hang
    # off it with their own text, summary and vectors, searchable in their own
    # right but not listed beside the parent.
    """
    ALTER TABLE publications ADD COLUMN parent_id INTEGER
      REFERENCES publications(id) ON DELETE SET NULL;
    ALTER TABLE publications ADD COLUMN parent_position INTEGER;
    CREATE INDEX idx_publications_parent
      ON publications(parent_id, parent_position);
    """,
    # 22 — hand-assigned credits (#40). `upsert_publication` rewrites this table
    # from the cached HTML on every re-parse, so without a provenance column a
    # credit entered by hand survives only until the next routine run. Mirrors
    # `publication_collections.source`, which solves the same problem.
    """
    ALTER TABLE publication_people ADD COLUMN source TEXT NOT NULL
      DEFAULT 'parsed';
    """,
    # 23 — the plain-language blurb (#38). Its own table, not a column on
    # `publication_enrichment`: a blurb has its own model and prompt version,
    # and the enrichment table already means "one reading of the piece, of
    # which one is primary" — a blurb is not a competing reading. One per
    # publication, regenerable. `source_enrichment_id` is what it was built
    # from, so promoting a different summary makes the blurb visibly stale
    # rather than quietly wrong.
    """
    CREATE TABLE publication_blurbs (
      publication_id INTEGER PRIMARY KEY
        REFERENCES publications(id) ON DELETE CASCADE,
      blurb TEXT NOT NULL,
      source_enrichment_id INTEGER
        REFERENCES publication_enrichment(id) ON DELETE SET NULL,
      model TEXT,
      provider TEXT,
      prompt_version INTEGER,
      prompt_tokens INTEGER,
      completion_tokens INTEGER,
      seconds REAL,
      created_at TEXT NOT NULL
    );
    """,
    # 24 — merged duplicate people (#47). merics.org itself carries two team
    # nodes for some people, and `_person_id` inserts on an unknown slug — so a
    # deleted duplicate is re-created by the next reparse and the credits
    # re-split. The duplicate therefore stays as a tombstone whose slug
    # redirects to the survivor inside `_person_id`.
    """
    ALTER TABLE people ADD COLUMN merged_into INTEGER REFERENCES people(id);
    """,
    # 25 — the embedding landscape (#49). Coordinates are cached because the
    # layout must be identical on every visit: spatial memory is the feature.
    # `placed` records provenance — 'fit' from a full t-SNE run, 'incremental'
    # for a point slotted in beside its neighbours without moving anyone.
    """
    CREATE TABLE landscape_coords (
      publication_id INTEGER PRIMARY KEY
        REFERENCES publications(id) ON DELETE CASCADE,
      x REAL NOT NULL,
      y REAL NOT NULL,
      placed TEXT NOT NULL,
      computed_at TEXT NOT NULL
    );
    """,
    # 26 — the upcoming layer (#56): hand-entered notes on what is coming.
    # Its own tables, not a `publications` row, so no existing query can pick
    # them up by accident — the recurring failure here is a base table that
    # quietly holds more than it says. Nothing inherits; a surface that wants
    # notes names `upcoming_*`.
    #
    # Status is derived, never stored (landed / shelved / expected); the CHECK
    # states the one impossible combination. `expected` is a quarter because
    # the knowledge is "roughly Q4" — a month would fabricate precision, and
    # 'YYYY-Qn' sorts lexicographically within and across years.
    """
    CREATE TABLE upcoming_notes (
      id                     INTEGER PRIMARY KEY,
      working_title          TEXT NOT NULL,
      note                   TEXT,
      expected               TEXT,
      created_at             TEXT NOT NULL,
      updated_at             TEXT NOT NULL,
      landed_publication_id  INTEGER REFERENCES publications(id) ON DELETE SET NULL,
      landed_at              TEXT,
      shelved_at             TEXT,
      shelved_reason         TEXT,
      CHECK (landed_publication_id IS NULL OR shelved_at IS NULL)
    );
    CREATE TABLE upcoming_topics (
      note_id    INTEGER NOT NULL REFERENCES upcoming_notes(id) ON DELETE CASCADE,
      topic_slug TEXT NOT NULL,
      position   INTEGER NOT NULL,
      PRIMARY KEY (note_id, topic_slug)
    );
    CREATE TABLE upcoming_people (
      note_id   INTEGER NOT NULL REFERENCES upcoming_notes(id) ON DELETE CASCADE,
      person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
      PRIMARY KEY (note_id, person_id)
    );
    """,
]

# heading, body — a hit in a section's own headline is a stronger signal than
# one in its prose, for the same reason a title beats a body.
SECTION_FTS_WEIGHTS = (8.0, 1.0)

# title, subtitle, description, body — a title hit beats a body hit. The
# description column is populated only for the handful of records with no body
# (#15), so its weight decides almost nothing.
FTS_WEIGHTS = (10.0, 5.0, 3.0, 1.0)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None) -> sqlite3.Connection:
    paths.ensure_dirs()
    conn = sqlite3.connect(path or paths.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL allows one writer at a time, and the long pipelines commit per row.
    # Without a busy timeout a write from the workbench fails instantly with
    # "database is locked" the moment `map-topics` or `enrich` is running —
    # which is exactly when the owner is using the UI to work the backlog.
    conn.execute("PRAGMA busy_timeout = 15000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {i}")
        conn.commit()


def upsert_sitemap_url(conn, url, path_prefix, lastmod, scope) -> None:
    """Record a sitemap entry. A changed lastmod resets a done page to pending."""
    ts = now()
    conn.execute(
        """
        INSERT INTO sitemap_urls (url, path_prefix, lastmod, scope, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
          path_prefix = excluded.path_prefix,
          lastmod = excluded.lastmod,
          scope = excluded.scope,
          last_seen = excluded.last_seen,
          status = CASE
            WHEN sitemap_urls.status = 'done'
             AND IFNULL(sitemap_urls.scraped_lastmod, '') <> IFNULL(excluded.lastmod, '')
            THEN 'pending' ELSE sitemap_urls.status END
        """,
        (url, path_prefix, lastmod, scope, ts, ts),
    )


def pending_urls(conn, scope="publication", limit=None):
    sql = (
        "SELECT url, lastmod FROM sitemap_urls "
        "WHERE scope = ? AND status IN ('pending', 'failed') ORDER BY url"
    )
    params = [scope]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def mark_url(conn, url, status, lastmod=None, error=None) -> None:
    conn.execute(
        "UPDATE sitemap_urls SET status = ?, scraped_lastmod = ?, last_error = ? WHERE url = ?",
        (status, lastmod, error, url),
    )


# Text the owner typed or pasted (#31). Every automatic step must skip these:
# a re-extraction that silently overwrote hand-entered content would destroy
# the only copy, and nothing downstream would report it.
AUTHORED_SOURCES = ("manual",)


def text_source(conn, publication_id):
    row = conn.execute("SELECT source FROM publication_text WHERE publication_id = ?",
                       (publication_id,)).fetchone()
    return row["source"] if row else None


def is_authored(conn, publication_id) -> bool:
    """True when the stored text was entered by hand and must not be replaced."""
    return text_source(conn, publication_id) in AUTHORED_SOURCES


def reindex_one(conn, publication_id) -> None:
    """Refresh a single record's FTS row. `rebuild_fts` does the whole table,
    which is wrong for a single edit — and leaving it stale means the text is
    saved but unfindable, which looks like the save failed."""
    conn.execute("DELETE FROM publication_fts WHERE rowid = ?", (publication_id,))
    conn.execute(
        """
        INSERT INTO publication_fts (rowid, title, subtitle, description, body)
        SELECT p.id, p.title, p.subtitle,
               CASE WHEN t.body IS NULL THEN p.og_description END, t.body
        FROM publications p LEFT JOIN publication_text t ON t.publication_id = p.id
        WHERE p.id = ?
        """, (publication_id,))


def upsert_text(conn, publication_id, body, word_count, source="html") -> None:
    conn.execute(
        """
        INSERT INTO publication_text (publication_id, body, word_count, source, extracted_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(publication_id) DO UPDATE SET
          body = excluded.body, word_count = excluded.word_count,
          source = excluded.source, extracted_at = excluded.extracted_at
        """,
        (publication_id, body, word_count, source, now()),
    )


def rebuild_fts(conn) -> int:
    """Rebuild the keyword index wholesale. Must be re-run after scrape, reparse
    or extract-text — nothing keeps it current on its own."""
    conn.execute("DELETE FROM publication_fts")
    conn.execute(
        """
        INSERT INTO publication_fts (rowid, title, subtitle, description, body)
        SELECT p.id, p.title, p.subtitle,
               CASE WHEN t.body IS NULL THEN p.og_description END,
               t.body
        FROM publications p LEFT JOIN publication_text t ON t.publication_id = p.id
        """
    )
    return conn.execute("SELECT COUNT(*) FROM publication_fts").fetchone()[0]


def fts_safe(query: str) -> str:
    """Make an arbitrary string safe as an FTS5 match expression.

    FTS5's match syntax is not English. `de-risking` parses as `de` `-`
    `risking` and raises "no such column: risking"; a trailing `?` on a natural
    question raises "syntax error near". Callers that catch OperationalError
    then turn either into a silent empty result — which is how the #17
    evaluation scored syntax errors as ranking failures, and how a question
    asked on the /ask page (#24) lost its keyword half without saying so.

    Anything that is not a bareword, an operator or an already-quoted phrase is
    rewritten as a quoted phrase with its punctuation blanked. `search` stays
    strict on purpose, so an interactive typo is reported rather than reinterpreted.
    """
    out = []
    for token in (query or "").split():
        # Leave the caller's own syntax alone: quoted phrases, NOT/prefix
        # markers, and boolean operators are deliberate, not accidents.
        if '"' in token or token.startswith(("-", "^")) or token in ("AND", "OR", "NOT"):
            out.append(token)
        elif token.replace("*", "").isalnum() and token.rstrip("*").isalnum():
            out.append(token)            # bareword, optionally a prefix search
        else:
            cleaned = "".join(c if c.isalnum() else " " for c in token).strip()
            if cleaned:
                out.append('"' + " ".join(cleaned.split()) + '"')
    return " ".join(out)


def search(conn, query, limit=10):
    """FTS5 keyword search, best first. Raises sqlite3.OperationalError on a
    malformed query — FTS5 match syntax, not SQL, is what the caller typed."""
    return conn.execute(
        f"""
        SELECT p.id, p.url, p.title, p.pub_type, p.date_published,
               snippet(publication_fts, -1, '[', ']', ' … ', 12) AS snippet,
               bm25(publication_fts, {", ".join(str(w) for w in FTS_WEIGHTS)}) AS rank
        FROM publication_fts JOIN publications p ON p.id = publication_fts.rowid
        WHERE publication_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()


def replace_sections(conn, publication_id, sections) -> int:
    """Rewrite one publication's sections. Idempotent, like every other step."""
    conn.execute("DELETE FROM publication_sections WHERE publication_id = ?",
                 (publication_id,))
    for s in sections:
        conn.execute(
            "INSERT INTO publication_sections (publication_id, position, heading, "
            "level, body, word_count, independent, is_boilerplate, "
            "chunk_index, chunk_total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (publication_id, s["position"], s["heading"], s["level"], s["body"],
             s["word_count"], 1 if s.get("independent") else 0,
             1 if s.get("is_boilerplate") else 0,
             s.get("chunk_index"), s.get("chunk_total")),
        )
    return len(sections)


def purge_orphan_embeddings(conn) -> int:
    """Drop section vectors whose section no longer exists.

    `replace_sections` deletes and reinserts, so every `extract-sections` mints
    fresh ids and orphans the old vectors. `embeddings.source_id` is polymorphic
    and cannot carry a foreign key, so nothing removes them automatically — and
    the stale copies do not merely waste space, they let their publication rank
    twice for the same text.
    """
    return conn.execute(
        "DELETE FROM embeddings WHERE source_type = 'section' "
        "AND source_id NOT IN (SELECT id FROM publication_sections)"
    ).rowcount


def rebuild_section_fts(conn) -> int:
    """Rebuild the section index wholesale — same contract as rebuild_fts:
    nothing keeps it current on its own."""
    conn.execute("DELETE FROM section_fts")
    conn.execute(
        "INSERT INTO section_fts (rowid, heading, body) "
        "SELECT id, heading, body FROM publication_sections"
    )
    return conn.execute("SELECT COUNT(*) FROM section_fts").fetchone()[0]


def matching_sections(conn, query, publication_ids):
    """Best-matching section per publication, for the ids given.

    Section text is a subset of the body, so any section hit implies a
    publication hit — this adds *where* the match is, it does not change which
    publications match. That keeps ranking on the proven publication-level bm25
    instead of comparing scores across two FTS tables.
    """
    if not publication_ids:
        return {}
    holes = ", ".join("?" * len(publication_ids))
    rows = conn.execute(
        f"""
        SELECT s.publication_id, s.heading, s.position, s.body,
               snippet(section_fts, 1, '[', ']', ' … ', 12) AS snippet,
               bm25(section_fts, {", ".join(str(w) for w in SECTION_FTS_WEIGHTS)}) AS rank
        FROM section_fts
        JOIN publication_sections s ON s.id = section_fts.rowid
        WHERE section_fts MATCH ? AND s.publication_id IN ({holes})
        ORDER BY rank
        """,
        (query, *publication_ids),
    ).fetchall()
    best = {}
    for r in rows:                      # ordered best-first, so first wins
        best.setdefault(r["publication_id"], r)
    return best


def pending_embeddings(conn, source_type, model, limit=None):
    """What still needs embedding, so a run resumes rather than restarts.

    Sections flagged `is_boilerplate` are excluded: a link list or a lone
    statistic retrieves on its format rather than its subject (#16).
    """
    if source_type == "section":
        sql = """
            SELECT s.id AS source_id, s.publication_id, s.heading AS title, s.body AS text
            FROM publication_sections s
            LEFT JOIN embeddings e ON e.source_id = s.id
                 AND e.source_type = 'section' AND e.model = ?
            WHERE e.id IS NULL AND s.is_boilerplate = 0
            ORDER BY s.id
        """
    elif source_type == "one_liner":
        sql = """
            SELECT n.publication_id AS source_id, n.publication_id, p.title,
                   n.summary_one_liner || ' ' || n.summary_short AS text
            FROM primary_enrichment n
            JOIN publications p ON p.id = n.publication_id
            LEFT JOIN embeddings e ON e.source_id = n.publication_id
                 AND e.source_type = 'one_liner' AND e.model = ?
            WHERE e.id IS NULL
            ORDER BY n.publication_id
        """
    else:
        raise ValueError(f"unknown source_type {source_type!r}")
    params = [model]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def store_embeddings(conn, source_type, model, rows, vectors, pack) -> None:
    ts = now()
    conn.executemany(
        """
        INSERT INTO embeddings
          (source_type, source_id, publication_id, model, dim, vector, embedded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_type, source_id, model) DO UPDATE SET
          vector = excluded.vector, dim = excluded.dim,
          embedded_at = excluded.embedded_at
        """,
        [(source_type, r["source_id"], r["publication_id"], model, len(v), pack(v), ts)
         for r, v in zip(rows, vectors)],
    )


def load_embeddings(conn, source_type, model):
    """Every stored vector for one index, as (rows, blobs) in a stable order."""
    return conn.execute(
        "SELECT source_id, publication_id, vector FROM embeddings "
        "WHERE source_type = ? AND model = ? ORDER BY id",
        (source_type, model),
    ).fetchall()


def sections_by_id(conn, ids):
    if not ids:
        return {}
    holes = ", ".join("?" * len(ids))
    return {r["id"]: r for r in conn.execute(
        f"""SELECT s.id, s.heading, s.body, s.publication_id, p.title, p.url,
                   p.pub_type, p.date_published
            FROM publication_sections s JOIN publications p ON p.id = s.publication_id
            WHERE s.id IN ({holes})""", ids).fetchall()}


# A long report is summarised from its executive summary rather than its first
# N words (#18). Both are correlated subqueries so the two enrichment worklists
# stay one SELECT each. `chunk_index` filters out the windows a long section was
# split into (#34) — an outline should list headings the author wrote.
EXEC_SUMMARY_SQL = """(
    SELECT s.body FROM publication_sections s
    WHERE s.publication_id = p.id
      AND (s.chunk_index IS NULL OR s.chunk_index = 0)
      AND (lower(s.heading) LIKE '%executive summary%'
        OR lower(s.heading) LIKE '%zusammenfassung%'
        OR lower(s.heading) LIKE '%key findings%'
        OR lower(s.heading) LIKE '%key takeaways%'
        -- MERICS names the same section several ways: "Main findings and
        -- conclusions" opens a RAND-style report, "Auf einen Blick" the German
        -- ones. Deliberately excludes "Conclusion", which comes at the end and
        -- assumes the reader has been through the argument.
        OR lower(s.heading) LIKE '%main findings%'
        OR lower(s.heading) LIKE '%auf einen blick%'
        OR lower(s.heading) LIKE '%at a glance%')
    ORDER BY s.position LIMIT 1)"""

# Order follows the scan, which follows `position` in practice. An outline read
# slightly out of order is still a fair picture of the document; nothing keys
# off it.
OUTLINE_SQL = """(
    SELECT group_concat(s.heading, char(10)) FROM publication_sections s
    WHERE s.publication_id = p.id
      AND (s.chunk_index IS NULL OR s.chunk_index = 0)
      AND s.heading IS NOT NULL AND s.heading != '')"""


def pending_enrichment(conn, limit=None, match=None):
    """Publications with body text and no enrichment row — the resume worklist.

    `match` restricts to an FTS query and orders by relevance, so a topic slice
    can be enriched first. Podcasts and paywalled items have no body row and so
    never appear here.
    """
    cols = f"""p.id, p.title, p.subtitle, p.pub_type, p.date_published,
              p.url, p.og_description, t.body, t.word_count,
              {EXEC_SUMMARY_SQL} AS exec_body, {OUTLINE_SQL} AS outline"""
    params = []
    if match:
        # No alias on publication_fts: FTS5's hidden match column is named after
        # the table, and aliasing it turns MATCH into "no such column".
        sql = f"""
            SELECT {cols}
            FROM publication_fts
            JOIN publications p       ON p.id = publication_fts.rowid
            JOIN publication_text t   ON t.publication_id = p.id
            LEFT JOIN primary_enrichment e ON e.publication_id = p.id
            WHERE publication_fts MATCH ? AND e.publication_id IS NULL
            ORDER BY bm25(publication_fts, {", ".join(str(w) for w in FTS_WEIGHTS)})
        """
        params.append(match)
    else:
        sql = f"""
            SELECT {cols}
            FROM publications p
            JOIN publication_text t   ON t.publication_id = p.id
            LEFT JOIN primary_enrichment e ON e.publication_id = p.id
            WHERE e.publication_id IS NULL
            ORDER BY p.id
        """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def enrichment_row(conn, publication_id):
    """One publication in the shape `enrich` expects, whether or not it already
    has a summary — the regenerate path (#24), where `pending_enrichment` by
    definition returns nothing. None when the record has no body text."""
    return conn.execute(
        f"""
        SELECT p.id, p.title, p.subtitle, p.pub_type, p.date_published,
               p.url, p.og_description, t.body, t.word_count,
               {EXEC_SUMMARY_SQL} AS exec_body, {OUTLINE_SQL} AS outline
        FROM publications p JOIN publication_text t ON t.publication_id = p.id
        WHERE p.id = ?
        """, (publication_id,)).fetchone()


# A summary the owner typed (#46). Named here rather than in `enrich` because
# `upsert_primary_enrichment` has to recognise it in order to refuse it.
HAND_WRITTEN_MODEL = "written by hand"

ENRICHMENT_COLS = """publication_id, is_primary, summary_one_liner, summary_short,
    key_findings, entities, model, provider, prompt_version, words_sent,
    prompt_tokens, completion_tokens, attempts, seconds, enriched_at"""


def _enrichment_values(publication_id, data, meta, is_primary):
    return (
        publication_id, 1 if is_primary else 0,
        data["summary_one_liner"].strip(), data["summary_short"].strip(),
        json.dumps(data["key_findings"], ensure_ascii=False),
        json.dumps(data["entities"], ensure_ascii=False),
        meta["model"], meta["provider"], meta["prompt_version"],
        meta["words_sent"], meta.get("prompt_tokens"), meta.get("completion_tokens"),
        meta.get("attempts", 1), meta.get("seconds"), now(),
    )


def add_enrichment(conn, publication_id, data, meta, is_primary=False) -> int:
    """Store a summary and return its row id (#18).

    `is_primary=True` raises if the publication already has one — the partial
    unique index enforces it, deliberately: a second primary appearing by
    accident is exactly the bug the index exists to make impossible.
    """
    holes = ", ".join("?" * 15)
    cur = conn.execute(
        f"INSERT INTO publication_enrichment ({ENRICHMENT_COLS}) VALUES ({holes})",
        _enrichment_values(publication_id, data, meta, is_primary))
    return cur.lastrowid


def upsert_primary_enrichment(conn, publication_id, data, meta) -> int:
    """Write the publication's primary summary, replacing it if one exists.

    The backfill path: `pending_enrichment` only ever offers publications with
    no summary at all, so in practice this inserts. Regeneration does NOT come
    here — it adds a candidate (#18) so nothing is overwritten unreviewed.
    """
    row = conn.execute(
        "SELECT id, model FROM publication_enrichment WHERE publication_id = ? "
        "AND is_primary = 1", (publication_id,)).fetchone()
    if row is None:
        return add_enrichment(conn, publication_id, data, meta, is_primary=True)
    if row["model"] == HAND_WRITTEN_MODEL:
        # Both callers today only reach records with no summary, so this cannot
        # fire — which is exactly why it is worth having (#46). A hand-written
        # summary is the only copy; a pipeline must add a candidate instead.
        raise ValueError(
            f"publication {publication_id} has a hand-written summary; add a "
            f"candidate rather than overwriting it")
    conn.execute(
        """
        UPDATE publication_enrichment SET
          summary_one_liner = ?, summary_short = ?, key_findings = ?, entities = ?,
          model = ?, provider = ?, prompt_version = ?, words_sent = ?,
          prompt_tokens = ?, completion_tokens = ?, attempts = ?, seconds = ?,
          enriched_at = ?
        WHERE id = ?
        """, (*_enrichment_values(publication_id, data, meta, True)[2:], row["id"]))
    return row["id"]


def update_enrichment(conn, enrichment_id, data, meta) -> int:
    """Rewrite one enrichment row in place, primary or not.

    Only the hand-written path uses this (#46): editing a typed summary should
    correct the row, not stack a new one beside it every time a sentence
    changes. A model never comes here — a regenerated reading is a candidate.
    """
    pid = conn.execute("SELECT publication_id FROM publication_enrichment "
                       "WHERE id = ?", (enrichment_id,)).fetchone()["publication_id"]
    conn.execute(
        """
        UPDATE publication_enrichment SET
          summary_one_liner = ?, summary_short = ?, key_findings = ?, entities = ?,
          model = ?, provider = ?, prompt_version = ?, words_sent = ?,
          prompt_tokens = ?, completion_tokens = ?, attempts = ?, seconds = ?,
          enriched_at = ?
        WHERE id = ?
        """, (*_enrichment_values(pid, data, meta, True)[2:], enrichment_id))
    return enrichment_id


def enrichments_for(conn, publication_id):
    """Every summary a publication holds, primary first, newest candidate next."""
    return conn.execute(
        "SELECT * FROM publication_enrichment WHERE publication_id = ? "
        "ORDER BY is_primary DESC, enriched_at DESC", (publication_id,)).fetchall()


def promote_enrichment(conn, enrichment_id) -> dict:
    """Make one summary its publication's primary. Returns the ids involved.

    The demotion must land before the promotion or the partial unique index
    rejects the second primary — the invariant holds at every intermediate
    step, not merely at the end.

    The caller must delete the one-liner vector afterwards: it describes the
    summary that *was* primary, and a vector for text nobody searches does not
    look stale, it looks like a confident wrong match (#24).
    """
    row = conn.execute(
        "SELECT publication_id, is_primary FROM publication_enrichment WHERE id = ?",
        (enrichment_id,)).fetchone()
    if row is None:
        raise ValueError(f"no summary with id {enrichment_id}")
    if row["is_primary"]:
        return {"publication_id": row["publication_id"], "demoted": None,
                "promoted": enrichment_id}
    old = conn.execute(
        "SELECT id FROM publication_enrichment WHERE publication_id = ? "
        "AND is_primary = 1", (row["publication_id"],)).fetchone()
    if old:
        conn.execute("UPDATE publication_enrichment SET is_primary = 0 WHERE id = ?",
                     (old["id"],))
    conn.execute("UPDATE publication_enrichment SET is_primary = 1 WHERE id = ?",
                 (enrichment_id,))
    return {"publication_id": row["publication_id"],
            "demoted": old["id"] if old else None, "promoted": enrichment_id}


def dismiss_enrichment(conn, enrichment_id) -> int:
    """Discard a candidate summary and any verdict on it. Returns its
    publication id. The primary cannot be dismissed — a publication with no
    primary is invisible to search, which no button should be able to cause.
    """
    row = conn.execute(
        "SELECT publication_id, is_primary FROM publication_enrichment WHERE id = ?",
        (enrichment_id,)).fetchone()
    if row is None:
        raise ValueError(f"no summary with id {enrichment_id}")
    if row["is_primary"]:
        raise ValueError("the primary summary cannot be dismissed; promote "
                         "another one first")
    conn.execute("DELETE FROM reviews WHERE scope = 'enrichment' AND subject_id = ?",
                 (enrichment_id,))
    conn.execute("DELETE FROM publication_enrichment WHERE id = ?", (enrichment_id,))
    return row["publication_id"]


def primary_enrichment_id(conn, publication_id):
    """The id a verdict on this publication's current summary should carry."""
    row = conn.execute(
        "SELECT id FROM publication_enrichment WHERE publication_id = ? "
        "AND is_primary = 1", (publication_id,)).fetchone()
    return row["id"] if row else None


def pending_topic_mapping(conn, limit=None, remap=False, match=None):
    """Publications to map onto the topic vocabulary (#4) — the resume worklist.

    Mapping reads the generated summary, not the body text, so the worklist is
    what has enrichment rather than what has text: podcasts and the paywalled
    handful have no summary and are therefore unmappable by this path.
    `remap=True` returns everything again, which is what a changed vocabulary
    needs.
    """
    # Interleaved by type, not ordered by id. Publication ids follow the scrape
    # batches, which group by type, so an id-ordered run that stops early
    # leaves a worklist of one type and a mapped set of another — and the topic
    # counts then describe that type rather than the corpus. Round-robin makes
    # any partial run representative.
    cols = """p.id, p.title, p.subtitle, p.pub_type, p.date_published,
              e.summary_one_liner, e.summary_short, e.key_findings,
              ROW_NUMBER() OVER (PARTITION BY p.pub_type ORDER BY p.id) AS turn"""
    where = "" if remap else " AND pt.publication_id IS NULL"
    params = []
    if match:
        # No alias on publication_fts — see pending_enrichment.
        sql = f"""
            SELECT DISTINCT {cols}
            FROM publication_fts
            JOIN publications p            ON p.id = publication_fts.rowid
            JOIN primary_enrichment e      ON e.publication_id = p.id
            LEFT JOIN publication_topics pt ON pt.publication_id = p.id
            WHERE publication_fts MATCH ?{where}
            ORDER BY turn, p.pub_type
        """
        params.append(match)
    else:
        sql = f"""
            SELECT DISTINCT {cols}
            FROM publications p
            JOIN primary_enrichment e       ON e.publication_id = p.id
            LEFT JOIN publication_topics pt ON pt.publication_id = p.id
            WHERE 1 = 1{where}
            ORDER BY turn, p.pub_type
        """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def replace_topics(conn, publication_id, slugs, meta) -> int:
    """Set one publication's topics, replacing whatever was there. Callers
    validate the slugs first — this writes what it is given."""
    conn.execute("DELETE FROM publication_topics WHERE publication_id = ?",
                 (publication_id,))
    ts = now()
    conn.executemany(
        """
        INSERT INTO publication_topics
          (publication_id, topic_slug, position, model, prompt_version, mapped_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(publication_id, slug, i, meta["model"], meta["prompt_version"], ts)
         for i, slug in enumerate(slugs, start=1)],
    )
    return len(slugs)


def topic_counts(conn, primary_only=False):
    """slug -> number of publications carrying it. `primary_only` counts just
    the model's first-ranked topic, which is the honest 'what is this about'."""
    where = " WHERE position = 1" if primary_only else ""
    return {r["topic_slug"]: r["n"] for r in conn.execute(
        f"SELECT topic_slug, COUNT(*) n FROM publication_topics{where} "
        f"GROUP BY topic_slug").fetchall()}


def attach_chapter(conn, child_id, parent_id, position=None) -> int:
    """Hang a publication off its parent report (#36). Returns the position.

    Appends when `position` is not given. Refuses to make a publication its own
    parent or to hang a chapter off a chapter: the model is one level deep, and
    a cycle would make `chapters_of` recurse forever.
    """
    if child_id == parent_id:
        raise ValueError("a publication cannot be its own parent")
    row = conn.execute("SELECT parent_id FROM publications WHERE id = ?",
                       (parent_id,)).fetchone()
    if row is None:
        raise ValueError(f"no publication {parent_id}")
    if row["parent_id"] is not None:
        raise ValueError(f"{parent_id} is itself a chapter — one level only")
    if conn.execute("SELECT 1 FROM publications WHERE parent_id = ? LIMIT 1",
                    (child_id,)).fetchone():
        raise ValueError(f"{child_id} has chapters of its own")
    if position is None:
        position = (conn.execute(
            "SELECT COALESCE(MAX(parent_position), 0) + 1 FROM publications "
            "WHERE parent_id = ?", (parent_id,)).fetchone()[0])
    conn.execute(
        "UPDATE publications SET parent_id = ?, parent_position = ? WHERE id = ?",
        (parent_id, position, child_id))
    return position


def detach_chapter(conn, child_id) -> None:
    conn.execute("UPDATE publications SET parent_id = NULL, "
                 "parent_position = NULL WHERE id = ?", (child_id,))


def chapters_of(conn, parent_id):
    """A parent's chapters in reading order, with their word counts."""
    return conn.execute(
        """SELECT p.id, p.title, p.url, p.parent_position, p.date_published,
                  COALESCE(t.word_count, 0) AS word_count,
                  e.summary_one_liner
           FROM publications p
           LEFT JOIN publication_text t ON t.publication_id = p.id
           LEFT JOIN primary_enrichment e ON e.publication_id = p.id
           WHERE p.parent_id = ?
           ORDER BY p.parent_position, p.id""", (parent_id,)).fetchall()


def parent_of(conn, child_id):
    """The report a chapter belongs to, or None."""
    return conn.execute(
        """SELECT p.id, p.title, p.url, p.pub_type FROM publications c
           JOIN publications p ON p.id = c.parent_id WHERE c.id = ?""",
        (child_id,)).fetchone()


def publications_with_topic(conn, slug, primary_only=False) -> set:
    """Ids carrying a topic — for filtering a result set already in memory."""
    where = " AND position = 1" if primary_only else ""
    return {r[0] for r in conn.execute(
        f"SELECT publication_id FROM publication_topics "
        f"WHERE topic_slug = ?{where}", (slug,))}


def topics_for_publications(conn, ids):
    """publication_id -> [slug, …] in the model's ranked order."""
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    out = {}
    for r in conn.execute(
        f"SELECT publication_id, topic_slug FROM publication_topics "
        f"WHERE publication_id IN ({marks}) ORDER BY publication_id, position",
            list(ids)).fetchall():
        out.setdefault(r["publication_id"], []).append(r["topic_slug"])
    return out


DEFAULT_TYPES_KEY = "catalog_default_types"


def get_setting(conn, key, fallback=None):
    """A stored preference, JSON-decoded. `fallback` when unset (#30)."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return fallback
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return fallback


def set_setting(conn, key, value) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at
        """, (key, json.dumps(value), now()))


BACKLOG_SCOPE = "backlog"

# Drupal titles every event page "Registration Form" / "Anmeldung", whatever
# the event. That is the durable signal: the Microsoft Dynamics form embedded
# below it stops working once the event goes offline, so detecting the embed
# would decay, while the title stays (owner, 2026-08-08).
#
# Nothing else separates them. No `<form>` or iframe does — the form is loaded
# by JavaScript, and every merics.org page embeds the same Google Tag Manager
# frame, so an iframe test would exclude the whole catalog.
# Matched exactly, not as a prefix. "Registration and accreditation policy"
# would be a real publication, and a loose prefix test excluded it — the same
# over-broad matching that swept two real reports into "site furniture"
# earlier. Anything not matched exactly still faces the date/words test.
EVENT_TITLES = ("registration form", "registration", "anmeldung",
                "anmeldeformular", "anmeldung zur veranstaltung")


def looks_like_event(title) -> bool:
    return (title or "").strip().lower().rstrip(" .!") in EVENT_TITLES


# Series the old untyped pages announce in their subtitle. "China Update
# 1/2019" is the run that became MERICS China Essentials in June 2020 — the
# same publication under its earlier name, so it joins the same series rather
# than starting a parallel one (owner, 2026-08-09).
SUBTITLE_SERIES = (
    (re.compile(r"^China Update\s+\d+\s*/\s*\d{4}", re.I),
     "MERICS Briefs", "MERICS China Essentials"),
)


def series_from_subtitle(subtitle):
    """-> (pub_type, series) when the subtitle names a known run, else None."""
    for pattern, pub_type, series in SUBTITLE_SERIES:
        if subtitle and pattern.search(subtitle.strip()):
            return pub_type, series
    return None


def propose_disposition(has_date, words, title=None) -> str:
    """Ingest, or exclude. A page with its own date-published field and real
    prose is a publication; the event pages carry ~64 words and no date."""
    if looks_like_event(title):
        return "exclude"
    if has_date and words >= 250:
        return "ingest"
    return "exclude"


def set_url_probe(conn, url, words, has_date, title, subtitle=None) -> None:
    named = series_from_subtitle(subtitle)
    conn.execute(
        """UPDATE sitemap_urls SET probe_words = ?, probe_has_date = ?,
           probe_title = ?, probe_subtitle = ?, proposed_type = ?,
           proposed_series = ?, disposition = COALESCE(disposition, ?)
           WHERE url = ?""",
        (words, 1 if has_date else 0, title, subtitle,
         named[0] if named else None, named[1] if named else None,
         "ingest" if named else propose_disposition(has_date, words, title),
         url))


def set_url_disposition(conn, url, disposition, note=None) -> None:
    """The owner's call on a root-level URL, overriding the proposal.

    Stamps `settled_at`, which takes the row off the working list — including
    for `todo`. A hand-over is not finished work, but it *is* off the owner's
    desk, and it stays visible in the "Handed over" section above. The list he
    works through has to empty as he works, or bulk confirming achieves
    nothing.
    """
    conn.execute(
        "UPDATE sitemap_urls SET disposition = ?, note = ?, settled_at = ? "
        "WHERE url = ?",
        (disposition, (note or "").strip() or None, now(), url))


def unsettle_url(conn, url) -> None:
    conn.execute("UPDATE sitemap_urls SET settled_at = NULL WHERE url = ?", (url,))


def homeless_urls(conn, disposition=None, include_settled=False):
    """Root-level URLs still needing a decision.

    A *confirmed* disposition removes the row from the list — the point of
    confirming in bulk is that the list shortens as you work. The proposal
    alone does not: `settled_at` is written only when the owner confirms.
    """
    sql = ("SELECT url, lastmod, probe_words, probe_has_date, probe_title, "
           "probe_subtitle, proposed_type, proposed_series, "
           "disposition, note, settled_at FROM sitemap_urls "
           "WHERE scope = 'root-level' AND status = 'pending'")
    if not include_settled:
        sql += " AND settled_at IS NULL"
    params = []
    if disposition:
        sql += " AND disposition = ?"
        params.append(disposition)
    return conn.execute(sql + " ORDER BY probe_words DESC, url", params).fetchall()



def park_backlog(conn, publication_id, note=None, verdict="parked") -> None:
    """Record a decision about a backlog item (#33).

    `parked` — already correct, nothing to do. A graphics-only piece is *meant*
    to have 34 words.
    `todo` — needs work, and `note` says what. The owner can explain any of
    these given the page; the note is where that explanation lands so it is
    acted on rather than lost in a chat message.

    Both come off the main list; `todo` items form the work queue.
    """
    upsert_review(conn, BACKLOG_SCOPE, publication_id, verdict, note)


def backlog_todo(conn):
    """Records the owner has left instructions on — the execution queue."""
    return conn.execute(
        """SELECT p.id, p.title, p.url, p.pub_type, p.date_published, r.note,
                  r.created_at
           FROM reviews r JOIN publications p ON p.id = r.subject_id
           WHERE r.scope = ? AND r.verdict = 'todo'
           ORDER BY r.created_at""", (BACKLOG_SCOPE,)).fetchall()


def unpark_backlog(conn, publication_id) -> None:
    conn.execute("DELETE FROM reviews WHERE scope = ? AND subject_id = ?",
                 (BACKLOG_SCOPE, publication_id))


def parked_ids(conn) -> dict:
    return {r["subject_id"]: r["note"] for r in conn.execute(
        "SELECT subject_id, note FROM reviews WHERE scope = ?", (BACKLOG_SCOPE,))}


def upsert_collection(conn, slug, name, url=None, blurb=None) -> None:
    """Register a standing project. Idempotent; re-registering keeps members."""
    conn.execute(
        """
        INSERT INTO collections (slug, name, url, blurb, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
          name = excluded.name, url = excluded.url, blurb = excluded.blurb
        """, (slug, name, url, blurb, now()))


def rename_collection(conn, slug, name) -> None:
    """Change the display name only. `slug` is the identity and is what
    `publication_collections` stores, so renaming must never touch it — the
    same rule the topic vocabulary follows (#37)."""
    conn.execute("UPDATE collections SET name = ? WHERE slug = ?", (name, slug))


def delete_collection(conn, slug) -> int:
    """Remove a project. Returns how many memberships went with it — the
    caller is expected to have said that number out loud first (#37)."""
    n = conn.execute("SELECT COUNT(*) c FROM publication_collections "
                     "WHERE collection_slug = ?", (slug,)).fetchone()["c"]
    conn.execute("DELETE FROM collections WHERE slug = ?", (slug,))
    return n


def collections(conn):
    """Every standing project with its member count."""
    return conn.execute(
        """
        SELECT c.*, COUNT(pc.publication_id) n
        FROM collections c
        LEFT JOIN publication_collections pc ON pc.collection_slug = c.slug
        GROUP BY c.slug ORDER BY c.name
        """).fetchall()


def add_to_collection(conn, publication_id, slug, source="manual") -> bool:
    """Attach a publication. Returns False when an existing membership is left
    alone.

    **`auto` never overwrites `manual`.** A curated membership has to survive
    every future re-scrape; detection re-runs constantly and would otherwise
    quietly undo hand corrections, which is the same trap as manual body text
    (#31).
    """
    row = conn.execute(
        "SELECT source FROM publication_collections WHERE publication_id = ? "
        "AND collection_slug = ?", (publication_id, slug)).fetchone()
    if row is not None and (row["source"] == "manual" or source == "auto"):
        return False
    conn.execute(
        """
        INSERT INTO publication_collections
          (publication_id, collection_slug, source, added_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(publication_id, collection_slug)
          DO UPDATE SET source = excluded.source, added_at = excluded.added_at
        """, (publication_id, slug, source, now()))
    return True


def remove_from_collection(conn, publication_id, slug) -> None:
    conn.execute(
        "DELETE FROM publication_collections WHERE publication_id = ? "
        "AND collection_slug = ?", (publication_id, slug))


def collection_members(conn, slug):
    return conn.execute(
        """
        SELECT p.id, p.title, p.pub_type, p.date_published, p.url, p.series,
               pc.source, e.summary_one_liner
        FROM publication_collections pc
        JOIN publications p ON p.id = pc.publication_id
        LEFT JOIN primary_enrichment e ON e.publication_id = p.id
        WHERE pc.collection_slug = ?
        ORDER BY p.date_published DESC
        """, (slug,)).fetchall()


def collections_for(conn, publication_id):
    return conn.execute(
        """SELECT c.slug, c.name, pc.source FROM publication_collections pc
           JOIN collections c ON c.slug = pc.collection_slug
           WHERE pc.publication_id = ? ORDER BY c.name""",
        (publication_id,)).fetchall()


CREDIT_ROLE_CHOICES = ("author", "host", "guest")


def find_people(conn, term, limit=20):
    """People matching `term`, prefix matches first (#46).

    Typing "Jac" means Jacob, not "Bojacz" — a plain `LIKE %term%` ordered by
    name buries the obvious answer. Substring matches still come back, because
    a surname typed first is the other common case.
    """
    term = term.strip()
    return conn.execute(
        """SELECT id, name, affiliation, is_current,
                  COALESCE(roster_title, job_title) AS title
           FROM people WHERE name LIKE ? AND merged_into IS NULL
           ORDER BY CASE
                      WHEN name LIKE ? THEN 0     -- starts with it
                      WHEN name LIKE ? THEN 1     -- a later word starts with it
                      ELSE 2 END,
                    is_current DESC, name
           LIMIT ?""",
        (f"%{term}%", f"{term}%", f"% {term}%", limit)).fetchall()


def find_parents(conn, term, limit=15):
    """Candidate parent reports for the chapter-attach picker (#36), by title.

    Chapters are excluded — a chapter of a chapter is refused at attach time,
    so offering one would only manufacture the error. Prefix ranking as in
    `find_people`: typing "Shaky" means *Shaky China*, not a match buried
    mid-title.
    """
    term = term.strip()
    return conn.execute(
        """SELECT p.id, p.title, p.pub_type, p.date_published,
                  (SELECT COUNT(*) FROM publications c
                   WHERE c.parent_id = p.id) AS chapters
           FROM publications p WHERE p.title LIKE ? AND p.parent_id IS NULL
           ORDER BY CASE
                      WHEN p.title LIKE ? THEN 0
                      WHEN p.title LIKE ? THEN 1
                      ELSE 2 END,
                    p.date_published DESC
           LIMIT ?""",
        (f"%{term}%", f"{term}%", f"% {term}%", limit)).fetchall()


def add_person(conn, name, job_title=None) -> int:
    """Create a person with no team-page slug — an external, keyed on name.

    Returns the existing id if the name is already taken, so the workbench
    cannot mint a duplicate by submitting twice.
    """
    return _person_id(conn, None, name.strip(), False, job_title)


def credit_person(conn, publication_id, person_id, role="author") -> bool:
    """Credit someone by hand (#40). False if that exact credit already exists.

    Appended after whatever the page itself carries, so byline order is not
    disturbed; `source = 'manual'` is what stops the next re-parse deleting it.
    """
    if role not in CREDIT_ROLE_CHOICES:
        raise ValueError(f"unknown role {role!r}")
    if conn.execute(
            "SELECT 1 FROM publication_people WHERE publication_id = ? "
            "AND person_id = ? AND role = ?",
            (publication_id, person_id, role)).fetchone():
        return False
    position = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS n FROM publication_people "
        "WHERE publication_id = ?", (publication_id,)).fetchone()["n"]
    conn.execute(
        "INSERT INTO publication_people "
        "(publication_id, person_id, role, position, source) "
        "VALUES (?, ?, ?, ?, 'manual')",
        (publication_id, person_id, role, position))
    return True


def uncredit_person(conn, publication_id, person_id, role) -> None:
    """Remove a credit, hand-assigned or parsed.

    A parsed credit can be wrong too — the interviewer read as the author —
    but removing one only lasts until the next re-parse re-reads the page.
    """
    conn.execute(
        "DELETE FROM publication_people WHERE publication_id = ? "
        "AND person_id = ? AND role = ?", (publication_id, person_id, role))


def set_shortlist(conn, publication_id, note=None) -> None:
    """Mark a publication as one that mattered (#25). Re-marking updates the
    note and keeps the original `added_at` — when you first noticed is the
    interesting date, not when you last reworded why."""
    conn.execute(
        """
        INSERT INTO shortlist (publication_id, note, added_at) VALUES (?, ?, ?)
        ON CONFLICT(publication_id) DO UPDATE SET note = excluded.note
        """, (publication_id, (note or "").strip() or None, now()))


def clear_shortlist(conn, publication_id) -> None:
    conn.execute("DELETE FROM shortlist WHERE publication_id = ?", (publication_id,))


def shortlisted_ids(conn) -> set:
    """Every shortlisted publication id — small by nature, so the surfaces
    fetch the whole set once and badge rows from it rather than joining."""
    return {r[0] for r in conn.execute("SELECT publication_id FROM shortlist")}


def shortlist_rows(conn):
    """The shortlist itself, most recently added first."""
    return conn.execute(
        """
        SELECT p.id, p.title, p.pub_type, p.date_published, s.note, s.added_at,
               e.summary_one_liner
        FROM shortlist s
        JOIN publications p ON p.id = s.publication_id
        LEFT JOIN primary_enrichment e ON e.publication_id = p.id
        ORDER BY s.added_at DESC
        """).fetchall()


def upsert_review(conn, scope, subject_id, verdict, note=None) -> None:
    """Record a verdict. Latest wins — a re-review replaces, never accumulates."""
    conn.execute(
        """
        INSERT INTO reviews (scope, subject_id, verdict, note, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(scope, subject_id) DO UPDATE SET
          verdict = excluded.verdict, note = excluded.note,
          created_at = excluded.created_at
        """,
        (scope, subject_id, verdict, note or None, now()),
    )


def reviews_by_subject(conn, scope):
    return {r["subject_id"]: r for r in conn.execute(
        "SELECT subject_id, verdict, note, created_at FROM reviews WHERE scope = ?",
        (scope,)).fetchall()}


def _person_id(conn, slug, name, is_internal, job_title) -> int:
    """Find or create a person. A job title, once seen, is never overwritten with
    nothing — most pages omit it and only some carry it."""
    if slug:
        row = conn.execute(
            "SELECT id, merged_into FROM people WHERE slug = ?", (slug,)).fetchone()
    else:
        row = conn.execute(
            "SELECT id, merged_into FROM people WHERE slug IS NULL AND name = ?",
            (name,)
        ).fetchone()
    if row and row["merged_into"]:
        # A merged duplicate (#47): the credit belongs to the survivor, whose
        # identity the duplicate node's spelling must not overwrite — only fill
        # what the survivor lacks.
        conn.execute(
            "UPDATE people SET job_title = COALESCE(job_title, ?), "
            "is_internal = MAX(is_internal, ?) WHERE id = ?",
            (job_title, 1 if is_internal else 0, row["merged_into"]),
        )
        return row["merged_into"]
    if row:
        conn.execute(
            "UPDATE people SET name = ?, job_title = COALESCE(?, job_title), "
            "is_internal = MAX(is_internal, ?) WHERE id = ?",
            (name, job_title, 1 if is_internal else 0, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO people (slug, name, is_internal, job_title) VALUES (?, ?, ?, ?)",
        (slug, name, 1 if is_internal else 0, job_title),
    )
    return cur.lastrowid


def merge_person(conn, duplicate_id, survivor_id) -> dict:
    """Merge a duplicate person into the survivor (#47).

    The duplicate stays as a tombstone whose slug redirects inside
    `_person_id` — deleting it would only have the next `reparse` re-create
    the row from the cached HTML and silently re-split the credits.
    """
    if duplicate_id == survivor_id:
        raise ValueError("cannot merge a person into themselves")
    dup = conn.execute(
        "SELECT * FROM people WHERE id = ?", (duplicate_id,)).fetchone()
    surv = conn.execute(
        "SELECT * FROM people WHERE id = ?", (survivor_id,)).fetchone()
    if not dup or not surv:
        raise ValueError("no such person")
    if dup["merged_into"]:
        raise ValueError(f"person {duplicate_id} is already merged "
                         f"into {dup['merged_into']}")
    if surv["merged_into"]:
        raise ValueError(f"person {survivor_id} is itself merged into "
                         f"{surv['merged_into']} — merge into that row")
    total = conn.execute(
        "SELECT COUNT(*) FROM publication_people WHERE person_id = ?",
        (duplicate_id,)).fetchone()[0]
    # A credit the survivor already holds is dropped, not doubled.
    conn.execute(
        "UPDATE OR IGNORE publication_people SET person_id = ? "
        "WHERE person_id = ?", (survivor_id, duplicate_id))
    shared = conn.execute(
        "SELECT COUNT(*) FROM publication_people WHERE person_id = ?",
        (duplicate_id,)).fetchone()[0]
    conn.execute("DELETE FROM publication_people WHERE person_id = ?",
                 (duplicate_id,))
    # Keep the chain one level deep, like parent_id (#36): anything already
    # pointing at the duplicate follows it to the survivor.
    conn.execute("UPDATE people SET merged_into = ? WHERE merged_into = ?",
                 (survivor_id, duplicate_id))
    # The survivor's own fields win; the duplicate only fills gaps.
    conn.execute(
        "UPDATE people SET job_title = COALESCE(job_title, ?), "
        "roster_title = COALESCE(roster_title, ?), "
        "is_internal = MAX(is_internal, ?) WHERE id = ?",
        (dup["job_title"], dup["roster_title"], dup["is_internal"],
         survivor_id))
    conn.execute("UPDATE people SET merged_into = ?, is_current = 0 "
                 "WHERE id = ?", (survivor_id, duplicate_id))
    return {"duplicate": dup["name"], "survivor": surv["name"],
            "moved": total - shared, "already_there": shared}


def duplicate_people(conn):
    """Names carried by more than one unmerged row — the next #47 pair.

    The site can mint a new duplicate team node at any time; this is the
    check that surfaces it instead of letting the credits quietly split.
    """
    return conn.execute(
        """SELECT name, COUNT(*) AS n, GROUP_CONCAT(id) AS ids
           FROM people WHERE merged_into IS NULL
           GROUP BY name HAVING n > 1 ORDER BY name""").fetchall()


def _tag_id(conn, name) -> int:
    conn.execute("INSERT OR IGNORE INTO site_tags (name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM site_tags WHERE name = ?", (name,)).fetchone()["id"]


def upsert_publication(conn, rec: dict, sitemap_lastmod=None) -> int:
    """Insert or replace one publication and its author/tag links. Idempotent."""
    cols = (
        "slug", "url", "title", "subtitle", "date_published", "pub_type", "series",
        "access", "pdf_url", "og_description",
    )
    values = [rec.get(c) for c in cols]
    conn.execute(
        f"""
        INSERT INTO publications ({", ".join(cols)}, sitemap_lastmod, scraped_at)
        VALUES ({", ".join("?" * len(cols))}, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
          {", ".join(f"{c} = excluded.{c}" for c in cols)},
          sitemap_lastmod = excluded.sitemap_lastmod,
          scraped_at = excluded.scraped_at
        """,
        (*values, sitemap_lastmod, now()),
    )
    pub_id = conn.execute(
        "SELECT id FROM publications WHERE url = ?", (rec["url"],)
    ).fetchone()["id"]

    # Hand-assigned credits are not the page's to remove (#40).
    conn.execute("DELETE FROM publication_people WHERE publication_id = ? "
                 "AND source = 'parsed'", (pub_id,))
    for position, p in enumerate(rec.get("people", [])):
        person_id = _person_id(
            conn, p["slug"], p["name"], p.get("is_internal", False), p.get("job_title")
        )
        conn.execute(
            "INSERT OR IGNORE INTO publication_people "
            "(publication_id, person_id, role, position, source) "
            "VALUES (?, ?, ?, ?, 'parsed')",
            (pub_id, person_id, p["role"], position),
        )

    conn.execute("DELETE FROM publication_site_tags WHERE publication_id = ?", (pub_id,))
    for tag in rec.get("site_tags", []):
        conn.execute(
            "INSERT OR IGNORE INTO publication_site_tags (publication_id, tag_id) VALUES (?, ?)",
            (pub_id, _tag_id(conn, tag)),
        )
    return pub_id


# --- the upcoming layer (#56) ------------------------------------------------
#
# Hand-entered notes on publications that do not exist yet. Nothing here is
# reachable from a publication query: the tables are named explicitly by the
# surfaces that want them, and by nothing else. Note text is internal
# knowledge — it never reaches an LLM, the FTS index or an MCP response.

UPCOMING_MAX_TOPICS = 3
QUARTER_RE = re.compile(r"^\d{4}-Q[1-4]$")


def quarter_of(date) -> str:
    """'2026-05-14' -> '2026-Q2'. Empty for a record with no date."""
    if not date or len(date) < 7:
        return ""
    return f"{date[:4]}-Q{(int(date[5:7]) + 2) // 3}"


def _quarter(value):
    """Validate an `expected` value. Blank is allowed and means "no idea yet";
    anything finer than a quarter is refused rather than truncated, so a date
    typed into the field cannot pass itself off as knowledge the owner has."""
    value = (value or "").strip()
    if not value:
        return None
    if not QUARTER_RE.match(value):
        raise ValueError(f"expected must be YYYY-Qn, not {value!r}")
    return value


def _upcoming_links(conn, note_id, topic_slugs, person_ids) -> None:
    """Replace a note's topics and people — the form submits the whole set."""
    known = set(topics.slugs())
    conn.execute("DELETE FROM upcoming_topics WHERE note_id = ?", (note_id,))
    for position, slug in enumerate(list(dict.fromkeys(
            s for s in topic_slugs if s))[:UPCOMING_MAX_TOPICS]):
        if slug not in known:
            raise ValueError(f"unknown topic slug {slug!r}")
        conn.execute("INSERT INTO upcoming_topics (note_id, topic_slug, position) "
                     "VALUES (?, ?, ?)", (note_id, slug, position))
    conn.execute("DELETE FROM upcoming_people WHERE note_id = ?", (note_id,))
    for person_id in dict.fromkeys(int(p) for p in person_ids if p):
        conn.execute("INSERT INTO upcoming_people (note_id, person_id) "
                     "VALUES (?, ?)", (note_id, person_id))


def add_upcoming_note(conn, working_title, note=None, expected=None,
                      topic_slugs=(), person_ids=()) -> int:
    title = (working_title or "").strip()
    if not title:
        raise ValueError("a note needs a working title")
    ts = now()
    cur = conn.execute(
        """INSERT INTO upcoming_notes
             (working_title, note, expected, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (title, (note or "").strip() or None, _quarter(expected), ts, ts))
    _upcoming_links(conn, cur.lastrowid, topic_slugs, person_ids)
    return cur.lastrowid


def edit_upcoming_note(conn, note_id, working_title, note=None, expected=None,
                       topic_slugs=(), person_ids=()) -> None:
    """Correct a note in place. `created_at` is left alone — it is what lead
    time is measured from, so a reworded title must not reset it."""
    title = (working_title or "").strip()
    if not title:
        raise ValueError("a note needs a working title")
    conn.execute(
        """UPDATE upcoming_notes SET working_title = ?, note = ?, expected = ?,
             updated_at = ? WHERE id = ?""",
        (title, (note or "").strip() or None, _quarter(expected), now(), note_id))
    _upcoming_links(conn, note_id, topic_slugs, person_ids)


def shelve_upcoming_note(conn, note_id, reason) -> None:
    """It is not happening. The reason is the point: planned-vs-actual only
    means something if the misses stay legible."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("shelving takes a reason")
    row = conn.execute("SELECT landed_publication_id FROM upcoming_notes "
                       "WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise ValueError(f"no note {note_id}")
    if row["landed_publication_id"]:
        raise ValueError("that note has landed — unlink it before shelving")
    conn.execute("UPDATE upcoming_notes SET shelved_at = ?, shelved_reason = ?, "
                 "updated_at = ? WHERE id = ?", (now(), reason, now(), note_id))


def link_upcoming_note(conn, note_id, publication_id) -> None:
    """It landed as this publication. Manual only (#56): the weekly job may
    later suggest candidates by title, it must never link one."""
    row = conn.execute("SELECT shelved_at FROM upcoming_notes WHERE id = ?",
                       (note_id,)).fetchone()
    if row is None:
        raise ValueError(f"no note {note_id}")
    if row["shelved_at"]:
        raise ValueError("that note is shelved — reopen it before linking")
    if not conn.execute("SELECT 1 FROM publications WHERE id = ?",
                        (publication_id,)).fetchone():
        raise ValueError(f"no publication {publication_id}")
    conn.execute("UPDATE upcoming_notes SET landed_publication_id = ?, "
                 "landed_at = ?, updated_at = ? WHERE id = ?",
                 (publication_id, now(), now(), note_id))


def reopen_upcoming_note(conn, note_id) -> None:
    """Undo a shelve or a link. Both are one click and both are the kind of
    thing that gets clicked on the wrong row; without this the only fix is
    SQL. The note goes back to *expected* and keeps its `created_at`."""
    conn.execute(
        """UPDATE upcoming_notes SET landed_publication_id = NULL,
             landed_at = NULL, shelved_at = NULL, shelved_reason = NULL,
             updated_at = ? WHERE id = ?""", (now(), note_id))


def upcoming_note(conn, note_id):
    return conn.execute("SELECT * FROM upcoming_notes WHERE id = ?",
                        (note_id,)).fetchone()


def upcoming_note_topics(conn, note_ids) -> dict:
    """note_id -> [slug, …] in entry order."""
    out = {}
    if not note_ids:
        return out
    marks = ", ".join("?" * len(note_ids))
    for r in conn.execute(
            f"""SELECT note_id, topic_slug FROM upcoming_topics
                WHERE note_id IN ({marks}) ORDER BY note_id, position""",
            list(note_ids)):
        out.setdefault(r["note_id"], []).append(r["topic_slug"])
    return out


def upcoming_note_people(conn, note_ids) -> dict:
    """note_id -> [{id, name}, …]."""
    out = {}
    if not note_ids:
        return out
    marks = ", ".join("?" * len(note_ids))
    for r in conn.execute(
            f"""SELECT up.note_id, pe.id, pe.name FROM upcoming_people up
                JOIN people pe ON pe.id = up.person_id
                WHERE up.note_id IN ({marks}) ORDER BY up.note_id, pe.name""",
            list(note_ids)):
        out.setdefault(r["note_id"], []).append({"id": r["id"], "name": r["name"]})
    return out
