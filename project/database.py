"""
SQLite persistence layer.

Design notes
- Schema is created idempotently and upgraded in place (columns are added when
  missing), so databases created by the first version of this project keep their data.
- Helper functions never commit. The Flask request lifecycle commits once per request
  (or rolls back on an unhandled error), which keeps multi-step operations atomic.
- Money is stored as integer minor units (cents) in the `amount_cents` columns.
"""
import json
import logging
import sqlite3
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from utils import utc_now_iso

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 2

ORDER_STATUSES = ("Processing", "In Transit", "Delivered", "Cancelled", "Returned")
PAYMENT_STATUSES = ("unpaid", "pending", "paid", "partially_refunded", "refunded", "failed")
TICKET_STATUSES = ("Open", "In Progress", "Waiting for Customer", "Resolved", "Closed")
TICKET_PRIORITIES = ("Low", "Medium", "High", "Urgent")

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    customer_name TEXT DEFAULT 'Customer',
    pending_intent TEXT,
    context_data TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    sender TEXT NOT NULL CHECK(sender IN ('user', 'bot')),
    text TEXT NOT NULL,
    predicted_intent TEXT,
    confidence REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('Processing', 'In Transit', 'Delivered', 'Cancelled', 'Returned')),
    order_date TEXT NOT NULL,
    estimated_delivery TEXT NOT NULL,
    carrier TEXT NOT NULL,
    tracking_number TEXT NOT NULL,
    items TEXT NOT NULL,
    total_amount REAL NOT NULL,
    return_eligible INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id TEXT PRIMARY KEY,
    conversation_id TEXT,
    customer_name TEXT NOT NULL,
    customer_email TEXT,
    issue_description TEXT NOT NULL,
    predicted_category TEXT NOT NULL,
    priority TEXT NOT NULL CHECK(priority IN ('Low', 'Medium', 'High', 'Urgent')),
    status TEXT NOT NULL CHECK(status IN ('Open', 'In Progress', 'Waiting for Customer', 'Resolved', 'Closed')),
    assigned_agent TEXT,
    resolution_notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ticket_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    agent_username TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES support_tickets(ticket_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'Support Agent',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    message_id INTEGER,
    rating TEXT CHECK(rating IN ('helpful', 'unhelpful')),
    comment TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    conversation_id TEXT,
    provider TEXT NOT NULL,
    provider_session_id TEXT UNIQUE,
    provider_payment_id TEXT,
    checkout_url TEXT,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'succeeded', 'failed', 'expired')),
    customer_email TEXT,
    idempotency_key TEXT UNIQUE NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    paid_at TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    provider_refund_id TEXT UNIQUE,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    currency TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'succeeded', 'failed')),
    initiated_by TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (payment_id) REFERENCES payments(id),
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS payment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    details TEXT,
    ip_address TEXT,
    created_at TEXT NOT NULL
);
"""

# Columns added after version 1. Each entry: (table, column, declaration).
# _ensure_columns() adds any that are missing, so old databases are upgraded in place.
ADDED_COLUMNS = [
    ("orders", "customer_email", "TEXT"),
    ("orders", "amount_cents", "INTEGER"),
    ("orders", "currency", "TEXT NOT NULL DEFAULT 'usd'"),
    ("orders", "payment_status", "TEXT NOT NULL DEFAULT 'unpaid' "
                                 "CHECK(payment_status IN ('unpaid','pending','paid','partially_refunded','refunded','failed'))"),
    ("orders", "updated_at", "TEXT"),
    ("support_tickets", "order_id", "TEXT"),
    ("agent_accounts", "access_level", "TEXT NOT NULL DEFAULT 'agent' CHECK(access_level IN ('admin','agent'))"),
    ("agent_accounts", "is_active", "INTEGER NOT NULL DEFAULT 1"),
    ("agent_accounts", "must_change_password", "INTEGER NOT NULL DEFAULT 0"),
    ("agent_accounts", "failed_login_count", "INTEGER NOT NULL DEFAULT 0"),
    ("agent_accounts", "locked_until", "TEXT"),
    ("agent_accounts", "last_login_at", "TEXT"),
    ("agent_accounts", "email", "TEXT"),
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_email ON orders(customer_email)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_status ON support_tickets(status)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_priority ON support_tickets(priority)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_created ON support_tickets(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_email ON support_tickets(customer_email)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_order ON support_tickets(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_ticket_notes_ticket ON ticket_notes(ticket_id)",
    "CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_refunds_payment ON refunds(payment_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)",
]

DEMO_ORDERS = [
    # order_id, customer_id, name, status, order_date, est_delivery, carrier, tracking, items, total, return_eligible, email, payment_status
    ("ORD1025", "CUST-101", "John Doe", "In Transit", "2026-10-08", "2026-10-14", "FedEx Express", "FDX-9988231",
     "Wireless Noise-Canceling Headphones (x1)", 149.99, 1, "john.doe@example.com", "paid"),
    ("ORD1026", "CUST-102", "Jane Smith", "Delivered", "2026-10-01", "2026-10-06", "UPS Ground", "UPS-4433120",
     "Ergonomic Office Chair (x1)", 289.50, 0, "jane.smith@example.com", "paid"),
    ("ORD1027", "CUST-103", "Michael Brown", "Processing", "2026-10-10", "2026-10-16", "DHL Express", "DHL-7711200",
     "Mechanical Gaming Keyboard RGB (x1)", 89.00, 1, "michael.brown@example.com", "paid"),
    ("ORD1028", "CUST-104", "Emily Davis", "Cancelled", "2026-10-02", "N/A", "N/A", "N/A",
     "Smart Fitness Watch (x1)", 199.00, 0, "emily.davis@example.com", "unpaid"),
    ("ORD1029", "CUST-105", "David Wilson", "Delivered", "2026-08-15", "2026-08-20", "FedEx Standard", "FDX-1122334",
     "Ultra HD Webcam 4K (x1)", 75.00, 0, "david.wilson@example.com", "paid"),
    ("ORD1030", "CUST-106", "Sarah Jenkins", "Returned", "2026-09-12", "2026-09-18", "UPS Return", "UPS-8877665",
     "Bluetooth Portable Speaker (x1)", 59.99, 0, "sarah.jenkins@example.com", "paid"),
    ("ORD1031", "CUST-107", "Priya Raman", "Delivered", "2026-10-28", "2026-11-02", "BlueDart Express", "BD-5520117",
     "Noise-Isolating Earbuds (x2)", 124.00, 1, "priya.raman@example.com", "paid"),
]


def utc_now():
    return utc_now_iso()


def get_db_connection(db_path):
    """Open a SQLite connection with FK enforcement, WAL journaling and a busy timeout."""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _ensure_columns(conn):
    """Add any missing columns. Returns the set of (table, column) pairs that were added."""
    added = set()
    for table, column, declaration in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            added.add((table, column))
            logger.info("Migration: added %s.%s", table, column)
    return added


def _backfill_after_upgrade(conn, added):
    """One-time data fixes for databases created before schema version 2."""
    if ("orders", "payment_status") in added:
        conn.execute("""
            UPDATE orders SET payment_status = 'paid'
            WHERE status IN ('In Transit', 'Delivered', 'Returned') AND payment_status = 'unpaid'
        """)
    if ("agent_accounts", "access_level") in added:
        conn.execute("UPDATE agent_accounts SET access_level = 'admin' WHERE username = 'admin'")
    conn.execute("UPDATE orders SET amount_cents = CAST(ROUND(total_amount * 100) AS INTEGER) "
                 "WHERE amount_cents IS NULL")
    conn.execute("UPDATE orders SET updated_at = order_date WHERE updated_at IS NULL")


def init_db(db_path, seed_data=True, bootstrap_username=None, bootstrap_password=None,
            on_bootstrap_created=None):
    """
    Create or upgrade the schema, optionally seed demo data and the first admin.

    The whole start-up runs in one IMMEDIATE transaction. Several gunicorn workers call this at
    the same time; SQLite serialises them, so the migrations and the admin creation happen exactly
    once. Returns an open connection.
    """
    conn = get_db_connection(db_path)
    conn.isolation_level = None  # explicit transaction control below
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in BASE_SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        added = _ensure_columns(conn)
        for statement in INDEXES:
            conn.execute(statement)
        _backfill_after_upgrade(conn, added)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        if seed_data:
            seed_demo_data(conn)
        if bootstrap_username and not get_agent_by_username(conn, bootstrap_username):
            if bootstrap_password is None:
                raise ValueError("bootstrap_password is required to create the first admin")
            ensure_bootstrap_admin(conn, bootstrap_username, generate_password_hash(bootstrap_password))
            if on_bootstrap_created:
                on_bootstrap_created(bootstrap_username)
        flag_legacy_default_password(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise
    conn.isolation_level = ""  # restore default implicit transactions for request handling
    return conn


def current_schema_version(conn):
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    return row["version"] if row else 0


def seed_demo_data(conn):
    """Insert demo orders (and matching sandbox payments for paid orders). Idempotent."""
    for (order_id, cust_id, name, status, order_date, est, carrier, tracking, items,
         total, return_eligible, email, payment_status) in DEMO_ORDERS:
        amount_cents = int(round(total * 100))
        conn.execute("""
            INSERT OR IGNORE INTO orders (
                order_id, customer_id, customer_name, status, order_date, estimated_delivery,
                carrier, tracking_number, items, total_amount, return_eligible,
                customer_email, amount_cents, currency, payment_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'usd', ?, ?)
        """, (order_id, cust_id, name, status, order_date, est, carrier, tracking, items, total,
              return_eligible, email, amount_cents, payment_status, utc_now()))
        # Backfill contact email for legacy rows that predate the email column.
        conn.execute("UPDATE orders SET customer_email = ? WHERE order_id = ? AND customer_email IS NULL",
                     (email, order_id))
        if payment_status == "paid":
            conn.execute("""
                INSERT OR IGNORE INTO payments (
                    order_id, provider, provider_session_id, provider_payment_id, amount_cents, currency,
                    status, customer_email, idempotency_key, created_at, updated_at, paid_at
                ) VALUES (?, 'sandbox', ?, ?, ?, 'usd', 'succeeded', ?, ?, ?, ?, ?)
            """, (order_id, f"seed_cs_{order_id}", f"seed_pi_{order_id}", amount_cents, email,
                  f"seed-{order_id}", order_date, order_date, order_date))


def ensure_bootstrap_admin(conn, username, password_hash):
    """Create the first admin account (caller decides whether it is needed). Returns True."""
    now = utc_now()
    conn.execute("""
        INSERT INTO agent_accounts (username, password_hash, full_name, role, created_at,
                                    access_level, must_change_password)
        VALUES (?, ?, 'System Administrator', 'Lead Support Specialist', ?, 'admin', 1)
    """, (username, password_hash, now))
    return True


LEGACY_DEFAULT_PASSWORDS = ("admin123", "agent123")


def flag_legacy_default_password(conn):
    """
    Accounts still using a password that was published in earlier versions must change it at next sign-in.
    Returns the number of accounts flagged.
    """
    flagged = 0
    for row in conn.execute("SELECT username, password_hash, must_change_password FROM agent_accounts").fetchall():
        if row["must_change_password"]:
            continue
        if any(check_password_hash(row["password_hash"], pw) for pw in LEGACY_DEFAULT_PASSWORDS):
            conn.execute("UPDATE agent_accounts SET must_change_password = 1 WHERE username = ?", (row["username"],))
            logger.warning("Account '%s' used a published default password; it must be changed at next sign-in.",
                           row["username"])
            flagged += 1
    return flagged


def ping(conn):
    conn.execute("SELECT 1").fetchone()
    return True


# ----------------------------------------------------------------------
# Conversations & messages
# ----------------------------------------------------------------------

def create_conversation(conn, conversation_id, customer_name="Customer"):
    now = utc_now()
    conn.execute("""
        INSERT INTO conversations (id, customer_name, pending_intent, context_data, created_at, updated_at)
        VALUES (?, ?, NULL, '{}', ?, ?)
    """, (conversation_id, customer_name or "Customer", now, now))
    return get_conversation(conn, conversation_id)


def get_conversation(conn, conversation_id):
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["context_data"] = json.loads(data["context_data"]) if data.get("context_data") else {}
    except json.JSONDecodeError:
        data["context_data"] = {}
    return data


def update_conversation_context(conn, conversation_id, pending_intent=None, context_data=None):
    conn.execute("""
        UPDATE conversations SET pending_intent = ?, context_data = ?, updated_at = ? WHERE id = ?
    """, (pending_intent, json.dumps(context_data or {}), utc_now(), conversation_id))


def save_message(conn, conversation_id, sender, text, predicted_intent=None, confidence=None):
    now = utc_now()
    cursor = conn.execute("""
        INSERT INTO messages (conversation_id, sender, text, predicted_intent, confidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (conversation_id, sender, text, predicted_intent, confidence, now))
    conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
    return cursor.lastrowid


