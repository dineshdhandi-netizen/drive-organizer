"""Re-classify files in already-organized folders. Find misroutes from earlier
phases. Walks the drive recursively, applies organizer.py's classifier on
every file, and flags any file whose current top-level folder disagrees with
the classifier's proposed top-level.

Only flags moves where the classifier has STRONG signal (matched a property or
a topic) — type-fallback "this is just a PDF" is not enough to demote a file
out of a curated folder.

Modes:
  (default)   dry-run: write plan CSV; no file changes
  --execute   move flagged files (preserves any year/month subpath)
  --limit N   stop after N mismatches (for fast iteration)
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from organizer import (  # noqa: E402
    classify_type,
    extract_text,
    match_property_by_content,
    match_property_by_filename,
    match_topic_in_text,
    property_dest,
    should_skip,
    topic_dest,
)


# Top-level folders to NOT re-evaluate (photos, dupes, empty, Misc by design)
SKIP_TOP = {
    "Photos",
    "Duplicates",
    "Brainy Baby",
    "CoinbaseWalletBackups",
    "Saved from Chrome",
    "Misc",
}

# Only extract content from these (skip Google-native pointers + tiny stub
# files that can't be read in streaming mode).
TEXT_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".doc"}


@dataclass
class Mismatch:
    source: Path
    current_top: str
    proposed_top: str
    reason: str

    def proposed_path(self, root: Path) -> Path:
        # Keep everything after the current top-level (year/month/etc.)
        rel = self.source.relative_to(root)
        sub_parts = rel.parts[1:]
        return root / self.proposed_top / Path(*sub_parts)


def classify_top_level(file: Path) -> tuple[str | None, str]:
    name = file.name
    if should_skip(name):
        return (None, "skip:system")

    prop = match_property_by_filename(name)
    if prop:
        return (property_dest(prop).parts[0], f"property-filename:{prop}")

    text = ""
    if file.suffix.lower() in TEXT_EXTS:
        text = extract_text(file)
    if text:
        prop = match_property_by_content(text)
        if prop:
            return (property_dest(prop).parts[0], f"property-content:{prop}")

    topic = match_topic_in_text(name, by="filename")
    if topic:
        return (topic_dest(topic).parts[0], f"topic-filename:{topic}")
    if text:
        topic = match_topic_in_text(text, by="content")
        if topic:
            return (topic_dest(topic).parts[0], f"topic-content:{topic}")

    # No strong signal — don't propose a move
    cat = classify_type(file.suffix)
    return (None, f"type-fallback:{cat}")


def find_mismatches(root: Path, limit: int | None = None) -> list[Mismatch]:
    rows: list[Mismatch] = []
    scanned = 0
    for top in sorted(root.iterdir()):
        if not top.is_dir():
            continue
        if top.name in SKIP_TOP:
            continue
        for file in top.rglob("*"):
            if not file.is_file():
                continue
            if should_skip(file.name):
                continue
            scanned += 1
            if scanned % 100 == 0:
                print(f"  scanned {scanned} files, {len(rows)} mismatches so far ...")
            current_top = file.relative_to(root).parts[0]
            proposed, reason = classify_top_level(file)
            if proposed and proposed != current_top:
                rows.append(Mismatch(file, current_top, proposed, reason))
                if limit and len(rows) >= limit:
                    print(f"  hit limit ({limit}), stopping early")
                    return rows
    print(f"  scanned {scanned} files total")
    return rows


def execute_moves(mismatches: list[Mismatch], root: Path, log_path: Path) -> tuple[int, int]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.exists()
    ok = fail = 0
    with log_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["timestamp", "filename", "source", "dest", "reason", "status"])
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for m in mismatches:
            try:
                dest = m.proposed_path(root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    stem, suffix = dest.stem, dest.suffix
                    i = 1
                    while dest.with_name(f"{stem}-{i}{suffix}").exists():
                        i += 1
                    dest = dest.with_name(f"{stem}-{i}{suffix}")
                shutil.move(str(m.source), str(dest))
                w.writerow([now_iso, m.source.name, str(m.source), str(dest),
                            f"recheck:{m.reason}", "ok"])
                ok += 1
            except Exception as e:
                w.writerow([now_iso, m.source.name, str(m.source), "",
                            f"recheck:{m.reason}", f"error: {e}"])
                fail += 1
    return ok, fail


def main() -> None:
    p = argparse.ArgumentParser(description="Re-classify and find misrouted files.")
    p.add_argument("root", type=Path, help="Drive root (e.g. G:\\My Drive)")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--plan-out", type=Path, default=Path("reports/recheck_plan.csv"))
    p.add_argument("--log", type=Path, default=Path("reports/recheck_moves.csv"))
    args = p.parse_args()

    if not args.root.is_dir():
        sys.exit(f"Not a directory: {args.root}")

    print(f"Mode:  {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"Root:  {args.root}")
    print(f"Skip:  {sorted(SKIP_TOP)}")
    print("\nScanning (extracts PDF/Office text — may take a few minutes) ...")

    t0 = time.time()
    mismatches = find_mismatches(args.root, args.limit)
    elapsed = time.time() - t0
    print(f"\nFound {len(mismatches)} mismatches in {elapsed:.1f}s.")

    if mismatches:
        pair = Counter((m.current_top, m.proposed_top) for m in mismatches)
        print("\nBy route (top 20):")
        for (cur, prop), n in pair.most_common(20):
            print(f"  {n:4}  {cur}  ->  {prop}")

    args.plan_out.parent.mkdir(parents=True, exist_ok=True)
    with args.plan_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["filename", "current_top", "proposed_top", "reason",
                    "source", "proposed_path"])
        for m in mismatches:
            w.writerow([m.source.name, m.current_top, m.proposed_top, m.reason,
                        str(m.source), str(m.proposed_path(args.root))])
    print(f"\nPlan: {args.plan_out.resolve()}")

    if args.execute:
        print("\n--- EXECUTING MOVES ---")
        ok, fail = execute_moves(mismatches, args.root, args.log)
        print(f"Moved: {ok}   Failed: {fail}")
    else:
        print("(Dry-run — open the plan CSV to review before --execute.)")


if __name__ == "__main__":
    main()
