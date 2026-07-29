# Motilal Oswal Registration Prototype

Mobile registration form -> queued lead -> automated WhatsApp confirmation,
fanned out across multiple WhatsApp numbers/machines so no single number
absorbs all traffic.

## Structure

```
frontend/          Static mobile form + thank-you screen (index.html, style.css, script.js, logo.svg)
backend/
  app.py            Flask API: serves the frontend, POST /api/register, GET /api/queue/stats
  config.py         Loads backend/config.json fresh on every read (so live tuning works)
  config.json       num_systems, message_delay_seconds, message_template, country_code, etc.
  db.py             SQLite-backed queue, shared by the API and every worker
  whatsapp_worker.py One process per WhatsApp number/machine; sends via WhatsApp Web (Selenium)
  requirements.txt
```

## How the pieces fit together

1. A visitor scans a QR/opens the link and fills the form (`frontend/index.html`).
2. On submit, `script.js` POSTs to `/api/register`. The backend validates the
   fields and inserts a row into the `leads` table in `backend/queue.db`.
3. Each lead is deterministically assigned to one of `num_systems` partitions
   (hash of the WhatsApp/contact number `% num_systems`), so a given phone
   number always routes to the same machine even across restarts.
4. Each `whatsapp_worker.py` process only picks up leads assigned to its own
   `SYSTEM_ID`, opens `wa.me`-style deep links in WhatsApp Web, and sends the
   templated message - waiting `message_delay_seconds` between sends.
5. On success the thank-you screen (image 2) shows, with the "Open an
   Account" button linking to `https://ekyc.motilaloswal.com/open-demat-account`.

## Setting up 3 systems

`backend/config.json`:

```json
{
  "num_systems": 3,
  "message_delay_seconds": 5,
  "message_template": "Hi {full_name}, thank you for registering with Motilal Oswal! ...",
  "country_code": "91"
}
```

- `num_systems`: how many machines/WhatsApp numbers you're splitting load
  across. Change this centrally in the shared `config.json` (or the copy on
  each machine, if `queue.db` isn't shared over a network drive - see below).
- `message_delay_seconds`: seconds between two WhatsApp sends **on the same
  machine**. Three machines running in parallel therefore send roughly 3x
  as fast in aggregate while each individual number stays throttled.
- Every worker re-reads `config.json` before each send, so you can tune the
  delay live without restarting the workers.

Run one worker per machine, each with its own `SYSTEM_ID` (0, 1, 2, ...):

```bash
# Machine / WhatsApp number 1
SYSTEM_ID=0 python whatsapp_worker.py

# Machine / WhatsApp number 2
SYSTEM_ID=1 python whatsapp_worker.py

# Machine / WhatsApp number 3
SYSTEM_ID=2 python whatsapp_worker.py
```

On first run per `SYSTEM_ID`, a Chrome window opens showing a WhatsApp Web QR
code - scan it once with that machine's WhatsApp number. The login session is
cached under `backend/whatsapp_profiles/system_<id>/`, so subsequent runs skip
the QR step.

**Important - `queue.db` must be reachable by all workers.** For a real
multi-machine deployment, either:
- run `app.py` and all `whatsapp_worker.py` processes against a network share
  or a small shared database (e.g. point `db_path`/swap SQLite for Postgres),
  or
- run everything on one machine and only vary `SYSTEM_ID` per Chrome profile
  (simplest for a prototype - it still uses 3 separate WhatsApp Web sessions
  and gives you the same load-splitting behavior, just not on 3 physical PCs).

## Handling ~250 simultaneous scans

- The Flask endpoint only does validation + a single SQLite insert, so
  accepting 250 form submissions in a burst is not the bottleneck.
- WhatsApp *sending* is inherently serial per number (that's the point of the
  delay + multiple numbers), so throughput is `num_systems / message_delay_seconds`
  messages/sec in aggregate. With 3 systems and a 5s delay that's ~36
  messages/minute; tune `num_systems` and `message_delay_seconds` in
  `config.json` to match your real volume/blocking-risk tradeoff.
- SQLite is opened in WAL mode (`backend/db.py`) so the API writing new leads
  and multiple workers reading/updating their own partitions don't block each
  other.

## Running locally

```bash
cd backend
pip install -r requirements.txt
python app.py
```

Then open `frontend/index.html` directly in a mobile-width browser (or serve
it via any static file server), with `API_BASE` in `script.js` pointed at
your Flask host (defaults to `http://localhost:5000`).

Start one or more workers in separate terminals as described above to start
sending WhatsApp confirmations.

## Notes / caveats (prototype)

- `whatsapp_worker.py` automates WhatsApp Web with Selenium - the practical,
  maintained equivalent of old wrappers like `pywhatsapp`/`pywhatkit`, which
  are effectively dead. This is unofficial automation of personal WhatsApp
  accounts; confirm this fits your organization's WhatsApp usage policy
  before running it at real volume.
- The logo is a hand-built SVG recreation (`frontend/logo.svg`), not the
  original brand asset file, since the source image wasn't available as a
  file on disk.
- Message content in `config.json` is a placeholder - swap in the real copy
  once provided.