def get_conversation_messages(conn, conversation_id):
    rows = conn.execute("""
        SELECT id, sender, text, predicted_intent, confidence, created_at
        FROM messages WHERE conversation_id = ? ORDER BY id ASC
    """, (conversation_id,)).fetchall()
    return [dict(row) for row in rows]


# ----------------------------------------------------------------------
# Orders
# ----------------------------------------------------------------------

def get_order(conn, order_id):
    if not order_id:
        return None
    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id.strip().upper(),)).fetchone()
    return dict(row) if row else None


def list_orders(conn, search=None, payment_status=None, limit=20, offset=0):
    query = "SELECT * FROM orders WHERE 1=1"
    params = []
    if payment_status and payment_status != "All":
        query += " AND payment_status = ?"
        params.append(payment_status)
    if search:
        pattern = f"%{_escape_like(search.strip())}%"
        query += " AND (order_id LIKE ? ESCAPE '\\' OR customer_email LIKE ? ESCAPE '\\' OR customer_name LIKE ? ESCAPE '\\')"
        params.extend([pattern, pattern, pattern])
    query += " ORDER BY order_date DESC, order_id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def count_orders(conn, search=None, payment_status=None):
    query = "SELECT COUNT(*) AS total FROM orders WHERE 1=1"
    params = []
    if payment_status and payment_status != "All":
        query += " AND payment_status = ?"
        params.append(payment_status)
    if search:
        pattern = f"%{_escape_like(search.strip())}%"
        query += " AND (order_id LIKE ? ESCAPE '\\' OR customer_email LIKE ? ESCAPE '\\' OR customer_name LIKE ? ESCAPE '\\')"
        params.extend([pattern, pattern, pattern])
    return conn.execute(query, params).fetchone()["total"]


