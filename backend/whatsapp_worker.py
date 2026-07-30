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


HOW THE IMAGE+CAPTION SEND WORKS (and why it looks convoluted)
--------------------------------------------------------------
Three separate traps make the "obvious" implementation fail:

1. NEVER click the "Photos & videos" row in the attach menu. WhatsApp's own
   handler on that row calls the hidden <input type=file>'s native .click(),
   which opens a REAL Windows file-picker dialog. Selenium cannot interact
   with native OS dialogs at all - they live outside the browser process - so
   the script just hangs there with a folder browser open. Instead we set the
   file directly on the input node, which never triggers the dialog.

2. Prefer CDP DOM.setFileInputFiles over element.send_keys(path). WhatsApp's
   file inputs are hidden (display:none), and ChromeDriver's send_keys
   requires an interactable element, so it can raise
   ElementNotInteractableException on them. setFileInputFiles operates on the
   DOM node directly with no visibility requirement.

3. NEVER type the caption with send_keys. Two independent reasons:
   - ChromeDriver rejects characters outside the Basic Multilingual Plane, and
     the message copy contains non-BMP emoji (U+1F389, U+1F4BB, U+1F5D3).
   - The caption box is a contenteditable where Enter SENDS. A raw "\\n" in
     send_keys therefore fires off the message at the first line break,
     splitting one message into several / sending a truncated caption.
   CDP Input.insertText sidesteps both: it commits text the way a paste/IME
   commit does, so emoji work and newlines become real line breaks without
   ever firing the Enter keydown that WhatsApp listens for.

WhatsApp Web's DOM changes periodically. If sending breaks after a WhatsApp
update, the selectors below are the first thing to re-check against the live
page (right-click element -> Inspect), and every failure now drops a
screenshot in backend/debug_screenshots/ showing exactly what was on screen.
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
ATTACH_BUTTON_SELECTOR = (
    "span[data-icon='attach-menu-plus'], span[data-icon='clip'], "
    "div[title='Attach'], button[aria-label='Attach']"
)
# "Photos & videos" and "New sticker" both expose an input[type=file] whose
# accept list contains image/*, so accept alone can't tell them apart - but
# only the Photos & videos input also accepts video mime types. Picking the
# sticker input is what previously made images send as stickers (a flow that
# has no caption field at all, hence the indefinite wait afterwards).
FILE_INPUT_SELECTOR = "input[type='file']"
# Every alternative below is caption-specific on purpose. A looser fallback
# such as //div[@contenteditable='true'] can match an unrelated editable
# element (e.g. the chat search box), which then silently swallows the caption
# while the real preview dialog sits there looking "stuck".
CAPTION_XPATH = (
    "//div[@contenteditable='true'][@aria-placeholder='Add a caption']"
    " | //div[@contenteditable='true'][@aria-label='Add a caption']"
    " | //div[@aria-placeholder='Add a caption']"
    " | //div[@aria-label='Add a caption']"
)
SEND_BUTTON_SELECTOR = (
    "span[data-icon='send'], span[data-icon='wds-ic-send-filled'], "
    "button[aria-label='Send'], div[aria-label='Send']"
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
    """Text-only path. Safe to prefill via the URL: percent-encoding carries
    emoji and newlines correctly, so only a single Enter is needed to send."""
    full_number = f"{country_code}{phone_10digit}"
    encoded_message = urllib.parse.quote(message)
    driver.get(f"{WHATSAPP_WEB_URL}/send?phone={full_number}&text={encoded_message}")

    wait = WebDriverWait(driver, 30)
    send_box = wait.until(EC.presence_of_element_located((By.XPATH, SEND_BOX_XPATH)))
    time.sleep(1.5)  # let the message box prefill before sending
    send_box.send_keys(Keys.ENTER)
    time.sleep(2)  # give WhatsApp Web time to actually dispatch before navigating away


# ---- Getting the image into WhatsApp's preview screen ----

def _open_attach_menu(driver):
    """Opens the attach menu so the hidden file inputs get mounted. Safe to
    click - it's the '+' button, not the row that spawns the native dialog."""
    wait = WebDriverWait(driver, 15)
    attach_btn = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, ATTACH_BUTTON_SELECTOR))
    )
    attach_btn.click()
    time.sleep(0.6)


def _cdp_set_file_input(driver, image_path):
    """Sets the file on the Photos-&-Videos input via CDP DOM.setFileInputFiles.

    No visibility requirement and no native dialog, unlike send_keys/clicking.
    Returns True if a suitable input was found and populated.
    """
    doc = driver.execute_cdp_cmd("DOM.getDocument", {"depth": -1, "pierce": True})
    root_node_id = doc["root"]["nodeId"]
    found = driver.execute_cdp_cmd(
        "DOM.querySelectorAll", {"nodeId": root_node_id, "selector": FILE_INPUT_SELECTOR}
    )

    candidates = []
    for node_id in found.get("nodeIds", []):
        try:
            raw = driver.execute_cdp_cmd("DOM.getAttributes", {"nodeId": node_id})["attributes"]
        except Exception:
            continue
        attrs = dict(zip(raw[0::2], raw[1::2]))
        accept = (attrs.get("accept") or "").lower()
        candidates.append((node_id, accept))

    # Photos & Videos accepts video too; the sticker input never does.
    ranked = [n for n, a in candidates if "video" in a] or [
        n for n, a in candidates if "image" in a
    ]
    if not ranked:
        return False

    driver.execute_cdp_cmd(
        "DOM.setFileInputFiles", {"files": [image_path], "nodeId": ranked[0]}
    )

    # setFileInputFiles populates input.files but WhatsApp is a React app that
    # reacts to the change event, so nudge it explicitly rather than relying on
    # the protocol to have dispatched one.
    driver.execute_script(
        """
        const inputs = Array.from(document.querySelectorAll("input[type=file]"));
        const el = inputs.find(i => ((i.getAttribute('accept')||'').toLowerCase().includes('video')))
                || inputs.find(i => ((i.getAttribute('accept')||'').toLowerCase().includes('image')));
        if (el && el.files && el.files.length) {
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        }
        """
    )
    return True


