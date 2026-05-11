"""Search the Drive index by content + filename.

Usage:
  python search.py "mortgage payoff"
  python search.py "policy number" --type pdf
  python search.py --filename "2025"
  python search.py "puma" --limit 50
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Some PDF extracts contain unicode chars Windows cp1252 console can't render.
# Reconfigure stdout to UTF-8 with replacement so we never crash on display.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def normalize_query(q: str) -> str:
    """Auto-quote whitespace-separated tokens containing '-' so FTS5 doesn't
    interpret the hyphen as a NOT operator. Tokens already quoted are kept."""
    out: list[str] = []
    in_quote = False
    buf = ""
    for ch in q:
        if ch == '"':
            in_quote = not in_quote
            buf += ch
        elif ch.isspace() and not in_quote:
            if buf:
                out.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return " ".join(
        f'"{tok}"' if "-" in tok and not (tok.startswith('"') and tok.endswith('"')) else tok
        for tok in out
    )


def fts_query(conn: sqlite3.Connection, query: str | None, filename: str | None,
              ext: str | None, limit: int) -> list[tuple[str, str, str]]:
    """Returns [(path, filename, snippet), ...]."""
    params: list = []
    if query:
        query = normalize_query(query)
        sql = """
            SELECT d.path, d.filename,
                   snippet(documents_fts, 1, '[', ']', ' ... ', 16) AS snip
            FROM documents_fts
            JOIN documents d ON d.id = documents_fts.rowid
            WHERE documents_fts MATCH ?
        """
        params.append(query)
        if filename:
            sql += " AND d.filename LIKE ?"
            params.append(f"%{filename}%")
        if ext:
            sql += " AND d.ext = ?"
            params.append(ext if ext.startswith(".") else f".{ext}")
        sql += " ORDER BY bm25(documents_fts) LIMIT ?"
    else:
        sql = "SELECT path, filename, '' AS snip FROM documents WHERE 1=1"
        if filename:
            sql += " AND filename LIKE ?"
            params.append(f"%{filename}%")
        if ext:
            sql += " AND ext = ?"
            params.append(ext if ext.startswith(".") else f".{ext}")
        sql += " ORDER BY path LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def main() -> None:
    p = argparse.ArgumentParser(description="Search the Drive content index.")
    p.add_argument("query", nargs="?", default=None, help="FTS5 MATCH query.")
    p.add_argument("--filename", default=None, help="Substring match against filename.")
    p.add_argument("--type", "--ext", dest="ext", default=None, help="Filter by extension (e.g. pdf).")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--db", type=Path, default=Path("reports/search.db"))
    args = p.parse_args()

    if not args.db.exists():
        sys.exit(f"Index not found: {args.db}\nBuild it first: python src/build_index.py \"G:\\My Drive\"")

    if not (args.query or args.filename or args.ext):
        sys.exit("Provide a query, --filename, or --type filter.")

    conn = sqlite3.connect(str(args.db))
    results = fts_query(conn, args.query, args.filename, args.ext, args.limit)
    conn.close()

    if not results:
        print("No matches.")
        return

    for path, filename, snippet in results:
        print(f"\n{filename}")
        print(f"  {path}")
        if snippet and args.query:
            cleaned = snippet.replace("\r", " ").replace("\n", " ").strip()
            if len(cleaned) > 280:
                cleaned = cleaned[:280] + "..."
            print(f"  {cleaned}")

    print(f"\n({len(results)} result{'s' if len(results) != 1 else ''})")


if __name__ == "__main__":
    main()
