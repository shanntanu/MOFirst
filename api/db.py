import zlib

import psycopg2
import psycopg2.extras

from config import get_config


def get_conn():
    # A fresh connection per request is the right call for serverless (no
    # long-lived process to hold a pool in); Neon/Supabase's pooled
    # connection string (the "-pooler" host) keeps this cheap.
    #
    # Don't add sslmode here - Neon/Supabase connection strings already
    # include "?sslmode=require", and passing it twice (once in the DSN,
    # once as a kwarg) makes psycopg2/libpq reject the connection outright.
    return psycopg2.connect(get_config()["database_url"])


def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    id SERIAL PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    contact_number TEXT NOT NULL,
                    whatsapp_number TEXT,
                    email TEXT,
                    agree_connect BOOLEAN NOT NULL DEFAULT FALSE,
                    assigned_system INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    sent_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_leads_system_status ON leads(assigned_system, status)"
            )
        conn.commit()
    finally:
        conn.close()


def assign_system(target_number, num_systems):
    """Deterministic partition so retries/restarts always route to the same worker."""
    if num_systems <= 1:
        return 0
    crc = zlib.crc32(target_number.encode("utf-8"))
    return crc % num_systems


def insert_lead(full_name, contact_number, whatsapp_number, email, agree_connect, num_systems):
    target_number = whatsapp_number or contact_number
    system_id = assign_system(target_number, num_systems)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO leads (full_name, contact_number, whatsapp_number, email,
                                    agree_connect, assigned_system, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                RETURNING id
                """,
                (full_name, contact_number, whatsapp_number, email, agree_connect, system_id),
            )
            lead_id = cur.fetchone()[0]
        conn.commit()
        return lead_id, system_id
    finally:
        conn.close()


def claim_next_pending(system_id):
    """Atomically claims the oldest pending lead for this system in one
    round trip - SKIP LOCKED means two workers polling the same system_id
    can never grab the same row."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE leads SET status = 'processing'
                WHERE id = (
                    SELECT id FROM leads
                    WHERE assigned_system = %s AND status = 'pending'
                    ORDER BY id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                (system_id,),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_sent(lead_id):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE leads SET status = 'sent', sent_at = now() WHERE id = %s", (lead_id,))
        conn.commit()
    finally:
        conn.close()


def mark_failed(lead_id, error):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE leads SET status = 'failed', error = %s WHERE id = %s",
                (str(error)[:500], lead_id),
            )
        conn.commit()
    finally:
        conn.close()


def queue_stats():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT assigned_system, status, COUNT(*) as cnt FROM leads GROUP BY assigned_system, status"
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