def set_order_status(conn, order_id, status):
    conn.execute("UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?", (status, utc_now(), order_id))


def set_order_payment_status(conn, order_id, payment_status):
    conn.execute("UPDATE orders SET payment_status = ?, updated_at = ? WHERE order_id = ?",
                 (payment_status, utc_now(), order_id))


# ----------------------------------------------------------------------
# Payments, refunds, provider events
# ----------------------------------------------------------------------

def create_payment(conn, **fields):
    now = utc_now()
    fields.setdefault("created_at", now)
    fields.setdefault("updated_at", now)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cursor = conn.execute(f"INSERT INTO payments ({columns}) VALUES ({placeholders})", list(fields.values()))
    return cursor.lastrowid


def update_payment(conn, payment_id, **fields):
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE payments SET {assignments} WHERE id = ?", list(fields.values()) + [payment_id])


def get_payment(conn, payment_id):
    row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    return dict(row) if row else None


def get_payment_by_session(conn, provider_session_id):
    row = conn.execute("SELECT * FROM payments WHERE provider_session_id = ?", (provider_session_id,)).fetchone()
    return dict(row) if row else None


def get_latest_pending_payment(conn, order_id, created_after_iso):
    row = conn.execute("""
        SELECT * FROM payments
        WHERE order_id = ? AND status = 'pending' AND created_at >= ?
        ORDER BY id DESC LIMIT 1
    """, (order_id, created_after_iso)).fetchone()
    return dict(row) if row else None


