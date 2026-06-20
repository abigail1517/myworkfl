"""
Fetches a video from Google Drive (folder: UPLOAD_FOLDER_ID),
posts it to X with a caption from table.csv, then moves the file
to PROCESSED_FOLDER_ID to avoid re-posting.

Required secrets / env vars:
    GOOGLE_CREDENTIALS_JSON   - full JSON with client_id, client_secret,
                                refresh_token, token_uri (see README)
    UPLOAD_FOLDER_ID          - Drive folder ID to pull videos from
    PROCESSED_FOLDER_ID       - Drive folder ID to move processed videos to
    X_STORAGE_STATE_JSON      - Playwright saved session for X
    POSTS_CSV_PATH            - path to caption CSV (default: table.csv)
    CAPTION_SOURCE            - "csv" or "custom" (default: csv)
    CUSTOM_CAPTION            - used when CAPTION_SOURCE=custom
    SHUFFLE_ORDER             - "true" to pick a random video (default: false)
    INTERVAL_MINUTES          - minutes between posts (default: 30)
    MAX_POSTS                 - max posts per run, 0 = unlimited (default: 0)
    ENABLE_ACTION_CAPTION     - "true"/"false" (default: true)
    ENABLE_CAPTION            - "true"/"false" (default: true)
    ENABLE_HASHTAGS           - "true"/"false" (default: true)
"""

import csv
import json
import os
import random
import socket
import sys
import time
import uuid

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Force stdout to flush immediately (critical for GitHub Actions logs) ──────
os.environ["PYTHONUNBUFFERED"] = "1"
sys.stdout.reconfigure(line_buffering=True)


def log(msg):
    """Print with timestamp and immediate flush."""
    ts = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[{ts}] {msg}", flush=True)


