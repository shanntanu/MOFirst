"""
WhatsApp Web sending worker for one "system" (one physical/virtual machine + one
WhatsApp number). Run one of these per machine, each with a different SYSTEM_ID,
so the 250-scan load is split across NUM_SYSTEMS WhatsApp numbers instead of
hammering a single number (which is what gets numbers blocked).

This worker never touches a local database - it polls the queue over plain
HTTPS from the API deployed on Vercel (`api/index.py`, backed by a hosted
Postgres). That means this machine only ever makes OUTBOUND requests: no
port needs to be opened or tunneled, and the form keeps accepting
submissions even if this machine is off - they just wait in the queue.

Usage:
    SYSTEM_ID=0 python whatsapp_worker.py
    SYSTEM_ID=1 python whatsapp_worker.py   # on a second machine/profile
    SYSTEM_ID=2 python whatsapp_worker.py   # on a third machine/profile

First run per SYSTEM_ID will show a QR code in the opened Chrome window -
scan it once with that system's WhatsApp number; the session is cached in
chrome_profile_root/system_<id> so future runs skip the QR step.

NOTE: this drives WhatsApp Web via Selenium (the practical, actively-maintained
equivalent of old wrappers like pywhatsapp/pywhatkit, which are unmaintained).
It is unofficial automation of a personal WhatsApp account - respect WhatsApp's
terms of use and your own organization's policies before running this at volume.


HOW IMAGE SENDING WORKS - attach menu + native dialog, driven by pyautogui
--------------------------------------------------------------------------
This matches a real recorded click flow (Selenium IDE, captured against a
live logged-in WhatsApp Web session): click the attach button, then click
"Photos & videos" (the 2nd item in that menu). The recording stops exactly
there because clicking that item makes WhatsApp call the hidden
<input type=file>'s native .click(), which opens a REAL Windows file-picker
dialog - and native OS dialogs exist outside the browser process entirely,
so Selenium cannot see or record anything that happens inside one.

The missing piece is pyautogui: unlike Selenium, it sends real OS-level
keystrokes, which a native dialog receives normally since it auto-focuses
its filename box the instant it opens. So the flow is:
  1. Open the chat (existing URL-based open_chat - the recorded flow instead
     clicked a specific chat already in the sidebar, which only works for
     whatever contact was open during recording, not for messaging arbitrary
     new numbers from the queue).
  2. Click the attach button, then click "Photos & videos".
  3. A native Windows "Open" dialog appears. pyautogui types the absolute
     image path (from message_image in config.json) and presses Enter - this
     selects that exact file and closes the dialog, exactly as if you had
     typed it into the filename box yourself.
  4. WhatsApp opens its own image-preview screen with a caption box.
  5. Copy the caption text onto the clipboard (CF_UNICODETEXT) and paste
     (Ctrl+V) into that caption box - so the image and the message text go
     out as ONE message, with the text as the image's caption. Pasting
     (rather than send_keys) is what makes the emoji-heavy, multi-line
     message text work at all: ChromeDriver's send_keys rejects characters
     outside the Basic Multilingual Plane (the message contains U+1F389,
     U+1F4BB, U+1F5D3), and a raw newline sent via send_keys fires Enter,
     which sends prematurely. A pasted newline is just a line break.
  6. Press Enter to send.

send_image (in config.json) controls whether this runs at all - false, or no
message_image configured, sends text-only instead.

pyautogui controls the physical keyboard, and native dialogs need real OS
focus - so this machine's screen/keyboard must be free while a worker is
mid-send (don't use the mouse/keyboard for something else at that instant),
and running more than one worker on one machine means their native-dialog
steps could collide if they land at the exact same moment. _ClipboardLock()
still serializes the clipboard portion (the caption paste) across workers.

WhatsApp Web's DOM changes periodically, and the CSS selectors below came
from one specific recording session - Meta's build tooling generates these
class names per-build, so they can and do change on WhatsApp updates. If
sending breaks, right-click the attach button / "Photos & videos" item in a
live session -> Inspect -> compare against ATTACH_BUTTON_SELECTOR /
PHOTOS_VIDEOS_SELECTOR below. Every failure also drops a screenshot in
backend/debug_screenshots/ showing exactly what was on screen.
"""