def get_succeeded_payment(conn, order_id):
    row = conn.execute("""
        SELECT * FROM payments WHERE order_id = ? AND status = 'succeeded'
        ORDER BY id DESC LIMIT 1
    """, (order_id,)).fetchone()
    return dict(row) if row else None


def list_payments(conn, status=None, limit=20, offset=0):
    query = "SELECT * FROM payments WHERE 1=1"
    params = []
    if status and status != "All":
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def count_payments(conn, status=None):
    if status and status != "All":
        return conn.execute("SELECT COUNT(*) AS n FROM payments WHERE status = ?", (status,)).fetchone()["n"]
    return conn.execute("SELECT COUNT(*) AS n FROM payments").fetchone()["n"]


def list_payments_for_order(conn, order_id):
    rows = conn.execute("SELECT * FROM payments WHERE order_id = ? ORDER BY id DESC", (order_id,)).fetchall()
    return [dict(r) for r in rows]


def create_refund(conn, **fields):
    now = utc_now()
    fields.setdefault("created_at", now)
    fields.setdefault("updated_at", now)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cursor = conn.execute(f"INSERT INTO refunds ({columns}) VALUES ({placeholders})", list(fields.values()))
    return cursor.lastrowid


def update_refund(conn, refund_id, **fields):
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE refunds SET {assignments} WHERE id = ?", list(fields.values()) + [refund_id])


