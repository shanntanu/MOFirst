# Motilal Oswal Registration Prototype

Mobile registration form -> queued lead -> automated WhatsApp confirmation,
fanned out across multiple WhatsApp numbers/machines so no single number
absorbs all traffic.

## Structure

```
public/              Static mobile form + thank-you screen - Vercel's zero-config
                     static-serving convention: everything here is served at
                     the site root automatically (index.html at "/", etc.)
  index.html, style.css, script.js, logo.svg, message.png (WhatsApp image)

api/                 Deployed to Vercel as serverless functions
  index.py            Flask app: POST /api/register, GET /api/queue/next,
                       POST /api/queue/<id>/sent, POST /api/queue/<id>/failed,
                       GET /api/queue/stats
  config.py           Reads NUM_SYSTEMS / WORKER_API_KEY / DATABASE_URL from env
  db.py               Postgres-backed queue (hosted DB - Neon/Supabase, etc.)

vercel.json           Routes /api/* to api/index.py; public/ needs no rewrite
requirements.txt      Python deps for the Vercel function (flask, psycopg2-binary)

backend/               Runs locally on your always-on machine - NOT deployed
  whatsapp_worker.py    One process per WhatsApp number; polls the Vercel API
                        over HTTPS and sends via WhatsApp Web (Selenium)
  config.py, config.json  Local-only worker settings (message text, delay,
                        image, which Vercel URL to poll)
  requirements.txt      selenium, requests
```

## How the pieces fit together

1. A visitor scans a QR/opens the link and fills the form, served from your
   Vercel deployment (`public/index.html`).
2. On submit, `script.js` POSTs to `/api/register` - same Vercel domain, so
   no CORS is involved. `api/index.py` validates the fields and inserts a row
   into the `leads` table in your hosted Postgres database.
3. Each lead is assigned to a system in strict round-robin order as they
   arrive - the 1st lead goes to system 0, the 2nd to system 1, the 3rd to
   system 2, the 4th back to system 0, and so on - splitting load evenly
   across your `NUM_SYSTEMS` numbers regardless of arrival patterns.
4. On the machine you keep WhatsApp Web logged into, `whatsapp_worker.py`
   polls `GET /api/queue/next?system_id=<SYSTEM_ID>` every couple of seconds
   over plain outbound HTTPS - no inbound port, no tunnel needed. When it
   gets a lead, it sends the message (+ image) via WhatsApp Web, then reports
   back `POST /api/queue/<id>/sent` or `.../failed`.
5. On success the thank-you screen shows, with the "Open an Account" button
   linking to `https://ekyc.motilaloswal.com/open-demat-account`.

**If a system goes quiet** (worker crashed, machine turned off, etc.), its
pending and failed leads don't just sit there forever. Every poll updates a
heartbeat for that `system_id`, and also checks for any *other* system whose
heartbeat has gone stale (no poll in `WORKER_STALE_SECONDS`, default 90) -
that system's pending/failed leads get reassigned to the one currently
polling, so whichever workers are actually running keep the whole queue
moving. Leads already mid-send (`processing`) or already `sent` are never
touched by this.

**Why this split:** Vercel functions are stateless and short-lived with no
persistent disk - fine for quick DB reads/writes, but incompatible with a
Selenium/Chrome session that must stay logged into WhatsApp Web for weeks.
So the queue/API lives on Vercel + a hosted Postgres, while the actual
WhatsApp sending stays on a machine you control, reaching out instead of
being reached.

## One-time setup

### 1. Create a free hosted Postgres database

Use [Neon](https://neon.tech) or [Supabase](https://supabase.com) (both have
a free tier). Create a project/database and copy its connection string
(use the **pooled** connection string if offered - e.g. Neon's `-pooler`
host - since each API request opens a fresh connection).

### 2. Deploy to Vercel

- Push this repo to GitHub and import it in Vercel, or run `vercel deploy`
  from the project root.
- In the Vercel project's **Environment Variables**, set:
  | Variable | Value |
  |---|---|
  | `DATABASE_URL` | the connection string from step 1 |
  | `NUM_SYSTEMS` | how many WhatsApp numbers/workers you're running (e.g. `3`) |
  | `WORKER_API_KEY` | a random secret string you make up - protects the queue endpoints from random internet traffic |
  | `WORKER_STALE_SECONDS` | optional, default `90` - how long a system can go without polling before its stuck leads get reassigned elsewhere |
- Deploy. You'll get a URL like `https://your-app.vercel.app` - opening it
  should show the registration form.

### 3. Configure and run the local worker(s)

Edit `backend/config.json`:

```json
{
  "api_base": "https://your-app.vercel.app",
  "worker_api_key": "the same secret you set in Vercel",
  "country_code": "91"
}
```

`api_base` / `worker_api_key` must match your Vercel deployment/env var -
these two (plus `country_code`, `chrome_profile_root`, `headless`) are
genuinely local to this machine and stay in `config.json`.

**`message_delay_seconds`, `message_template`, `message_image`, `send_image`,
and `msg_limit` are controlled from `https://your-app.vercel.app/admin.html`
instead** - not `config.json`. They live in the same Postgres database as the
lead queue, so:
- Open `/admin.html`, enter the same `worker_api_key`, click **Load**, edit,
  **Save**.
