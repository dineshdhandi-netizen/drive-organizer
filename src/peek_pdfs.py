"""Quick helper: print first ~2 pages of given PDFs so a human can identify them."""

from __future__ import annotations

import sys
from pathlib import Path

from pypdf import PdfReader


def peek(pdf_path: Path, max_pages: int = 2, max_chars: int = 1800) -> None:
    print("=" * 70)
    print(f"FILE: {pdf_path.name}")
    print("=" * 70)
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as e:
        print(f"  (cannot open: {e})")
        return
    n = min(max_pages, len(reader.pages))
    for i in range(n):
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception as e:
            text = f"(extract error: {e})"
        print(f"\n--- Page {i + 1} ---")
        snippet = text.strip()[:max_chars]
        print(snippet if snippet else "(no extractable text)")
    print()


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python peek_pdfs.py <pdf> [<pdf> ...]")
        sys.exit(1)
    for arg in sys.argv[1:]:
        peek(Path(arg))


if __name__ == "__main__":
    main()
