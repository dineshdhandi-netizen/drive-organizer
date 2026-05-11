"""Year-fold files inside one or more target folders.

Files within a target folder are moved into <target>/<YYYY>/ subfolders
based on the file's date. Date is determined via this cascade:

  1. EXIF DateTimeOriginal (images only)
  2. Filename timestamp patterns:
       - 20230415-... or 2023-04-15-...
       - IMG_20230415_... / VID_20230415_...
       - 13-digit epoch-millis (Android camera, e.g. 1477591464911.jpg)
  3. File modification time (mtime)

Files where no year can be determined go to <target>/Undated/.

Modes:
  (default)   dry-run — print plan, change nothing
  --execute   actually move files
  --limit N   process only first N files per target
  --hybrid    inside Undated/, sub-classify by file type (PDF/, Photo/, etc.)
              — useful for property folders where doc-type matters

Usage:
  python date_organizer.py "G:\\My Drive\\Resume"
  python date_organizer.py --execute "G:\\My Drive\\Resume" "G:\\My Drive\\Personal"
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic"}

# Filename date patterns. Try each in order; first hit wins.
# Each regex must expose 'y' (4-digit year) and 'm' (2-digit month) groups.
FILENAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?P<y>19[89]\d|20\d{2})[\-_]?(?P<m>0[1-9]|1[0-2])[\-_]?(0[1-9]|[12]\d|3[01])"),
    re.compile(r"(?:IMG|VID|MVIMG|PXL)[_\-](?P<y>19[89]\d|20\d{2})(?P<m>0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])"),
    re.compile(r"\b(?P<y>19[89]\d|20\d{2})[\-_](?P<m>0[1-9]|1[0-2])\b"),  # YYYY-MM only
]

# Epoch milliseconds pattern: a 13-digit number standing alone.
EPOCH_MS_PATTERN = re.compile(r"\b(\d{13})\b")

YearMonth = tuple[int, int]


# Used by --hybrid to sub-classify undated files.
TYPE_BUCKETS: list[tuple[str, set[str]]] = [
    ("Photos",        IMAGE_EXTS),
    ("Videos",        {".mp4", ".mov", ".avi", ".mkv", ".webm"}),
    ("PDFs",          {".pdf"}),
    ("Documents",     {".doc", ".docx", ".odt", ".rtf", ".txt", ".md"}),
    ("Spreadsheets",  {".xls", ".xlsx", ".csv", ".tsv", ".ods"}),
    ("Presentations", {".ppt", ".pptx", ".odp", ".key"}),
    ("Archives",      {".zip", ".rar", ".7z", ".tar", ".gz"}),
    ("GoogleDocs",    {".gdoc", ".gsheet", ".gslides", ".gform", ".gdraw"}),
]


SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


def is_year(n: int) -> bool:
    return 1985 <= n <= datetime.now().year + 1


def date_from_exif(path: Path) -> YearMonth | None:
    if path.suffix.lower() not in IMAGE_EXTS:
        return None
    try:
        from PIL import Image  # lazy
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            for tag_id in (36867, 306):  # DateTimeOriginal, DateTime
                v = exif.get(tag_id)
                if v:
                    m = re.match(r"(\d{4})[:\-/](\d{2})", str(v))
                    if m:
                        y, mo = int(m.group(1)), int(m.group(2))
                        if is_year(y) and 1 <= mo <= 12:
                            return (y, mo)
    except Exception:
        return None
    return None


def date_from_filename(name: str) -> YearMonth | None:
    for pat in FILENAME_PATTERNS:
        m = pat.search(name)
        if m:
            y = int(m.group("y"))
            mo = int(m.group("m"))
            if is_year(y) and 1 <= mo <= 12:
                return (y, mo)
    m = EPOCH_MS_PATTERN.search(name)
    if m:
        try:
            ts = int(m.group(1)) / 1000
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            if is_year(dt.year):
                return (dt.year, dt.month)
        except (ValueError, OSError, OverflowError):
            pass
    return None


def date_from_mtime(path: Path) -> YearMonth | None:
    try:
        dt = datetime.fromtimestamp(path.stat().st_mtime)
        return (dt.year, dt.month) if is_year(dt.year) else None
    except OSError:
        return None


def determine_date(path: Path) -> tuple[YearMonth | None, str]:
    """Return ((year, month), source-of-date)."""
    d = date_from_exif(path)
    if d is not None:
        return d, "exif"
    d = date_from_filename(path.name)
    if d is not None:
        return d, "filename"
    d = date_from_mtime(path)
    if d is not None:
        return d, "mtime"
    return None, "none"


def type_bucket(ext: str) -> str:
    ext = ext.lower()
    for bucket, exts in TYPE_BUCKETS:
        if ext in exts:
            return bucket
    return "Other"


def should_skip(name: str) -> bool:
    lower = name.lower()
    if lower in SKIP_NAMES:
        return True
    if name.startswith("."):
        return True
    return False


@dataclass
class Move:
    source: Path
    dest_dir: Path
    reason: str

    @property
    def dest_path(self) -> Path:
        return self.dest_dir / self.source.name


def plan_target(target: Path, hybrid: bool, by_month: bool, limit: int | None) -> list[Move]:
    moves: list[Move] = []
    count = 0
    for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file():
            continue
        if should_skip(entry.name):
            continue
        ym, source = determine_date(entry)
        if ym is not None:
            year, month = ym
            if by_month:
                dest = target / str(year) / f"{month:02d}"
                reason = f"year={year} month={month:02d} via {source}"
            else:
                dest = target / str(year)
                reason = f"year={year} via {source}"
        else:
            if hybrid:
                dest = target / "Undated" / type_bucket(entry.suffix)
            else:
                dest = target / "Undated"
            reason = "undated"
        if entry.parent == dest:
            continue
        moves.append(Move(source=entry, dest_dir=dest, reason=reason))
        count += 1
        if limit is not None and count >= limit:
            break
    return moves


def unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(1, 10000):
        candidate = dest.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many collisions for {dest}")


def execute_moves(moves: list[Move], log_path: Path) -> tuple[int, int]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.exists()
    ok = fail = 0
    with log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "filename", "source", "dest", "reason", "status"])
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for mv in moves:
            try:
                mv.dest_dir.mkdir(parents=True, exist_ok=True)
                target = unique_dest(mv.dest_path)
                shutil.move(str(mv.source), str(target))
                writer.writerow([now_iso, mv.source.name, str(mv.source), str(target), mv.reason, "ok"])
                ok += 1
            except Exception as e:
                writer.writerow([now_iso, mv.source.name, str(mv.source), str(mv.dest_path), mv.reason, f"error: {e}"])
                fail += 1
    return ok, fail


def print_summary(target: Path, moves: list[Move]) -> None:
    print(f"\n--- {target.name} ---  ({len(moves)} moves planned)")
    by_dest = Counter(str(mv.dest_dir.relative_to(target)) for mv in moves)
    for d, n in sorted(by_dest.items()):
        print(f"  {n:4}  {d}")


def main() -> None:
    p = argparse.ArgumentParser(description="Year-fold files inside one or more folders.")
    p.add_argument("targets", nargs="+", type=Path, help="Folder(s) to year-fold.")
    p.add_argument("--execute", action="store_true", help="Actually move files (default is dry-run).")
    p.add_argument("--limit", type=int, default=None, help="Process only first N files per target.")
    p.add_argument("--hybrid", action="store_true", help="In Undated, sub-classify by file type.")
    p.add_argument("--by-month", action="store_true", help="Use <year>/<MM>/ subfolders.")
    p.add_argument("--log", type=Path, default=Path("reports/date_moves.csv"))
    p.add_argument("--plan-out", type=Path, default=Path("reports/date_plan.csv"))
    args = p.parse_args()

    print(f"Mode:    {'EXECUTE (real moves)' if args.execute else 'DRY-RUN'}")
    if args.limit:
        print(f"Limit:   {args.limit} files per target")
    if args.hybrid:
        print("Hybrid:  Undated/<Type>/ buckets enabled")
    if args.by_month:
        print("Detail:  year/month sub-foldering")

    all_moves: list[Move] = []
    for target in args.targets:
        if not target.is_dir():
            print(f"Skipping (not a directory): {target}", file=sys.stderr)
            continue
        moves = plan_target(target, hybrid=args.hybrid, by_month=args.by_month, limit=args.limit)
        print_summary(target, moves)
        all_moves.extend(moves)

    args.plan_out.parent.mkdir(parents=True, exist_ok=True)
    with args.plan_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["filename", "source", "dest_dir", "reason"])
        for mv in all_moves:
            w.writerow([mv.source.name, str(mv.source), str(mv.dest_dir), mv.reason])

    print(f"\nTotal planned moves across {len(args.targets)} target(s): {len(all_moves)}")
    print(f"Plan written to: {args.plan_out.resolve()}")

    if args.execute:
        print("\n--- EXECUTING MOVES ---")
        ok, fail = execute_moves(all_moves, args.log)
        print(f"Succeeded: {ok}   Failed: {fail}")
        print(f"Log appended to: {args.log.resolve()}")
    else:
        print("\n(Dry-run — no files moved.)")


if __name__ == "__main__":
    main()
