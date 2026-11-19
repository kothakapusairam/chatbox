import hashlib
import hmac
import io
import json
import time
import urllib.error

import pytest

import database
import payments
from conftest import add_order, login_admin, create_user, login_as
from payments import PaymentError, ProviderError, StripeProvider

WEBHOOK_SECRET = "whsec_test_secret_value"


def stripe_signature(payload: bytes, secret=WEBHOOK_SECRET, timestamp=None):
    ts = str(int(timestamp if timestamp is not None else time.time()))
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_opener(responses, calls):
    """Returns an opener that records each request and replies from a queue of (status, body) tuples."""
    def opener(request, timeout=None):
        calls.append(request)
        status, body = responses.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "err", {},
                                         FakeResponse(json.dumps(body).encode()))
        return FakeResponse(json.dumps(body).encode())
    return opener


# ---------------- signature verification ----------------

def test_valid_stripe_signature_is_accepted():
    provider = StripeProvider("sk_test", WEBHOOK_SECRET)
    payload = json.dumps({"id": "evt_1", "type": "x", "data": {"object": {}}}).encode()
    event = provider.parse_webhook(payload, stripe_signature(payload))
    assert event["id"] == "evt_1"


@pytest.mark.parametrize("problem", ["tampered", "wrong_secret", "stale", "missing", "garbage"])
def test_invalid_stripe_signatures_are_rejected(problem):
    provider = StripeProvider("sk_test", WEBHOOK_SECRET)
    payload = b'{"id":"evt_2","type":"x"}'
    if problem == "tampered":
        header = stripe_signature(b'{"id":"evt_evil"}')
    elif problem == "wrong_secret":
        header = stripe_signature(payload, secret="other")
    elif problem == "stale":
        header = stripe_signature(payload, timestamp=time.time() - 3600)
    elif problem == "missing":
        header = ""
    else:
        header = "t=notanumber,v1=abc"
    with pytest.raises(PaymentError) as info:
        provider.parse_webhook(payload, header)
    assert info.value.status == 400


