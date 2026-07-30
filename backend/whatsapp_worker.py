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


HOW IMAGE SENDING WORKS - pure clipboard paste, no attach menu, no clicks
--------------------------------------------------------------------------
Clicking WhatsApp's attach menu ("Photos & videos") makes WhatsApp call the
hidden <input type=file>'s native .click(), which opens a REAL Windows
file-picker dialog. Selenium cannot interact with native OS dialogs at all -
they live outside the browser process - so that path hangs indefinitely.

So sending never touches the attach menu or any file input:
  1. Copy ONLY the image (CF_DIB) onto the Windows clipboard.
  2. Open the chat.
  3. Click the chat's message box and press Ctrl+V - WhatsApp opens its own
     image-preview screen with a caption box.
  4. Explicitly locate and click that caption box (see CAPTION_XPATH) - do
     NOT assume driver.switch_to.active_element is it. That was tried and
     failed: it's a guess about focus, not a verified one.
  5. Overwrite the clipboard with ONLY the caption text (CF_UNICODETEXT), now
     that the image paste has fully landed, then paste (Ctrl+V) into the
     caption box.

     Two things that look like reasonable shortcuts but are NOT, both
     verified against real failures on this exact flow:
       - Putting BOTH the image and the text on the clipboard together (in
         one session, so a single paste could serve either target) sounds
         appealing, but WhatsApp's paste handler checks "does the clipboard
         contain an image?" first and unconditionally treats ANY paste as an
         image paste when it does - so the second Ctrl+V re-pasted the image
         instead of ever reading the text. Each format must be on the
         clipboard alone at the moment its paste happens.
       - Overwriting the clipboard with text using a fresh Open/Empty/Close
         cycle immediately after the image paste, without confirming the
         caption box actually exists and has real focus first, silently
         drops the text - the paste event has nowhere real to land.

     Pasting (rather than send_keys) is also what makes the emoji-heavy,
     multi-line message text work at all: ChromeDriver's send_keys rejects
     characters outside the Basic Multilingual Plane (the message contains
     U+1F389, U+1F4BB, U+1F5D3), and a raw newline sent via send_keys fires
     Enter, which sends prematurely. A pasted newline is just a line break.
  6. Press Enter to send.

send_image (in config.json) controls whether this runs at all - false, or no
message_image configured, sends text-only instead.

The OS clipboard is one machine-wide resource. If more than one worker runs
on this machine, two of them pasting at the same moment could hand the wrong
image/text to the wrong chat - _ClipboardLock() holds for the ENTIRE
copy-image -> paste-image -> copy-text -> paste-text sequence across worker
processes so that can't happen.

WhatsApp Web's DOM changes periodically. If sending breaks after a WhatsApp
update, the selectors below are the first thing to re-check against the live
page (right-click element -> Inspect), and every failure drops a screenshot
in backend/debug_screenshots/ showing exactly what was on screen.
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


def _copy_image_to_clipboard(image_path):
    """Puts ONLY the image on the Windows clipboard as CF_DIB.

    Deliberately image-only, not combined with the text in one clipboard
    session - WhatsApp's paste handler treats ANY paste as an image paste
    whenever the clipboard contains image data, no matter which field is
    focused, so having both formats present at once made a text paste
    re-attach the image instead of ever reading the text.

    CF_DIB has no alpha channel, so a transparent PNG is flattened onto white
    first - otherwise transparent regions come out black.
    """
    import io

    import win32clipboard
    from PIL import Image

    with Image.open(image_path) as src:
        rgba = src.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.split()[3])

        buf = io.BytesIO()
        flattened.save(buf, "BMP")
        # A BMP file starts with a 14-byte BITMAPFILEHEADER that CF_DIB omits.
        dib = buf.getvalue()[14:]

    _open_clipboard_with_retry()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
    finally:
        win32clipboard.CloseClipboard()


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


def send_image_with_caption(driver, phone_10digit, caption, image_path, country_code):
    """Copy image -> open chat -> paste image (Ctrl+V) -> explicitly locate
    and click the real caption box -> copy text -> paste text (Ctrl+V) ->
    Enter. The image and text are never on the clipboard at the same time
    (WhatsApp treats any paste as an image paste whenever the clipboard has
    image data, regardless of focus), and the caption box is located and
    clicked explicitly rather than assumed via active_element."""
    if not Path(image_path).is_file():
        raise FileNotFoundError(f"message_image not found on disk: {image_path}")

    open_chat(driver, phone_10digit, country_code)
    wait = WebDriverWait(driver, 30)

    with _ClipboardLock():
        # 1. Image only on the clipboard.
        _copy_image_to_clipboard(image_path)

        # 2. Click the chat box, paste - opens the image preview screen.
        send_box = wait.until(EC.presence_of_element_located((By.XPATH, SEND_BOX_XPATH)))
        send_box.click()
        time.sleep(0.3)
        send_box.send_keys(Keys.CONTROL, "v")

        # 3. Wait for and click the actual caption box - confirms the image
        # paste really landed and gives the text paste a real, verified
        # target instead of guessing at whatever has focus.
        caption_box = wait.until(EC.presence_of_element_located((By.XPATH, CAPTION_XPATH)))
        caption_box.click()
        time.sleep(0.5)

        # 4. Only now does the text go on the clipboard, replacing the image -
        # the caption box is confirmed present and clicked, so this paste has
        # somewhere real to land.
        _copy_text_to_clipboard(caption)
        caption_box.send_keys(Keys.CONTROL, "v")
        time.sleep(0.5)

        if not (caption_box.get_attribute("textContent") or "").strip():
            raise RuntimeError(
                "caption box was still empty after pasting - the text paste didn't register"
            )

    # 5. Send.
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

    try:
        while True:
            config = get_config()  # re-read so delay/template/image edits apply live

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
                print(f"[system {system_id}] sent to {target_number} (lead #{lead['id']})")
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
