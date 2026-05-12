"""Tag photos via Drive API + Claude Haiku 4.5 vision (Batches API).

Lists image files in Google Drive (scoped to a folder by name, default
'Photos'), downloads each through the Drive API, resizes client-side, then
submits to the Anthropic Batches API for tagging (50% cost discount). The
returned description goes into search.db keyed on the file's Drive
webViewLink, so search results are clickable Drive links.

Reuses credentials.json + token.json from gdrive_index.py.

Usage:
  python gdrive_photo_tagger.py --dry-run                  # cost estimate, no API calls
  python gdrive_photo_tagger.py --limit 10                 # tag 10 photos as a test
  python gdrive_photo_tagger.py --folder-name Photos       # tag everything under Photos/
  python gdrive_photo_tagger.py --folder-id <id>           # tag everything under a specific folder
  python gdrive_photo_tagger.py --all                      # tag every image in your Drive
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
RESIZE_LONG_EDGE = 1024
DEFAULT_BATCH_SIZE = 300  # 300 ~150KB images = ~45MB encoded, well under 256MB cap

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


def load_credentials(creds_path: Path, token_path: Path):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit(
            "Missing dependency. Run:\n"
            "  pip install google-api-python-client google-auth-oauthlib"
        )
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                sys.exit(f"credentials.json not found at {creds_path}")
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service(creds):
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_folder_id(service, folder_name: str) -> str | None:
    q = (
        f"name = '{folder_name}' "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    result = service.files().list(q=q, fields="files(id, name)", pageSize=10).execute()
    files = result.get("files", [])
    if not files:
        return None
    return files[0]["id"]


def list_images_recursive(service, folder_id: str):
    """Yield every image file under folder_id (recursively)."""
    page_token = None
    while True:
        result = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            pageSize=1000,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink, size)",
            pageToken=page_token,
        ).execute()
        for f in result.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                yield from list_images_recursive(service, f["id"])
            elif f["mimeType"].startswith("image/"):
                yield f
        page_token = result.get("nextPageToken")
        if not page_token:
            break


def list_all_images(service):
    """Yield every image in Drive (no folder filter)."""
    q = "mimeType contains 'image/' and trashed = false"
    page_token = None
    while True:
        result = service.files().list(
            q=q,
            pageSize=1000,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink, size)",
            pageToken=page_token,
        ).execute()
        for f in result.get("files", []):
            yield f
        page_token = result.get("nextPageToken")
        if not page_token:
            break


def download_image(service, file_id: str) -> bytes | None:
    from googleapiclient.errors import HttpError
    try:
        return service.files().get_media(fileId=file_id).execute()
    except HttpError as e:
        print(f"  download error for {file_id}: {e}", file=sys.stderr)
        return None


def resize_to_jpeg_b64(raw: bytes) -> str | None:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        sys.exit("Missing pillow. Run: pip install pillow")
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            w, h = img.size
            scale = RESIZE_LONG_EDGE / max(w, h)
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80, optimize=True)
            return base64.standard_b64encode(buf.getvalue()).decode("utf-8")
    except (UnidentifiedImageError, OSError) as e:
        print(f"  resize error: {e}", file=sys.stderr)
        return None


def build_batch_request(custom_id: str, image_b64: str):
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    return Request(
        custom_id=custom_id,
        params=MessageCreateParamsNonStreaming(
            model="claude-haiku-4-5",
            max_tokens=80,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # silent no-op below min prefix
            }],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": image_b64,
                    }},
                    {"type": "text", "text": "Describe this photo."},
                ],
            }],
        ),
    )


def submit_and_wait(client, requests, label: str) -> dict[str, str]:
    batch = client.messages.batches.create(requests=requests)
    print(f"  {label}: batch {batch.id} submitted ({len(requests)} photos)")
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        rc = b.request_counts
        print(f"  {label}: {b.processing_status}  "
              f"processing={rc.processing}  ok={rc.succeeded}  err={rc.errored}")
        time.sleep(60)
    out: dict[str, str] = {}
    for r in client.messages.batches.results(batch.id):
        if r.result.type == "succeeded":
            msg = r.result.message
            text = next((b.text for b in msg.content if b.type == "text"), "").strip()
            out[r.custom_id] = text
        else:
            print(f"  {r.custom_id}: {r.result.type}", file=sys.stderr)
    return out


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def upsert_tag(conn: sqlite3.Connection, file_info: dict, description: str) -> None:
    path = file_info.get("webViewLink") or f"drive://{file_info['id']}"
    name = file_info["name"]
    ext = Path(name).suffix.lower() or ".jpg"
    size = int(file_info.get("size", 0) or 0)
    modified = file_info.get("modifiedTime", "")
    try:
        mtime = datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        mtime = time.time()
    existing = conn.execute("SELECT id FROM documents WHERE path = ?", (path,)).fetchone()
    if existing:
        conn.execute("DELETE FROM documents WHERE id = ?", (existing[0],))
        conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (existing[0],))
    conn.execute(
        """INSERT INTO documents (path, filename, ext, size, mtime, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (path, name, ext, size, mtime, time.time()),
    )
    rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO documents_fts(rowid, filename, content) VALUES (?, ?, ?)",
        (rowid, name, description),
    )