def test_webhook_endpoint_rejects_bad_signature(tmp_path):
    from conftest import _make_app
    app = _make_app(tmp_path, PAYMENT_PROVIDER="stripe", STRIPE_SECRET_KEY="sk_test_x",
                    STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET)
    client = app.test_client()
    res = client.post("/api/payments/webhook", data=b'{"id":"evt_9"}',
                      headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"})
    assert res.status_code == 400


# ---------------- provider request construction ----------------

def test_stripe_checkout_request_is_well_formed_and_idempotent():
    calls = []
    provider = StripeProvider("sk_test_abc", WEBHOOK_SECRET,
                              opener=fake_opener([(200, {"id": "cs_test_1", "url": "https://checkout.stripe.com/x",
                                                         "payment_intent": None})], calls))
    result = provider.create_checkout_session(
        reference="ref1", amount_cents=4999, currency="usd", customer_email="a@b.co",
        description="Order ORD1", success_url="https://s", cancel_url="https://c",
        metadata={"order_id": "ORD1"}, idempotency_key="checkout-ref1", expires_in_minutes=60)
    assert result["id"] == "cs_test_1"
    req = calls[0]
    assert req.get_method() == "POST"
    assert req.full_url == "https://api.stripe.com/v1/checkout/sessions"
    assert req.get_header("Authorization") == "Bearer sk_test_abc"
    assert req.get_header("Idempotency-key") == "checkout-ref1"
    body = req.data.decode()
    assert "line_items%5B0%5D%5Bprice_data%5D%5Bunit_amount%5D=4999" in body
    assert "metadata%5Border_id%5D=ORD1" in body


def test_stripe_refund_request_carries_idempotency_key():
    calls = []
    provider = StripeProvider("sk_test_abc", WEBHOOK_SECRET,
                              opener=fake_opener([(200, {"id": "re_1", "status": "pending", "amount": 500})], calls))
    result = provider.refund(payment_intent="pi_1", amount_cents=500, reason="x", metadata={},
                             idempotency_key="refund-key-1")
    assert result == {"id": "re_1", "status": "pending", "amount": 500}
    assert calls[0].get_header("Idempotency-key") == "refund-key-1"


def test_provider_errors_are_classified_definitive_or_transient():
    calls = []
    rejected = StripeProvider("k", "s", opener=fake_opener([(402, {"error": {"message": "card declined"}})], calls))
    with pytest.raises(ProviderError) as definitive:
        rejected.refund(payment_intent="pi", amount_cents=1, reason="", metadata={}, idempotency_key="k")
    assert definitive.value.definitive is True
    assert "card declined" in definitive.value.message

    outage = StripeProvider("k", "s", opener=fake_opener([(503, {"error": {"message": "unavailable"}})], []))
    with pytest.raises(ProviderError) as transient:
        outage.refund(payment_intent="pi", amount_cents=1, reason="", metadata={}, idempotency_key="k")
    assert transient.value.definitive is False


# ---------------- checkout & sandbox lifecycle ----------------

def _start_sandbox_checkout(client, email="buyer@example.com"):
    res = client.post("/api/orders/ORD3001/checkout", json={"email": email})
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def test_checkout_requires_matching_order_email(client, db):
    add_order(db, "ORD3001", amount_cents=7500, email="buyer@example.com")
    assert client.post("/api/orders/ORD3001/checkout", json={"email": "x@y.co"}).status_code == 404
    assert client.post("/api/orders/ORD3001/checkout", json={}).status_code == 404
    assert client.post("/api/orders/ORD9999/checkout", json={"email": "buyer@example.com"}).status_code == 404


def test_sandbox_success_marks_order_paid_once(client, db):
    add_order(db, "ORD3001", amount_cents=7500, email="buyer@example.com")
    checkout = _start_sandbox_checkout(client)
    assert checkout["amount_cents"] == 7500
    session_id = checkout["checkout_url"].rsplit("/", 1)[-1]
    page = client.get(f"/payments/sandbox/{session_id}")
    assert page.status_code == 200 and b"Simulate successful payment" in page.data

    done = client.post(f"/payments/sandbox/{session_id}/complete", data={"outcome": "success"})
    assert done.status_code == 302 and "status=success" in done.headers["Location"]
    order = database.get_order(db, "ORD3001")
    assert order["payment_status"] == "paid"
    payment = database.get_payment_by_session(db, session_id)
    assert payment["status"] == "succeeded"

    again = client.post(f"/payments/sandbox/{session_id}/complete", data={"outcome": "success"})
    assert again.status_code == 302  # idempotent: no second effect
    assert database.count_payments(db) >= 1


def test_already_paid_order_cannot_start_another_checkout(client, db):
    add_order(db, "ORD3001", amount_cents=7500, email="buyer@example.com")
    checkout = _start_sandbox_checkout(client)
    session_id = checkout["checkout_url"].rsplit("/", 1)[-1]
    client.post(f"/payments/sandbox/{session_id}/complete", data={"outcome": "success"})
    res = client.post("/api/orders/ORD3001/checkout", json={"email": "buyer@example.com"})
    assert res.status_code == 409 and res.get_json()["error"] == "already_paid"


def test_pending_checkout_is_reused_for_double_clicks(client, db):
    add_order(db, "ORD3002", amount_cents=1200, email="buyer@example.com")
    first = client.post("/api/orders/ORD3002/checkout", json={"email": "buyer@example.com"}).get_json()
    second = client.post("/api/orders/ORD3002/checkout", json={"email": "buyer@example.com"}).get_json()
    assert first["checkout_url"] == second["checkout_url"]


def test_cancelled_sandbox_checkout_resets_order_to_unpaid(client, db):
    add_order(db, "ORD3003", amount_cents=1500, email="buyer@example.com")
    res = client.post("/api/orders/ORD3003/checkout", json={"email": "buyer@example.com"}).get_json()
    session_id = res["checkout_url"].rsplit("/", 1)[-1]
    client.post(f"/payments/sandbox/{session_id}/complete", data={"outcome": "cancelled"})
    assert database.get_order(db, "ORD3003")["payment_status"] == "unpaid"
    assert database.get_payment_by_session(db, session_id)["status"] == "expired"


def test_webhook_amount_mismatch_never_marks_order_paid(tmp_path):
    from conftest import _make_app
    app = _make_app(tmp_path, PAYMENT_PROVIDER="stripe", STRIPE_SECRET_KEY="sk_test_x",
                    STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET)
    client = app.test_client()
    conn = database.get_db_connection(app.config["DATABASE_PATH"])
    add_order(conn, "ORD3004", amount_cents=9000, email="buyer@example.com")
    pid = database.create_payment(conn, order_id="ORD3004", provider="stripe", provider_session_id="cs_m1",
                                  amount_cents=9000, currency="usd", status="pending",
                                  idempotency_key="k-m1")
    conn.commit()
    event = {"id": "evt_mismatch", "type": "checkout.session.completed",
             "data": {"object": {"id": "cs_m1", "payment_status": "paid", "payment_intent": "pi_m1",
                                 "amount_total": 100, "currency": "usd"}}}
    body = json.dumps(event).encode()
    res = client.post("/api/payments/webhook", data=body,
                      headers={"Stripe-Signature": stripe_signature(body), "Content-Type": "application/json"})
    assert res.status_code == 200 and res.get_json()["status"] == "amount_mismatch"
    conn2 = database.get_db_connection(app.config["DATABASE_PATH"])
    assert database.get_order(conn2, "ORD3004")["payment_status"] != "paid"
    assert database.get_payment(conn2, pid)["status"] == "failed"
    conn2.close()


def test_webhook_is_idempotent_and_processes_success_once(tmp_path):
    from conftest import _make_app
    app = _make_app(tmp_path, PAYMENT_PROVIDER="stripe", STRIPE_SECRET_KEY="sk_test_x",
                    STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET)
    client = app.test_client()
    conn = database.get_db_connection(app.config["DATABASE_PATH"])
    add_order(conn, "ORD3005", amount_cents=2000, email="buyer@example.com")
    database.create_payment(conn, order_id="ORD3005", provider="stripe", provider_session_id="cs_w1",
                            amount_cents=2000, currency="usd", status="pending", idempotency_key="k-w1")
    conn.commit()
    event = {"id": "evt_once", "type": "checkout.session.completed",
             "data": {"object": {"id": "cs_w1", "payment_status": "paid", "payment_intent": "pi_w1",
                                 "amount_total": 2000, "currency": "usd"}}}
    body = json.dumps(event).encode()
    headers = {"Stripe-Signature": stripe_signature(body), "Content-Type": "application/json"}
    first = client.post("/api/payments/webhook", data=body, headers=headers).get_json()
    second = client.post("/api/payments/webhook", data=body, headers=headers).get_json()
    assert first["status"] == "succeeded"
    assert second["status"] == "duplicate"
    assert database.get_order(conn, "ORD3005")["payment_status"] == "paid"
    conn.close()


def test_partial_then_full_refund_and_limits(client, db, app):
    add_order(db, "ORD3006", amount_cents=10000, email="buyer@example.com")
    checkout = client.post("/api/orders/ORD3006/checkout", json={"email": "buyer@example.com"}).get_json()
    session_id = checkout["checkout_url"].rsplit("/", 1)[-1]
    client.post(f"/payments/sandbox/{session_id}/complete", data={"outcome": "success"})

    token = login_admin(client)
    res = client.post("/api/agent/orders/ORD3006/refunds", json={"amount_cents": 2500, "reason": "Damaged"},
                      headers={"X-CSRF-Token": token})
    assert res.status_code == 201
    assert database.get_order(db, "ORD3006")["payment_status"] == "partially_refunded"

    too_much = client.post("/api/agent/orders/ORD3006/refunds", json={"amount_cents": 99999, "reason": "x"},
                           headers={"X-CSRF-Token": token})
    assert too_much.status_code == 400

    rest = client.post("/api/agent/orders/ORD3006/refunds", json={"reason": "Goodwill"},
                       headers={"X-CSRF-Token": token})
    assert rest.status_code == 201
    assert database.get_order(db, "ORD3006")["payment_status"] == "refunded"

    none_left = client.post("/api/agent/orders/ORD3006/refunds", json={"reason": "again"},
                            headers={"X-CSRF-Token": token})
    assert none_left.status_code == 409


def test_refund_requires_reason_and_replays_idempotently(client, db):
    add_order(db, "ORD3007", amount_cents=3000, email="buyer@example.com")
    checkout = client.post("/api/orders/ORD3007/checkout", json={"email": "buyer@example.com"}).get_json()
    client.post(f"/payments/sandbox/{checkout['checkout_url'].rsplit('/', 1)[-1]}/complete",
                data={"outcome": "success"})
    token = login_admin(client)
    assert client.post("/api/agent/orders/ORD3007/refunds", json={},
                       headers={"X-CSRF-Token": token}).status_code == 400
    headers = {"X-CSRF-Token": token, "Idempotency-Key": "click-123"}
    first = client.post("/api/agent/orders/ORD3007/refunds", json={"reason": "Dup test"}, headers=headers)
    second = client.post("/api/agent/orders/ORD3007/refunds", json={"reason": "Dup test"}, headers=headers)
    assert first.status_code == 201 and second.status_code == 201
    assert first.get_json()["refund"]["id"] == second.get_json()["refund"]["id"]
    assert database.refunded_total_cents(db, database.get_succeeded_payment(db, "ORD3007")["id"]) == 3000


def test_agent_refund_above_limit_needs_admin(client, db, app):
    add_order(db, "ORD3008", amount_cents=50000, email="buyer@example.com")
    checkout = client.post("/api/orders/ORD3008/checkout", json={"email": "buyer@example.com"}).get_json()
    client.post(f"/payments/sandbox/{checkout['checkout_url'].rsplit('/', 1)[-1]}/complete",
                data={"outcome": "success"})
    admin_token = login_admin(client)
    password = create_user(client, admin_token, "frontdesk")
    client.post("/api/agent/logout", headers={"X-CSRF-Token": admin_token})
    agent_token = login_as(client, "frontdesk", password)
    res = client.post("/api/agent/orders/ORD3008/refunds", json={"reason": "big one"},
                      headers={"X-CSRF-Token": agent_token})
    assert res.status_code == 403 and res.get_json()["error"] == "refund_limit"


def test_cancelling_paid_order_with_failed_refund_opens_high_priority_ticket(client, db, app):
    class BrokenProvider:
        name = "sandbox"

        def refund(self, **kwargs):
            raise ProviderError("Bank rejected the refund.", definitive=True)

    add_order(db, "ORD3009", status="Processing", payment_status="paid", amount_cents=4000,
              email="buyer@example.com")
    pay_row = database.create_payment(db, order_id="ORD3009", provider="sandbox", provider_session_id="cs_fail",
                                      amount_cents=4000, currency="usd", status="succeeded",
                                      idempotency_key="k-fail", provider_payment_id="pi_fail")
    db.commit()
    result = payments.cancel_order(db, BrokenProvider(), app.config, "ORD3009", actor="test")
    assert result["outcome"] == "cancelled_refund_failed"
    ticket = database.get_ticket(db, result["ticket_id"])
    assert ticket["priority"] == "High" and ticket["order_id"] == "ORD3009"
    assert database.get_order(db, "ORD3009")["status"] == "Cancelled"
    assert pay_row


def test_admin_overview_reports_payment_totals(client, db):
    token = login_admin(client)
    res = client.get("/api/admin/overview")
    body = res.get_json()
    assert body["payments"]["provider"] == "sandbox"
    assert body["payments"]["collected_cents"] >= 0
    assert "model_loaded" in body["system"]
    assert token


def test_production_refuses_insecure_configuration(tmp_path):
    from app import create_app
    with pytest.raises(RuntimeError) as info:
        create_app("production", overrides={
            "SECRET_KEY": None, "PUBLIC_BASE_URL": "http://insecure.example", "PAYMENT_PROVIDER": "sandbox",
            "BOOTSTRAP_ADMIN_PASSWORD": "Strong-Pass-1", "DATABASE_PATH": str(tmp_path / "p.db"),
        })
    message = str(info.value)
    assert "SECRET_KEY" in message and "https" in message and "sandbox" in message


def test_sandbox_checkout_page_is_not_available_with_stripe(tmp_path):
    from conftest import _make_app
    app = _make_app(tmp_path, PAYMENT_PROVIDER="stripe", STRIPE_SECRET_KEY="sk_test_x",
                    STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET)
    assert app.test_client().get("/payments/sandbox/cs_anything").status_code == 404
