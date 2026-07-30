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
    # Confirmed by directly inspecting a live caption box: WhatsApp builds it
    # with Meta's Lexical editor, as a <p class="selectable-text
    # copyable-text ..."><span data-lexical-text="true">...</span></p>
    # structure inside a contenteditable root - NOT a plain aria-labeled div,
    # which is why the guesses below never matched anything real. This walks
    # up from that confirmed real structure to its editable ancestor, rather
    # than guessing at attributes we can't see.
    "//p[contains(concat(' ', normalize-space(@class), ' '), ' selectable-text ')]"
    "[contains(concat(' ', normalize-space(@class), ' '), ' copyable-text ')]"
    "/ancestor::div[@contenteditable='true'][1]"
    " | //div[@contenteditable='true'][@aria-placeholder='Add a caption']"
    " | //div[@contenteditable='true'][@aria-label='Add a caption']"
    " | //div[@aria-placeholder='Add a caption']"
    " | //div[@aria-label='Add a caption']"
)


def _find_visible_caption_box(driver):
    """A custom wait condition, used instead of EC.visibility_of_element_located.

    Verified by testing: if a hidden duplicate exists elsewhere in the DOM
    (e.g. the main chat box, built with the same Lexical text structure,
    just covered by the image-preview overlay), CAPTION_XPATH can match BOTH
    it and the real caption box. Selenium's find_element (singular) always
    returns whichever match comes FIRST in document order, regardless of
    which one is actually visible - so if that first match happens to be the
    hidden duplicate, EC.visibility_of_element_located would poll that same
    hidden element forever and never even consider the second match. This
    checks every match's actual visibility directly instead of trusting
    document order to put the right one first.
    """
    for el in driver.find_elements(By.XPATH, CAPTION_XPATH):
        try:
            if el.is_displayed():
                return el
        except Exception:
            continue
    return False
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