def get_refund(conn, refund_id):
    row = conn.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone()
    return dict(row) if row else None


def get_refund_by_provider_id(conn, provider_refund_id):
    row = conn.execute("SELECT * FROM refunds WHERE provider_refund_id = ?", (provider_refund_id,)).fetchone()
    return dict(row) if row else None


def get_refund_by_idempotency_key(conn, key):
    row = conn.execute("SELECT * FROM refunds WHERE idempotency_key = ?", (key,)).fetchone()
    return dict(row) if row else None


def list_refunds_for_order(conn, order_id):
    rows = conn.execute("SELECT * FROM refunds WHERE order_id = ? ORDER BY id DESC", (order_id,)).fetchall()
    return [dict(r) for r in rows]


def refunded_total_cents(conn, payment_id):
    """Sum of refunds that are pending or succeeded (failed refunds do not count)."""
    row = conn.execute("""
        SELECT COALESCE(SUM(amount_cents), 0) AS total FROM refunds
        WHERE payment_id = ? AND status IN ('pending', 'succeeded')
    """, (payment_id,)).fetchone()
    return int(row["total"])


def record_payment_event(conn, provider, event_id, event_type, payload):
    """
    Store a webhook event. Returns False if the event was already recorded.
    The UNIQUE constraint makes this safe under concurrent deliveries.
    """
    try:
        conn.execute("""
            INSERT INTO payment_events (provider, event_id, event_type, payload, received_at)
            VALUES (?, ?, ?, ?, ?)
        """, (provider, event_id, event_type, payload, utc_now()))
        return True
    except sqlite3.IntegrityError:
        return False


def last_payment_event_time(conn):
    row = conn.execute("SELECT MAX(received_at) AS last FROM payment_events").fetchone()
    return row["last"] if row else None


# ----------------------------------------------------------------------
# Support tickets
# ----------------------------------------------------------------------