# ── Identity ────────────────────────────────────────────────────────────────
RUN_TAG = os.getenv("GITHUB_RUN_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
CLAIM_PREFIX = "CLAIMED_"

# ── Config from env ──────────────────────────────────────────────────────────
STORAGE_STATE_PATH = "x_storage_state.json"
CSV_PATH = os.environ.get("POSTS_CSV_PATH", "table.csv")
CAPTION_SOURCE = os.environ.get("CAPTION_SOURCE", "csv").strip().lower()
CUSTOM_CAPTION_RAW = os.environ.get("CUSTOM_CAPTION", "")
SHUFFLE = os.environ.get("SHUFFLE_ORDER", "false").lower() == "true"
INTERVAL_MINUTES = int(os.environ.get("INTERVAL_MINUTES", "30"))
MAX_POSTS = int(os.environ.get("MAX_POSTS", "0"))

# ── Caption / hashtag toggles ────────────────────────────────────────────────
ENABLE_ACTION_CAPTION = os.environ.get("ENABLE_ACTION_CAPTION", "true").strip().lower() == "true"
ENABLE_CAPTION        = os.environ.get("ENABLE_CAPTION",        "true").strip().lower() == "true"
ENABLE_HASHTAGS       = os.environ.get("ENABLE_HASHTAGS",       "true").strip().lower() == "true"

# ── Retry config ─────────────────────────────────────────────────────────────
MAX_RETRIES    = 3
RETRY_WAIT_SEC = 30


# ── Google Drive helpers ─────────────────────────────────────────────────────

def get_env(name, required=True):
    value = os.getenv(name)
    if value is None:
        if required:
            sys.exit(f"Missing required environment variable: {name}")
        return ""
    return value.strip()


def get_drive_service():
    log("Connecting to Google Drive…")
    raw = get_env("GOOGLE_CREDENTIALS_JSON")
    info = json.loads(raw)
    creds = Credentials(
        token=info.get("access_token"),
        refresh_token=info["refresh_token"],
        token_uri=info.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=info["client_id"],
        client_secret=info["client_secret"],
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    log("Refreshing Google credentials…")
    creds.refresh(Request())
    log("Google Drive connected.")
    return build("drive", "v3", credentials=creds)


def claim_file(service, file_id, current_name):
    claimed_name = f"{CLAIM_PREFIX}{RUN_TAG}__{current_name}"
    log(f"Claiming '{current_name}'…")
    service.files().update(fileId=file_id, body={"name": claimed_name}).execute()
    check = service.files().get(fileId=file_id, fields="id,name").execute()
    if check.get("name") != claimed_name:
        log(f"Lost claim race on '{current_name}'; skipping.")
        return None
    log(f"Claim confirmed: '{claimed_name}'")
    return claimed_name


def release_claim(service, file_id, original_name):
    try:
        service.files().update(fileId=file_id, body={"name": original_name}).execute()
        log(f"Released claim on '{original_name}'.")
    except Exception as e:
        log(f"Warning: could not release claim on {file_id}: {e}")


def fetch_video_from_drive():
    """
    Returns (file_meta_dict, local_path) or (None, None) if no videos left.
    Uses chunked download with progress logging so large files don't
    appear stuck.
    """
    service = get_drive_service()
    folder_id = get_env("UPLOAD_FOLDER_ID")

    log("Listing files in upload folder…")
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        orderBy="createdTime asc",
        pageSize=20,
        fields="files(id,name,mimeType,size)",
    ).execute()

    files = results.get("files", [])
    log(f"Found {len(files)} file(s) in folder.")

    if not files:
        log("No files found in the upload folder.")
        return None, None

    if SHUFFLE:
        random.shuffle(files)

    for file in files:
        name = file["name"]
        mime = file.get("mimeType", "")
        size_bytes = int(file.get("size", 0))
        size_mb = size_bytes / (1024 * 1024)

        log(f"Checking: '{name}' ({mime}, {size_mb:.1f} MB)")

        if name.startswith(CLAIM_PREFIX):
            log(f"Skipping '{name}' — already claimed.")
            continue
        if not mime.startswith("video/"):
            log(f"Skipping '{name}' — not a video ({mime}).")
            continue

        claimed = claim_file(service, file["id"], name)
        if claimed is None:
            continue

        local_path = f"/tmp/{name}"
        log(f"Downloading '{name}' ({size_mb:.1f} MB) to {local_path}…")

        # Chunked download with progress so we can see it's not stuck
        request = service.files().get_media(fileId=file["id"])
        buf = io.FileIO(local_path, mode="wb")
        downloader = MediaIoBaseDownload(buf, request, chunksize=8 * 1024 * 1024)

        done = False
        last_logged_pct = -1
        while not done:
            status, done = downloader.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                # Log every 10% to avoid spamming
                if pct >= last_logged_pct + 10:
                    log(f"  Download progress: {pct}%")
                    last_logged_pct = pct

        buf.close()
        log(f"Download complete: {local_path}")

        file["original_name"] = name
        file["claimed_name"] = claimed
        file["_service"] = service
        return file, local_path

    log("No unclaimed video files found in the upload folder.")
    return None, None


def move_to_processed(service, file_id, original_name):
    upload_id = get_env("UPLOAD_FOLDER_ID")
    processed_id = get_env("PROCESSED_FOLDER_ID")
    log(f"Moving '{original_name}' to processed folder…")
    service.files().update(
        fileId=file_id,
        addParents=processed_id,
        removeParents=upload_id,
        body={"name": original_name},
    ).execute()
    log(f"Moved '{original_name}' to processed folder.")


# ── Caption helpers ──────────────────────────────────────────────────────────

def load_caption_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Caption", "").strip()]
    if not rows:
        sys.exit(f"No caption rows found in {path}")
    return rows


def build_text_csv(row):
    parts = []

    if ENABLE_ACTION_CAPTION:
        action = row.get("Action Caption", "").strip()
        if action:
            parts.append(action)

    if ENABLE_CAPTION:
        caption = row.get("Caption", "").strip()
        if caption:
            parts.append(caption)

    if ENABLE_HASHTAGS:
        hashtags = row.get("Hashtags", "").strip()
        if hashtags:
            if parts:
                parts.append("")
            parts.append(hashtags)

    return "\n".join(parts)


def build_text_custom(raw):
    text = raw.replace("\\n", "\n").strip()
    if not ENABLE_HASHTAGS:
        filtered_lines = []
        for line in text.splitlines():
            tokens = line.strip().split()
            if tokens and all(t.startswith("#") for t in tokens):
                continue
            cleaned = " ".join(t for t in tokens if not t.startswith("#")).strip()
            if cleaned:
                filtered_lines.append(cleaned)
        text = "\n".join(filtered_lines).strip()
    return text


# ── Playwright helpers ───────────────────────────────────────────────────────

def save_debug_screenshot(page, label="debug"):
    try:
        path = f"/tmp/screenshot_{label}_{int(time.time())}.png"
        page.screenshot(path=path)
        log(f"Debug screenshot saved: {path}")
    except Exception as e:
        log(f"Could not save screenshot: {e}")


def wait_for_mask_gone(page, timeout=30000):
    log("Waiting for mask overlay to clear…")
    try:
        page.wait_for_selector(
            '[data-testid="mask"]', state="hidden", timeout=timeout
        )
        log("Mask overlay gone.")
    except PWTimeout:
        log("Mask still present — force-removing via JS.")
        page.evaluate("""
            () => {
                const mask = document.querySelector('[data-testid="mask"]');
                if (mask) mask.remove();
                const layers = document.getElementById('layers');
                if (layers) layers.style.pointerEvents = 'none';
            }
        """)
        page.wait_for_timeout(1000)


def wait_for_page_idle(page, timeout=30000):
    log("Waiting for network idle…")
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
        log("Network idle.")
    except PWTimeout:
        log("Network idle timeout — continuing anyway.")


def navigate_to_compose(page):
    for attempt in range(1, 4):
        log(f"Navigating to compose (attempt {attempt}/3)…")

        log("  → Going to x.com/home…")
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=45000)
        wait_for_page_idle(page, timeout=15000)
        log("  → Waiting 3s for page settle…")
        page.wait_for_timeout(3000)
        wait_for_mask_gone(page, timeout=20000)

        log("  → Going to x.com/compose/post…")
        page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=45000)
        wait_for_page_idle(page, timeout=15000)
        log("  → Waiting 4s for compose settle…")
        page.wait_for_timeout(4000)
        wait_for_mask_gone(page, timeout=20000)

        log("  → Checking for textarea…")
        try:
            page.wait_for_selector(
                '[data-testid="tweetTextarea_0"]',
                state="attached",
                timeout=20000,
            )
            log("Compose textarea ready.")
            return True
        except PWTimeout:
            save_debug_screenshot(page, f"compose_fail_attempt{attempt}")
            log(f"Textarea not found on attempt {attempt}.")
            if attempt < 3:
                log("  Waiting 5s before retry…")
                page.wait_for_timeout(5000)

    return False


