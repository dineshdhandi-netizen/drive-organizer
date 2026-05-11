"""Tag photos with Claude vision, store descriptions in search.db.

Walks an image folder, sends each photo to Claude Haiku 4.5 via the Batches
API (50% cost discount, async, up to 100k requests / 256MB per batch), gets
a ~30-word description, and stores it in the same SQLite FTS5 index built
by build_index.py — so a search like "beach kids sandcastle" finds tagged
photos alongside indexed PDFs and docs.

Cost estimate for ~5,000 photos (Haiku 4.5 + Batches API 50% discount):
  Input  : ~600 image tokens x 5,000  = 3M tokens x $0.50/1M = $1.50
  Output : ~50 tokens x 5,000         = 250K tokens x $2.50/1M = $0.63
  Total  : ~$2-3 (one-time, photos already tagged are skipped on rerun)

Setup:
  1. Get an Anthropic API key from https://console.anthropic.com/
  2. set ANTHROPIC_API_KEY=sk-ant-...     (PowerShell: $env:ANTHROPIC_API_KEY=...)
  3. pip install anthropic                (already in requirements.txt)

Usage:
  python photo_tagger.py "G:\\My Drive\\Misc\\Photos"
  python photo_tagger.py "G:\\My Drive\\Misc\\Photos" --batch-size 300 --limit 100
  python photo_tagger.py "G:\\My Drive\\Misc\\Photos" --dry-run  # cost estimate, no API calls
"""

from __future__ import annotations

import argparse
import base64
import io
import sqlite3
import sys
import time
from pathlib import Path


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Max long-edge after resizing. Haiku 4.5 downsamples to ~1.15M pixels anyway —
# resizing client-side saves bandwidth and reduces batch size.
RESIZE_LONG_EDGE = 1024

# Roughly how many requests fit in one 256MB batch after resize+base64.
DEFAULT_BATCH_SIZE = 400

# System prompt is fixed across all requests — same bytes every time means the
# (small) prefix is cacheable in principle. For Haiku 4.5 the minimum prefix is
# 4096 tokens, so this short prompt won't actually fire the cache — including
# cache_control is harmless (silent no-op).
SYSTEM_PROMPT = (
    "You write concise, searchable descriptions of photos. Output a single "
    "line, maximum 30 words. Include: who is in the photo (count, rough age, "
    "any visible name/text), where (indoor/outdoor, type of place), what's "
    "happening (activity, mood), and notable objects. No filler like 'this "
    "photo shows' or 'image of'. Be specific."
)


SCHEMA = """
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
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def already_indexed(conn: sqlite3.Connection, path: Path) -> bool:
    row = conn.execute("SELECT id FROM documents WHERE path = ?", (str(path),)).fetchone()
    return row is not None


def upsert_tag(conn: sqlite3.Connection, path: Path, description: str) -> None:
    try:
        st = path.stat()
    except OSError:
        return
    existing = conn.execute(
        "SELECT id FROM documents WHERE path = ?", (str(path),)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (existing[0],))
        conn.execute("DELETE FROM documents WHERE id = ?", (existing[0],))
    conn.execute(
        """INSERT INTO documents (path, filename, ext, size, mtime, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (str(path), path.name, path.suffix.lower(), st.st_size, st.st_mtime, time.time()),
    )
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO documents_fts(rowid, filename, content) VALUES (?, ?, ?)",
        (row_id, path.name, description),
    )


def resize_and_encode(path: Path) -> tuple[str, str] | None:
    """Return (base64_data, media_type) for a JPEG-encoded resized image,
    or None if the file can't be read."""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        sys.exit("Missing pillow. Run: pip install pillow")
    try:
        with Image.open(path) as img:
            img.load()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            w, h = img.size
            scale = RESIZE_LONG_EDGE / max(w, h)
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80, optimize=True)
            data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
            return data, "image/jpeg"
    except (UnidentifiedImageError, OSError) as e:
        print(f"  skip {path.name}: {e}", file=sys.stderr)
        return None


