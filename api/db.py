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
            # Single-row table (id is always 1) holding the settings the
            # admin page edits - stored centrally here, not in the local
            # backend/config.json, so every worker (wherever it runs) and the
            # admin page (on Vercel) see and change the same live values.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY,
                    message_delay_seconds INTEGER NOT NULL DEFAULT 5,
                    message_template TEXT NOT NULL DEFAULT '',
                    message_image TEXT,
                    send_image BOOLEAN NOT NULL DEFAULT TRUE,
                    msg_limit INTEGER,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("INSERT INTO settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        conn.commit()
    finally:
        conn.close()


def insert_lead(full_name, contact_number, whatsapp_number, email, agree_connect, num_systems):
    """Assigns leads to systems in strict round-robin order: lead 1 -> system
    0, lead 2 -> system 1, lead 3 -> system 2, lead 4 -> system 0, and so on -
    rather than hashing the phone number, which is deterministic but not
    perfectly even.

    Postgres's leads.id (a SERIAL) already increases strictly with every
    insert, so id % num_systems IS round-robin by insertion order - no
    separate counter table needed. The id has to be reserved from the
    sequence up front (nextval) so it can be used in the same statement that
    computes assigned_system; letting Postgres assign the id as a side effect
    of the INSERT would make it unavailable until after the row already
    exists, too late to put in the same row.
    """
    num_systems = max(1, int(num_systems))  # a 0/negative divisor would make Postgres error on the modulo

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH next_id AS (
                    SELECT nextval(pg_get_serial_sequence('leads', 'id')) AS id
                )
                INSERT INTO leads (id, full_name, contact_number, whatsapp_number, email,
                                    agree_connect, assigned_system, status)
                SELECT id, %s, %s, %s, %s, %s, (id %% %s), 'pending'
                FROM next_id
                RETURNING id, assigned_system
                """,
                (full_name, contact_number, whatsapp_number, email, agree_connect, num_systems),
            )
            lead_id, system_id = cur.fetchone()
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


def get_settings():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT message_delay_seconds, message_template, message_image,
                       send_image, msg_limit
                FROM settings WHERE id = 1
                """
            )
            row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def update_settings(message_delay_seconds, message_template, message_image, send_image, msg_limit):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE settings
                SET message_delay_seconds = %s,
                    message_template = %s,
                    message_image = %s,
                    send_image = %s,
                    msg_limit = %s,
                    updated_at = now()
                WHERE id = 1
                RETURNING message_delay_seconds, message_template, message_image,
                          send_image, msg_limit
                """,
                (message_delay_seconds, message_template, message_image, send_image, msg_limit),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()
