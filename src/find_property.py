"""Scan PDFs for any of our known property addresses across ALL pages.

Reports which property (if any) each PDF references, plus context snippets.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from pypdf import PdfReader


PROPERTIES: dict[str, tuple[str, ...]] = {
    "6525 Abrams Dr":         ("6525 abrams", "6525  abrams"),
    "2916 Puma":              ("2916 puma", "2916  puma"),
    "2933 Noble Oaks Dr":     ("2933 noble", "noble oaks"),
    "3000 Duchess Trl":       ("3000 duchess", "duchess trl", "duchess trail"),
    "337 Metropolitan Dr":    ("337 metropolitan",),
    "Hyderabad Flat #1":      ("hyderabad",),
}


def extract_all_text(pdf_path: Path, page_cap: int = 30) -> str:
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as e:
        return f"__ERROR__: {e}"
    chunks: list[str] = []
    for i, page in enumerate(reader.pages):
        if i >= page_cap:
            break
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            chunks.append("")
    return "\n".join(chunks)


def scan(pdf_path: Path) -> None:
    print("=" * 72)
    print(f"FILE: {pdf_path.name}")
    text = extract_all_text(pdf_path)
    if text.startswith("__ERROR__"):
        print(f"  {text}")
        return
    lower = text.lower()

    matches: list[tuple[str, str]] = []
    for prop, keywords in PROPERTIES.items():
        for kw in keywords:
            idx = lower.find(kw)
            if idx != -1:
                start = max(0, idx - 60)
                end = min(len(text), idx + 120)
                snippet = re.sub(r"\s+", " ", text[start:end]).strip()
                matches.append((prop, snippet))
                break

    if matches:
        for prop, snippet in matches:
            print(f"  -> {prop}")
            print(f"     context: ...{snippet}...")
    else:
        print("  -> NO known property address found.")
        # Look for any "property address" lines as a fallback hint.
        for m in re.finditer(r"property\s*address[^a-z0-9]{0,4}([^\n]{5,120})", lower, re.IGNORECASE):
            print(f"     fallback hint: 'property address {m.group(1).strip()}'")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python find_property.py <pdf> [<pdf> ...]")
        sys.exit(1)
    for arg in sys.argv[1:]:
        scan(Path(arg))


if __name__ == "__main__":
    main()
