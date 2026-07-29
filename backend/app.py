import re
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import db
from config import get_config

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = Flask(__name__, static_folder=None)
CORS(app)

PHONE_RE = re.compile(r"^[6-9]\d{9}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@app.before_request
def _ensure_db():
    db.init_db()


@app.route("/")
def serve_index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(FRONTEND_DIR, filename)


@app.route("/api/register", methods=["POST"])
def register():
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

    lead_id, system_id = db.insert_lead(
        full_name, contact_number, whatsapp_number, email, agree_connect
    )

    return jsonify({"success": True, "lead_id": lead_id, "assigned_system": system_id}), 201


@app.route("/api/queue/stats", methods=["GET"])
def queue_stats():
    return jsonify({"config": get_config(), "stats": db.queue_stats()})


if __name__ == "__main__":
    db.init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
