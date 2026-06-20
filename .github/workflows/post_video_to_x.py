"""
Fetches a video from Google Drive (folder: UPLOAD_FOLDER_ID),
posts it to X with a caption from table.csv, then moves the file
to PROCESSED_FOLDER_ID to avoid re-posting.

Runs in a loop, posting one video every INTERVAL_MINUTES (default: 30).

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

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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
MAX_POSTS = int(os.environ.get("MAX_POSTS", "0"))  # 0 = unlimited

# ── Retry config ─────────────────────────────────────────────────────────────
MAX_RETRIES = 3          # retries per post attempt
RETRY_WAIT_SEC = 30      # wait between retries


# ── Google Drive helpers ─────────────────────────────────────────────────────

def get_env(name, required=True):
    value = os.getenv(name)
    if value is None:
        if required:
            sys.exit(f"Missing required environment variable: {name}")
        return ""
    return value.strip()


def get_drive_service():
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
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def claim_file(service, file_id, current_name):
    claimed_name = f"{CLAIM_PREFIX}{RUN_TAG}__{current_name}"
    service.files().update(fileId=file_id, body={"name": claimed_name}).execute()
    check = service.files().get(fileId=file_id, fields="id,name").execute()
    if check.get("name") != claimed_name:
        print(f"Lost claim race on '{current_name}'; skipping.")
        return None
    return claimed_name


def release_claim(service, file_id, original_name):
    try:
        service.files().update(fileId=file_id, body={"name": original_name}).execute()
        print(f"Released claim on '{original_name}'.")
    except Exception as e:
        print(f"Warning: could not release claim on {file_id}: {e}")


def fetch_video_from_drive():
    """
    Returns (file_meta_dict, local_path) or (None, None) if no videos left.
    """
    service = get_drive_service()
    folder_id = get_env("UPLOAD_FOLDER_ID")

    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        orderBy="createdTime asc",
        pageSize=20,
        fields="files(id,name,mimeType)",
    ).execute()

    files = results.get("files", [])
    if not files:
        print("No files found in the upload folder.")
        return None, None

    if SHUFFLE:
        random.shuffle(files)

    for file in files:
        name = file["name"]
        mime = file.get("mimeType", "")

        if name.startswith(CLAIM_PREFIX):
            print(f"Skipping '{name}' — already claimed.")
            continue
        if not mime.startswith("video/"):
            print(f"Skipping '{name}' — not a video ({mime}).")
            continue

        claimed = claim_file(service, file["id"], name)
        if claimed is None:
            continue

        print(f"Claimed '{name}' as '{claimed}'. Downloading…")
        local_path = f"/tmp/{name}"
        data = service.files().get_media(fileId=file["id"]).execute()
        with open(local_path, "wb") as f:
            f.write(data)

        file["original_name"] = name
        file["claimed_name"] = claimed
        file["_service"] = service
        return file, local_path

    print("No unclaimed video files found in the upload folder.")
    return None, None


def move_to_processed(service, file_id, original_name):
    upload_id = get_env("UPLOAD_FOLDER_ID")
    processed_id = get_env("PROCESSED_FOLDER_ID")
    service.files().update(
        fileId=file_id,
        addParents=processed_id,
        removeParents=upload_id,
        body={"name": original_name},
    ).execute()
    print(f"Moved '{original_name}' to processed folder.")


# ── Caption helpers ──────────────────────────────────────────────────────────

def load_caption_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Caption", "").strip()]
    if not rows:
        sys.exit(f"No caption rows found in {path}")
    return rows


def build_text_csv(row):
    action = row.get("Action Caption", "").strip()
    caption = row.get("Caption", "").strip()
    hashtags = row.get("Hashtags", "").strip()
    parts = [p for p in [action, caption, "", hashtags] if p is not None]
    return "\n".join(parts)


def build_text_custom(raw):
    return raw.replace("\\n", "\n").strip()


# ── Playwright helpers ───────────────────────────────────────────────────────

def save_debug_screenshot(page, label="debug"):
    """Save a screenshot to help diagnose failures."""
    try:
        path = f"/tmp/screenshot_{label}_{int(time.time())}.png"
        page.screenshot(path=path)
        print(f"Debug screenshot saved: {path}")
    except Exception as e:
        print(f"Could not save screenshot: {e}")


def wait_for_mask_gone(page, timeout=30000):
    """Wait for X's #layers mask overlay to disappear."""
    try:
        page.wait_for_selector(
            '[data-testid="mask"]', state="hidden", timeout=timeout
        )
        print("Mask overlay gone.")
    except PWTimeout:
        print("Mask still present — force-removing via JS.")
        page.evaluate("""
            () => {
                const mask = document.querySelector('[data-testid="mask"]');
                if (mask) mask.remove();
                const layers = document.getElementById('layers');
                if (layers) layers.style.pointerEvents = 'none';
            }
        """)
        page.wait_for_timeout(1000)