def iter_photos(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]


def build_request(idx: int, path: Path, encoded: tuple[str, str]):
    """Build a Batches API Request for a single photo."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    data, media_type = encoded
    return Request(
        custom_id=f"photo-{idx}",
        params=MessageCreateParamsNonStreaming(
            model="claude-haiku-4-5",
            max_tokens=80,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # silent no-op if under min prefix
            }],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": data,
                    }},
                    {"type": "text", "text": "Describe this photo."},
                ],
            }],
        ),
    )


def submit_and_wait(client, requests, label: str) -> dict[str, str]:
    """Submit one batch, poll until done, return {custom_id: description}."""
    batch = client.messages.batches.create(requests=requests)
    print(f"  {label}: batch {batch.id} submitted ({len(requests)} photos)")

    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        rc = b.request_counts
        print(f"  {label}: status={b.processing_status} "
              f"processing={rc.processing} succeeded={rc.succeeded} errored={rc.errored}")
        time.sleep(60)

    out: dict[str, str] = {}
    for result in client.messages.batches.results(batch.id):
        if result.result.type == "succeeded":
            msg = result.result.message
            text = next((b.text for b in msg.content if b.type == "text"), "")
            out[result.custom_id] = text.strip()
        else:
            print(f"  {result.custom_id}: {result.result.type}", file=sys.stderr)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Tag photos with Claude vision (Batches API).")
    p.add_argument("target", type=Path, help="Folder of photos (walked recursively).")
    p.add_argument("--db", type=Path, default=Path("reports/search.db"))
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--limit", type=int, default=None, help="Process at most N photos.")
    p.add_argument("--force", action="store_true", help="Re-tag photos already in index.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print cost estimate and exit. No API calls, no charges.")
    args = p.parse_args()

    if not args.target.is_dir():
        sys.exit(f"Not a directory: {args.target}")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(args.db))
    ensure_schema(conn)

    all_photos = iter_photos(args.target)
    if args.force:
        photos = all_photos
    else:
        photos = [p for p in all_photos if not already_indexed(conn, p)]
    if args.limit:
        photos = photos[: args.limit]

    print(f"Photos found:      {len(all_photos)}")
    print(f"Photos to process: {len(photos)} (others already in index)")

    # Cost estimate at Haiku 4.5 + Batches API discount.
    n = len(photos)
    in_tokens = n * 600
    out_tokens = n * 60
    cost = (in_tokens * 0.50 + out_tokens * 2.50) / 1_000_000
    print(f"Est. cost:         ~${cost:.2f}  ({in_tokens:,} input + {out_tokens:,} output tokens)")

    if args.dry_run or n == 0:
        if args.dry_run:
            print("(Dry-run — no API calls.)")
        return

    try:
        import anthropic
    except ImportError:
        sys.exit("Missing anthropic SDK. Run: pip install anthropic")
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY env var not set. See setup instructions in the docstring.")

    client = anthropic.Anthropic()

    # Build batches: encode photos lazily, chunk into batch_size-sized groups.
    custom_id_to_path: dict[str, Path] = {}
    pending: list = []
    chunk_idx = 0

    def flush_chunk() -> None:
        nonlocal chunk_idx
        if not pending:
            return
        chunk_idx += 1
        results = submit_and_wait(client, list(pending), f"batch {chunk_idx}")
        for cid, desc in results.items():
            path = custom_id_to_path.get(cid)
            if path and desc:
                upsert_tag(conn, path, desc)
        conn.commit()
        pending.clear()

    for idx, path in enumerate(photos):
        encoded = resize_and_encode(path)
        if encoded is None:
            continue
        cid = f"photo-{idx}"
        custom_id_to_path[cid] = path
        pending.append(build_request(idx, path, encoded))
        if len(pending) >= args.batch_size:
            flush_chunk()
    flush_chunk()

    conn.close()
    print("\nDone. Tagged photos are now searchable via search.py.")


if __name__ == "__main__":
    main()
