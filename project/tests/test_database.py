import sqlite3

import database
from werkzeug.security import generate_password_hash

LEGACY_SCHEMA = """
CREATE TABLE conversations (id TEXT PRIMARY KEY, customer_name TEXT DEFAULT 'Customer', pending_intent TEXT,
  context_data TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
  sender TEXT NOT NULL CHECK(sender IN ('user','bot')), text TEXT NOT NULL, predicted_intent TEXT,
  confidence REAL, created_at TEXT NOT NULL);
CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, customer_name TEXT NOT NULL,
  status TEXT NOT NULL, order_date TEXT NOT NULL, estimated_delivery TEXT NOT NULL, carrier TEXT NOT NULL,
  tracking_number TEXT NOT NULL, items TEXT NOT NULL, total_amount REAL NOT NULL,
  return_eligible INTEGER NOT NULL DEFAULT 1);
CREATE TABLE support_tickets (ticket_id TEXT PRIMARY KEY, conversation_id TEXT, customer_name TEXT NOT NULL,
  customer_email TEXT, issue_description TEXT NOT NULL, predicted_category TEXT NOT NULL, priority TEXT NOT NULL,
  status TEXT NOT NULL, assigned_agent TEXT, resolution_notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE ticket_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL, agent_username TEXT NOT NULL,
  note TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE agent_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, full_name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'Support Agent',
  created_at TEXT NOT NULL);
CREATE TABLE feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, message_id INTEGER,
  rating TEXT, comment TEXT, created_at TEXT NOT NULL);
"""


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_legacy_v1_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(LEGACY_SCHEMA)
    raw.execute("""INSERT INTO orders VALUES ('ORD1025','CUST-101','John Doe','In Transit','2026-10-08',
                   '2026-10-14','FedEx Express','FDX-9988231','Headphones (x1)',149.99,1)""")
    raw.execute("""INSERT INTO agent_accounts (username,password_hash,full_name,role,created_at)
                   VALUES ('admin', ?, 'Alex', 'Lead', '2026-10-01')""", (generate_password_hash("admin123"),))
    raw.execute("""INSERT INTO agent_accounts (username,password_hash,full_name,role,created_at)
                   VALUES ('agent1', ?, 'Maya', 'Support', '2026-10-01')""", (generate_password_hash("agent123"),))
    raw.execute("""INSERT INTO support_tickets VALUES ('TKT-AAAAAA',NULL,'Ann','a@x.com','Broken','damaged_product',
                   'High','Open',NULL,'', '2026-10-02','2026-10-02')""")
    raw.commit()
    raw.close()

    conn = database.init_db(str(path), seed_data=True)
    try:
        assert {"customer_email", "amount_cents", "payment_status", "currency"} <= _columns(conn, "orders")
        assert {"access_level", "is_active", "must_change_password", "failed_login_count"} <= \
            _columns(conn, "agent_accounts")
        assert "order_id" in _columns(conn, "support_tickets")
        assert database.current_schema_version(conn) == database.SCHEMA_VERSION

        order = database.get_order(conn, "ORD1025")
        assert order["amount_cents"] == 14999
        assert order["payment_status"] == "paid"
        assert order["customer_email"] == "john.doe@example.com"

        assert database.get_agent_by_username(conn, "agent1")["must_change_password"] == 1  # published agent123
        admin = database.get_agent_by_username(conn, "admin")
        assert admin["access_level"] == "admin"
        assert admin["must_change_password"] == 1  # legacy default password forces a change
        assert database.get_ticket(conn, "TKT-AAAAAA")["customer_name"] == "Ann"  # data preserved
    finally:
        conn.close()


def test_init_is_idempotent_and_does_not_duplicate_seed_rows(tmp_path):
    path = str(tmp_path / "idem.db")
    database.init_db(path).close()
    conn = database.init_db(path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        payments = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        database.init_db(path).close()
        conn2 = database.get_db_connection(path)
        assert conn2.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == count
        assert conn2.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == payments
        conn2.close()
    finally:
        conn.close()


def test_order_ids_are_case_insensitive_and_money_is_integer(tmp_path):
    conn = database.init_db(str(tmp_path / "money.db"))
    try:
        order = database.get_order(conn, " ord1026 ")
        assert order["order_id"] == "ORD1026"
        assert isinstance(order["amount_cents"], int)
    finally:
        conn.close()


def test_like_search_escapes_wildcards(tmp_path):
    conn = database.init_db(str(tmp_path / "like.db"))
    try:
        database.create_support_ticket(conn, "TKT-0000AA", None, "Ann", None, "100% sure", "billing")
        database.create_support_ticket(conn, "TKT-0000BB", None, "Bob", None, "fine", "billing")
        assert database.count_tickets(conn, search="%") == 1
        assert database.count_tickets(conn, search="_") == 0
    finally:
        conn.close()