def js_focus_and_type(page, text):
    log("Focusing textarea via JS…")
    page.evaluate("""
        () => {
            const el = document.querySelector('[data-testid="tweetTextarea_0"]');
            if (el) { el.focus(); el.click(); }
        }
    """)
    page.wait_for_timeout(800)
    page.keyboard.press("Control+a")
    page.wait_for_timeout(200)

    log(f"Typing {len(text)} characters…")
    for i, char in enumerate(text):
        if char == "\n":
            page.keyboard.press("Enter")
        else:
            page.keyboard.type(char, delay=15)
        # Log every 50 chars so we can see typing is progressing
        if (i + 1) % 50 == 0:
            log(f"  Typed {i + 1}/{len(text)} chars…")

    page.wait_for_timeout(500)

    log("Verifying text landed in textarea…")
    text_present = page.evaluate("""
        () => {
            const el = document.querySelector('[data-testid="tweetTextarea_0"]');
            return el ? el.innerText.trim().length > 0 : false;
        }
    """)
    if not text_present:
        log("Warning: caption may not have landed — retrying type.")
        page.evaluate("""
            () => {
                const el = document.querySelector('[data-testid="tweetTextarea_0"]');
                if (el) { el.focus(); el.click(); }
            }
        """)
        page.wait_for_timeout(500)
        for char in text:
            if char == "\n":
                page.keyboard.press("Enter")
            else:
                page.keyboard.type(char, delay=25)
        page.wait_for_timeout(500)
    else:
        log("Text confirmed in textarea.")


# ── X / Playwright posting ───────────────────────────────────────────────────

