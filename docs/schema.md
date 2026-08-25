# Catalog schema and query recipes

How to query `data/catalog.db` directly. This document is the access layer —
the CLI, the workbench and the MCP server (#23) all answer through
`pubbrain/queries.py`, and the recipes below are what that layer implements.

**It versions with `pubbrain/db.py`. A migration that leaves this file
untouched is incomplete.**

Schema version is `PRAGMA user_version`, currently **26**. Migrations are the
`MIGRATIONS` list in `db.py`, applied in order on every `connect()`.

The local web workbench (`python -m pubbrain web`, #19) reads this same
database through `pubbrain/queries.py`, which is the shared query layer:
ranking and filtering logic lives there, not in view code or in the CLI, so
the recipes below, the CLI and the web pages cannot drift apart.

## Tables

### `publications` — one row per publication, the spine of the catalog

| Column | Meaning |
|---|---|
| `id` | Primary key; also the `rowid` of `publication_fts` and the FK everything else hangs off |
| `slug` | Last path segment of the URL. Also the cache filename: `data/raw/<slug>.html` |
| `url` | Canonical merics.org URL. **The real identity key** — upserts conflict on this |
| `title` | Page title |
| `subtitle` | Often absent |
| `date_published` | `YYYY-MM-DD` text, so string comparison and `BETWEEN` sort correctly. No NULLs at present; range 2014-02-24 → 2026-07-29 |
| `pub_type` | The page's own type: Comment, MERICS Briefs, Podcast, Report, Tracker, Interview, External publication, Executive Memo |
| `series` | e.g. "MERICS China Essentials", "China Economic Indicators". NULL for standalone pieces. Filterable in the workbench facet, `queries.list_publications(series=…)` and the MCP tool (#32) — exact names, so copy them from `filter_options` |
| `is_flagship` | **Never populated** — see traps |
| `access` | `public` (1,349) or `member` (5, paywalled: metadata only, no body text) |
| `pdf_url` | Absolute URL of the attached PDF where the page offers one (577 rows) |
| `pdf_path` | Local copy under `data/pdf/<slug>.pdf`, where one was downloaded (66 rows). `download-pdfs` fills it; `pdf_url` is populated on 577 |
| `og_description` | **Not a description** — merics.org's `og:description` carries the whole article (median 6,580 chars, max 70,432), so for 99% of records with body text this is a second copy of `publication_text.body` (#15). Useful only for the 11 records that have no body |
| `sitemap_lastmod` | The sitemap's `lastmod` at scrape time |
| `scraped_at` | UTC ISO timestamp of the last successful parse |
| `parent_id` | The report this is a chapter of, or NULL (#36). One level only |
| `parent_position` | Reading order within the parent. NULL when `parent_id` is |

> ⚠️ **`WHERE parent_id IS NULL` belongs in every count of "publications".**
>
> A chapter inherits its parent's `pub_type`, so `SELECT COUNT(*) … GROUP BY
> pub_type` silently turns "Reports" into reports-plus-their-chapters. 14
> reports currently hold 62 chapters between them. The catalog listing, the type and
> year filters, and `status` all exclude chapters; search deliberately does not,
> and labels each hit with its parent instead.

**A report appears once, carrying its executive summary** (owner, 2026-08-09).
The chapters hang off it with their own text, summary, sections and vectors —
findable in their own right, not listed beside the parent. The parents
therefore hold their landing-page abstract (226–880 words), *not* the full PDF,
which is what would duplicate the chapters. `extract-text --force` is how a
parent is reverted once its chapters land.

**Use `parent_id` only where the parent is itself the MERICS publication.**
Owner decision (2026-08-09), after first choosing collections and reversing:
the ETNC volumes are multi-institute and MERICS wrote only some chapters, but
their parent pages are real records that link those chapters, so they take
`parent_id` like any other report. A container that is *not* a publication
(a dashboard, a standing project) is `publication_collections` instead — flat,
unordered, many-to-many, and its members stay in the listing.

### `people` — analysts, hosts and guests

| Column | Meaning |
|---|---|
| `id` | Primary key |
| `slug` | Team-page slug; NULL for people with no merics.org page (externals are keyed on `name` instead) |
| `name` | Display name |
| `is_internal` | **Superseded by `affiliation` — do not query it** (traps) |
| `job_title` | Title as printed on a publication page, where one was printed |
| `affiliation` | `staff` \| `affiliate` \| `external` \| `unknown` |
| `is_current` | 1 = on a current merics.org roster today. 0 = former, or never MERICS |
| `roster_title` | Title as the roster lists it. NULL for anyone not currently on one |
| `roster_source` | `experts` \| `leadership` — which listing they were found on |
| `merged_into` | Set = this row is a **tombstone** for a merged duplicate (#47). Points at the surviving row |

`affiliation`, `is_current`, `roster_title` and `roster_source` are all written
by `sync-roster` from the site's own listings, not inferred from job titles.

**Any query that reads `people` directly needs `WHERE merged_into IS NULL`.**
merics.org itself carries two team nodes for some people (a `-0` slug, a second
transliteration), and each row of such a pair collects its own credits — the
one credit gap where the record *looks* complete. `pubbrain merge-person
DUPLICATE SURVIVOR` moves the credits and leaves the duplicate as a tombstone
rather than deleting it: `_person_id` inserts on an unknown slug, so a deleted
row would be re-created by the next `reparse` and the credits re-split. The
tombstone's slug redirects inside `_person_id`, which is what makes the merge
survive. All shipped surfaces (`list_people`, `people_matching`,
`find_people`, `person_page`, `status`) already exclude tombstones —
`person_page` on a tombstone id returns the survivor — and `status` warns when
a name is carried by more than one live row, which is how the site's next
duplicate surfaces.

**`affiliation` is ever-held, `is_current` is a snapshot of today's roster.**
`staff` + `is_current = 0` is not a contradiction: it means a former staffer.
Max J. Zenglein reads `staff` / former / "Former Chief Economist" and still has
a 2026 byline — people publish up to their departure and the back catalogue
stays. Always report both fields, never `affiliation` alone.

### `publication_people` — who did what

`(publication_id, person_id, role, position, source)`, primary key on the first
three. `role` is `author`, `host` or `guest`; `position` preserves byline order.
One person can hold two roles on the same publication — which is why **`role` is
part of a credit's identity**: deleting by `(publication_id, person_id)` alone
takes both.

**`source` is `parsed` or `manual` (#40), and re-parse only replaces `parsed`.**
`upsert_publication` rewrites this table from the cached HTML on every scrape,
so without the guard a credit entered by hand is destroyed by the next routine
run, silently. A hand-assigned credit is appended after the printed byline and
is otherwise an ordinary row: it counts as output, reaches the person page, and
`queries.coverage_caveats` reports how many there are, since a count that is
partly entered rather than read should say so.

**Not every role is a credit (#27).** A credit means `author` or `guest` —
`queries.CREDIT_ROLES`. Hosting is excluded from every count and ranking: a
recurring podcast host collects one link per episode, which made the
second-most-credited person in the catalog someone whose output is a role
(93 links, 92 of them hosting). The rows are kept and podcast pages still use
them; they simply do not count as anyone's output, and a person page reports
hosting as a number instead of as 92 publications.

Three people have nothing but host links and are still listed, with zero
credits — two of them are MERICS staff, so dropping them from the People list
would answer a different question than the one asked.

```sql
-- Output, the way every surface counts it
SELECT pe.name, COUNT(*) n FROM people pe
JOIN publication_people pp ON pp.person_id = pe.id
WHERE pp.role IN ('author', 'guest') AND pe.merged_into IS NULL
GROUP BY pe.id ORDER BY n DESC;
```

### `publication_text` — extracted body prose

`(publication_id PK, body, word_count, source, extracted_at)`. 1,191 rows,
2.88M words. `source` is `html` (1,132), `pdf` (57, #6) or `manual` (2, #31).

**One row per publication, so the sources compete.** A record with a 37-word
landing-page abstract and a 3,747-word PDF holds the PDF. `pdf.import_text`
never shortens a record across sources and never overwrites `manual`; it does
replace its own earlier `pdf` output, which is what makes re-extraction
idempotent (#34).

**A PDF linked by more than one publication is refused** — that is what a
compilation looks like, and importing it into each sharer copies the document N
times. The Italy chapter briefly held all 75,890 words of the 2026 ETNC report.

**But sharing is evidence, not proof** (#42). A Brief that merely *cites* a
report shares its PDF without being a chapter of anything: *Mind the Gap* sat at
130 words because one Brief linked the same file. `extract-pdf-text
--allow-shared` overrides it, and `--allow-outsized` overrides the
too-long-for-its-type guard. **Both require `--only`**, refused before a
database is opened — a bulk run with a guard off wholesale is the accident
these guards exist to prevent.

The 220 publications with no row: 212 podcasts (no prose on the page), the 5
member-only items (paywalled), 1 report that exists only as a PDF, and 2 public
Comments that are **single-image pieces** — id 189 is an infographic ("China's
nuclear industry goes global"), id 529 a timeline graphic ("Timeline: 100 days
National Security Law"). Verified against the live pages: their content field
holds only `media-image` paragraphs, there is no prose and no PDF. Correctly
text-less, not a parser gap (#6).

### `publication_fts` — FTS5 keyword index

Columns `title`, `subtitle`, `description`, `body`; `rowid` = `publications.id`.
Tokenizer `porter unicode61 remove_diacritics 2`, so "tariffs" matches "tariff"
and "Grunberg" matches "Grünberg".

**`description` is populated only where a record has no body** (11 rows — the
paywalled items, where it is the only text there is). Indexing it everywhere
put a second copy of every article in the index, weighted 3× above the
original, which distorted every bm25 ordering; migration 6 fixed that (#15).

An ordinary FTS5 table, not external-content — it holds its own copy of the
text, ~35 MB of the 64 MB database (14 MB content copy, 21 MB inverted index).
The duplication buys not having to keep triggers in sync across two source
tables. **Nothing keeps it in sync**; see traps.

### `publication_enrichment` — LLM-generated summaries (#5, #18)

**A publication may hold several summaries; exactly one is primary.** Keyed on
its own `id`, not on `publication_id`.

**Some rows were written from pictures, not text** (#35). `model` is the only
trace — a vision row carries the vision model's id and `words_sent = 0`. They
are ordinary enrichment rows on purpose, so search, the counts and topic
mapping reach them with no special-casing; nothing downstream needs to know.
Where a record already had a summary the vision pass adds a **candidate**
rather than overwriting, since a caption-derived summary is thin rather than
absent and the picture might read worse.

**Three rows in this table were written by no model at all**, and `model` is
where that is recorded:

| `model` | what it is |
|---|---|
| `written by hand` | typed on the record page (#46). `upsert_primary_enrichment` **refuses to overwrite one** — it is the only copy |
| `executive summary (verbatim)` | the report's own executive summary, copied in. Superseded: the owner found it accurate but not digestible, so those records were re-summarised *from* it instead |
| a model id | everything else |

A hand-written summary is otherwise an ordinary row: promoted through the usual
path, so the one-liner vector is rebuilt and the topics go back on
`map-topics`' worklist. Editing one updates it in place rather than stacking a
new row per sitting.

> ⚠️ **Never join this table directly. Use the `primary_enrichment` view.**
>
> `publication_id` is not unique here, so a join to the base table returns one
> row *per summary*: the publication appears twice in search results and every
> count inflates. Nothing errors, nothing warns — the results just quietly get
> worse. The view is `SELECT * FROM publication_enrichment WHERE is_primary = 1`
> and `publication_id` is unique within it, enforced by a partial unique index.
> Touch the base table only to manage candidates.

| Column | Meaning |
|---|---|
| `id` | Row identity. What `reviews.subject_id` names for scope `enrichment` |
| `publication_id` | Not unique — see the warning above |
| `is_primary` | `1` for the summary actually in use. Exactly one per publication, enforced by `one_primary_per_publication` |
| `summary_one_liner` | The recall unit. Capped at 30 words by validation, not by hope — a longer answer is re-asked, never stored |
| `summary_short` | 3–5 sentences: the argument and the so-what |
| `key_findings` | **JSON array** of strings. Read with `json_each` |
| `entities` | **JSON object** of name lists (`people`, `organizations`, `places`, `policies`). Read with `json_each` / `json_tree` |
| `model` | The model that generated the row, e.g. `provider/large-instruct` |
| `provider` | The `llm.PROVIDERS` entry the row came from |
| `prompt_version` | `enrich.PROMPT_VERSION` at generation time |
| `words_sent` | Body words actually sent, capped at 6,000 |
| `prompt_tokens` / `completion_tokens` / `seconds` | Summed across retries, so they are the true cost of the row |
| `attempts` | 1 unless validation forced a re-ask |
| `enriched_at` | UTC ISO timestamp |

`model`, `provider` and `prompt_version` exist so a re-run can replace rows
selectively — regenerate everything one model wrote, or everything written
under an older prompt, without touching the rest:

```sql
SELECT model, prompt_version, COUNT(*), AVG(attempts)
FROM primary_enrichment GROUP BY model, prompt_version;
```

**Candidates (#18).** Regenerating a summary *adds* one rather than
overwriting: the existing primary keeps serving search until it is promoted,
so two models' readings can be compared side by side before anything changes.
Promotion demotes the incumbent (it is kept, not deleted) and **deletes the
one-liner vector**, which described the summary that is no longer being
searched. Dismissing deletes a candidate; the primary cannot be dismissed,
because a publication with no primary is invisible to search.

```sql
-- Publications holding more than one summary — the comparison worklist
SELECT publication_id, COUNT(*) n FROM publication_enrichment
GROUP BY publication_id HAVING n > 1;
```

**Only publications with body text can be enriched**, so the ceiling is 1,108
rows, not 1,328 — the 212 podcasts and the paywalled items have no prose to
summarize and never enter the worklist. `enrich` derives its worklist from
what is missing, so re-running it resumes rather than duplicating.

Entity names are extracted, not inferred — the prompt forbids adding anything
absent from the text. `pubbrain enrichment --full` prints a grounding score
(share of names literally present in the body) as a cheap hallucination check.

**Coverage is complete for what can be enriched — 1,108 of 1,328.** The gap is
not a backlog: the 212 podcasts and the paywalled items have no body text, so
they have no summary and never will from this path. Joining this table to
`publications` therefore drops every podcast silently. `LEFT JOIN` unless that
filter is deliberate, and check the denominator first:

```sql
SELECT (SELECT COUNT(*) FROM primary_enrichment) AS enriched,
       (SELECT COUNT(*) FROM publication_text)   AS enrichable;
```

### `publication_blurbs` — the plain-language layer (#38)

`(publication_id PK, blurb, source_enrichment_id, model, provider,
prompt_version, …)`. One per publication, regenerable, written by `blurb`.

**Not a competing summary, which is why it is not a `publication_enrichment`
row.** That table means "one reading of the piece, of which one is primary"; a
blurb is the same reading in a different register — the one-liner explained to
someone who does not have the vocabulary. It is generated **from the primary
summary, not the body text**, so it cannot re-introduce detail the summary
correctly dropped.

**`source_enrichment_id` is what keeps it honest.** Promote a different
candidate (#18) and the blurb no longer describes the summary it sits beside;
`blurb.for_publication` returns `stale` for exactly that case and
`blurb --include-stale` re-runs them. Without it a blurb written from a dismissed
reading would sit under the new one looking current.

**Never in FTS, and deliberately.** Plain wording would dilute a keyword index
built on the terms the documents actually use — the same reason the one-liner
is written in the register of the piece. Queries about *what a publication is*
read this; queries that *find* a publication must not.

### `publication_sections` — body text split at its headings (#16)

`(id, publication_id, position, heading, level, body, word_count, independent,
is_boilerplate, chunk_index, chunk_total)`. 9,923 rows over the 1,135
publications with body text. Built by `extract-sections`, deterministic and
local — no model.

**A row is a retrieval unit, not always a section the author wrote.** Count
authored sections with `chunk_index IS NULL OR chunk_index = 0`; a plain
`COUNT(*)` counts windows and overstates the structure — 3,305 of the rows are
windows, over 230 publications.

- `heading` is NULL for the text before the first heading, which is kept so no
  prose is lost. `level` is the markdown depth, NULL for that preamble.
- Headings whose section has under 20 words are dropped — they are labels
  ("Analysis", "Update"), not sections.
- `independent = 1` means the sections are separate stories rather than parts
  of one argument. Set only for MERICS Briefs and Trackers with two or more
  non-boilerplate `##` sections; 288 publications qualify. **Nothing reads this
  column yet** — it is computed for section-level enrichment (#16, not built).
  Do not treat it as load-bearing.
- **`is_boilerplate = 1` marks a standing feature, not a story** — 511 rows.
  METRIX (one statistic), MERICS CHINA DIGEST and Short Takes (link
  collections), Policy/Corporate News (Tracker chapter subsections), Buzzword
  and Graphic of the Week, What to Watch. They stay searchable but are
  **excluded from embeddings** (#17): a link list retrieves on its format
  rather than any subject. The set lives in `sections.STANDING_FEATURES` and is
  matched on heading text, since these appear at both `##` and `###`.

- **`chunk_index` / `chunk_total` mark a window, not an authored boundary**
  (#34). NULL on a real section. `embed.MAX_WORDS` truncates at 1,000 words
  silently, so a section over `sections.CHUNK_CEILING` (700) is stored as
  overlapping ~350-word windows sharing the parent's `heading` and `level`.
  Before this, 156 sections were longer than the embedder read and one
  56,582-word report was represented by its opening page. `position` stays a
  single sequence over the publication, so windows and sections interleave in
  document order.

**PDF text carries recovered headings, not authored markup** (#34). `pdftotext`
emits no structure, so `pdf.to_markdown` infers headings from line height via
`-bbox-layout` and writes them as `##`/`###` — 50 of the 56 PDF-sourced records
get real chapter boundaries this way. The other 6 fall back to flat text plus
windowing, because a document whose body copy sits above the modal line height
(the 2014 China Monitors, *Shaky China*) would otherwise turn every line into a
heading. Depth is assigned by relative type size, so `##` vs `###` is *this
document's* hierarchy and does not compare across records.

**`##` and `###` do not mean what the nesting suggests, and it varies by type.**
In a MERICS Brief a `##` is an individual story and a `###` is a recurring
standing feature — METRIX, MERICS CHINA DIGEST, the Europe-China Diplomatic
Tracker, Short takes. In a Report the headings are one argument's structure.
Never treat depth as importance, and never split a Report into recall units.

### `section_fts` — FTS5 index over sections

Columns `heading`, `body`; `rowid` = `publication_sections.id`. Same tokenizer
as `publication_fts`. Ranked with `bm25(section_fts, 8.0, 1.0)` — a hit in a
section's own headline beats one in its prose.

**Sections add locality, not matches.** A section's text is a subset of its
publication's body, so any section hit implies a publication hit. `search`
therefore ranks on `publication_fts` as before and uses `section_fts` only to
say *which part* matched. Do not merge bm25 scores across the two tables — they
are computed over different corpora and are not comparable.

Like `publication_fts`, it is **not self-maintaining**: re-run
`python -m pubbrain extract-sections` after `extract-text`.

### `embeddings` — local vectors for semantic search (#17)

`(id, source_type, source_id, publication_id, model, dim, vector, embedded_at)`,
unique on `(source_type, source_id, model)`. 10,547 rows, ~31 MB.

| `source_type` | n | `source_id` is | what it is |
|---|---|---|---|
| `section` | 9,412 | `publication_sections.id` | body prose, boilerplate excluded |
| `one_liner` | 1,135 | `publications.id` | one-liner + short summary |

Two indexes on purpose — they retrieve differently and can be compared rather
than one overwriting the other. `model` is part of the key, so switching models
builds a separate index instead of silently mixing two vector spaces.

**Vectors are stored L2-normalised**, so cosine similarity is a plain dot
product. `embed.unpack()` reads a blob back to a 768-float array. There is no
ANN index and none is needed at this size — a full scan is microseconds.

**`embeddinggemma` is asymmetric**: documents and queries use different
prefixes (`embed.DOC_TEMPLATE` / `QUERY_TEMPLATE`). Encoding a query as a
document degrades retrieval and nothing downstream will show it, so always go
through `embed.embed_query()` rather than hand-rolling the call.

Rebuilding the whole index takes about a minute and costs nothing — it runs
locally through ollama. Treat re-embedding as cheap.

**`extract-sections` invalidates every section vector.** It deletes and
reinserts sections, so their ids change and the old vectors become orphans
pointing at rows that no longer exist. `source_id` is polymorphic and cannot
carry a foreign key, so nothing removes them automatically — the command calls
`purge_orphan_embeddings` itself and tells you to re-run `embed`. Skipping that
leaves duplicate vectors that let a publication rank twice for the same text.
The invariant to check:

```sql
SELECT COUNT(*) AS orphans FROM embeddings
WHERE source_type = 'section'
  AND source_id NOT IN (SELECT id FROM publication_sections);   -- must be 0
```

```sql
-- Which publications have no section vectors at all
SELECT p.id, p.title FROM publications p
LEFT JOIN embeddings e ON e.publication_id = p.id AND e.source_type = 'section'
WHERE e.id IS NULL;
```

### `publication_topics` — the controlled vocabulary, mapped (#4)

| Column | Meaning |
|---|---|
| `publication_id` | |
| `topic_slug` | A slug from `pubbrain/topics.yaml`. **Not a foreign key** — the vocabulary is a file, not a table |
| `position` | The model's own ranking. `1` is what the piece is mainly about |
| `model`, `prompt_version`, `mapped_at` | Provenance, as on `publication_enrichment` |

`PRIMARY KEY (publication_id, topic_slug)`, 1–4 topics per publication.

**Query by slug, display by name.** The slug is the identity; `topics.yaml`
holds the label, so a topic can be renamed without touching this table.
`topics.labels()` maps one to the other and callers must tolerate a missing
key — a slug retired from the YAML sits here until its publications are
re-mapped.

**`position = 1` is the trustworthy signal.** Single-argument pieces get one
or two topics and they are reliable. A MERICS Brief is several unrelated
stories (#16) and legitimately gets three or four, where the last is
sometimes stretched from a single clause. Filter on `position = 1` for "what
is this about"; take all rows for "what does this touch".

**Coverage is enrichment's, not the catalog's.** Mapping reads the generated
summary, so the 1,108 records with body text are mappable and the 212
podcasts and paywalled handful are not — they carry no topic at all. A topic
filter silently excludes 17% of the catalog; say so rather than implying the
count is complete.

```sql
-- Everything on Taiwan, most recent first
SELECT p.date_published, p.pub_type, p.title
FROM publication_topics pt JOIN publications p ON p.id = pt.publication_id
WHERE pt.topic_slug = 'taiwan'
ORDER BY p.date_published DESC;

-- Per-topic counts, primary only
SELECT topic_slug, COUNT(*) n FROM publication_topics
WHERE position = 1 GROUP BY topic_slug ORDER BY n DESC;
```

### `site_tags` / `publication_site_tags` — the site's own tags

18 tags, and only **147 of 1,328 publications (11%) carry any tag at all**.
Stored for reference only and **not the topic field** (Brief §4) —
`publication_topics` above is. At 11% coverage a tag miss is close to
meaningless as evidence — never let an empty tag probe stand for absence.

### `collections` / `publication_collections` — standing projects (#32)

`collections` is keyed on `slug`; `publication_collections` is
`(publication_id, collection_slug, source, added_at)`. A collection is a
dashboard or a series — a *container* that is not itself a publication. Where
the container **is** a MERICS publication, use `parent_id` instead (above).

Members keep their own `pub_type` and `series` — two China-Russia Dashboard
members are China Essentials Briefs and one is a Security and Risk Tracker —
which is why membership is a relation and not a column. Members also **stay in
the ordinary listing**; unlike chapters, they are not hidden behind a parent.

**`source` is `auto` or `manual`, and `auto` never overwrites `manual`.**
Detection re-runs constantly and would otherwise undo hand corrections.

**A dashboard is a collection and nothing else** (#32, owner 2026-08-09).
There is no `Dashboard` `pub_type` and no record for the four dashboard pages;
they were ingested as records and removed the same day. The reasoning is worth
keeping because the pages invite the question again: their *members* are all
ordinary searchable records, and what has no record is each landing page's own
prose — 3,898 words on the China-Russia Dashboard, written with OSW and the
Swedish National China Centre. That text is reachable on merics.org and through
the project page, and the owner judged that sufficient. So a project page is
navigation; only a publication is content.

**ETNC is the worked example of collection-versus-series.** Twelve annual
volumes, 2015–2026, and the run spans two record types: the older six are
`External publication` (a merics.org landing page for a report published
elsewhere), the newer ones are `Report` with MERICS chapters under `parent_id`.
It is a collection and **not** a `series`, because `series` means a recurring
*MERICS format* everywhere else in this catalog — China Essentials, Economic
Indicators, the Trackers — and ETNC is a multi-institute network MERICS
contributes to. There is no ETNC publication, only volumes, which is the
collection rule exactly. Owner decision, 2026-08-09.

**Two detection directions, and they scan different scopes on purpose.**

| | direction | scope |
|---|---|---|
| `collect.links_to` / `detect` | member links *to* the collection page | the member's own `<article>` |
| `collect.from_page` | the collection page links *to* the member | the **whole** page |

The asymmetry looks like a bug and is not. A series landing page is a Drupal
*listing*: its members are teaser cards outside the page's own article, so
applying the article rule here takes all eight collections to **zero** members.
Inbound needs the article rule for the opposite reason — a naive scan returned
66 pages for the Tech Observatory, 60 of them a site-wide promo.

**The price of the whole-page scan is `collect.PROMO_VIEWS` (#41).** The
`latest_newsletter` Drupal view appears on five of the eight collection pages
and contributes exactly one link each, which put a 2026 Brief into a 2020
mini-series. Links inside a promo view are dropped; anything added there in
future must be re-derived, since `--from-page` fetches live and does not write
`data/raw/`, so past harvests cannot be re-checked offline.

### `shortlist` — the publications that mattered (#25)

`(publication_id PK, note, added_at)`. **The only human signal in this
database.** Everything else is scraped from merics.org or generated by a model;
this is the owner's own judgement that a piece was important, made waves, or
was especially well received. The `note` carries the reason and is the part
worth reading.

Its own table rather than a `reviews` scope, deliberately: a verdict describes
a *generated row* and is deleted when that row is (#18), whereas this describes
the publication and must outlive any number of summary rewrites.

**It changes search ranking.** `queries.hybrid_find` adds
`queries.SHORTLIST_BOOST` to a shortlisted record's fused score — worth two
positions. Two consequences that are easy to lose:

- **Every surface that shows ranked results must show the badge.** A result
  that ranks higher for a reason the page does not display reads as the ranking
  being broken. `hybrid_find` sets `shortlisted` on every hit, boosted or not.
- **`boost_shortlist=False` is the honest-measurement path.** With the boost
  on, any retrieval evaluation measures retrieval *plus* curation and the
  recorded baseline (87% recall@1, #17) stops being comparable.
  `scripts/eval_retrieval.py` sidesteps this by fusing directly rather than
  calling `hybrid_find`; if it is ever switched over, it must pass the flag.

The size was calibrated against the worst case rather than the typical one.
Fused across several rankers, almost any boost is only a tiebreaker; but when
one ranker supplies the whole list — keyword-only, which is what search
degrades to when ollama is down — RRF gaps are uniform and the boost moves a
result exactly two places.

```sql
-- What you marked, and why
SELECT p.date_published, p.title, s.note
FROM shortlist s JOIN publications p ON p.id = s.publication_id
ORDER BY s.added_at DESC;
```

### `reviews` — human verdicts on generated rows

| Column | Meaning |
|---|---|
| `scope` | What kind of thing was reviewed. Only `enrichment` so far (#18/#19/#21) |
| `subject_id` | Id in the scope's table — for `enrichment`, the **`publication_enrichment.id`**, not the publication (#18): with candidates in play, a verdict has to name the summary it read |
| `verdict` | For `enrichment`: `confirmed` (spot-checked ok) \| `flagged` (misleads — see `note`) |
| `note` | Free text. Spot-check rows are prefixed `fable-sample:`; the owner's flags carry his own words |
| `created_at` | UTC ISO timestamp |

`UNIQUE (scope, subject_id)` — one verdict per subject, a re-flag replaces it,
clearing a flag deletes the row. **Trust is the default (#21):** a summary
needs no verdict to be used; `flagged` marks the exceptions, `confirmed`
records spot checks (30-sample review, 2026-08-08: 30 ok, 0 flagged — #18).
**A verdict describes the row as it was when reviewed**: regenerating a
summary invalidates its verdict, and whatever regenerates must delete the
matching review row. Written by the workbench's flag form, never by any
pipeline step.

### `landscape_coords` — the embedding landscape's cached layout (#49)

`(publication_id PK → publications, x, y, placed, computed_at)`. One row per
point on the Insights landscape: a 2D t-SNE projection of the one-liner
vectors, parents only, Insights exclusions applied (no podcasts).

**The cache is the contract, not an optimisation.** The layout must be
identical on every visit — spatial memory is the feature — so coordinates are
never recomputed on read. `placed` says how a row got its position: `fit`
(the full deterministic t-SNE run, `pubbrain landscape --refit`) or
`incremental` (a new publication slotted in at the weighted centroid of its
nearest already-placed neighbours; nobody else moves — the web data endpoint
does this on the way past).

Traps: **a missing vector does not remove a point** — a publication whose
summary is being regenerated (#24) keeps its place rather than flickering off
the map; only leaving scope (becoming a chapter, deletion, an excluded type)
prunes a row. And the coordinates are meaningless outside their own fit —
never join `x`/`y` against anything but rendering; distances between runs, or
to points placed incrementally, are not comparable measurements.

### `upcoming_notes` / `upcoming_topics` / `upcoming_people` — what is coming (#56)

`upcoming_notes (id PK, working_title, note, expected, created_at, updated_at,
landed_publication_id → publications, landed_at, shelved_at, shelved_reason)`,
plus `upcoming_topics (note_id, topic_slug, position)` and
`upcoming_people (note_id, person_id)`.

Hand-entered notes on publications that **do not exist yet** — the catalog
knows what merics.org published, this is what the owner knows is coming.
Nothing generates them and nothing links them automatically.

**Two rules, and both are the reason these are their own tables:**

- **Views opt in; nothing inherits.** No publication query reaches them.
  `_insight_scope`, search, the FTS index, embeddings and every list are
  unchanged by their presence — a surface that wants notes names `upcoming_*`
  explicitly. This is deliberate defence against the failure mode the rest of
  this file documents (`parent_id IS NULL`, `merged_into IS NULL`,
  `primary_enrichment`): a base table that quietly holds more than it says.
- **Note text is internal knowledge and never leaves the machine.** It is not
  published material: no cloud-LLM call receives it (topics are hand-picked
  from the frozen vocabulary, never mapped by a model), it appears in **no MCP
  tool response**, and it is excluded from FTS, embeddings and the retrieval
  eval. Anything remote (#48) leaves it behind unless the owner decides
  otherwise.

**Status is derived, never stored**: `landed_publication_id` set → *landed*;
`shelved_at` set → *shelved*; neither → *expected*. A `CHECK` refuses the one
impossible combination, and `queries.upcoming_notes` computes the rest — a
status column would be a second copy free to disagree.

**`expected` is a quarter (`'YYYY-Qn'`), not a date.** The knowledge behind it
is "roughly Q4"; a month would fabricate precision. It sorts lexicographically
within and across years, and `db._quarter` refuses anything finer rather than
truncating it. Nuance ("late in the quarter, probably slipping") belongs in
`note`.

**Lifecycle is link, don't delete.** A landed note keeps its row — it is the
only record of how the pipeline maps to output, and `queries.upcoming_notes`
reports median lead time (`created_at` → `date_published`) and expected-vs-
actual quarter hit rate from it, held back below three landings. Shelving
takes a reason for the same reason. Nothing deletes a note; `reopen` undoes a
mis-clicked shelve or link.

### `sitemap_urls` — the crawl worklist

`(url PK, path_prefix, lastmod, scope, status, scraped_lastmod, last_error,
first_seen, last_seen)`. `scope` is `publication` (has a type prefix),
`root-level` (no prefix — 170 of these, unresolved, #10) or `excluded`.
`status` is `pending` \| `done` \| `skipped` \| `failed` \| `gone`. A changed
`lastmod` resets `done` back to `pending`, which is what drives re-scraping.

**`gone` means the sitemap outlived the page** (#13): the URL is still listed
but merics.org either redirects it to a section landing page or answers 403 /
404 / 410. Five publication URLs are in this state — four redirect to
`/en/analysis` or `/en/opportunities`, one 403s on every user-agent. They are
not retried, because `failed` would re-fetch them on every run forever, and
they are not `skipped`, because that means "this page is not a publication"
which reads as a parser judgement about a page that exists. They were real
podcast episodes, so an absence claim still has to own them —
`coverage_caveats` counts them.

## Traps

- **`is_internal` is superseded by `affiliation`.** It is a crude scrape-time
  guess; `affiliation` plus `is_current` is the answer. Do not query
  `is_internal`.
- **`pub_type` is the page's own value, not the URL prefix, and the two cut
  across each other in both directions.** `/en/analysis/` (4) and
  `/en/short-analysis/` (1) are typed `Tracker`; `/en/briefing/` (12) are
  `MERICS Briefs`; and 6 URLs under `/en/report/` are typed
  `External publication`. Filter on `pub_type` — but do not describe the result
  as "everything under /en/report".
- **422 publications have no `author`-role person, and 236 of those link no
  person at all** (#12). The 236 are not "a further" 236: they are the subset
  that credits nobody. Their composition — 122 MERICS Briefs, 55 Comments, 26
  Podcasts, 16 Interviews, 15 Reports, 1 Tracker, 1 External publication — is
  worth knowing, because 15 uncredited Reports is the surprising bucket. All
  212 podcasts fall in the 422 (a podcast credits a host and guests, not an
  author; 184 have a host, 26 credit nobody at all). An inner join silently
  drops all of these; `LEFT JOIN` when the total must be right.
- **Credits are incomplete even where they exist**, so `LEFT JOIN` is not a
  cure. 54 publications that already credit someone fail to link a person
  named in their own title — mostly interviews, where the interviewee appears
  in the headline but never in the authors field. "Everything by X"
  under-reports even after every precaution above. Cross-check with a title
  search (`MATCH 'title:"Max J. Zenglein"'`) before reporting a count as
  complete.
- **`is_flagship` is always 0.** It is not written by the parser, so
  `WHERE is_flagship = 1` returns nothing — that is not the same as MERICS
  having no flagship reports.
- **`pdf_path` is populated on 59 of the 542 records with a `pdf_url`**, and
  those 59 are a deliberate selection, not a backlog: `download-pdfs
  --needed-only` fetches only where the record is thin, the PDF is
  single-owner and MERICS-hosted. A NULL `pdf_path` mostly means "not worth
  downloading", so do not read it as a coverage gap.
- **`url` is the real key; join and dedupe on `url` or `id`.** `slug` carries a
  `UNIQUE` constraint, so a collision cannot corrupt a query — but it is only
  the last URL segment, so ingesting the root-level pages (#10) could make one
  fail to insert. Treat `slug` as the cache filename it is, not an identifier.
- **The FTS index is not self-maintaining.** No triggers. Re-run
  `python -m pubbrain index-fts` after `scrape`, `reparse` or `extract-text`,
  or searches silently return stale results. `python -m pubbrain status` flags
  it, and this is the same check in read-only SQL — both numbers must be 0:

  ```sql
  SELECT (SELECT COUNT(*) FROM publications) -
         (SELECT COUNT(*) FROM publication_fts)            AS missing_rows,
         (SELECT COUNT(*) FROM publications p
          JOIN publication_fts f ON f.rowid = p.id
          WHERE f.title IS NOT p.title)                    AS stale_titles;
  ```
- **Hyphenated terms must be quoted, or the query throws.** This is the one
  that bites hardest, because the vocabulary it breaks is MERICS' own:

  | Written as | Result |
  |---|---|
  | `de-risking` | `OperationalError: no such column: risking` |
  | `US-China` | `OperationalError: no such column: China` |
  | `"de risking"` | 116 hits — correctly matches "de-risking" |

  The tokenizer splits on the hyphen, so `de-risking` reaches the parser as
  `de` `-` `risking` and the `-` is read as a column filter. Write hyphenated
  terms as quoted phrases: `'"de risking"'`, `'"US China"'`, `'"EU China"'`.
  Inside the phrase the hyphen may be a space or a hyphen — both tokenize the
  same.

  **The hyphen is only the most common case.** Any punctuation is syntax to
  FTS5: a trailing `?` on a natural question raises too, which cost the /ask
  page (#24) its keyword ranking until it was noticed. `db.fts_safe` rewrites
  arbitrary text into a valid expression, leaving quoted phrases, `AND`/`OR`/
  `NOT` and `chip*` prefixes alone. Every programmatic caller goes through it;
  `search` stays strict so an interactive typo is reported.
- **FTS `MATCH` takes FTS5 syntax, not SQL `LIKE`.** Bare terms are AND-ed;
  `"rare earth"` is a phrase; `OR` and `NOT` must be uppercase (lowercase `or`
  is just another search term — `rare earth OR semiconductor` finds 308,
  lowercase `or` finds 41). `:` is a column filter, which is useful on purpose
  — `title:overcapacity` finds 9 where a bare `overcapacity` finds 126 — and
  `*` is a prefix search (`AI*` → 686). A malformed expression raises
  `sqlite3.OperationalError`, never an empty result, so an error is never a
  "no coverage" answer.
- **Site tags are not topics** (Brief §4). Use them as a weak reference probe
  only, and say so in any answer that leans on them.
- **Enrichment is not in the FTS index.** `publication_fts` covers title,
  subtitle, description and body only, so a `MATCH` never searches the
  generated one-liners or findings. Query those with `LIKE` on
  `primary_enrichment`, and do not treat an FTS miss as evidence that no
  summary mentions the term.
- **`key_findings` and `entities` are JSON text, not tables.** `WHERE entities
  LIKE '%Xi Jinping%'` also matches a publication whose *places* list contains
  it. Use `json_each` when the distinction matters:

  ```sql
  SELECT p.title, j.value AS person
  FROM primary_enrichment e
  JOIN publications p ON p.id = e.publication_id
  JOIN json_each(e.entities, '$.people') j
  WHERE j.value LIKE '%Xi Jinping%';
  ```
- **`External publication` means "MERICS made a landing page for it", not
  "MERICS published externally"** (#12). The 16 records of that type are
  merics.org pages under `/en/external-publication/…` that point offsite — they
  are in the catalog *because* a landing page exists. Where MERICS publishes
  with a partner and no landing page is made, the piece is absent from the
  sitemap, absent from here, and **unmeasurable**: nothing on merics.org
  records it, so no query can count what is missing. `coverage_caveats` carries
  this as a constant clause with no number, which is the only caveat in that
  list that cannot be computed. A person query under-reports for this reason
  too, and no join fixes it.
- **The `upcoming_*` tables are not the catalog and must not be joined into
  it** (#56). They hold internal notes on unpublished work: never counted as
  publications, never in an FTS or embedding result, and never in anything an
  LLM or an MCP client sees. A coverage answer says "not covered" when only a
  note exists, because nothing has been published.

## Query recipes

**Several of these now exist as MCP tools** (`pubbrain/mcp_server.py`, #23) and
as functions in `pubbrain/queries.py`: `find`, `search`, `publication`,
`person`, `list_publications`, `coverage_check`, `status`, `glossary`. Prefer
those — they carry the caveats a raw query does not. The SQL stays here because
it is what they wrap, and because a new question needs a new query first.

Ranking uses `bm25(publication_fts, 10.0, 5.0, 3.0, 1.0)` — title, subtitle,
description, body. Lower is better, so plain `ORDER BY` puts the best first.
`snippet()`'s column index of `-1` picks whichever column matched best, which
is what keeps records with no body text readable in results.

Two of those weighted columns are nearly always empty — `subtitle` is NULL for
1,153 of 1,328 rows, and `description` is populated for just 11 (#15) — so in
practice ranking is title against body. The weights are not doing subtle work;
do not tune them on the assumption that all four columns carry text.

### All Reports in a year

`parent_id IS NULL` is not optional here — without it a report that publishes
its chapters as their own pages is counted once per chapter (#36).

```sql
SELECT date_published, title, url
FROM publications
WHERE pub_type = 'Report'
  AND parent_id IS NULL
  AND date_published BETWEEN '2025-01-01' AND '2025-12-31'
ORDER BY date_published DESC;
```

### A report and its chapters

```sql
SELECT c.parent_position, c.title, t.word_count
FROM publications c
LEFT JOIN publication_text t ON t.publication_id = c.id
WHERE c.parent_id = (SELECT id FROM publications WHERE slug = 'shaky-china-five-scenarios-xi-jinpings-third-term')
ORDER BY c.parent_position;
```

### Everything by ⟨analyst⟩, grouped by role

Returns authored pieces first, then hosted, then guest appearances, with the
person's affiliation and current/former status on every row. Nothing hidden,
nothing conflated — a guest appearance is not authorship, but it is not
invisible either.

```sql
SELECT pp.role,
       p.date_published,
       p.pub_type,
       p.title,
       p.url,
       pe.name,
       pe.affiliation,
       CASE pe.is_current WHEN 1 THEN 'current' ELSE 'former' END AS status,
       COALESCE(pe.roster_title, pe.job_title) AS person_title
FROM people pe
JOIN publication_people pp ON pp.person_id = pe.id
JOIN publications p        ON p.id = pp.publication_id
WHERE pe.slug = 'rebecca-arcesati'          -- or: pe.name = 'Rebecca Arcesati'
ORDER BY CASE pp.role WHEN 'author' THEN 0 WHEN 'host' THEN 1 ELSE 2 END,
         p.date_published DESC;
```

`pe.slug` is the reliable lookup. **`pe.name` is not interchangeable with it** —
the row is `Max J. Zenglein`, so `WHERE name = 'Max Zenglein'` returns nothing,
while merics.org's own podcast titles write it without the initial. Match on
slug, or on `name LIKE '%Zenglein%'`.

Report the role split alongside the list, and count publications rather than
role rows — a person can be both host and guest on one podcast, which is why
Arcesati's 86 role rows are 85 publications:

```sql
SELECT pp.role, COUNT(*) AS role_rows,
       COUNT(DISTINCT pp.publication_id) AS publications
FROM people pe JOIN publication_people pp ON pp.person_id = pe.id
WHERE pe.slug = 'rebecca-arcesati'
GROUP BY pp.role;
```

Always state current-vs-former in the answer: a former staffer's back catalogue
is still MERICS output, but they cannot be asked about it.

### Has MERICS written about X?

```sql
SELECT p.date_published, p.pub_type, p.title, p.url,
       snippet(publication_fts, -1, '[', ']', ' … ', 12) AS snippet
FROM publication_fts
JOIN publications p ON p.id = publication_fts.rowid
WHERE publication_fts MATCH '"rare earth"'
ORDER BY bm25(publication_fts, 10.0, 5.0, 3.0, 1.0)
LIMIT 20;
```

`python -m pubbrain search '"rare earth"'` is the same query as a smoke test.

### `coverage_check` — and how to report a negative

**This recipe is live as `queries.coverage_check`, and as the MCP tool of the
same name (#23).** Use one of those unless you are working out a new query; the
SQL below is what they wrap.

Three probes, then an honest verdict. Run all three before concluding anything:

```sql
-- 1. Full text: titles, subtitles, descriptions, body prose.
SELECT COUNT(*) FROM publication_fts WHERE publication_fts MATCH '<query>';

-- 2. Site tags — a weak reference probe, NOT the topic field.
SELECT p.title, p.url, st.name AS tag
FROM publications p
JOIN publication_site_tags pst ON pst.publication_id = p.id
JOIN site_tags st              ON st.id = pst.tag_id
WHERE st.name LIKE '%<term>%';

-- 3. People: the query may name an analyst rather than a subject.
SELECT name, affiliation, is_current FROM people WHERE name LIKE '%<term>%';
```

Before concluding "no", make sure the query itself was not the problem: a
hyphenated term raises an error rather than returning nothing (traps), and
`sqlite3.OperationalError` is never a negative result.

If all three come back empty, the negative must state its own scope rather than
claim a completeness the catalog does not have:

> No coverage found, within these limits: *(the clauses from
> `queries.coverage_caveats`)*

**The clauses are measured per call, not written down here.** Each names a gap
that has an issue behind it — un-ingested legacy URLs (#10), pages that failed
scraping (#13), records crediting nobody (#12), records with no body text — and
each drops out of the wording on its own once its count reaches zero. A
paragraph maintained by hand in this file would go on citing gaps after they
closed, and would be a second copy to keep in step with the code besides.

**A positive answer is scope-limited by the same clauses.** "126 publications
mention overcapacity" is bounded by the un-ingested pages and by podcasts being
matchable on title alone. Report a count as a floor, not a total.

### Most active people over a date range, by role

```sql
SELECT pe.name,
       pe.affiliation,
       CASE pe.is_current WHEN 1 THEN 'current' ELSE 'former' END AS status,
       SUM(pp.role = 'author') AS authored,
       SUM(pp.role = 'host')   AS hosted,
       SUM(pp.role = 'guest')  AS guested,
       COUNT(*)                AS total
FROM publication_people pp
JOIN people pe        ON pe.id = pp.person_id
JOIN publications p   ON p.id = pp.publication_id
WHERE p.date_published BETWEEN '2025-01-01' AND '2025-12-31'
GROUP BY pe.id
ORDER BY total DESC
LIMIT 15;
```

Hosts dominate this table by design — a podcast host racks up appearances far
faster than an analyst writes reports. Read the columns, not the total.

## Not available yet

Topic queries are available — see `publication_topics`. What is still missing
is a topic surface in the workbench and the MCP server (#22): the data exists,
the filters do not.

Semantic search now exists. **`python -m pubbrain find '<query>'` is the one to
reach for** — it fuses bm25 and both vector indexes by rank (reciprocal rank
fusion; their scores are not comparable, so they are never summed).

Measured on 23 fixed questions (`scripts/eval_retrieval.py`), combined recall@1:
keyword 57%, sections 74%, one-liners 83%, **hybrid 87%**. Hybrid wins at every
cut-off, and by the widest margin at recall@3 (96% against 91%), because the
keyword half rescues named entities — "Made in China 2025", an analyst's name —
where an embedding returns a merely-similar concept. Use `find` by default,
`search` when the exact string is known.
Use it when the query is a concept rather than a term — keyword search AND-s
its words, so a phrase like "punishing a country economically for a political
decision" returns noise, while the vector index finds the coercion literature.
Use `search` for names, exact terms and phrases, where bm25 is stronger.