def already_tagged(conn: sqlite3.Connection, file_info: dict) -> bool:
    path = file_info.get("webViewLink") or f"drive://{file_info['id']}"
    row = conn.execute("SELECT id FROM documents WHERE path = ?", (path,)).fetchone()
    return row is not None


def main() -> None:
    p = argparse.ArgumentParser(description="Tag photos via Drive API + Claude vision.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--folder-name", default="Photos",
                   help="Top-level folder name to scope (default: Photos).")
    g.add_argument("--folder-id", default=None, help="Drive folder ID to scope.")
    g.add_argument("--all", action="store_true", help="Tag every image in your Drive.")
    p.add_argument("--db", type=Path, default=Path("reports/search.db"))
    p.add_argument("--credentials", type=Path, default=Path("credentials.json"))
    p.add_argument("--token", type=Path, default=Path("token.json"))
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--limit", type=int, default=None, help="Tag at most N new photos.")
    p.add_argument("--force", action="store_true", help="Re-tag photos already indexed.")
    p.add_argument("--dry-run", action="store_true",
                   help="Count photos and print cost estimate, no API calls.")
    args = p.parse_args()

    creds = load_credentials(args.credentials, args.token)
    service = build_service(creds)

    if args.all:
        scope_desc = "all images in Drive"
        image_iter = list_all_images(service)
    else:
        folder_id = args.folder_id
        if not folder_id:
            folder_id = find_folder_id(service, args.folder_name)
            if not folder_id:
                sys.exit(f"Folder not found by name: {args.folder_name}")
            print(f"Resolved '{args.folder_name}' -> folder ID {folder_id}")
        scope_desc = f"images under folder {folder_id}"
        image_iter = list_images_recursive(service, folder_id)

    print(f"Scope: {scope_desc}")
    print("Listing image files (this can take a minute) ...")

    all_files = list(image_iter)
    print(f"Found {len(all_files)} image files.")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(args.db))
    ensure_schema(conn)

    if args.force:
        todo = all_files
    else:
        todo = [f for f in all_files if not already_tagged(conn, f)]
        skipped = len(all_files) - len(todo)
        if skipped:
            print(f"Skipping {skipped} already-tagged photos (use --force to re-tag).")
    if args.limit:
        todo = todo[: args.limit]

    n = len(todo)
    in_tokens = n * 600
    out_tokens = n * 60
    cost = (in_tokens * 0.50 + out_tokens * 2.50) / 1_000_000
    print(f"\nPhotos to tag: {n}")
    print(f"Est. cost:     ~${cost:.2f}  ({in_tokens:,} input + {out_tokens:,} output tokens, "
          f"Haiku 4.5 + Batches discount)")

    if args.dry_run or n == 0:
        if args.dry_run:
            print("\n(Dry-run — no API calls.)")
        return

    try:
        import anthropic
    except ImportError:
        sys.exit("Missing anthropic SDK. Run: pip install anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY env var not set.")

    client = anthropic.Anthropic()

    # Process in chunks of batch-size.
    chunk_idx = 0
    requests_buf: list = []
    cid_to_file: dict[str, dict] = {}

    def flush() -> None:
        nonlocal chunk_idx
        if not requests_buf:
            return
        chunk_idx += 1
        results = submit_and_wait(client, list(requests_buf), f"batch {chunk_idx}")
        for cid, desc in results.items():
            f = cid_to_file.get(cid)
            if f and desc:
                upsert_tag(conn, f, desc)
        conn.commit()
        print(f"  batch {chunk_idx}: stored {len(results)} tags")
        requests_buf.clear()
        cid_to_file.clear()

    for idx, f in enumerate(todo):
        if idx % 25 == 0 and idx > 0:
            print(f"  prepared {idx} / {n} photos for batching ...")
        raw = download_image(service, f["id"])
        if raw is None:
            continue
        b64 = resize_to_jpeg_b64(raw)
        if b64 is None:
            continue
        cid = f"photo-{idx}"
        cid_to_file[cid] = f
        requests_buf.append(build_batch_request(cid, b64))
        if len(requests_buf) >= args.batch_size:
            flush()
    flush()
    conn.close()
    print("\nDone. Tagged photos are now searchable via search.py.")


if __name__ == "__main__":
    main()