def post_video_to_x(local_path, caption_text):
    log("Launching browser…")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        log("Browser launched.")
        context = browser.new_context(
            storage_state=STORAGE_STATE_PATH,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        log("Browser context and page created.")

        try:
            # ── 1. Check session ──────────────────────────────────────────
            log("Loading X home to verify session…")
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=45000)
            wait_for_page_idle(page, timeout=15000)

            if "login" in page.url:
                sys.exit(
                    "Session expired (redirected to login). "
                    "Refresh X_STORAGE_STATE_JSON secret."
                )
            log(f"Session valid. Current URL: {page.url}")

            page.wait_for_timeout(3000)
            wait_for_mask_gone(page, timeout=20000)

            # ── 2. Navigate to compose ────────────────────────────────────
            ready = navigate_to_compose(page)
            if not ready:
                save_debug_screenshot(page, "compose_not_ready")
                raise RuntimeError("Could not load compose textarea after 3 attempts.")

            page.wait_for_timeout(1000)

            # ── 3. Type caption ───────────────────────────────────────────
            if caption_text.strip():
                log("Starting caption input…")
                js_focus_and_type(page, caption_text)
                log(f"Caption typed successfully ({len(caption_text)} chars).")
            else:
                log("No caption text — skipping text input (video only post).")

            # ── 4. Attach video ───────────────────────────────────────────
            log("Looking for file input element…")
            file_input = page.locator('input[data-testid="fileInput"]').first
            try:
                file_input.wait_for(state="attached", timeout=10000)
                log("File input found via data-testid.")
            except PWTimeout:
                log("fileInput not found directly — clicking attachments button…")
                page.evaluate("""
                    () => {
                        const btn = document.querySelector('[data-testid="attachments"]');
                        if (btn) btn.click();
                    }
                """)
                page.wait_for_timeout(1500)
                file_input = page.locator('input[type="file"]').first
                file_input.wait_for(state="attached", timeout=10000)
                log("File input found via type=file fallback.")

            log(f"Setting file: {local_path}")
            file_input.set_input_files(local_path)
            log("Video file set on input. Waiting for upload to begin…")

            # ── 5. Wait for upload ────────────────────────────────────────
            progress_appeared = False
            log("Waiting for upload progress bar to appear (up to 25s)…")
            try:
                page.wait_for_selector(
                    '[data-testid="progressBar"]', state="visible", timeout=25000
                )
                progress_appeared = True
                log("Upload started — progress bar visible.")
            except PWTimeout:
                log("Progress bar did not appear — upload may have started silently.")

            if progress_appeared:
                log("Waiting for upload to complete (up to 5 min)…")
                start = time.time()
                try:
                    # Poll and log every 15s while waiting
                    while True:
                        try:
                            page.wait_for_selector(
                                '[data-testid="progressBar"]',
                                state="detached",
                                timeout=15000,
                            )
                            log("Upload complete — progress bar gone.")
                            break
                        except PWTimeout:
                            elapsed = int(time.time() - start)
                            log(f"  Still uploading… ({elapsed}s elapsed)")
                            if elapsed > 300:
                                log("Warning: upload exceeded 5 min — continuing anyway.")
                                break
                except Exception as e:
                    log(f"Upload wait error: {e}")
            else:
                log("Waiting 15s as fallback for silent upload…")
                for i in range(15):
                    time.sleep(1)
                    if (i + 1) % 5 == 0:
                        log(f"  Fallback wait: {i + 1}s / 15s")

            log("Giving X 5s extra for server-side processing…")
            page.wait_for_timeout(5000)
            wait_for_mask_gone(page, timeout=20000)

            # ── 6. Submit post ────────────────────────────────────────────
            log("Waiting for post button to become enabled…")
            try:
                page.wait_for_selector(
                    '[data-testid="tweetButton"]:not([aria-disabled="true"])',
                    state="attached",
                    timeout=20000,
                )
                log("Post button is enabled.")
            except PWTimeout:
                log("Warning: button still shows disabled; attempting click anyway.")
                save_debug_screenshot(page, "button_disabled")

            page.wait_for_timeout(1000)

            log("Clicking post button via JS…")
            clicked = page.evaluate("""
                () => {
                    const btn = document.querySelector('[data-testid="tweetButton"]');
                    if (btn) { btn.click(); return true; }
                    return false;
                }
            """)

            if not clicked:
                save_debug_screenshot(page, "button_not_found")
                raise RuntimeError("Post button not found in DOM.")

            log("Post button clicked. Waiting for confirmation…")

            # ── 7. Confirm submission ─────────────────────────────────────
            try:
                page.wait_for_url(
                    lambda url: "/home" in url or "/compose" not in url,
                    timeout=25000,
                )
                log("Post confirmed — navigated away from compose.")
            except PWTimeout:
                log("No URL change — checking if compose dialog closed…")
                try:
                    page.wait_for_selector(
                        '[data-testid="tweetTextarea_0"]',
                        state="detached",
                        timeout=8000,
                    )
                    log("Compose textarea gone — post likely submitted.")
                except PWTimeout:
                    save_debug_screenshot(page, "post_unconfirmed")
                    log("Warning: could not confirm post submission. Manual check advised.")

            page.wait_for_timeout(3000)
            log("Closing browser…")

        finally:
            browser.close()
            log("Browser closed.")

    log("Posted to X successfully.")


