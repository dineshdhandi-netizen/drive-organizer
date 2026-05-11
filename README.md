# drive-organizer

A content-aware Python toolkit for taking a 20-year-old, 6,000-file Google Drive from chaos to a tidy, deduplicated, year/month-foldered, full-text-searchable archive.

Built as a portfolio rebuild project — modern Python 3.13 (pathlib, dataclasses, type hints), defensive scripting (dry-run safety, full audit logs, no destructive deletes), and content-aware classification using PDF/Office text extraction.

## What it does, in four phases

### Phase 1 — `organizer.py`: organize loose top-level files

Classifies files dumped at the root of a target folder using a cascade:

1. **Property by filename** (`"2916 Puma..."` → property folder)
2. **Property by labeled content** — opens the file, looks for `Property Address:`, `Covered Property:`, `Subject Property:` etc., and matches the address that follows. Avoids the mailing-address false-positive trap.
3. **Topic by filename** (`Tax Return`, `Resume`, `Honda Odyssey...`)
4. **Topic by content** (`Form W-2`, `Policy Number`, `Internal Revenue`)
5. **Type fallback** — `Misc/PDFs/`, `Misc/Documents/`, photos year-foldered by EXIF.

Used to organize 423 files at the root of Google Drive into existing + new topic folders. Every move logged to `reports/moves.csv` for undo.

### Phase 2 — `date_organizer.py` + `dedupe.py`: deep cleanup

`date_organizer.py` walks any folder and sub-folders its files into `<year>/` or `<year>/<MM>/` subfolders based on:

1. EXIF `DateTimeOriginal` (images)
2. Filename timestamp patterns (`YYYYMMDD`, `IMG_YYYYMMDD_*`, 13-digit epoch-ms)
3. File modification time (mtime)

`--hybrid` mode adds a `Undated/<Type>/` bucket for files without a determinable date.

`dedupe.py` finds files with identical `(name, size)` across one or more folders, optionally confirms with SHA-256 (`--hash`), and quarantines duplicates under `<drive-root>/Duplicates/<original-path>/` — never deletes.

Used to organize ~5,900 photos/files across 35 folders and recover 273 MB of duplicate photos/videos.

### Phase 3 — `build_index.py` + `search.py`: searchability

`build_index.py` walks the Drive, extracts text from PDF / DOCX / PPTX / XLSX / TXT, and stores it in a **SQLite FTS5** full-text index (`reports/search.db`). Incremental — re-running only processes new or modified files.

`search.py` queries the index:

```bash
python src/search.py "mortgage payoff"           # FTS5 content search
python src/search.py "W-2" --type pdf            # by content + extension
python src/search.py --filename "2023"           # filename substring
python src/search.py "policy number" --limit 50  # bounded results
```

Hyphenated terms get auto-quoted so the FTS5 parser doesn't read `-` as `NOT`. Windows console unicode mismatches are handled gracefully.

### Phase 4 — Git checkpoints

Each phase committed separately, so any phase is independently revertable.

## Layout

```
drive-organizer/
├── src/
│   ├── inventory.py        # Phase 1 - scan & report (no changes)
│   ├── organizer.py        # Phase 1 - content-aware classify & move
│   ├── peek_pdfs.py        # Phase 1 - quick PDF-content sanity helper
│   ├── find_property.py    # Phase 1 - label-based property finder
│   ├── date_organizer.py   # Phase 2 - year / year-month sub-foldering
│   ├── dedupe.py           # Phase 2 - cross-folder duplicate quarantine
│   ├── build_index.py      # Phase 3 - SQLite FTS5 index builder
│   └── search.py           # Phase 3 - content + filename search CLI
├── reports/                # All output goes here (gitignored)
│   ├── inventory.csv
│   ├── plan.csv, moves.csv, date_moves.csv, dedupe_moves.csv
│   └── search.db
├── requirements.txt
└── README.md
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# or: source .venv/bin/activate  # macOS / Linux
pip install -r requirements.txt
```

Dependencies: `pypdf`, `pillow`, `python-docx`, `python-pptx`, `openpyxl`.

## Safety model

Every script defaults to **dry-run**. `--execute` is required to actually move files. Every move is appended to a CSV log under `reports/` with timestamp + source + dest + reason + status — full reversibility.

Destructive operations don't exist: `dedupe.py` quarantines duplicates to `Duplicates/` rather than deleting; Google Drive's Trash gives an additional 30-day grace period for further recovery.

## Result on the source corpus

Starting state: a 20-year-old personal Google Drive — 35 root folders + 424 loose root-level files + ~60 GB across 11 phone-camera dumps + property records scattered across the root.

End state: 1 system file (`desktop.ini`) at root, all loose files in topic folders, all archive folders year-foldered, all property records routed to the correct address folder under `Auctus Realty LLC/`, all phone-camera dumps year/month sub-foldered, 89 cross-folder duplicates quarantined, and a 458-document SQLite FTS5 search index covering every PDF / DOCX / PPTX / XLSX in the Drive.