class _ClipboardLock:
    """Serializes clipboard use across worker processes.

    The OS clipboard is a single machine-wide resource. If several workers run
    on one machine, two of them pasting at once would hand the wrong image to
    the wrong chat, so any clipboard-based send must hold this lock.
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


def _copy_image_to_clipboard(image_path):
    """Puts the image on the Windows clipboard as CF_DIB.

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

    # OpenClipboard fails with "Access is denied" whenever another process
    # (a clipboard manager, Office, RDP, another browser) momentarily holds the
    # clipboard - observed in practice, so retry rather than failing the lead.
    last_error = None
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
        except Exception as exc:
            last_error = exc
            time.sleep(0.4)
            continue
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
            return
        finally:
            win32clipboard.CloseClipboard()
    raise RuntimeError(f"could not open the Windows clipboard after retries: {last_error}")


def _attach_image_via_clipboard(driver, image_path):
    """Fallback: paste the image into the chat with Ctrl+V.

    WhatsApp Web opens the same preview-with-caption screen on paste as it does
    for an attachment. Uses a real key event on the message box so Chrome reads
    the actual OS clipboard - CDP-synthesised key events carry no clipboard
    payload and would paste nothing.
    """
    with _ClipboardLock():
        _copy_image_to_clipboard(image_path)
        send_box = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, SEND_BOX_XPATH))
        )
        send_box.click()
        time.sleep(0.3)
        send_box.send_keys(Keys.CONTROL, "v")
        time.sleep(1.5)


def _insert_text(driver, element, text):
    """Types a caption containing emoji and newlines into a contenteditable.

    send_keys is unusable here: ChromeDriver rejects non-BMP emoji outright,
    and a raw newline fires Enter, which sends the message mid-caption.
    """
    element.click()
    time.sleep(0.2)
    try:
        # Commits text like a paste/IME commit: emoji-safe, and newlines become
        # line breaks without firing the Enter keydown WhatsApp sends on. This
        # is exactly the case Input.insertText is documented for.
        driver.execute_cdp_cmd("Input.insertText", {"text": text})
        time.sleep(0.3)
    except Exception:
        pass

    # Check the result rather than trusting the call: insertText can no-op
    # without raising if focus isn't where we think it is, and sending an image
    # with a silently empty caption is worse than failing the lead outright.
    if (element.get_attribute("textContent") or "").strip():
        return

    # execCommand still routes through the browser's editing pipeline, so it
    # emits the beforeinput/input events React needs to register the value.
    driver.execute_script(
        "document.execCommand('insertText', false, arguments[0]);", text
    )
    time.sleep(0.3)
    if not (element.get_attribute("textContent") or "").strip():
        raise RuntimeError(
            "could not type the caption into the image preview - the caption box "
            "was still empty after both insertText and execCommand"
        )


def _click_send(driver, wait):
    try:
        send_btn = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, SEND_BUTTON_SELECTOR))
        )
        send_btn.click()
        return
    except Exception:
        # With the caption already fully inserted, a single Enter is safe here
        # and is the documented way to send from the preview screen.
        caption_box = driver.find_element(By.XPATH, CAPTION_XPATH)
        caption_box.send_keys(Keys.ENTER)


def send_image_with_caption(driver, phone_10digit, caption, image_path, country_code):
    if not Path(image_path).is_file():
        raise FileNotFoundError(f"message_image not found on disk: {image_path}")

    open_chat(driver, phone_10digit, country_code)
    wait = WebDriverWait(driver, 30)

    # Preferred path: no clicks into the attach menu's rows at all.
    attached = _cdp_set_file_input(driver, image_path)
    if not attached:
        # The inputs are usually only mounted once the menu has been opened.
        _open_attach_menu(driver)
        attached = _cdp_set_file_input(driver, image_path)
    if not attached:
        _attach_image_via_clipboard(driver, image_path)

    caption_box = wait.until(EC.presence_of_element_located((By.XPATH, CAPTION_XPATH)))
    time.sleep(1)  # let the image preview finish rendering before typing
    _insert_text(driver, caption_box, caption)
    time.sleep(0.5)

    _click_send(driver, wait)

    # The preview dialog closing is the observable signal that WhatsApp
    # accepted the send; without this the worker could report success for a
    # message still sitting unsent on screen.
    WebDriverWait(driver, 60).until(
        EC.invisibility_of_element_located((By.XPATH, CAPTION_XPATH))
    )
    time.sleep(2)  # let the upload finish before navigating to the next chat


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

            try:
                if image_rel_path:
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