- Every running worker re-fetches these on its next loop iteration (every
  couple of seconds) and applies the change immediately - no restart needed,
  even mid-run.
- `config.json` still carries fallback values for these 5 fields (used only
  if a worker can't reach the settings API at all, e.g. no network), and
  `init_db()` seeds sensible defaults into Postgres the first time it runs.
- `msg_limit` caps how many messages **this one worker/number** will send
  **per run**, as a safety net against WhatsApp flagging/blocking a number
  for sending too much. Once reached, the worker prints a message and stops
  itself (it does not crash or lose the queue - pending leads just stay
  queued). Leave empty for no limit. Restart the worker (after raising
  `msg_limit` on the admin page if needed) to resume sending - the sent-count
  resets to 0 each run, so raising the limit alone doesn't require a restart,
  but getting past an already-reached limit does.

Install deps and run one worker per WhatsApp number, each with its own
`SYSTEM_ID` (must match `NUM_SYSTEMS` on Vercel - i.e. values `0` through
`NUM_SYSTEMS - 1`):

```bash
cd backend
pip install -r requirements.txt

# WhatsApp number 1
set SYSTEM_ID=0 && python whatsapp_worker.py

# WhatsApp number 2 (separate terminal/machine)
set SYSTEM_ID=1 && python whatsapp_worker.py

# WhatsApp number 3 (separate terminal/machine)
set SYSTEM_ID=2 && python whatsapp_worker.py
```

(PowerShell: `$env:SYSTEM_ID=0; python whatsapp_worker.py`)

On first run per `SYSTEM_ID`, a Chrome window opens showing a WhatsApp Web QR
code - scan it once with that number's WhatsApp. The session is cached under
`backend/whatsapp_profiles/system_<id>/`, so future runs skip the QR step.
Keep these processes running continuously (e.g. via `pm2`, `systemd`, Windows
Task Scheduler, or NSSM) so sending resumes automatically after a reboot.

## Handling ~250 simultaneous scans

- `POST /api/register` only validates + inserts one row, so a burst of 250
  submissions hits the database, not a bottleneck in the app logic. Neon/
  Supabase free tiers comfortably handle this volume of simple inserts.
- WhatsApp *sending* is inherently serial per number (that's the point of
  the delay + multiple numbers), so throughput is roughly
  `NUM_SYSTEMS / message_delay_seconds` messages/sec in aggregate. With 3
  workers and a 5s delay that's ~36 messages/minute; tune `NUM_SYSTEMS` (on
  Vercel) and `message_delay_seconds` (per worker) to match your real
  volume/blocking-risk tradeoff.
- `claim_next_pending` in `api/db.py` uses `FOR UPDATE SKIP LOCKED`, so two
  workers can never accidentally grab the same lead.

## Local testing without deploying

You can run the API locally against the same hosted Postgres before
deploying to Vercel:

```bash
cd api
pip install -r ../requirements.txt
set DATABASE_URL=<your connection string> && python index.py
```

Then in `public/index.html`, temporarily uncomment/set
`window.MO_API_BASE = "http://localhost:5000";` and open the form directly.

## Notes / caveats (prototype)

- `whatsapp_worker.py` automates WhatsApp Web with Selenium - the practical,
  maintained equivalent of old wrappers like `pywhatsapp`/`pywhatkit`, which
  are effectively dead. This is unofficial automation of personal WhatsApp
  accounts; confirm this fits your organization's WhatsApp usage policy
  before running it at real volume.
- The logo is a hand-built SVG recreation (`public/logo.svg`) plus the
  actual brand PNG you supplied (`public/MO Logo.png`), used in the form header.
- Image+caption sending deliberately avoids three traps, all documented in the
  module docstring of `whatsapp_worker.py`:
  1. It never clicks the attach menu's "Photos & videos" row - that makes
     WhatsApp open a native Windows file dialog, which Selenium cannot touch,
     and the script hangs. The file is set on the input via CDP
     `DOM.setFileInputFiles` instead.
  2. Clipboard paste (`Ctrl+V`) is the fallback, not the primary, because the
     OS clipboard is machine-wide: with several workers on one machine, two
     concurrent pastes could send the wrong image to the wrong chat. A
     cross-process lock (`_ClipboardLock`) guards it.
  3. The caption is inserted with CDP `Input.insertText`, never `send_keys` -
     ChromeDriver rejects non-BMP emoji (the copy contains U+1F389, U+1F4BB,
     U+1F5D3), and a raw newline in the caption box fires Enter, which sends
     a truncated message.
- Selector fragility remains the most likely future breakage. If sending stops
  working after a WhatsApp update, re-check `CAPTION_XPATH`,
  `SEND_BUTTON_SELECTOR`, `ATTACH_BUTTON_SELECTOR` and `FILE_INPUT_SELECTOR` at
  the top of `whatsapp_worker.py` (right-click the element in WhatsApp Web ->
  Inspect). Every failure saves a screenshot to `backend/debug_screenshots/`
  showing exactly what was on screen at the time.
- `WORKER_API_KEY` is a simple shared secret, not full auth - fine for a
  prototype with a small number of trusted workers, but rotate it if it
  ever leaks, and don't reuse it as a real password anywhere.
