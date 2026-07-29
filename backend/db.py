import sqlite3
import threading
import time
import zlib
from pathlib import Path

from config import get_config

_local = threading.local()


def _db_path():
    return Path(__file__).parent / get_config()["db_path"]


def get_conn():
    # SQLite connections aren't thread-safe to share; keep one per thread.
    if getattr(_local, "conn", None) is None:
        conn = sqlite3.connect(_db_path(), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            contact_number TEXT NOT NULL,
            whatsapp_number TEXT,
            email TEXT,
            agree_connect INTEGER NOT NULL DEFAULT 0,
            assigned_system INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            created_at REAL NOT NULL,
            sent_at REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_leads_system_status ON leads(assigned_system, status)"
    )
    conn.commit()


def assign_system(target_number, num_systems):
    """Deterministic partition so retries/restarts always route to the same worker."""
    if num_systems <= 1:
        return 0
    crc = zlib.crc32(target_number.encode("utf-8"))
    return crc % num_systems


def insert_lead(full_name, contact_number, whatsapp_number, email, agree_connect):
    num_systems = get_config()["num_systems"]
    target_number = whatsapp_number or contact_number
    system_id = assign_system(target_number, num_systems)

    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO leads (full_name, contact_number, whatsapp_number, email,
                            agree_connect, assigned_system, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            full_name,
            contact_number,
            whatsapp_number,
            email,
            1 if agree_connect else 0,
            system_id,
            time.time(),
        ),
    )
    conn.commit()
    return cur.lastrowid, system_id


def get_next_pending_for_system(system_id):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT * FROM leads
        WHERE assigned_system = ? AND status = 'pending'
        ORDER BY id ASC
        LIMIT 1
        """,
        (system_id,),
    ).fetchone()
    return row


def claim_lead(lead_id):
    """Atomically flip pending -> processing so two workers never grab the same row."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE leads SET status = 'processing' WHERE id = ? AND status = 'pending'",
        (lead_id,),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_sent(lead_id):
    conn = get_conn()
    conn.execute(
        "UPDATE leads SET status = 'sent', sent_at = ? WHERE id = ?",
        (time.time(), lead_id),
    )
    conn.commit()


def mark_failed(lead_id, error):
    conn = get_conn()
    conn.execute(
        "UPDATE leads SET status = 'failed', error = ? WHERE id = ?",
        (str(error)[:500], lead_id),
    )
    conn.commit()


def queue_stats():
    conn = get_conn()
    rows = conn.execute(
        "SELECT assigned_system, status, COUNT(*) as cnt FROM leads GROUP BY assigned_system, status"
    ).fetchall()
    return [dict(r) for r in rows]
