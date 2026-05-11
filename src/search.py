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


def fts_query(conn: sqlite3.Connection, query: str | None, filename: str | None,
              ext: str | None, limit: int) -> list[tuple[str, str, float, str]]:
    """Returns [(path, filename, rank, snippet), ...]."""
    sql = """
        SELECT d.path, d.filename, documents_fts.rank,
               snippet(documents_fts, 1, '[', ']', ' ... ', 16) AS snip
        FROM documents_fts
        JOIN documents d ON d.id = documents_fts.rowid
        WHERE 1=1
    """
    params: list = []
    if query:
        sql += " AND documents_fts MATCH ?"
        params.append(query)
    if filename:
        sql += " AND d.filename LIKE ?"
        params.append(f"%{filename}%")
    if ext:
        sql += " AND d.ext = ?"
        params.append(ext if ext.startswith(".") else f".{ext}")
    if query:
        sql += " ORDER BY documents_fts.rank LIMIT ?"
    else:
        sql += " ORDER BY d.path LIMIT ?"
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

    for path, filename, rank, snippet in results:
        print(f"\n{filename}")
        print(f"  {path}")
        if snippet and args.query:
            # Snippet has [...] around matches; clean for terminal display.
            cleaned = snippet.replace("\r", " ").replace("\n", " ").strip()
            if len(cleaned) > 280:
                cleaned = cleaned[:280] + "..."
            print(f"  {cleaned}")

    print(f"\n({len(results)} result{'s' if len(results) != 1 else ''})")


if __name__ == "__main__":
    main()
