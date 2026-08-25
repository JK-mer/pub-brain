"""Live progress for a running `pubbrain enrich` backfill (#5).

    python scripts/enrich_progress.py            # one snapshot
    python scripts/enrich_progress.py --watch    # refresh until done

Rate and ETA come from the recent tail rather than the whole run, so a pause —
a rate-limit window, a resumed run — decays out instead of skewing the estimate
for the rest of the night.
"""

import argparse
import pathlib
import re
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pubbrain import paths  # noqa: E402

TAIL = 60          # rows the rate is measured over
LOG = paths.DATA_DIR / "enrich.log"


def snapshot(conn):
    done, total = conn.execute(
        "SELECT (SELECT COUNT(*) FROM primary_enrichment),"
        "       (SELECT COUNT(*) FROM publication_text)"
    ).fetchone()
    retried = conn.execute(
        "SELECT COUNT(*) FROM primary_enrichment WHERE attempts > 1"
    ).fetchone()[0]
    recent = conn.execute(
        "SELECT enriched_at FROM primary_enrichment ORDER BY enriched_at DESC LIMIT ?",
        (TAIL,),
    ).fetchall()
    last = conn.execute(
        "SELECT p.id, p.title FROM primary_enrichment e "
        "JOIN publications p ON p.id = e.publication_id "
        "ORDER BY e.enriched_at DESC LIMIT 1"
    ).fetchone()

    per_min = None
    if len(recent) > 1:
        newest = datetime.fromisoformat(recent[0][0])
        oldest = datetime.fromisoformat(recent[-1][0])
        span = (newest - oldest).total_seconds() / 60
        if span > 0:
            per_min = (len(recent) - 1) / span
    return done, total, retried, per_min, last


def failures():
    """Publications the model never got right — they have no row, so the log is
    the only place they are counted."""
    if not LOG.exists():
        return 0, 0
    text = LOG.read_text(errors="replace")
    return (len(re.findall(r"unusable after", text)),
            len(re.findall(r"^\d\d:\d\d:\d\d ERROR id=\S+ (?!unusable)", text, re.M)))


def render(conn):
    done, total, retried, per_min, last = snapshot(conn)
    pct = done / total * 100 if total else 0
    bar = "█" * round(pct / 4) + "░" * (25 - round(pct / 4))
    invalid, errors = failures()

    out = [f"  enriched   {done:>5,} / {total:,}   {pct:5.1f}%  [{bar}]"]
    if per_min:
        left = (total - done) / per_min
        eta = time.strftime("%H:%M", time.localtime(time.time() + left * 60))
        out.append(f"  rate       {per_min:.1f}/min over the last {min(TAIL, done)}"
                   f"   ->  {int(left // 60)}h {int(left % 60):02d}m left, done ~{eta}")
    if done:
        out.append(f"  retries    {retried} ({retried / done * 100:.0f}%)"
                   f"   unusable {invalid}   errors {errors}")
    if last:
        out.append(f"  last       id={last[0]}  {last[1][:58]}")
    return "\n".join(out), done, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True)
    while True:
        text, done, total = render(conn)
        if args.watch:
            print("\033[2J\033[H", end="")     # clear, so the block stays in place
        print(f"\n{text}\n")
        if not args.watch or done >= total:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
