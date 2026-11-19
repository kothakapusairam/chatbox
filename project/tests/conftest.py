import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FLASK_ENV", "testing")

import database  # noqa: E402
import security  # noqa: E402
from app import create_app  # noqa: E402

ADMIN_PASSWORD = "TestAdmin-2026"
NEW_ADMIN_PASSWORD = "AdminPass-2026"
ALL_BUCKETS = ["chat", "ticket_create", "ticket_lookup", "feedback", "login", "checkout"]


def _make_app(tmp_path, **overrides):
    security.rate_limiter.reset()
    settings = {
        "DATABASE_PATH": str(tmp_path / "test.db"),
        "BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "PUBLIC_BASE_URL": "http://localhost",
        "RATE_LIMITS": {k: (100000, 60) for k in ALL_BUCKETS},
    }
    settings.update(overrides)
    return create_app("testing", overrides=settings)


@pytest.fixture
def app(tmp_path):
    application = _make_app(tmp_path)
    yield application
    security.rate_limiter.reset()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    """A connection to the test database, committed and closed by the fixture."""
    conn = database.get_db_connection(app.config["DATABASE_PATH"])
    yield conn
    conn.commit()
    conn.close()


def login_admin(client, password=ADMIN_PASSWORD):
    """Sign in as the bootstrap admin, rotating the temporary password. Returns the CSRF token."""
    res = client.post("/api/agent/login", json={"username": "admin", "password": password})
    assert res.status_code == 200, res.get_json()
    token = res.get_json()["csrf_token"]
    if res.get_json()["must_change_password"]:
        res = client.post("/api/agent/change-password",
                          json={"current_password": password, "new_password": NEW_ADMIN_PASSWORD},
                          headers={"X-CSRF-Token": token})
        assert res.status_code == 200, res.get_json()
        token = res.get_json()["csrf_token"]
    return token


def create_user(client, token, username, access="agent", password="Agent-Pass-2026"):
    res = client.post("/api/admin/users", json={
        "username": username, "full_name": f"User {username}", "password": password,
        "access_level": access,
    }, headers={"X-CSRF-Token": token})
    assert res.status_code == 201, res.get_json()
    return password


def login_as(client, username, password):
    res = client.post("/api/agent/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.get_json()
    token = res.get_json()["csrf_token"]
    if res.get_json()["must_change_password"]:
        new = "Changed-Pass-2026"
        res = client.post("/api/agent/change-password",
                          json={"current_password": password, "new_password": new},
                          headers={"X-CSRF-Token": token})
        assert res.status_code == 200
        token = res.get_json()["csrf_token"]
    return token


def add_order(db, order_id, *, status="Processing", payment_status="unpaid", amount_cents=5000,
              email="buyer@example.com", return_eligible=1, delivered="2026-11-02"):
    db.execute("""
        INSERT INTO orders (order_id, customer_id, customer_name, status, order_date, estimated_delivery,
            carrier, tracking_number, items, total_amount, return_eligible, customer_email, amount_cents,
            currency, payment_status, updated_at)
        VALUES (?, 'CUST-T', 'Test Buyer', ?, '2026-10-30', ?, 'TestEx', 'TRK-1', 'Test item (x1)', ?, ?, ?, ?,
                'usd', ?, '2026-10-30T00:00:00+00:00')
    """, (order_id, status, delivered, amount_cents / 100, return_eligible, email, amount_cents, payment_status))
    db.commit()
