"""Undo file moves recorded in one or more CSV move logs.

Reads CSVs produced by organizer.py / date_organizer.py / dedupe.py,
sorts rows by timestamp descending, and for each successful move,
puts the file back where it came from.

Safety:
  - Default mode is dry-run; --execute applies the reversals
  - Skips rows whose 'status' isn't 'ok' (failed moves are NOT reversed)
  - Skips if the file at the new location is missing (already reversed / moved again)
  - Skips if the original location is occupied (would clobber a different file)
  - Every reversal appended to reports/reverse_log.csv

Schemas understood:
  moves.csv / date_moves.csv:
      timestamp, filename, source, dest, reason, status
  dedupe_moves.csv:
      timestamp, filename, kept_path, moved_from, moved_to, status

Usage:
  python reverse.py reports/moves.csv
  python reverse.py --execute reports/moves.csv reports/date_moves.csv
  python reverse.py --execute reports/dedupe_moves.csv
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Reversal:
    timestamp: str          # for ordering (string-sortable ISO format)
    original: Path          # where to put the file back
    current: Path           # where the file currently lives
    csv_source: str         # which log this came from (for the reversal log)
    reason: str             # original reason this move happened


def detect_schema(header: list[str]) -> str:
    cols = {c.strip().lower() for c in header}
    if "moved_from" in cols and "moved_to" in cols:
        return "dedupe"
    if "source" in cols and "dest" in cols:
        return "moves"
    raise ValueError(f"Unknown CSV schema: {header}")


def parse_csv(path: Path) -> list[Reversal]:
    rows: list[Reversal] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return rows
        schema = detect_schema(list(reader.fieldnames))
        for r in reader:
            if r.get("status", "").strip() != "ok":
                continue
            if schema == "dedupe":
                original = Path(r["moved_from"])
                current = Path(r["moved_to"])
                reason = "dedupe"
            else:
                original = Path(r["source"])
                current = Path(r["dest"])
                reason = r.get("reason", "")
            rows.append(Reversal(
                timestamp=r.get("timestamp", ""),
                original=original,
                current=current,
                csv_source=path.name,
                reason=reason,
            ))
    return rows


@dataclass
class Result:
    status: str  # "would-reverse", "reversed", "skip-missing", "skip-collision", "error:<msg>"


def plan_reversal(rev: Reversal) -> Result:
    if not rev.current.exists():
        return Result(status="skip-missing")
    if rev.original.exists():
        return Result(status="skip-collision")
    return Result(status="would-reverse")


def do_reversal(rev: Reversal) -> Result:
    try:
        if not rev.current.exists():
            return Result(status="skip-missing")
        if rev.original.exists():
            return Result(status="skip-collision")
        rev.original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(rev.current), str(rev.original))
        return Result(status="reversed")
    except Exception as e:
        return Result(status=f"error:{e}")


def write_log(rows: list[tuple[Reversal, Result]], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.exists()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with log_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["timestamp", "csv_source", "current", "original",
                        "original_reason", "status"])
        for rev, res in rows:
            w.writerow([now_iso, rev.csv_source, str(rev.current),
                        str(rev.original), rev.reason, res.status])


def main() -> None:
    p = argparse.ArgumentParser(description="Reverse moves logged in one or more CSVs.")
    p.add_argument("csvs", nargs="+", type=Path, help="One or more move-log CSVs.")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--log", type=Path, default=Path("reports/reverse_log.csv"))
    p.add_argument("--limit", type=int, default=None, help="Reverse at most N moves (testing).")
    args = p.parse_args()

    all_reversals: list[Reversal] = []
    for csv_path in args.csvs:
        if not csv_path.is_file():
            print(f"Skipping (not a file): {csv_path}", file=sys.stderr)
            continue
        rows = parse_csv(csv_path)
        print(f"{csv_path.name}: {len(rows)} successful moves loaded")
        all_reversals.extend(rows)

    # Reverse-chronological so we undo most-recent moves first.
    all_reversals.sort(key=lambda r: r.timestamp, reverse=True)
    if args.limit:
        all_reversals = all_reversals[: args.limit]

    print(f"\nMode:  {'EXECUTE (real reversals)' if args.execute else 'DRY-RUN'}")
    print(f"Total reversals queued: {len(all_reversals)}")

    results: list[tuple[Reversal, Result]] = []
    for rev in all_reversals:
        if args.execute:
            res = do_reversal(rev)
        else:
            res = plan_reversal(rev)
        results.append((rev, res))

    status_counts = Counter(r[1].status for r in results)
    print("\nStatus breakdown:")
    for st, n in status_counts.most_common():
        print(f"  {n:5}  {st}")

    print("\nFirst 10 actions:")
    for rev, res in results[:10]:
        arrow = "  <-  " if res.status in ("would-reverse", "reversed") else "  ??  "
        print(f"  [{res.status:18}] {rev.original}{arrow}{rev.current}")

    write_log(results, args.log)
    print(f"\nLog: {args.log.resolve()}")
    if not args.execute:
        print("(Dry-run — re-run with --execute to actually reverse moves.)")


if __name__ == "__main__":
    main()
