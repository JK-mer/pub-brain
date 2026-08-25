"""Spike for #14: measure the enrichment footprint and check output quality.

Not wired into the CLI and not the real pipeline — #5 decides that shape. This
exists to answer two questions on a sample: what a full run costs in requests,
tokens and wall-clock, and whether the model's output is usable.

  python scripts/spike_enrich.py --sample 10
  python scripts/spike_enrich.py --sample 6 --models provider/large-instruct,provider/small-instruct
"""

import argparse
import json
import pathlib
import random
import re
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pubbrain import llm, paths  # noqa: E402

SYSTEM = """You summarize think-tank publications for a personal recall tool.
The reader works at MERICS in a non-analyst role and wants to remember what \
the institute has published and who works on what.

Reply with JSON only, no code fence, matching exactly:
{"summary_one_liner": "...", "summary_short": "...",
 "key_findings": ["...", "..."], "entities": {"people": [], "organizations": [],
 "places": [], "policies": []}}

Rules:
- summary_one_liner: AT MOST 25 words. This is the recall unit — it must be \
what someone would say to remind themselves of this piece a year later. \
Specific, not generic. Never start with "This publication" or "The report".
- summary_short: 3-5 sentences, the argument and the so-what.
- key_findings: 3-5 concrete claims the piece actually makes.
- entities: only names that literally appear in the text. Do not infer or add \
context. Empty lists are fine."""


def prompt_for(rec, cap_words):
    body = " ".join((rec["body"] or "").split()[:cap_words])
    head = f"Title: {rec['title']}\nType: {rec['pub_type']}\nDate: {rec['date_published']}"
    if rec["subtitle"]:
        head += f"\nSubtitle: {rec['subtitle']}"
    # og_description is the whole article again (#15) — only worth sending when
    # there is no body, which is the paywalled handful.
    if rec["og_description"] and not body:
        head += f"\nDescription: {rec['og_description'][:8000]}"
    return f"{head}\n\nBody:\n{body}"


def sample(conn, n, seed=20260807):
    """Stratified by length so the long tail is represented, not averaged away."""
    rows = conn.execute("""
        SELECT p.id, p.title, p.subtitle, p.pub_type, p.date_published,
               p.og_description, t.body, t.word_count
        FROM publications p JOIN publication_text t ON t.publication_id = p.id
        ORDER BY t.word_count
    """).fetchall()
    rng = random.Random(seed)
    picked, size = [], len(rows)
    for i in range(n):                       # one from each length band
        lo, hi = int(size * i / n), int(size * (i + 1) / n) - 1
        picked.append(rows[rng.randint(lo, max(lo, hi))])
    return picked


def parse(content):
    """The model is asked for bare JSON; accept a fenced block too."""
    if not content:
        return None
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def grounded(entities, body):
    """Share of extracted entity names that literally occur in the source.
    A cheap, objective hallucination check — the model was told to extract,
    not to infer, so anything absent is invented."""
    low = body.lower()
    names = [n for group in (entities or {}).values() if isinstance(group, list)
             for n in group if isinstance(n, str) and n.strip()]
    if not names:
        return None, 0, []
    missing = [n for n in names if n.lower() not in low]
    return round((len(names) - len(missing)) / len(names), 3), len(names), missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--cap", type=int, default=6000, help="max body words sent")
    ap.add_argument("--provider", default=llm.DEFAULT_PROVIDER,
                    choices=sorted(llm.PROVIDERS))
    ap.add_argument("--models", default=None, help="comma-separated")
    # Reasoning models think before answering; too low a ceiling truncates mid-thought
    # and the JSON never arrives.
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--reasoning-effort", default=None, help="e.g. none, high, max")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    recs = sample(conn, args.sample)
    out_path = pathlib.Path(args.out) if args.out else pathlib.Path("spike-results.jsonl")
    out_path.write_text("")          # rows are appended as they land, so a
    results = []                     # crash costs one row, not the whole run

    def record(row):
        results.append(row)
        with out_path.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    chosen = args.models or llm.PROVIDERS[args.provider]["default_model"]
    for model in [m.strip() for m in chosen.split(",") if m.strip()]:
        print(f"\n=== {model} — {len(recs)} publications, cap {args.cap} words ===",
              flush=True)
        for rec in recs:
            user = prompt_for(rec, args.cap)
            try:
                res = llm.chat(
                    [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
                    model=model, provider=args.provider,
                    max_tokens=args.max_tokens,
                    reasoning_effort=args.reasoning_effort,
                )
            except Exception as exc:                    # a bad row must not end the run
                print(f"  FAIL id={rec['id']}: {type(exc).__name__}: {str(exc)[:120]}",
                      flush=True)
                record({"model": model, "id": rec["id"], "error": str(exc)[:300]})
                continue

            data = parse(res["content"])
            one = (data or {}).get("summary_one_liner", "")
            score, n_ent, missing = grounded((data or {}).get("entities"), rec["body"])
            row = {
                "model": model, "id": rec["id"], "title": rec["title"],
                "pub_type": rec["pub_type"], "words": rec["word_count"],
                "sent_words": min(rec["word_count"], args.cap),
                "prompt_tokens": res["prompt_tokens"],
                "completion_tokens": res["completion_tokens"],
                "seconds": res["seconds"], "finish_reason": res["finish_reason"],
                "json_ok": data is not None,
                "one_liner": one, "one_liner_words": len(one.split()),
                "n_findings": len((data or {}).get("key_findings") or []),
                "entity_grounding": score, "n_entities": n_ent,
                "ungrounded": missing[:5],
                "summary_short": (data or {}).get("summary_short", ""),
                "key_findings": (data or {}).get("key_findings") or [],
                "entities": (data or {}).get("entities") or {},
                # kept only when parsing failed, to tell truncation from bad JSON
                "raw": None if data else (res["content"] or "")[-400:],
            }
            record(row)
            flag = "ok " if row["json_ok"] and row["one_liner_words"] <= 25 else "CHECK"
            print(f"  {flag} {rec['word_count']:>6,}w -> {res['prompt_tokens']:>6} tok "
                  f"{res['seconds']:>6.1f}s  {row['one_liner_words']:>2}w one-liner  "
                  f"ground={score}", flush=True)

    print(f"\nwrote {len(results)} rows to {out_path}")


if __name__ == "__main__":
    main()
