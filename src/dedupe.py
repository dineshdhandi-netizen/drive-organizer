"""Find files duplicated across one or more folders.

Two files are considered duplicates when they share the same lowercase
filename AND the same byte size. With --hash, a SHA-256 check is added
for ambiguous matches (slower; downloads streaming files).

Modes:
  (default)    dry-run: prints a report and writes CSV; no files touched
  --execute    move duplicates into <drive-root>/Duplicates/<source-folder>/...
               keeping the first canonical occurrence in place
  --hash       confirm duplicates with SHA-256 (full content; slow)
  --drive-root R   where the Duplicates/ tree gets rooted (default: parent
                   of the first target)

Usage:
  python dedupe.py "G:\\My Drive\\HTC device photos 110815" "G:\\My Drive\\Pixel"
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


@dataclass
class FileEntry:
    path: Path
    size: int
    canonical: bool = False  # True for the kept copy of a group


def walk_files(root: Path) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name.lower() in SKIP_NAMES:
            continue
        if p.name.startswith("."):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size == 0:
            continue
        entries.append(FileEntry(path=p, size=size))
    return entries


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while True:
                buf = f.read(chunk)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest()
    except OSError as e:
        return f"__ERROR__:{e}"


def group_by_name_size(entries: list[FileEntry]) -> dict[tuple[str, int], list[FileEntry]]:
    groups: dict[tuple[str, int], list[FileEntry]] = defaultdict(list)
    for e in entries:
        key = (e.path.name.lower(), e.size)
        groups[key].append(e)
    return {k: v for k, v in groups.items() if len(v) > 1}


def confirm_with_hash(group: list[FileEntry]) -> list[list[FileEntry]]:
    """Split a same-name+size group by SHA-256 — true duplicate sub-groups."""
    by_hash: dict[str, list[FileEntry]] = defaultdict(list)
    for e in group:
        by_hash[sha256_of(e.path)].append(e)
    return [v for v in by_hash.values() if len(v) > 1]


def pick_canonical(group: list[FileEntry]) -> FileEntry:
    """Keep the file with the shortest path string — usually the most
    organized location (e.g. one already year-foldered ranks below root)."""
    return sorted(group, key=lambda e: (len(str(e.path)), str(e.path)))[0]


def human_size(b: int) -> str:
    if b >= 1024**3:
        return f"{b / 1024**3:.2f} GB"
    if b >= 1024**2:
        return f"{b / 1024**2:.2f} MB"
    if b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"


def write_report(groups: dict[tuple[str, int], list[FileEntry]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["filename", "size_bytes", "copy_count", "canonical", "duplicate_paths"])
        for (name, size), entries in sorted(groups.items()):
            canonical = pick_canonical(entries)
            dups = [str(e.path) for e in entries if e is not canonical]
            w.writerow([name, size, len(entries), str(canonical.path), " | ".join(dups)])


def print_summary(groups: dict[tuple[str, int], list[FileEntry]]) -> None:
    total_groups = len(groups)
    total_dup_copies = sum(len(v) - 1 for v in groups.values())
    total_dup_bytes = sum((len(v) - 1) * v[0].size for v in groups.values())
    print(f"\nDuplicate groups:       {total_groups}")
    print(f"Total duplicate files:  {total_dup_copies}")
    print(f"Bytes recoverable:      {human_size(total_dup_bytes)}")

    print("\nTop 10 largest single-file dup groups:")
    by_size = sorted(groups.items(), key=lambda kv: (len(kv[1]) - 1) * kv[1][0].size, reverse=True)
    for (name, size), entries in by_size[:10]:
        savings = (len(entries) - 1) * size
        print(f"  {len(entries)}x  {human_size(size):>10}  ({human_size(savings)} dup) {name}")


def execute_moves(groups: dict[tuple[str, int], list[FileEntry]], drive_root: Path,
                  log_path: Path) -> tuple[int, int]:
    dup_root = drive_root / "Duplicates"
    dup_root.mkdir(exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    is_new = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "filename", "kept_path", "moved_from", "moved_to", "status"])
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for (name, size), entries in groups.items():
            canonical = pick_canonical(entries)
            for e in entries:
                if e is canonical:
                    continue
                try:
                    # Preserve the source folder structure under Duplicates/
                    rel = e.path.relative_to(drive_root)
                    target = dup_root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        stem, suffix = target.stem, target.suffix
                        i = 1
                        while target.with_name(f"{stem}-{i}{suffix}").exists():
                            i += 1
                        target = target.with_name(f"{stem}-{i}{suffix}")
                    shutil.move(str(e.path), str(target))
                    writer.writerow([now_iso, name, str(canonical.path), str(e.path), str(target), "ok"])
                    ok += 1
                except Exception as ex:
                    writer.writerow([now_iso, name, str(canonical.path), str(e.path), "", f"error: {ex}"])
                    fail += 1
    return ok, fail


def main() -> None:
    p = argparse.ArgumentParser(description="Find duplicate files across folders.")
    p.add_argument("targets", nargs="+", type=Path)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--hash", action="store_true", help="Confirm duplicates with SHA-256 (slow).")
    p.add_argument("--drive-root", type=Path, default=None,
                   help="Where Duplicates/ gets rooted. Defaults to parent of first target.")
    p.add_argument("--report", type=Path, default=Path("reports/dedupe_report.csv"))
    p.add_argument("--log", type=Path, default=Path("reports/dedupe_moves.csv"))
    args = p.parse_args()

    print(f"Mode:  {'EXECUTE' if args.execute else 'DRY-RUN (report only)'}")
    if args.hash:
        print("Hash:  SHA-256 confirmation enabled (slower)")

    all_entries: list[FileEntry] = []
    for t in args.targets:
        if not t.is_dir():
            print(f"Skipping (not a directory): {t}", file=sys.stderr)
            continue
        print(f"Walking {t} ...")
        entries = walk_files(t)
        print(f"  found {len(entries)} files")
        all_entries.extend(entries)

    print(f"\nTotal files scanned: {len(all_entries)}")

    candidate_groups = group_by_name_size(all_entries)
    print(f"Candidate duplicate groups (by name+size): {len(candidate_groups)}")

    if args.hash and candidate_groups:
        print("Confirming with SHA-256 ...")
        confirmed: dict[tuple[str, int], list[FileEntry]] = {}
        for key, group in candidate_groups.items():
            for sub in confirm_with_hash(group):
                confirmed[(key[0] + ":" + sha256_of(sub[0].path)[:8], key[1])] = sub
        candidate_groups = confirmed
        print(f"Confirmed groups after hash: {len(candidate_groups)}")

    print_summary(candidate_groups)
    write_report(candidate_groups, args.report)
    print(f"\nReport: {args.report.resolve()}")

    if args.execute:
        drive_root = args.drive_root or args.targets[0].parent
        print(f"\n--- EXECUTING: moving duplicates under {drive_root}/Duplicates/ ---")
        ok, fail = execute_moves(candidate_groups, drive_root, args.log)
        print(f"Moved: {ok}   Failed: {fail}")
        print(f"Log: {args.log.resolve()}")
    else:
        print("\n(Dry-run — no files moved. Re-run with --execute to apply.)")


if __name__ == "__main__":
    main()
