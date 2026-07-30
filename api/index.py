import os
import re
import sys
from functools import wraps

from flask import Flask, jsonify, request

# Vercel's Python runtime imports this file directly by path rather than as
# part of a package, so api/'s own directory is never added to sys.path -
# without this, "import db" / "from config import get_config" fail with
# ModuleNotFoundError even though db.py and config.py sit right next to this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from config import get_config

app = Flask(__name__)

PHONE_RE = re.compile(r"^[6-9]\d{9}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@app.route("/api/register", methods=["POST"])
def register():
    """Public endpoint the form on the same Vercel domain calls directly -
    same-origin, so no CORS setup needed here."""
    payload = request.get_json(silent=True) or {}

    full_name = (payload.get("full_name") or "").strip()
    contact_number = (payload.get("contact_number") or "").strip()
    whatsapp_number = (payload.get("whatsapp_number") or "").strip() or None
    email = (payload.get("email") or "").strip() or None
    agree_connect = bool(payload.get("agree_connect"))

    if not full_name:
        return jsonify({"error": "Full name is required"}), 400
    if not PHONE_RE.match(contact_number):
        return jsonify({"error": "A valid 10-digit contact number is required"}), 400
    if whatsapp_number and not PHONE_RE.match(whatsapp_number):
        return jsonify({"error": "Whatsapp number must be a valid 10-digit number"}), 400
    if email and not EMAIL_RE.match(email):
        return jsonify({"error": "Email id is not valid"}), 400

    db.init_db()
    num_systems = get_config()["num_systems"]
    lead_id, system_id = db.insert_lead(
        full_name, contact_number, whatsapp_number, email, agree_connect, num_systems
    )

    return jsonify({"success": True, "lead_id": lead_id, "assigned_system": system_id}), 201


def require_worker_key(fn):
    """Everything below is only ever called by your local whatsapp_worker.py
    (a server-to-server HTTP call, not a browser), so it's gated by a shared
    secret instead of CORS."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = get_config()["worker_api_key"]
        if expected and request.headers.get("X-Worker-Key") != expected:
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


@app.route("/api/queue/next", methods=["GET"])
@require_worker_key
def queue_next():
    system_id = int(request.args.get("system_id", "0"))
    db.init_db()
    lead = db.claim_next_pending(system_id)
    return jsonify({"lead": lead})


@app.route("/api/queue/<int:lead_id>/sent", methods=["POST"])
@require_worker_key
def queue_mark_sent(lead_id):
    db.mark_sent(lead_id)
    return jsonify({"success": True})


@app.route("/api/queue/<int:lead_id>/failed", methods=["POST"])
@require_worker_key
def queue_mark_failed(lead_id):
    payload = request.get_json(silent=True) or {}
    db.mark_failed(lead_id, payload.get("error", "unknown error"))
    return jsonify({"success": True})


@app.route("/api/queue/stats", methods=["GET"])
def queue_stats():
    return jsonify({"config": get_config(), "stats": db.queue_stats()})


@app.route("/api/settings", methods=["GET"])
@require_worker_key
def settings_get():
    """Backs the admin page (public/admin.html) AND is polled by
    whatsapp_worker.py, so message_delay_seconds/message_template/
    message_image/send_image/msg_limit can be changed from the browser and
    take effect on running workers without touching config.json or
    restarting anything."""
    db.init_db()
    return jsonify(db.get_settings())


@app.route("/api/settings", methods=["POST"])
@require_worker_key
def settings_update():
    payload = request.get_json(silent=True) or {}

    try:
        message_delay_seconds = int(payload.get("message_delay_seconds"))
        if message_delay_seconds < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "message_delay_seconds must be a non-negative integer"}), 400

    message_template = payload.get("message_template")
    if not message_template or not str(message_template).strip():
        return jsonify({"error": "message_template cannot be empty"}), 400

    message_image = (payload.get("message_image") or "").strip() or None
    send_image = bool(payload.get("send_image"))

    msg_limit_raw = payload.get("msg_limit")
    if msg_limit_raw in (None, "", "null"):
        msg_limit = None
    else:
        try:
            msg_limit = int(msg_limit_raw)
            if msg_limit < 1:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "msg_limit must be a positive integer or empty for unlimited"}), 400

    db.init_db()
    updated = db.update_settings(
        message_delay_seconds, message_template, message_image, send_image, msg_limit
    )
    return jsonify({"success": True, "settings": updated})


if __name__ == "__main__":
    # Local testing only: `python index.py` with DATABASE_URL set in your
    # environment. Vercel itself runs this file as a WSGI serverless
    # function via the `app` object above, never this block.
    db.init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