# ── Single post cycle ────────────────────────────────────────────────────────

def run_one_post():
    log("Starting post cycle — fetching video from Drive…")
    file_meta, local_path = fetch_video_from_drive()
    if file_meta is None:
        return False

    service = file_meta["_service"]
    original_name = file_meta["original_name"]
    file_id = file_meta["id"]

    log("Building caption…")
    if CAPTION_SOURCE == "custom":
        if not CUSTOM_CAPTION_RAW.strip():
            release_claim(service, file_id, original_name)
            sys.exit("CAPTION_SOURCE=custom but CUSTOM_CAPTION is empty.")
        caption = build_text_custom(CUSTOM_CAPTION_RAW)
    else:
        rows = load_caption_rows(CSV_PATH)
        caption = build_text_csv(random.choice(rows))

    log("\n── Post content ──────────────────────────────────────")
    log(f"  Action caption : {'ON' if ENABLE_ACTION_CAPTION else 'OFF'}")
    log(f"  Caption        : {'ON' if ENABLE_CAPTION else 'OFF'}")
    log(f"  Hashtags       : {'ON' if ENABLE_HASHTAGS else 'OFF'}")
    log(f"  Text to post   : {repr(caption) if caption.strip() else '(no text — video only)'}")
    log("──────────────────────────────────────────────────────\n")

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                log(f"Retry attempt {attempt}/{MAX_RETRIES} — waiting {RETRY_WAIT_SEC}s…")
                time.sleep(RETRY_WAIT_SEC)
            post_video_to_x(local_path, caption)
            last_error = None
            break
        except SystemExit:
            raise
        except Exception as e:
            last_error = e
            log(f"Attempt {attempt} failed: {e}")

    if last_error is not None:
        log(f"All {MAX_RETRIES} attempts failed. Releasing claim.")
        release_claim(service, file_id, original_name)
        try:
            os.remove(local_path)
        except OSError:
            pass
        raise last_error

    move_to_processed(service, file_id, original_name)
    try:
        os.remove(local_path)
    except OSError:
        pass

    return True


# ── Scheduler loop ───────────────────────────────────────────────────────────

def sleep_with_countdown(seconds):
    remaining = seconds
    while remaining > 0:
        chunk = min(60, remaining)
        mins, secs = divmod(remaining, 60)
        log(f"Next post in {mins}m {secs}s…")
        time.sleep(chunk)
        remaining -= chunk


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("Writing X session state from secret…")
    state_json = get_env("X_STORAGE_STATE_JSON")
    with open(STORAGE_STATE_PATH, "w") as f:
        f.write(state_json)
    log("Session state written.")

    interval_seconds = INTERVAL_MINUTES * 60
    post_count = 0

    log(f"Scheduler started — posting every {INTERVAL_MINUTES} minute(s).")
    log(f"Caption source  : {CAPTION_SOURCE}")
    log(f"Action caption  : {'ON' if ENABLE_ACTION_CAPTION else 'OFF'}")
    log(f"Caption         : {'ON' if ENABLE_CAPTION else 'OFF'}")
    log(f"Hashtags        : {'ON' if ENABLE_HASHTAGS else 'OFF'}")
    log(f"Max posts       : {MAX_POSTS if MAX_POSTS else 'unlimited'}")

    while True:
        log(f"\n{'='*55}")
        log(f"Post #{post_count + 1} starting")
        log(f"{'='*55}")

        try:
            posted = run_one_post()
        except SystemExit:
            raise
        except Exception as e:
            log(f"ERROR in post cycle: {e}")
            log("Continuing scheduler after interval.")
            main._failed = getattr(main, "_failed", 0) + 1
            if main._failed >= 5:
                sys.exit("5 consecutive failures — exiting.")
            sleep_with_countdown(interval_seconds)
            continue

        main._failed = 0

        if not posted:
            log("No videos left in upload folder. Exiting scheduler.")
            break

        post_count += 1
        log(f"Post #{post_count} complete ✓")

        if MAX_POSTS and post_count >= MAX_POSTS:
            log(f"Reached MAX_POSTS={MAX_POSTS}. Exiting.")
            break

        log(f"Sleeping {INTERVAL_MINUTES} minute(s) before next post…")
        sleep_with_countdown(interval_seconds)

    log(f"Scheduler finished. Total posts made: {post_count}")


if __name__ == "__main__":
    main()
