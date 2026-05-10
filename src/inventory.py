"""Inventory scanner for messy folders.

Scans the top level of a directory (no recursion), classifies each file
by extension and a best-guess topic from its filename, prints a summary,
and writes a per-file CSV report.

Usage:
    python src/inventory.py "G:\\My Drive"
    python src/inventory.py "G:\\My Drive" -o reports/drive_inventory.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


TYPE_RULES: dict[str, tuple[str, ...]] = {
    "PDF":          (".pdf",),
    "Image":        (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".heic"),
    "Document":     (".doc", ".docx", ".odt", ".rtf", ".txt", ".md"),
    "Spreadsheet":  (".xls", ".xlsx", ".csv", ".tsv", ".ods"),
    "Presentation": (".ppt", ".pptx", ".odp", ".key"),
    "Archive":      (".zip", ".rar", ".7z", ".tar", ".gz"),
    "Video":        (".mp4", ".mov", ".avi", ".mkv", ".webm"),
    "Audio":        (".mp3", ".wav", ".flac", ".aac", ".m4a"),
    # Google-native files are tiny pointer files (~178 B) on a Drive mount;
    # the real content lives in the cloud.
    "GoogleNative": (".gdoc", ".gsheet", ".gslides", ".gform", ".gdraw"),
}

# First-match-wins. Order topics from most specific to least.
TOPIC_RULES: dict[str, tuple[str, ...]] = {
    "Taxes":      ("tax", "w2", "w-2", "1099", "irs", "1040"),
    "Insurance":  ("insurance", "policy", "coverage", "claim"),
    "RealEstate": ("mortgage", "mtg", "lease", "deed", "hoa", "puma", "abrams", "solar", "warranty", "rental"),
    "Bank":       ("bank", "checking", "savings", "statement", "boa", "ach", "icici"),
    "Trading":    ("trading", "brokerage", "coinbase", "wallet", "robinhood", "schwab", "fidelity"),
    "Work":       ("qbr", "h2o.ai", "h2o", "att-", "att+", "client", "proposal", "deck", "partner"),
    "Health":     ("hsa", "medical", "doctor", "rx", "prescription", "dental", "health"),
    "Travel":     ("flight", "airline", "hotel", "trip", "visa", "passport", "itinerary"),
    "Legal":      ("uscis", "immigration", "court", "lawyer", "license", "certificate"),
    "Family":     ("wedding", "birthday", "b-day", "anniversary", "engagement", "baby", "stuti", "shloak"),
    "Resume":     ("resume", "cv", "linkedin"),
}


@dataclass
class FileRow:
    name: str
    extension: str
    size_bytes: int
    modified_iso: str
    age_days: int
    type_category: str
    topic_guess: str


def classify_type(extension: str) -> str:
    ext = extension.lower()
    for category, exts in TYPE_RULES.items():
        if ext in exts:
            return category
    return "Other"


def guess_topic(name: str) -> str:
    lower = name.lower()
    for topic, keywords in TOPIC_RULES.items():
        if any(kw in lower for kw in keywords):
            return topic
    return "Unknown"


def scan(target: Path) -> list[FileRow]:
    if not target.is_dir():
        raise SystemExit(f"Not a directory: {target}")
    now = datetime.now(timezone.utc)
    rows: list[FileRow] = []
    for entry in target.iterdir():
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except OSError as e:
            print(f"WARN: cannot stat {entry.name}: {e}", file=sys.stderr)
            continue
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        ext = entry.suffix
        rows.append(FileRow(
            name=entry.name,
            extension=ext,
            size_bytes=stat.st_size,
            modified_iso=mtime.isoformat(timespec="seconds"),
            age_days=(now - mtime).days,
            type_category=classify_type(ext),
            topic_guess=guess_topic(entry.name),
        ))
    return rows


def human_size(b: int) -> str:
    if b >= 1024**3:
        return f"{b / 1024**3:.2f} GB"
    if b >= 1024**2:
        return f"{b / 1024**2:.2f} MB"
    if b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"


def print_summary(rows: list[FileRow], target: Path) -> None:
    total = len(rows)
    total_bytes = sum(r.size_bytes for r in rows)
    types = Counter(r.type_category for r in rows)
    topics = Counter(r.topic_guess for r in rows)
    oldest = min(rows, key=lambda r: r.modified_iso) if rows else None
    newest = max(rows, key=lambda r: r.modified_iso) if rows else None

    print(f"\nTarget:  {target}")
    print(f"Scanned: {total} files, {human_size(total_bytes)} total")
    if oldest and newest:
        print(f"Oldest:  {oldest.modified_iso[:10]}  ({oldest.name})")
        print(f"Newest:  {newest.modified_iso[:10]}  ({newest.name})")

    print("\nBy type:")
    for t, n in types.most_common():
        print(f"  {t:14} {n:4}")

    print("\nBy topic guess:")
    for t, n in topics.most_common():
        print(f"  {t:14} {n:4}")

    crossed = Counter((r.topic_guess, r.type_category) for r in rows)
    print("\nTopic x Type (top 15):")
    for (topic, type_), n in crossed.most_common(15):
        print(f"  {topic:12} / {type_:12} {n:4}")


def write_csv(rows: list[FileRow], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(FileRow.__annotations__.keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def main() -> None:
    parser = argparse.ArgumentParser(description="Inventory the top level of a folder (non-recursive).")
    parser.add_argument("target", type=Path, help="Folder to scan.")
    parser.add_argument("-o", "--output", type=Path, default=Path("reports/inventory.csv"),
                        help="CSV report path. Default: reports/inventory.csv")
    args = parser.parse_args()

    rows = scan(args.target)
    print_summary(rows, args.target)
    write_csv(rows, args.output)
    print(f"\nCSV written: {args.output.resolve()}")


if __name__ == "__main__":
    main()