import os
import tempfile
import time
import urllib.parse
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import get_config

WHATSAPP_WEB_URL = "https://web.whatsapp.com"
SEND_BOX_XPATH = "//footer//div[@contenteditable='true']"
QR_CODE_SELECTOR = "canvas[aria-label='Scan this QR code to link a device!'], div[data-testid='qrcode']"
# Deliberately narrow - a broader fallback like //div[@contenteditable='true']
# risks matching some unrelated editable element elsewhere on the page (e.g.
# the search box), which then silently swallows the caption paste while the
# real preview dialog sits there looking untouched.
CAPTION_XPATH = (
    "//div[@contenteditable='true'][@aria-placeholder='Add a caption']"
    " | //div[@contenteditable='true'][@aria-label='Add a caption']"
    " | //div[@aria-placeholder='Add a caption']"
    " | //div[@aria-label='Add a caption']"
)
# From a Selenium IDE recording against a live session, combined with the
# semantic aria-label/title guesses used before that recording existed - the
# recorded selector is tried first since it reflects the actual current
# WhatsApp Web build.
ATTACH_BUTTON_SELECTOR = (
    ".x100vrsf .html-span .xxk0z11, "
    "span[data-icon='attach-menu-plus'], span[data-icon='clip'], "
    "div[title='Attach'], button[aria-label='Attach']"
)
# ".html-button:nth-child(2)" is positional - "Photos & videos" is the 2nd
# item in the attach menu (Document, Photos & videos, Camera, ...). Falls
# back to matching the item's exact visible text if the position ever shifts.
PHOTOS_VIDEOS_SELECTOR = ".html-button:nth-child(2) .x140p0ai"
PHOTOS_VIDEOS_TEXT_XPATH = "//span[normalize-space()='Photos & videos']"


# ---- Remote queue client (talks to api/index.py on Vercel) ----

def fetch_next_lead(system_id, config):
    resp = requests.get(
        f"{config['api_base']}/api/queue/next",
        params={"system_id": system_id},
        headers={"X-Worker-Key": config["worker_api_key"]},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("lead")


def report_sent(lead_id, config):
    resp = requests.post(
        f"{config['api_base']}/api/queue/{lead_id}/sent",
        headers={"X-Worker-Key": config["worker_api_key"]},
        timeout=15,
    )
    resp.raise_for_status()


def report_failed(lead_id, error, config):
    resp = requests.post(
        f"{config['api_base']}/api/queue/{lead_id}/failed",
        json={"error": str(error)[:500]},
        headers={"X-Worker-Key": config["worker_api_key"]},
        timeout=15,
    )
    resp.raise_for_status()


# ---- Selenium / WhatsApp Web ----

def build_driver(system_id, config):
    profile_root = os.path.abspath(config["chrome_profile_root"])
    profile_dir = os.path.join(profile_root, f"system_{system_id}")
    os.makedirs(profile_dir, exist_ok=True)

    opts = Options()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--start-maximized")
    if config.get("headless"):
        # WhatsApp Web + QR login generally needs a real window; headless is
        # only reliable after a session is already cached in the profile dir.
        opts.add_argument("--headless=new")

    driver = webdriver.Chrome(options=opts)
    return driver


def wait_for_login(driver, timeout=120):
    driver.get(WHATSAPP_WEB_URL)
    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, QR_CODE_SELECTOR))
        )
        print("Scan the QR code shown in the browser window to link this WhatsApp number...")
        WebDriverWait(driver, timeout).until(
            EC.invisibility_of_element_located((By.CSS_SELECTOR, QR_CODE_SELECTOR))
        )
    except Exception:
        pass  # already logged in from a cached session, no QR shown


def open_chat(driver, phone_10digit, country_code):
    full_number = f"{country_code}{phone_10digit}"
    driver.get(f"{WHATSAPP_WEB_URL}/send?phone={full_number}")
    wait = WebDriverWait(driver, 30)
    wait.until(EC.presence_of_element_located((By.XPATH, SEND_BOX_XPATH)))
    time.sleep(1.5)  # let the chat fully render before interacting


