"""Index the text inside Google-native files (.gdoc / .gsheet / .gslides).

These files appear as ~178-byte pointer files on a Drive-for-Desktop mount;
the real content lives in Google's cloud. This script uses the Google Drive
API to export each one as text and merge it into the existing search.db
(the SQLite FTS5 index built by build_index.py).

First-run setup (one-time, ~10 minutes):
  1. https://console.cloud.google.com/  →  create or pick a project
  2. APIs & Services → Library → enable "Google Drive API"
  3. APIs & Services → Credentials → "Create credentials" → OAuth client ID
       Application type: Desktop app
       Name: drive-organizer (or anything)
  4. Download the JSON, save as credentials.json in this project's root
     (already gitignored — see .gitignore)
  5. pip install -r requirements.txt  (adds google-api-python-client etc.)

First execution will open a browser asking you to consent. After consent,
a token.json is cached locally and you won't be prompted again.

Usage:
  python gdrive_index.py "G:\\My Drive"
  python gdrive_index.py "G:\\My Drive" --db reports/search.db --limit 50
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path


SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

GOOGLE_NATIVE_EXTS = {".gdoc", ".gdocument", ".gsheet", ".gslides", ".gdraw"}

# How to ask Drive to convert each native type to text we can index.
EXPORT_MIME: dict[str, str] = {
    ".gdoc":      "text/plain",
    ".gdocument": "text/plain",
    ".gsheet":    "text/csv",     # CSV is a clean text format for spreadsheets
    ".gslides":   "text/plain",
    ".gdraw":     "image/svg+xml",  # drawings — text-bearing if any
}


def load_credentials(creds_path: Path, token_path: Path):
    """Lazy-import google libs so the rest of the project doesn't depend on them."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit(
            "Missing dependency. Run:\n"
            "  pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                sys.exit(
                    f"credentials.json not found at {creds_path}.\n"
                    "See the setup instructions in this script's docstring."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service(creds):
    from googleapiclient.discovery import build  # lazy
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def read_doc_id(pointer_path: Path) -> str | None:
    try:
        data = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("doc_id") or data.get("resource_id", "").split(":")[-1] or None


def export_text(service, doc_id: str, mime: str) -> str:
    from googleapiclient.errors import HttpError
    try:
        result = service.files().export(fileId=doc_id, mimeType=mime).execute()
    except HttpError as e:
        return f"__ERROR__: {e}"
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace")
    return str(result)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Same documents/documents_fts schema as build_index.py — safe if already present."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS documents (
        id        INTEGER PRIMARY KEY,
        path      TEXT UNIQUE NOT NULL,
        filename  TEXT NOT NULL,
        ext       TEXT NOT NULL,
        size      INTEGER NOT NULL,
        mtime     REAL NOT NULL,
        indexed_at REAL NOT NULL
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
        filename, content, tokenize='unicode61'
    );
    """)


def upsert(conn: sqlite3.Connection, path: Path, text: str) -> str:
    try:
        st = path.stat()
    except OSError:
        return "error"
    existing = conn.execute(
        "SELECT id, size, mtime FROM documents WHERE path = ?", (str(path),)
    ).fetchone()
    if existing and existing[1] == st.st_size and existing[2] == st.st_mtime:
        return "skipped"
    if existing:
        conn.execute("DELETE FROM documents WHERE id = ?", (existing[0],))
        conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (existing[0],))
    conn.execute(
        """INSERT INTO documents (path, filename, ext, size, mtime, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (str(path), path.name, path.suffix.lower(), st.st_size, st.st_mtime, time.time()),
    )
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO documents_fts(rowid, filename, content) VALUES (?, ?, ?)",
        (row_id, path.name, text or ""),
    )
    return "inserted"


def main() -> None:
    p = argparse.ArgumentParser(description="Index Google-native file content via Drive API.")
    p.add_argument("target", type=Path, help="Drive root (e.g. G:\\My Drive)")
    p.add_argument("--db", type=Path, default=Path("reports/search.db"))
    p.add_argument("--credentials", type=Path, default=Path("credentials.json"))
    p.add_argument("--token", type=Path, default=Path("token.json"))
    p.add_argument("--limit", type=int, default=None, help="Process at most N files (testing).")
    args = p.parse_args()

    if not args.target.is_dir():
        sys.exit(f"Not a directory: {args.target}")

    creds = load_credentials(args.credentials, args.token)
    service = build_service(creds)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(args.db))
    ensure_schema(conn)

    inserted = skipped = errored = 0
    n = 0
    t0 = time.time()
    print(f"Looking for Google-native files under {args.target} ...")
    for path in args.target.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in GOOGLE_NATIVE_EXTS:
            continue
        n += 1
        doc_id = read_doc_id(path)
        if not doc_id:
            errored += 1
            continue
        mime = EXPORT_MIME.get(path.suffix.lower(), "text/plain")
        text = export_text(service, doc_id, mime)
        if text.startswith("__ERROR__"):
            errored += 1
            print(f"  error: {path.name}  {text}", file=sys.stderr)
            continue
        result = upsert(conn, path, text)
        if result == "inserted":
            inserted += 1
        elif result == "skipped":
            skipped += 1
        if n % 25 == 0:
            conn.commit()
            elapsed = time.time() - t0
            print(f"  {n} processed | {inserted} new | {skipped} skipped | "
                  f"{n / max(elapsed, 1):.1f}/s")
        if args.limit and n >= args.limit:
            break
    conn.commit()
    conn.close()
    print(f"\nDone. Saw {n} Google-native files. "
          f"Inserted: {inserted}  Skipped: {skipped}  Errored: {errored}")


if __name__ == "__main__":
    main()
