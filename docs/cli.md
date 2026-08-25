# Running pub-brain

Every pipeline stage is a subcommand of `python -m pubbrain`.


```sh
pip install -r requirements.txt

python -m pubbrain sync-sitemap     # refresh the URL worklist from the sitemap
python -m pubbrain scrape           # fetch pending pages (--limit N for a test run)
python -m pubbrain reparse          # re-run the page parser over the cache (no network)
python -m pubbrain extract-text     # parse body text out of the cached HTML (no network)
python -m pubbrain sync-roster      # refresh who is currently at MERICS, re-classify people
python -m pubbrain extract-sections # split body text at its headings (no network)
python -m pubbrain index-fts        # rebuild the keyword search index (no network)
python -m pubbrain find QUERY       # hybrid keyword + vector search (start here)
python -m pubbrain search QUERY     # keyword only — exact terms and phrases
python -m pubbrain embed            # local vectors for semantic search (ollama)
python -m pubbrain semantic-search Q # vector search — finds paraphrase, not just words
python -m pubbrain enrich           # LLM summaries for publications that have none
python -m pubbrain vision           # summaries for publications that are a picture
python -m pubbrain enrichment       # print stored summaries for review (--full)
python -m pubbrain chapters PARENT REFS…   # attach a report's chapters (#36)
python -m pubbrain add-pdf-record URL      # a publication that exists only as a PDF
python -m pubbrain map-topics       # assign vocabulary topics to summaries (--remap)
python -m pubbrain topics           # the vocabulary with its counts (--samples N)
python -m pubbrain landscape        # 2D layout for the Insights landscape (--refit)
python -m pubbrain web              # local workbench on http://127.0.0.1:8901 (--host 0.0.0.0 for the LAN)
python -m pubbrain mcp              # serve the catalog as MCP tools (stdio)
python -m pubbrain status           # worklist and catalog counts
```

Run `sync-roster` after `reparse`: reparsing can introduce people the roster
has not classified yet. Run `index-fts` and `extract-sections` after anything
that changes titles or body text — neither index keeps itself current, and
`status` flags the publication one when it has gone stale.

Querying the catalog is documented in [docs/schema.md](docs/schema.md):
tables, the traps that bite query writers, and copy-pasteable SQL recipes.

## MCP

`.mcp.json` registers the server for Claude Code in this directory — no setup
beyond restarting it. For Claude Desktop, add the same command to its
`claude_desktop_config.json` with `PYTHONPATH` pointing at this repo in `env`
(done on this machine, #58) — the server itself is cwd-independent, so no
`cwd` is needed anywhere. The tools wrap
`queries.py`, so they answer exactly as the CLI and the workbench do; only
`flag_summary` writes.

`--transport streamable-http` serves the same tools over HTTP on 127.0.0.1:8902
for a future tunnelled deployment. Nothing about that is set up yet, and the
server binds localhost either way.

Only the MCP server needs the `mcp` package. Every other command runs without
it.

All steps are re-runnable: `sync-sitemap` re-queues a page only when its
sitemap `lastmod` changed, and `scrape` upserts by URL.

`enrich` needs an API key in the desktop keyring (`secret-tool`); it is never
read from a file or an env var. Its worklist is whatever has body text and no
summary yet, so an interrupted run resumes by re-running the same command, and
`--limit` bounds a trial. `--sensitive` takes the Xinjiang/Taiwan/Hong Kong
slice first, which is where a model's own alignment would show up.

Everything lands under `data/` (gitignored): `catalog.db` plus a `raw/` cache
of every fetched page, so re-parsing never means re-crawling. Set
`PUBBRAIN_DATA_DIR` to put it elsewhere.

Crawling is rate-limited to one request every 2s by default (`--delay`).