def send_text_message(driver, phone_10digit, message, country_code):
    """Text-only path (used when send_image is false, or message_image isn't
    configured). Safe to prefill via the URL: percent-encoding carries emoji
    and newlines correctly, so only a single Enter is needed to send."""
    full_number = f"{country_code}{phone_10digit}"
    encoded_message = urllib.parse.quote(message)
    driver.get(f"{WHATSAPP_WEB_URL}/send?phone={full_number}&text={encoded_message}")

    wait = WebDriverWait(driver, 30)
    send_box = wait.until(EC.presence_of_element_located((By.XPATH, SEND_BOX_XPATH)))
    time.sleep(1.5)  # let the message box prefill before sending
    send_box.send_keys(Keys.ENTER)
    time.sleep(2)  # give WhatsApp Web time to actually dispatch before navigating away


# ---- Clipboard helpers (Windows) ----

class _ClipboardLock:
    """Serializes clipboard use across worker processes.

    The OS clipboard is a single machine-wide resource. If several workers run
    on one machine, two of them copying/pasting at once could hand the wrong
    image or text to the wrong chat, so the whole paste-image-then-paste-text
    sequence for one message holds this lock for its entire duration.
    """

    def __init__(self, timeout=90, stale_after=120):
        self.path = Path(tempfile.gettempdir()) / "mo_whatsapp_clipboard.lock"
        self.timeout = timeout
        self.stale_after = stale_after
        self.fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                return self
            except FileExistsError:
                # Reclaim a lock abandoned by a worker that crashed mid-send.
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_after:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise TimeoutError("timed out waiting for the OS clipboard lock")
                time.sleep(0.5)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except OSError:
            pass


def _open_clipboard_with_retry():
    """OpenClipboard fails with "Access is denied" whenever another process
    (a clipboard manager, Office, RDP, another browser) momentarily holds the
    clipboard - observed in practice, so retry rather than failing the lead."""
    import win32clipboard

    last_error = None
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.4)
    raise RuntimeError(f"could not open the Windows clipboard after retries: {last_error}")


def _copy_text_to_clipboard(text):
    """Puts ONLY the caption text on the Windows clipboard as CF_UNICODETEXT.

    Must only be called once the image paste has actually landed and the
    caption box is confirmed present+focused (see send_image_with_caption) -
    calling this too early, before there's a real text target for the paste
    to land in, is what silently dropped the caption in an earlier version.
    """
    import win32clipboard

    _open_clipboard_with_retry()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _click_attach_button(driver, wait):
    btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ATTACH_BUTTON_SELECTOR)))
    btn.click()


def _click_photos_videos(driver, wait):
    try:
        item = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, PHOTOS_VIDEOS_SELECTOR)))
    except Exception:
        # Position-based selector didn't match (menu order changed) - fall
        # back to the item's exact visible text.
        item = wait.until(EC.element_to_be_clickable((By.XPATH, PHOTOS_VIDEOS_TEXT_XPATH)))
    item.click()


def _select_file_in_native_dialog(image_path, wait_seconds=2.0):
    """Feeds the image path into the real Windows "Open" file-picker dialog
    that WhatsApp's "Photos & videos" click triggers.

    Selenium cannot see or interact with this dialog at all - it's a native
    OS window, not a browser tab/frame. pyautogui instead sends real
    OS-level keystrokes, which the dialog receives normally because it
    auto-focuses its filename box the moment it opens. Typing the full path
    and pressing Enter is equivalent to browsing to and picking that file.
    """
    import pyautogui

    time.sleep(wait_seconds)  # let the native dialog finish opening/focusing
    pyautogui.write(image_path, interval=0.01)
    time.sleep(0.3)
    pyautogui.press("enter")