def create_support_ticket(conn, ticket_id, conversation_id, customer_name, customer_email,
                          issue_description, predicted_category, priority="Medium",
                          status="Open", assigned_agent=None, order_id=None):
    now = utc_now()
    if conversation_id:
        exists = conn.execute("SELECT id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not exists:
            conn.execute("""
                INSERT INTO conversations (id, customer_name, pending_intent, context_data, created_at, updated_at)
                VALUES (?, ?, NULL, '{}', ?, ?)
            """, (conversation_id, customer_name or "Customer", now, now))
    conn.execute("""
        INSERT INTO support_tickets (
            ticket_id, conversation_id, customer_name, customer_email, issue_description,
            predicted_category, priority, status, assigned_agent, resolution_notes,
            created_at, updated_at, order_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
    """, (ticket_id, conversation_id, customer_name, customer_email, issue_description,
          predicted_category, priority, status, assigned_agent, now, now, order_id))
    return get_ticket(conn, ticket_id)


def get_ticket(conn, ticket_id):
    if not ticket_id:
        return None
    row = conn.execute("SELECT * FROM support_tickets WHERE ticket_id = ?", (ticket_id.strip().upper(),)).fetchone()
    return dict(row) if row else None


def _escape_like(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _ticket_filters(status, priority, search):
    query = " WHERE 1=1"
    params = []
    if status and status != "All":
        query += " AND status = ?"
        params.append(status)
    if priority and priority != "All":
        query += " AND priority = ?"
        params.append(priority)
    if search:
        pattern = f"%{_escape_like(search.strip())}%"
        query += (" AND (ticket_id LIKE ? ESCAPE '\\' OR customer_name LIKE ? ESCAPE '\\' "
                  "OR issue_description LIKE ? ESCAPE '\\' OR predicted_category LIKE ? ESCAPE '\\' "
                  "OR customer_email LIKE ? ESCAPE '\\')")
        params.extend([pattern] * 5)
    return query, params


def get_tickets(conn, status=None, priority=None, search=None, limit=20, offset=0):
    where, params = _ticket_filters(status, priority, search)
    rows = conn.execute(f"SELECT * FROM support_tickets{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                        params + [limit, offset]).fetchall()
    return [dict(r) for r in rows]


def count_tickets(conn, status=None, priority=None, search=None):
    where, params = _ticket_filters(status, priority, search)
    return conn.execute(f"SELECT COUNT(*) AS total FROM support_tickets{where}", params).fetchone()["total"]


def get_ticket_stats(conn):
    stats = {"total": 0, "open": 0, "in_progress": 0, "waiting": 0, "resolved": 0, "closed": 0, "urgent_high": 0}
    rows = conn.execute("SELECT status, priority, COUNT(*) AS count FROM support_tickets GROUP BY status, priority")
    for row in rows.fetchall():
        cnt, st, pr = row["count"], row["status"], row["priority"]
        stats["total"] += cnt
        key = {"Open": "open", "In Progress": "in_progress", "Waiting for Customer": "waiting",
               "Resolved": "resolved", "Closed": "closed"}.get(st)
        if key:
            stats[key] += cnt
        if pr in ("Urgent", "High") and st not in ("Resolved", "Closed"):
            stats["urgent_high"] += cnt
    return stats


def update_ticket(conn, ticket_id, status=None, priority=None, assigned_agent=None,
                  resolution_notes=None, clear_assignee=False):
    existing = get_ticket(conn, ticket_id)
    if not existing:
        return None
    updates = ["updated_at = ?"]
    params = [utc_now()]
    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if priority is not None:
        updates.append("priority = ?")
        params.append(priority)
    if clear_assignee:
        updates.append("assigned_agent = NULL")
    elif assigned_agent is not None:
        updates.append("assigned_agent = ?")
        params.append(assigned_agent)
    if resolution_notes is not None:
        updates.append("resolution_notes = ?")
        params.append(resolution_notes)
    params.append(existing["ticket_id"])
    conn.execute(f"UPDATE support_tickets SET {', '.join(updates)} WHERE ticket_id = ?", params)
    return get_ticket(conn, existing["ticket_id"])


def add_ticket_note(conn, ticket_id, agent_username, note):
    clean_id = ticket_id.strip().upper()
    now = utc_now()
    cursor = conn.execute("""
        INSERT INTO ticket_notes (ticket_id, agent_username, note, created_at) VALUES (?, ?, ?, ?)
    """, (clean_id, agent_username, note.strip(), now))
    conn.execute("UPDATE support_tickets SET updated_at = ? WHERE ticket_id = ?", (now, clean_id))
    return cursor.lastrowid


def get_ticket_notes(conn, ticket_id):
    rows = conn.execute("""
        SELECT id, ticket_id, agent_username, note, created_at
        FROM ticket_notes WHERE ticket_id = ? ORDER BY created_at ASC, id ASC
    """, (ticket_id.strip().upper(),)).fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------
# Agents / admins
# ----------------------------------------------------------------------

AGENT_UPDATABLE = {"full_name", "role", "access_level", "is_active", "email", "password_hash",
                   "must_change_password", "failed_login_count", "locked_until", "last_login_at"}


def get_agent_by_username(conn, username):
    row = conn.execute("SELECT * FROM agent_accounts WHERE username = ?", ((username or "").strip().lower(),)).fetchone()
    return dict(row) if row else None


def list_agents(conn):
    rows = conn.execute("""
        SELECT id, username, full_name, role, access_level, is_active, must_change_password,
               email, created_at, last_login_at, locked_until
        FROM agent_accounts ORDER BY username ASC
    """).fetchall()
    return [dict(r) for r in rows]


def list_active_agent_usernames(conn):
    return [r["username"] for r in conn.execute(
        "SELECT username FROM agent_accounts WHERE is_active = 1 ORDER BY username").fetchall()]


def create_agent(conn, username, password_hash, full_name, access_level="agent", role="Support Specialist",
                 must_change_password=True, email=None):
    cursor = conn.execute("""
        INSERT INTO agent_accounts (username, password_hash, full_name, role, created_at, access_level,
                                    is_active, must_change_password, email)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
    """, (username.strip().lower(), password_hash, full_name, role, utc_now(), access_level,
          1 if must_change_password else 0, email))
    return cursor.lastrowid


def update_agent(conn, username, **fields):
    unknown = set(fields) - AGENT_UPDATABLE
    if unknown:
        raise ValueError(f"Cannot update agent fields: {sorted(unknown)}")
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE agent_accounts SET {assignments} WHERE username = ?",
                 list(fields.values()) + [username.strip().lower()])


def count_active_admins(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM agent_accounts WHERE access_level = 'admin' AND is_active = 1") \
        .fetchone()["n"]


# ----------------------------------------------------------------------
# Audit log
# ----------------------------------------------------------------------

def add_audit(conn, actor, action, target_type=None, target_id=None, details=None, ip_address=None):
    conn.execute("""
        INSERT INTO audit_log (actor, action, target_type, target_id, details, ip_address, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (actor or "system", action, target_type, str(target_id) if target_id is not None else None,
          json.dumps(details, default=str) if details is not None else None, ip_address, utc_now()))


def list_audit(conn, limit=50, offset=0):
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item["details"]) if item.get("details") else None
        result.append(item)
    return result


def count_audit(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]


# ----------------------------------------------------------------------
# Feedback
# ----------------------------------------------------------------------

def save_feedback(conn, conversation_id, message_id, rating, comment=""):
    now = utc_now()
    if conversation_id and not conn.execute("SELECT id FROM conversations WHERE id = ?",
                                            (conversation_id,)).fetchone():
        conn.execute("""
            INSERT INTO conversations (id, customer_name, pending_intent, context_data, created_at, updated_at)
            VALUES (?, 'Customer', NULL, '{}', ?, ?)
        """, (conversation_id, now, now))
    cursor = conn.execute("""
        INSERT INTO feedback (conversation_id, message_id, rating, comment, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (conversation_id, message_id, rating, comment, now))
    return cursor.lastrowid


def list_tickets_for_order(conn, order_id):
    rows = conn.execute("SELECT * FROM support_tickets WHERE order_id = ? ORDER BY created_at DESC",
                        (order_id,)).fetchall()
    return [dict(r) for r in rows]
