# Motilal Oswal Registration Prototype

Mobile registration form -> queued lead -> automated WhatsApp confirmation,
fanned out across multiple WhatsApp numbers/machines so no single number
absorbs all traffic.

## Structure

```
frontend/            Static mobile form + thank-you screen
  index.html, style.css, script.js, logo.svg, message.png (WhatsApp image)

api/                 Deployed to Vercel as serverless functions
  index.py            Flask app: POST /api/register, GET /api/queue/next,
                       POST /api/queue/<id>/sent, POST /api/queue/<id>/failed,
                       GET /api/queue/stats
  config.py           Reads NUM_SYSTEMS / WORKER_API_KEY / DATABASE_URL from env
  db.py               Postgres-backed queue (hosted DB - Neon/Supabase, etc.)

vercel.json           Routes /api/* to api/index.py, everything else to frontend/
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
   Vercel deployment (`frontend/index.html`).
2. On submit, `script.js` POSTs to `/api/register` - same Vercel domain, so
   no CORS is involved. `api/index.py` validates the fields and inserts a row
   into the `leads` table in your hosted Postgres database.
3. Each lead is deterministically assigned to one of `NUM_SYSTEMS` partitions
   (hash of the WhatsApp/contact number `% NUM_SYSTEMS`), so a given phone
   number always routes to the same worker even across restarts.
4. On the machine you keep WhatsApp Web logged into, `whatsapp_worker.py`
   polls `GET /api/queue/next?system_id=<SYSTEM_ID>` every couple of seconds
   over plain outbound HTTPS - no inbound port, no tunnel needed. When it
   gets a lead, it sends the message (+ image) via WhatsApp Web, then reports
   back `POST /api/queue/<id>/sent` or `.../failed`.
5. On success the thank-you screen shows, with the "Open an Account" button
   linking to `https://ekyc.motilaloswal.com/open-demat-account`.

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
- Deploy. You'll get a URL like `https://your-app.vercel.app` - opening it
  should show the registration form.

### 3. Configure and run the local worker(s)

Edit `backend/config.json`:

```json
{
  "api_base": "https://your-app.vercel.app",
  "worker_api_key": "the same secret you set in Vercel",
  "message_delay_seconds": 5,
  "message_template": "Hi {first_name}, ...",
  "message_image": "../frontend/message.png",
  "country_code": "91"
}
```

- `api_base` / `worker_api_key` must match your Vercel deployment/env var.
- `message_delay_seconds`: seconds between two WhatsApp sends **on the same
  machine**. Running 3 workers in parallel therefore sends roughly 3x as
  fast in aggregate while each individual number stays throttled.
- `message_template` supports `{first_name}` (first word of the submitted
  full name), `{full_name}`, and `{contact_number}` placeholders.
- `message_image`: path (relative to `backend/`) to an image sent alongside
  the message as a caption. Set to `null` to send text-only messages instead.
- The worker re-reads `config.json` before each send, so you can tune the
  delay/template/image live without restarting it.

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

Then in `frontend/index.html`, temporarily uncomment/set
`window.MO_API_BASE = "http://localhost:5000";` and open the form directly.

## Notes / caveats (prototype)

- `whatsapp_worker.py` automates WhatsApp Web with Selenium - the practical,
  maintained equivalent of old wrappers like `pywhatsapp`/`pywhatkit`, which
  are effectively dead. This is unofficial automation of personal WhatsApp
  accounts; confirm this fits your organization's WhatsApp usage policy
  before running it at real volume.
- The logo is a hand-built SVG recreation (`frontend/logo.svg`) plus the
  actual brand PNG you supplied (`frontend/MO Logo.png`), used in the form header.
- Image sending automates WhatsApp Web's attach-photo flow (click attach,
  feed the file input, type the caption, hit enter). This is the most
  UI-fragile part of the script - if WhatsApp changes their DOM, the
  `ATTACH_BUTTON_SELECTOR` / `IMAGE_FILE_INPUT_SELECTOR` / `CAPTION_XPATH`
  constants at the top of `whatsapp_worker.py` are the first things to
  re-check (right-click the relevant element in WhatsApp Web -> Inspect).
- `WORKER_API_KEY` is a simple shared secret, not full auth - fine for a
  prototype with a small number of trusted workers, but rotate it if it
  ever leaks, and don't reuse it as a real password anywhere.