def fetch_remote_settings(config):
    """message_delay_seconds / message_template / message_image / send_image
    / msg_limit are editable from public/admin.html, which writes them into
    the same Postgres database via GET/POST /api/settings - not into this
    machine's config.json. Pulling them here on every loop iteration is what
    makes an edit on the admin page take effect on a running worker without
    a restart.

    config.json still supplies these as a fallback (used only if this fetch
    fails, e.g. no network) plus everything that's genuinely local to this
    machine: api_base, worker_api_key, country_code, chrome_profile_root,
    headless.
    """
    try:
        resp = requests.get(
            f"{config['api_base']}/api/settings",
            headers={"X-Worker-Key": config["worker_api_key"]},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        print(f"could not fetch remote settings, using local config.json values instead: {exc}")
        return {}


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


def _foreground_window_title():
    import win32gui

    try:
        return win32gui.GetWindowText(win32gui.GetForegroundWindow())
    except Exception:
        return ""


def _activate_browser_window(driver, title_hint="WhatsApp"):
    """Forces the Chrome/WhatsApp window to be the actual OS-focused window.

    This matters because pyautogui sends real keystrokes to whichever window
    the operating system currently has focus on - NOT necessarily the Chrome
    window Selenium is driving. Starting this script doesn't put Chrome in
    the foreground by itself (the terminal you launched it from often still
    is), so without this, pyautogui's keystrokes for the native file dialog
    can go to the wrong window entirely and silently do nothing useful - no
    error, just an image that never attaches.

    Windows can also refuse a plain SetForegroundWindow call from a
    background process. Tapping Alt first is the standard workaround for
    that - but it's real OS-level key state, held down and then released as
    two separate calls. If anything ever went wrong between those two calls,
    Alt could stay stuck "held down", turning a later ordinary keystroke
    (e.g. the Enter that submits the native file dialog) into an unintended
    Alt-shortcut - Alt+Enter and friends do real things in Chrome. So the
    Alt tap is now only used as a fallback (plain SetForegroundWindow usually
    just works, since this process is the one that spawned the Chrome
    window), and it's wrapped in try/finally so Alt is released no matter
    what happens in between, instead of appearing right next to
    SetForegroundWindow with nothing guaranteeing cleanup order.
    """
    import win32con
    import win32gui

    try:
        driver.switch_to.window(driver.current_window_handle)
    except Exception:
        pass

    matches = []

    def _enum_handler(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and title_hint.lower() in win32gui.GetWindowText(hwnd).lower():
            matches.append(hwnd)

    win32gui.EnumWindows(_enum_handler, None)
    if not matches:
        return False

    hwnd = matches[0]
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    try:
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        pass

    import win32api

    win32api.keybd_event(0x12, 0, 0, 0)  # ALT down
    try:
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False
    finally:
        win32api.keybd_event(0x12, 0, 2, 0)  # ALT up - guaranteed, whatever happened above


def _click_attach_button(driver, wait):
    _activate_browser_window(driver)
    time.sleep(0.2)
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


def _select_file_in_native_dialog(image_path, max_wait_seconds=10.0):
    """Feeds the image path into the real Windows "Open" file-picker dialog
    that WhatsApp's "Photos & videos" click triggers.

    Selenium cannot see or interact with this dialog at all - it's a native
    OS window, not a browser tab/frame. pyautogui instead sends real
    OS-level keystrokes, which the dialog receives normally because it
    auto-focuses its filename box the moment it opens - AS LONG AS that
    dialog (or its parent Chrome window) is genuinely the OS-focused window,
    which _activate_browser_window is what makes true before this runs.
    """
    import pyautogui

    # Confirmed by an earlier live run: the dialog's real title is "Open".
    # By the time this function starts, Chrome may have ALREADY opened and
    # focused it (the click that triggers it can complete faster than our
    # own code reaches this line) - so this checks whether the CURRENT
    # foreground window already is the dialog, not whether it has "changed"
    # from some earlier snapshot. Waiting for a change was the actual bug:
    # if the dialog was already focused when we took that snapshot, it could
    # never register as "changed away from" itself.
    deadline = time.time() + max_wait_seconds
    title = _foreground_window_title()
    while "open" not in title.lower():
        if time.time() > deadline:
            raise RuntimeError(
                "the native file-picker dialog never became the focused window "
                f"(current foreground window is {title!r}) - the image was never attached"
            )
        time.sleep(0.2)
        title = _foreground_window_title()

    time.sleep(0.4)  # let the dialog finish rendering now that it's confirmed focused
    pyautogui.write(image_path, interval=0.02)
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(0.5)

    # Confirm the dialog actually closed (i.e. accepted the path) rather than
    # staying open with an error of its own (bad path, file not found, etc.).
    closing_deadline = time.time() + 5
    title = _foreground_window_title()
    while "open" in title.lower():
        if time.time() > closing_deadline:
            raise RuntimeError(
                "the file dialog was still open after pressing Enter - it likely "
                "rejected the path (check message_image resolves to a real file)"
            )
        time.sleep(0.2)
        title = _foreground_window_title()


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

    # A real recorded manual flow clicks into the message box itself before
    # ever touching the attach button - our code was skipping straight from
    # "chat opened" to "click attach", never actually focusing the compose
    # area first. Matching the recorded flow exactly here.
    send_box = wait.until(EC.presence_of_element_located((By.XPATH, SEND_BOX_XPATH)))
    send_box.click()
    time.sleep(0.3)

    _click_attach_button(driver, wait)
    time.sleep(0.5)
    _click_photos_videos(driver, wait)

    # The native dialog is not part of the DOM, so there's nothing Selenium
    # can wait on here - _select_file_in_native_dialog's own sleep is the
    # only synchronization available for it opening and gaining focus.
    _select_file_in_native_dialog(image_path)

    # Give WhatsApp a moment to finish rendering the image preview and wiring
    # up the caption box's own paste handling before we touch it - pasting
    # immediately after it merely appears in the DOM (present != actually
    # ready) is what silently swallowed the paste before, with nothing to
    # show for it and nothing raised.
    wait.until(_find_visible_caption_box)
    time.sleep(1.5)

    caption_box = _paste_caption_with_retry(driver, wait, caption)

    caption_box.send_keys(Keys.ENTER)
    time.sleep(3)  # image uploads can take longer than a plain text send


def _paste_caption_with_retry(driver, wait, caption, attempts=4):
    """Clicks the caption box and pastes, re-locating and re-clicking fresh
    on every attempt (not reusing one cached element) and checking the
    result each time - a single click+paste attempt was proving unreliable
    right after the image preview appears, failing silently with no
    exception and nothing visibly pasted."""
    last_seen = ""
    for attempt in range(1, attempts + 1):
        caption_box = wait.until(_find_visible_caption_box)
        caption_box.click()
        time.sleep(0.4)

        with _ClipboardLock():
            _copy_text_to_clipboard(caption)
            caption_box.send_keys(Keys.CONTROL, "v")
            time.sleep(0.6)

        last_seen = (caption_box.get_attribute("textContent") or "").strip()
        if last_seen:
            return caption_box

        time.sleep(0.8 * attempt)  # back off a bit more each retry

    raise RuntimeError(
        f"caption box was still empty after {attempts} paste attempts "
        f"(last seen content: {last_seen!r}) - the text paste didn't register"
    )


def _is_driver_alive(driver):
    """A cheap probe for whether the Chrome session is still usable.

    Checking the exception message text (e.g. "invalid session id") is
    brittle across ChromeDriver versions/locales - actually trying a trivial
    call and seeing if IT also fails is a much more reliable signal that the
    browser process itself is gone, not just that one particular action
    failed for some other reason.
    """
    try:
        _ = driver.title
        return True
    except Exception:
        return False


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
            config = get_config()  # local file: re-read so api_base/etc edits apply live
            remote = fetch_remote_settings(config)
            if remote:
                config = {**config, **remote}  # admin.html's values win over config.json's

            msg_limit = config.get("msg_limit")
            if msg_limit is not None and sent_count >= msg_limit:
                print(f"[system {system_id}] msg_limit ({msg_limit}) reached - stopping "
                      f"so this number doesn't send too much and get blocked. Restart "
                      f"this worker (or raise msg_limit on the admin page) to keep sending.")
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

                if not _is_driver_alive(driver):
                    # Without this, every subsequent lead would fail the exact
                    # same way forever - Chrome crashing once shouldn't take
                    # down the whole run, just this one send.
                    print(f"[system {system_id}] the Chrome session has crashed/closed - "
                          f"restarting the browser and logging back in...")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = build_driver(system_id, config)
                    wait_for_login(driver)
                    print(f"[system {system_id}] browser restarted, resuming...")

            time.sleep(config["message_delay_seconds"])
    finally:
        driver.quit()


if __name__ == "__main__":
    system_id = int(os.environ.get("SYSTEM_ID", "0"))
    run_worker(system_id)
