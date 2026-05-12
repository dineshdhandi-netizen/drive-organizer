"""Tag photos via Drive API + Vertex AI Gemini vision.

Lists image files in a Google Drive folder (default 'Photos'), downloads each
via the Drive API, resizes client-side, then calls Gemini 2.0 Flash via Vertex
AI to generate a ~30-word description. Results land in the same search.db
used by the rest of the project.

Why Vertex AI: usage bills against your GCP project, so the $300 90-day free
credit covers it. In Cloud Shell, no API key is needed — Application Default
Credentials are automatic.

Cost estimate (Vertex AI Gemini 2.0 Flash, 9.5k photos): ~$0.50 — well within
the free credit.

Setup (one-time, in Cloud Shell):
  gcloud services enable aiplatform.googleapis.com
  pip install google-cloud-aiplatform

Usage:
  python src/gemini_photo_tagger.py --dry-run             # cost + photo count, no API calls
  python src/gemini_photo_tagger.py --limit 10            # test 10 photos
  python src/gemini_photo_tagger.py                       # full run
  python src/gemini_photo_tagger.py --workers 20          # more parallelism
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
RESIZE_LONG_EDGE = 1024
DEFAULT_WORKERS = 10
DEFAULT_MODEL = "gemini-2.0-flash"
DEFAULT_LOCATION = "us-central1"

SYSTEM_PROMPT = (
    "You write concise, searchable photo descriptions. Output a single line, "
    "max 30 words. Include who (count, age, visible names/text), where "
    "(indoor/outdoor, location type), what (activity, mood), and key objects. "
    "No filler like 'this image shows' or 'a photo of'. Be specific."
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


# ---------- Google Drive (same as gdrive_photo_tagger.py) ----------

def load_drive_credentials(creds_path: Path, token_path: Path):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("Missing google-auth-oauthlib. Run: pip install -r requirements.txt")
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


def build_drive_service(creds):
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
    return files[0]["id"] if files else None


def list_images_recursive(service, folder_id: str):
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


def download_image(service, file_id: str) -> bytes | None:
    from googleapiclient.errors import HttpError
    try:
        return service.files().get_media(fileId=file_id).execute()
    except HttpError as e:
        print(f"  download error for {file_id}: {e}", file=sys.stderr)
        return None


def resize_to_jpeg_bytes(raw: bytes) -> bytes | None:
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
            return buf.getvalue()
    except (UnidentifiedImageError, OSError) as e:
        print(f"  resize error: {e}", file=sys.stderr)
        return None


# ---------- Vertex AI / Gemini ----------

def init_gemini(project_id: str, location: str, model_name: str):
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel
    except ImportError:
        sys.exit(
            "Missing google-cloud-aiplatform. In Cloud Shell run:\n"
            "  pip install google-cloud-aiplatform"
        )
    vertexai.init(project=project_id, location=location)
    return GenerativeModel(model_name, system_instruction=SYSTEM_PROMPT)


def describe_image(model, image_bytes: bytes, max_retries: int = 3) -> str | None:
    from vertexai.generative_models import Part
    delay = 1.0
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = model.generate_content(
                [
                    Part.from_data(data=image_bytes, mime_type="image/jpeg"),
                    "Describe this photo.",
                ],
                generation_config={
                    "max_output_tokens": 100,
                    "temperature": 0.3,
                },
            )
            text = (response.text or "").strip()
            return text or None
        except Exception as e:
            last_err = e
            time.sleep(delay)
            delay *= 2
    print(f"  gemini error after retries: {last_err}", file=sys.stderr)
    return None


# ---------- SQLite ----------

_db_lock = threading.Lock()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def already_tagged(conn: sqlite3.Connection, file_info: dict) -> bool:
    path = file_info.get("webViewLink") or f"drive://{file_info['id']}"
    row = conn.execute("SELECT id FROM documents WHERE path = ?", (path,)).fetchone()
    return row is not None


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
    with _db_lock:
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


# ---------- Per-photo work (runs on worker threads) ----------

def tag_one(drive_service, model, file_info: dict) -> tuple[dict, str | None]:
    raw = download_image(drive_service, file_info["id"])
    if raw is None:
        return file_info, None
    resized = resize_to_jpeg_bytes(raw)
    if resized is None:
        return file_info, None
    desc = describe_image(model, resized)
    return file_info, desc


# ---------- Main ----------

def main() -> None:
    p = argparse.ArgumentParser(description="Tag photos via Drive API + Vertex AI Gemini.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--folder-name", default="Photos", help="Drive folder to scope to.")
    g.add_argument("--folder-id", default=None, help="Drive folder ID directly.")
    p.add_argument("--db", type=Path, default=Path("reports/search.db"))
    p.add_argument("--credentials", type=Path, default=Path("credentials.json"))
    p.add_argument("--token", type=Path, default=Path("token.json"))
    p.add_argument("--project", default=None,
                   help="GCP project ID (default: from $GOOGLE_CLOUD_PROJECT or gcloud config)")
    p.add_argument("--location", default=DEFAULT_LOCATION)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--force", action="store_true", help="Re-tag photos already in DB.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print cost + photo count, no API calls.")
    args = p.parse_args()

    project_id = (
        args.project
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCLOUD_PROJECT")
    )
    if not project_id:
        # Cloud Shell sets this; fall back to gcloud config
        try:
            import subprocess
            project_id = subprocess.check_output(
                ["gcloud", "config", "get-value", "project"], text=True
            ).strip()
        except Exception:
            project_id = None
    if not project_id:
        sys.exit("Could not determine GCP project. Pass --project <PROJECT_ID>.")

    creds = load_drive_credentials(args.credentials, args.token)
    drive_service = build_drive_service(creds)

    folder_id = args.folder_id
    if not folder_id:
        folder_id = find_folder_id(drive_service, args.folder_name)
        if not folder_id:
            sys.exit(f"Folder not found by name: {args.folder_name}")
        print(f"Resolved '{args.folder_name}' -> folder ID {folder_id}")

    print(f"Project:  {project_id}")
    print(f"Location: {args.location}")
    print(f"Model:    {args.model}")
    print(f"Workers:  {args.workers}")
    print("\nListing image files (this can take a minute) ...")

    all_files = list(list_images_recursive(drive_service, folder_id))
    print(f"Found {len(all_files)} image files.")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(args.db), check_same_thread=False)
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
    image_tokens = n * 290    # Gemini Flash uses ~290 tokens per 1024px image
    output_tokens = n * 80
    cost_usd = (image_tokens * 0.10 + output_tokens * 0.40) / 1_000_000
    print(f"\nPhotos to tag: {n}")
    print(f"Est. cost:     ~${cost_usd:.2f}  (image: {image_tokens:,} tokens, "
          f"output: {output_tokens:,} tokens, Gemini 2.0 Flash)")

    if args.dry_run or n == 0:
        if args.dry_run:
            print("\n(Dry-run — no API calls.)")
        return

    print("\nInitializing Vertex AI ...")
    model = init_gemini(project_id, args.location, args.model)

    t0 = time.time()
    processed = ok = errs = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(tag_one, drive_service, model, f): f for f in todo}
        for fut in concurrent.futures.as_completed(futures):
            try:
                file_info, desc = fut.result()
            except Exception as e:
                print(f"  worker error: {e}", file=sys.stderr)
                errs += 1
                continue
            processed += 1
            if desc:
                upsert_tag(conn, file_info, desc)
                ok += 1
            else:
                errs += 1
            if processed % 25 == 0:
                conn.commit()
                rate = processed / (time.time() - t0 + 1e-9)
                eta_min = (n - processed) / rate / 60 if rate > 0 else 0
                print(f"  {processed}/{n}  ok={ok}  err={errs}  "
                      f"{rate:.1f}/s  ETA {eta_min:.1f} min")

    conn.commit()
    conn.close()
    elapsed = time.time() - t0
    print(f"\nDone in {elapsed/60:.1f} min.  Tagged: {ok}   Failed: {errs}")
    print("Tagged photos are now searchable via search.py.")


if __name__ == "__main__":
    main()