def wait_for_page_idle(page, idle_ms=2000, timeout=30000):
    """
    Wait until no network requests have fired for idle_ms.
    Falls back gracefully on timeout.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeout:
        pass  # Not critical — page may keep polling


def navigate_to_compose(page):
    """
    Reliably navigate to compose page and wait for textarea to be ready.
    Retries navigation up to 3 times if textarea doesn't appear.
    """
    for attempt in range(1, 4):
        print(f"Navigating to compose (attempt {attempt})…")

        # Always go via home first to reset state
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=45000)
        wait_for_page_idle(page, timeout=15000)
        page.wait_for_timeout(3000)
        wait_for_mask_gone(page, timeout=20000)

        # Now navigate to compose
        page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=45000)
        wait_for_page_idle(page, timeout=15000)
        page.wait_for_timeout(4000)
        wait_for_mask_gone(page, timeout=20000)

        # Check textarea appeared
        try:
            page.wait_for_selector(
                '[data-testid="tweetTextarea_0"]',
                state="attached",
                timeout=20000,
            )
            print("Compose textarea ready.")
            return True
        except PWTimeout:
            save_debug_screenshot(page, f"compose_fail_attempt{attempt}")
            print(f"Textarea not found on attempt {attempt}.")
            if attempt < 3:
                page.wait_for_timeout(5000)

    return False


def js_focus_and_type(page, text):
    """
    Focus the tweet textarea via JS then type using Playwright keyboard.
    Bypasses pointer-event overlays entirely.
    """
    # Focus via JS
    page.evaluate("""
        () => {
            const el = document.querySelector('[data-testid="tweetTextarea_0"]');
            if (el) {
                el.focus();
                el.click();
            }
        }
    """)
    page.wait_for_timeout(800)

    # Clear anything already in the box
    page.keyboard.press("Control+a")
    page.wait_for_timeout(200)

    # Type character by character
    for char in text:
        if char == "\n":
            page.keyboard.press("Enter")
        else:
            page.keyboard.type(char, delay=15)

    page.wait_for_timeout(500)

    # Verify text landed by checking box is non-empty
    text_present = page.evaluate("""
        () => {
            const el = document.querySelector('[data-testid="tweetTextarea_0"]');
            return el ? el.innerText.trim().length > 0 : false;
        }
    """)
    if not text_present:
        print("Warning: caption may not have landed in textbox — retrying type.")
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


# ── X / Playwright posting ───────────────────────────────────────────────────

def post_video_to_x(local_path, caption_text):
    """
    Open X compose in a fresh browser, attach video, fill caption, submit.
    A brand-new browser is launched for every post to avoid stale state.
    """
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

        try:
            # ── 1. Check session valid ────────────────────────────────────
            print("Loading X home…")
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=45000)
            wait_for_page_idle(page, timeout=15000)

            if "login" in page.url:
                sys.exit(
                    "Session expired (redirected to login). "
                    "Refresh X_STORAGE_STATE_JSON secret."
                )

            page.wait_for_timeout(3000)
            wait_for_mask_gone(page, timeout=20000)

            # ── 2. Navigate to compose (with retry) ───────────────────────
            ready = navigate_to_compose(page)
            if not ready:
                save_debug_screenshot(page, "compose_not_ready")
                raise RuntimeError("Could not load compose textarea after 3 attempts.")

            page.wait_for_timeout(1000)

            # ── 3. Type caption ───────────────────────────────────────────
            print("Typing caption…")
            js_focus_and_type(page, caption_text)
            print(f"Caption typed ({len(caption_text)} chars).")

            # ── 4. Attach video ───────────────────────────────────────────
            print("Attaching video…")
            file_input = page.locator('input[data-testid="fileInput"]').first
            try:
                file_input.wait_for(state="attached", timeout=10000)
            except PWTimeout:
                # Fallback: click media button to reveal input
                print("fileInput not found directly — clicking attachments button…")
                page.evaluate("""
                    () => {
                        const btn = document.querySelector('[data-testid="attachments"]');
                        if (btn) btn.click();
                    }
                """)
                page.wait_for_timeout(1500)
                file_input = page.locator('input[type="file"]').first
                file_input.wait_for(state="attached", timeout=10000)

            file_input.set_input_files(local_path)
            print("Video file set. Waiting for upload…")

            # ── 5. Wait for upload ────────────────────────────────────────
            # Wait for progress bar to appear (confirms upload started)
            progress_appeared = False
            try:
                page.wait_for_selector(
                    '[data-testid="progressBar"]', state="visible", timeout=25000
                )
                progress_appeared = True
                print("Upload started (progress bar visible).")
            except PWTimeout:
                print("Progress bar did not appear — upload may have started silently.")

            if progress_appeared:
                try:
                    page.wait_for_selector(
                        '[data-testid="progressBar"]', state="detached", timeout=300000
                    )
                    print("Upload complete (progress bar gone).")
                except PWTimeout:
                    print("Warning: upload progress bar timed out after 5 min; continuing.")
            else:
                # Give it extra time if progress bar never appeared
                page.wait_for_timeout(15000)

            # Extra buffer for X server-side processing
            page.wait_for_timeout(5000)
            wait_for_mask_gone(page, timeout=20000)

            # ── 6. Submit post ────────────────────────────────────────────
            print("Submitting post…")

            # Wait for button to be enabled
            try:
                page.wait_for_selector(
                    '[data-testid="tweetButton"]:not([aria-disabled="true"])',
                    state="attached",
                    timeout=20000,
                )
                print("Post button is enabled.")
            except PWTimeout:
                print("Warning: tweetButton still shows disabled; trying anyway.")
                save_debug_screenshot(page, "button_disabled")

            page.wait_for_timeout(1000)

            # JS click bypasses any residual pointer-event overlay
            clicked = page.evaluate("""
                () => {
                    const btn = document.querySelector('[data-testid="tweetButton"]');
                    if (btn) {
                        btn.click();
                        return true;
                    }
                    return false;
                }
            """)

            if not clicked:
                save_debug_screenshot(page, "button_not_found")
                raise RuntimeError("Post button not found in DOM.")

            print("Post button clicked.")

            # ── 7. Confirm submission ─────────────────────────────────────
            try:
                page.wait_for_url(
                    lambda url: "/home" in url or "/compose" not in url,
                    timeout=25000,
                )
                print("Post confirmed — navigated away from compose.")
            except PWTimeout:
                # Check if compose closed (modal dismissed)
                try:
                    page.wait_for_selector(
                        '[data-testid="tweetTextarea_0"]',
                        state="detached",
                        timeout=8000,
                    )
                    print("Compose closed — post likely submitted.")
                except PWTimeout:
                    save_debug_screenshot(page, "post_unconfirmed")
                    print("Warning: could not confirm post. Manual check advised.")

            page.wait_for_timeout(3000)

        finally:
            browser.close()

    print("Posted to X successfully.")


# ── Single post cycle ────────────────────────────────────────────────────────

def run_one_post():
    """
    Fetch one video, post it, move to processed.
    Returns True if posted, False if no videos available.
    Retries up to MAX_RETRIES times on failure.
    """
    file_meta, local_path = fetch_video_from_drive()
    if file_meta is None:
        return False

    service = file_meta["_service"]
    original_name = file_meta["original_name"]
    file_id = file_meta["id"]

    if CAPTION_SOURCE == "custom":
        if not CUSTOM_CAPTION_RAW.strip():
            release_claim(service, file_id, original_name)
            sys.exit("CAPTION_SOURCE=custom but CUSTOM_CAPTION is empty.")
        caption = build_text_custom(CUSTOM_CAPTION_RAW)
    else:
        rows = load_caption_rows(CSV_PATH)
        caption = build_text_csv(random.choice(rows))

    print(f"\nCaption:\n{caption}\n")
    print(f"Video: {local_path}\n")

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                print(f"Retry attempt {attempt}/{MAX_RETRIES}…")
                time.sleep(RETRY_WAIT_SEC)
            post_video_to_x(local_path, caption)
            last_error = None
            break
        except SystemExit:
            # sys.exit() calls — don't retry, re-raise immediately
            raise
        except Exception as e:
            last_error = e
            print(f"Attempt {attempt} failed: {e}")

    if last_error is not None:
        print(f"All {MAX_RETRIES} attempts failed. Releasing claim.")
        release_claim(service, file_id, original_name)
        # Clean up local file
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
    """Sleep for `seconds`, printing a countdown every 60s."""
    remaining = seconds
    while remaining > 0:
        chunk = min(60, remaining)
        mins, secs = divmod(remaining, 60)
        print(f"  Next post in {mins}m {secs}s…")
        time.sleep(chunk)
        remaining -= chunk


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Write X session state from secret
    state_json = get_env("X_STORAGE_STATE_JSON")
    with open(STORAGE_STATE_PATH, "w") as f:
        f.write(state_json)

    interval_seconds = INTERVAL_MINUTES * 60
    post_count = 0

    print(f"Scheduler started — posting every {INTERVAL_MINUTES} minute(s).")
    if MAX_POSTS:
        print(f"Will stop after {MAX_POSTS} post(s).")
    else:
        print("Running until no videos remain or job timeout is reached.")

    while True:
        print(f"\n{'='*55}")
        print(f"Post #{post_count + 1} | {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
        print(f"{'='*55}")

        try:
            posted = run_one_post()
        except SystemExit:
            raise
        except Exception as e:
            print(f"ERROR in post cycle: {e}")
            print("Continuing scheduler — will retry next interval.")
            post_count_failed = getattr(main, "_failed", 0) + 1
            main._failed = post_count_failed
            if post_count_failed >= 5:
                sys.exit("Too many consecutive failures (5). Exiting.")
            print(f"Sleeping {INTERVAL_MINUTES}m before next attempt…")
            sleep_with_countdown(interval_seconds)
            continue

        # Reset failure counter on success
        main._failed = 0

        if not posted:
            print("No videos left in upload folder. Exiting scheduler.")
            break

        post_count += 1
        print(f"Post #{post_count} done ✓")

        if MAX_POSTS and post_count >= MAX_POSTS:
            print(f"Reached MAX_POSTS={MAX_POSTS}. Exiting.")
            break

        print(f"\nSleeping {INTERVAL_MINUTES} minute(s) before next post…")
        sleep_with_countdown(interval_seconds)

    print(f"\nScheduler finished. Total posts made: {post_count}")


if __name__ == "__main__":
    main()