def send_image_with_caption(driver, phone_10digit, caption, image_path, country_code):
    """Open chat -> click attach -> click "Photos & videos" -> a native
    Windows file dialog opens, which pyautogui fills in (Selenium cannot
    touch it) -> WhatsApp's image-preview screen appears with a caption box
    -> paste the caption text into it, so image and text go out as one
    message -> Enter to send."""
    if not Path(image_path).is_file():
        raise FileNotFoundError(f"message_image not found on disk: {image_path}")

    open_chat(driver, phone_10digit, country_code)
    wait = WebDriverWait(driver, 30)

    _click_attach_button(driver, wait)
    time.sleep(0.5)
    _click_photos_videos(driver, wait)

    # The native dialog is not part of the DOM, so there's nothing Selenium
    # can wait on here - _select_file_in_native_dialog's own sleep is the
    # only synchronization available for it opening and gaining focus.
    _select_file_in_native_dialog(image_path)

    caption_box = wait.until(EC.presence_of_element_located((By.XPATH, CAPTION_XPATH)))
    caption_box.click()
    time.sleep(0.5)

    with _ClipboardLock():
        _copy_text_to_clipboard(caption)
        caption_box.send_keys(Keys.CONTROL, "v")
        time.sleep(0.5)

    if not (caption_box.get_attribute("textContent") or "").strip():
        raise RuntimeError(
            "caption box was still empty after pasting - the text paste didn't register"
        )

    caption_box.send_keys(Keys.ENTER)
    time.sleep(3)  # image uploads can take longer than a plain text send


def _save_debug_screenshot(driver, system_id, lead_id):
    """Captures what WhatsApp Web actually looked like at the moment of
    failure - selector mismatches from a WhatsApp UI update are otherwise
    very hard to diagnose from an exception message alone."""
    try:
        debug_dir = Path(__file__).parent / "debug_screenshots"
        debug_dir.mkdir(exist_ok=True)
        path = debug_dir / f"system{system_id}_lead{lead_id}_{time.strftime('%Y%m%d-%H%M%S')}.png"
        driver.save_screenshot(str(path))
        return path
    except Exception:
        return None


def run_worker(system_id):
    config = get_config()

    driver = build_driver(system_id, config)
    wait_for_login(driver)
    print(f"System {system_id} ready. Polling {config['api_base']} every 2s "
          f"with delay={config['message_delay_seconds']}s between sends...")

    sent_count = 0

    try:
        while True:
            config = get_config()  # re-read so delay/template/image edits apply live

            msg_limit = config.get("msg_limit")
            if msg_limit is not None and sent_count >= msg_limit:
                print(f"[system {system_id}] msg_limit ({msg_limit}) reached - stopping "
                      f"so this number doesn't send too much and get blocked. Restart "
                      f"this worker (or raise msg_limit in config.json) to keep sending.")
                break

            try:
                lead = fetch_next_lead(system_id, config)
            except requests.RequestException as exc:
                print(f"[system {system_id}] queue poll failed: {exc}")
                time.sleep(5)
                continue

            if lead is None:
                time.sleep(2)
                continue

            target_number = lead["whatsapp_number"] or lead["contact_number"]
            first_name = (lead["full_name"] or "").split(" ")[0] or lead["full_name"]
            message = config["message_template"].format(
                full_name=lead["full_name"],
                first_name=first_name,
                contact_number=lead["contact_number"],
            )
            image_rel_path = config.get("message_image")
            should_send_image = bool(config.get("send_image", True)) and bool(image_rel_path)

            try:
                if should_send_image:
                    image_path = str((Path(__file__).parent / image_rel_path).resolve())
                    send_image_with_caption(
                        driver, target_number, message, image_path, config["country_code"]
                    )
                else:
                    send_text_message(driver, target_number, message, config["country_code"])
                report_sent(lead["id"], config)
                sent_count += 1
                limit_note = f"/{config.get('msg_limit')}" if config.get("msg_limit") is not None else ""
                print(f"[system {system_id}] sent to {target_number} (lead #{lead['id']}) "
                      f"[{sent_count}{limit_note} this run]")
            except Exception as exc:
                report_failed(lead["id"], exc, config)
                screenshot_path = _save_debug_screenshot(driver, system_id, lead["id"])
                print(f"[system {system_id}] FAILED lead #{lead['id']}: {exc}")
                if screenshot_path:
                    print(f"[system {system_id}] screenshot of the stuck/failed state: {screenshot_path}")

            time.sleep(config["message_delay_seconds"])
    finally:
        driver.quit()


if __name__ == "__main__":
    system_id = int(os.environ.get("SYSTEM_ID", "0"))
    run_worker(system_id)
