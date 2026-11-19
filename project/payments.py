"""
Payments: provider adapters, checkout, webhook processing, refunds and cancellations.

Flow
  1. Customer (or chatbot) asks to pay -> start_checkout() creates a pending payment and a
     hosted checkout session with the provider. The customer pays on the provider's page.
  2. The provider calls our webhook -> handle_event() marks the payment succeeded/failed.
     Webhooks are the source of truth; the browser redirect is only informational.
  3. Refunds (agent/admin/cancellation) -> refund_order() calls the provider with an
     idempotency key, records the refund and recalculates the order's payment status.

Card data never touches this application: Stripe Checkout hosts the card form (PCI SAQ-A).
The 'sandbox' provider simulates the same lifecycle locally for development and tests.
"""
import hashlib
import hmac
import json
import logging
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import database
from utils import format_cents, new_ticket_id, utc_now_iso

logger = logging.getLogger(__name__)
WEBHOOK_TOLERANCE_SECONDS = 300


class PaymentError(Exception):
    """Expected, user-presentable failure. `status` is the HTTP status to return."""

    def __init__(self, message, status=400, code="payment_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class ProviderError(PaymentError):
    """Failure talking to the provider. `definitive` means the provider rejected the request."""

    def __init__(self, message, definitive=False, status=502):
        super().__init__(message, status=status, code="provider_error")
        self.definitive = definitive


# ----------------------------------------------------------------------
# Provider adapters
# ----------------------------------------------------------------------

class SandboxProvider:
    """Local simulator with the same contract as StripeProvider. Never used in production."""
    name = "sandbox"

    def __init__(self, public_base_url):
        self.public_base_url = public_base_url

    def create_checkout_session(self, *, reference, amount_cents, currency, customer_email, description,
                                success_url, cancel_url, metadata, idempotency_key, expires_in_minutes):
        session_id = f"cs_sandbox_{uuid.uuid4().hex[:24]}"
        return {"id": session_id, "url": f"{self.public_base_url}/payments/sandbox/{session_id}",
                "payment_intent": None}

    def refund(self, *, payment_intent, amount_cents, reason, metadata, idempotency_key):
        return {"id": f"re_sandbox_{uuid.uuid4().hex[:24]}", "status": "succeeded", "amount": amount_cents}

    def parse_webhook(self, payload, signature_header):
        raise PaymentError("The sandbox provider does not accept webhooks.", 400, "unsupported")


class StripeProvider:
    """
    Minimal Stripe client using only the standard library (no SDK dependency).
    Covers Checkout Sessions, Refunds and webhook signature verification.
    """
    name = "stripe"

    def __init__(self, secret_key, webhook_secret, api_base="https://api.stripe.com", timeout=15, opener=None):
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def _request(self, method, path, form=None, idempotency_key=None):
        data = urllib.parse.urlencode(form or [], doseq=True).encode("utf-8") if form is not None else None
        request = urllib.request.Request(f"{self.api_base}{path}", data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.secret_key}")
        if data is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        if idempotency_key:
            request.add_header("Idempotency-Key", idempotency_key[:255])
        try:
            with self._opener(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            message = "Payment provider rejected the request."
            try:
                message = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", message)
            except (ValueError, AttributeError):
                pass
            definitive = 400 <= exc.code < 500 and exc.code != 429
            raise ProviderError(message, definitive=definitive) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError("Payment provider is temporarily unreachable.", definitive=False) from exc

    def create_checkout_session(self, *, reference, amount_cents, currency, customer_email, description,
                                success_url, cancel_url, metadata, idempotency_key, expires_in_minutes):
        form = [
            ("mode", "payment"),
            ("success_url", success_url),
            ("cancel_url", cancel_url),
            ("client_reference_id", reference),
            ("expires_at", str(int(time.time()) + max(31, expires_in_minutes) * 60)),
            ("line_items[0][quantity]", "1"),
            ("line_items[0][price_data][currency]", currency),
            ("line_items[0][price_data][unit_amount]", str(amount_cents)),
            ("line_items[0][price_data][product_data][name]", description[:250]),
        ]
        if customer_email:
            form.append(("customer_email", customer_email))
        for key, value in metadata.items():
            form.append((f"metadata[{key}]", str(value)[:500]))
        result = self._request("POST", "/v1/checkout/sessions", form, idempotency_key)
        return {"id": result["id"], "url": result["url"], "payment_intent": result.get("payment_intent")}

    def refund(self, *, payment_intent, amount_cents, reason, metadata, idempotency_key):
        form = [("payment_intent", payment_intent), ("amount", str(amount_cents)),
                ("reason", "requested_by_customer")]
        for key, value in metadata.items():
            form.append((f"metadata[{key}]", str(value)[:500]))
        result = self._request("POST", "/v1/refunds", form, idempotency_key)
        return {"id": result["id"], "status": result.get("status", "pending"), "amount": result.get("amount")}

    def parse_webhook(self, payload, signature_header, now=None):
        """Verify the Stripe-Signature header (HMAC-SHA256 over 't.payload') and return the event."""
        if not self.webhook_secret:
            raise PaymentError("Webhook secret is not configured.", 500, "config")
        timestamp, signatures = None, []
        for part in (signature_header or "").split(","):
            key, _, value = part.strip().partition("=")
            if key == "t":
                timestamp = value
            elif key == "v1":
                signatures.append(value)
        if not timestamp or not signatures:
            raise PaymentError("Missing webhook signature.", 400, "invalid_signature")
        try:
            ts = int(timestamp)
        except ValueError as exc:
            raise PaymentError("Malformed webhook timestamp.", 400, "invalid_signature") from exc
        current = int(now if now is not None else time.time())
        if abs(current - ts) > WEBHOOK_TOLERANCE_SECONDS:
            raise PaymentError("Webhook timestamp outside tolerance.", 400, "invalid_signature")
        signed = f"{timestamp}.".encode("utf-8") + payload
        expected = hmac.new(self.webhook_secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, sig) for sig in signatures):
            raise PaymentError("Webhook signature verification failed.", 400, "invalid_signature")
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaymentError("Webhook payload is not valid JSON.", 400, "invalid_payload") from exc


def build_provider(config):
    name = config["PAYMENT_PROVIDER"]
    if name == "sandbox":
        return SandboxProvider(config["PUBLIC_BASE_URL"])
    if name == "stripe":
        return StripeProvider(config["STRIPE_SECRET_KEY"], config["STRIPE_WEBHOOK_SECRET"],
                              api_base=config["STRIPE_API_BASE"])
    raise RuntimeError(f"Unsupported PAYMENT_PROVIDER: {name}")


# ----------------------------------------------------------------------
# Checkout
# ----------------------------------------------------------------------

def _recalculate_order_payment_status(conn, order_id, payment):
    refunded = database.refunded_total_cents(conn, payment["id"])
    if refunded >= payment["amount_cents"]:
        status = "refunded"
    elif refunded > 0:
        status = "partially_refunded"
    else:
        status = "paid"
    database.set_order_payment_status(conn, order_id, status)
    return status


def start_checkout(conn, provider, config, order, customer_email, actor="customer", ip=None):
    """Create (or reuse) a hosted checkout session for an unpaid order. Returns checkout details."""
    order_id = order["order_id"]
    if order["status"] in ("Cancelled", "Returned"):
        raise PaymentError("This order is no longer payable.", 409, "order_not_payable")
    if order["payment_status"] == "paid":
        raise PaymentError("This order has already been paid.", 409, "already_paid")
    amount_cents = int(order.get("amount_cents") or 0)
    if amount_cents <= 0:
        raise PaymentError("This order has no payable amount.", 409, "order_not_payable")
    currency = (order.get("currency") or config["CURRENCY"]).lower()
    ttl = int(config["CHECKOUT_TTL_MINUTES"])

    created_after = (datetime.now(timezone.utc) - timedelta(minutes=ttl)).isoformat()
    existing = database.get_latest_pending_payment(conn, order_id, created_after)
    if existing and existing.get("checkout_url"):
        return _checkout_result(existing, ttl, reused=True)

    reference = uuid.uuid4().hex
    base = config["PUBLIC_BASE_URL"]
    session = provider.create_checkout_session(
        reference=reference,
        amount_cents=amount_cents,
        currency=currency,
        customer_email=customer_email,
        description=f"Order {order_id}",
        success_url=f"{base}/payments/return?order={order_id}&status=success",
        cancel_url=f"{base}/payments/return?order={order_id}&status=cancelled",
        metadata={"order_id": order_id, "reference": reference},
        idempotency_key=f"checkout-{reference}",
        expires_in_minutes=ttl,
    )
    payment_id = database.create_payment(
        conn,
        order_id=order_id,
        provider=provider.name,
        provider_session_id=session["id"],
        checkout_url=session["url"],
        amount_cents=amount_cents,
        currency=currency,
        status="pending",
        customer_email=customer_email,
        idempotency_key=f"checkout-{reference}",
    )
    if order["payment_status"] == "unpaid":
        database.set_order_payment_status(conn, order_id, "pending")
    database.add_audit(conn, actor, "checkout.created", "order", order_id,
                       {"payment_id": payment_id, "amount_cents": amount_cents}, ip)
    payment = database.get_payment(conn, payment_id)
    return _checkout_result(payment, ttl, reused=False)


def _checkout_result(payment, ttl, reused):
    return {
        "payment_id": payment["id"],
        "order_id": payment["order_id"],
        "checkout_url": payment["checkout_url"],
        "amount_cents": payment["amount_cents"],
        "currency": payment["currency"],
        "expires_in_minutes": ttl,
        "reused": reused,
    }


# ----------------------------------------------------------------------
# Provider events (webhooks and sandbox completion share this path)
# ----------------------------------------------------------------------

def _mark_paid(conn, payment, session_obj, actor):
    if payment["status"] == "succeeded":
        return "duplicate"
    if int(session_obj.get("amount_total") or -1) != payment["amount_cents"] \
            or (session_obj.get("currency") or "").lower() != payment["currency"]:
        database.update_payment(conn, payment["id"], status="failed", failure_reason="amount_mismatch")
        database.add_audit(conn, "payments", "payment.amount_mismatch", "payment", payment["id"],
                           {"expected": payment["amount_cents"], "received": session_obj.get("amount_total")})
        logger.error("Payment %s amount mismatch; refusing to mark as paid.", payment["id"])
        return "amount_mismatch"
    database.update_payment(conn, payment["id"], status="succeeded",
                            provider_payment_id=session_obj.get("payment_intent"), paid_at=utc_now_iso())
    database.set_order_payment_status(conn, payment["order_id"], "paid")
    database.add_audit(conn, actor, "payment.succeeded", "order", payment["order_id"],
                       {"payment_id": payment["id"], "amount_cents": payment["amount_cents"]})
    return "succeeded"


def handle_event(conn, event):
    """Apply a provider event to local state. Returns a short outcome string."""
    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if event_type.startswith("checkout.session."):
        payment = database.get_payment_by_session(conn, obj.get("id", ""))
        if not payment:
            return "unknown_session"
        if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            if obj.get("payment_status") in ("paid", "no_payment_required", None) or \
                    event_type == "checkout.session.async_payment_succeeded":
                return _mark_paid(conn, payment, obj, "payments")
            database.update_payment(conn, payment["id"], status="pending")
            return "awaiting_async_payment"
        if event_type in ("checkout.session.async_payment_failed", "checkout.session.expired"):
            if payment["status"] == "succeeded":
                return "ignored"
            new_status = "expired" if event_type.endswith("expired") else "failed"
            database.update_payment(conn, payment["id"], status=new_status,
                                    failure_reason=event_type.rsplit(".", 1)[-1])
            order = database.get_order(conn, payment["order_id"])
            if order and order["payment_status"] == "pending":
                database.set_order_payment_status(conn, payment["order_id"], "unpaid")
            database.add_audit(conn, "payments", f"payment.{new_status}", "order", payment["order_id"],
                               {"payment_id": payment["id"]})
            return new_status
        return "ignored"

    if event_type.startswith("refund."):
        return _handle_refund_object(conn, obj)

    return "ignored"


def _handle_refund_object(conn, obj):
    refund = database.get_refund_by_provider_id(conn, obj.get("id", ""))
    if not refund:
        return "unknown_refund"
    provider_status = obj.get("status", "pending")
    if provider_status == "succeeded":
        new_status = "succeeded"
    elif provider_status in ("failed", "canceled"):
        new_status = "failed"
    else:
        new_status = "pending"
    database.update_refund(conn, refund["id"], status=new_status)
    payment = database.get_payment(conn, refund["payment_id"])
    if payment:
        _recalculate_order_payment_status(conn, payment["order_id"], payment)
    database.add_audit(conn, "payments", f"refund.{new_status}", "order", refund["order_id"],
                       {"refund_id": refund["id"]})
    return f"refund_{new_status}"


def sandbox_complete(conn, session_id, outcome):
    """Simulate the provider finishing a sandbox checkout. Uses the same code path as webhooks."""
    payment = database.get_payment_by_session(conn, session_id)
    if not payment:
        raise PaymentError("Checkout session not found.", 404, "not_found")
    if payment["provider"] != "sandbox":
        raise PaymentError("This is not a sandbox checkout session.", 400, "not_sandbox")
    event_id = f"evt_sandbox_{uuid.uuid4().hex}"
    if outcome == "success":
        event = {"id": event_id, "type": "checkout.session.completed",
                 "data": {"object": {"id": session_id, "payment_status": "paid",
                                     "payment_intent": f"pi_sandbox_{payment['id']}",
                                     "amount_total": payment["amount_cents"],
                                     "currency": payment["currency"]}}}
    else:
        event = {"id": event_id, "type": "checkout.session.expired",
                 "data": {"object": {"id": session_id}}}
    return handle_event(conn, event)


# ----------------------------------------------------------------------
# Refunds & cancellations
# ----------------------------------------------------------------------

def refund_order(conn, provider, config, order_id, *, amount_cents=None, reason="", actor,
                 is_admin=False, idempotency_key=None, ip=None):
    """
    Refund all or part of an order's successful payment. Returns the refund record.
    Replaying the same idempotency key returns the original refund instead of refunding twice.
    """
    if idempotency_key:
        replay = database.get_refund_by_idempotency_key(conn, idempotency_key)
        if replay:
            return replay
    order = database.get_order(conn, order_id)
    if not order:
        raise PaymentError("Order not found.", 404, "not_found")
    payment = database.get_succeeded_payment(conn, order["order_id"])
    if not payment:
        raise PaymentError("There is no successful payment on this order to refund.", 409, "not_paid")
    already = database.refunded_total_cents(conn, payment["id"])
    refundable = payment["amount_cents"] - already
    if refundable <= 0:
        raise PaymentError("This order has already been fully refunded.", 409, "already_refunded")
    amount = refundable if amount_cents is None else int(amount_cents)
    if amount <= 0 or amount > refundable:
        raise PaymentError(f"Refund amount must be between 1 and {refundable} cents.", 400, "invalid_amount")
    if not is_admin and amount > int(config["AGENT_REFUND_LIMIT_CENTS"]):
        raise PaymentError("This amount exceeds your refund limit. Ask an administrator to issue it.",
                           403, "refund_limit")
    if not payment.get("provider_payment_id"):
        raise PaymentError("The payment has no provider reference yet; try again shortly.", 409, "not_ready")

    key = idempotency_key or f"refund-{uuid.uuid4().hex}"
    refund_id = database.create_refund(
        conn, payment_id=payment["id"], order_id=order["order_id"], amount_cents=amount,
        currency=payment["currency"], reason=(reason or "")[:300], status="pending",
        initiated_by=actor, idempotency_key=key,
    )
    try:
        result = provider.refund(payment_intent=payment["provider_payment_id"], amount_cents=amount,
                                 reason=reason, metadata={"refund_id": refund_id, "order_id": order["order_id"]},
                                 idempotency_key=key)
    except ProviderError as exc:
        if exc.definitive:
            database.update_refund(conn, refund_id, status="failed", failure_reason=exc.message[:300])
            _recalculate_order_payment_status(conn, order["order_id"], payment)
        database.add_audit(conn, actor, "refund.error", "order", order["order_id"],
                           {"refund_id": refund_id, "message": exc.message}, ip)
        raise

    provider_status = result.get("status", "pending")
    status = "succeeded" if provider_status == "succeeded" else (
        "failed" if provider_status in ("failed", "canceled") else "pending")
    database.update_refund(conn, refund_id, provider_refund_id=result.get("id"), status=status)
    _recalculate_order_payment_status(conn, order["order_id"], payment)
    database.add_audit(conn, actor, "refund.created", "order", order["order_id"],
                       {"refund_id": refund_id, "amount_cents": amount, "status": status, "reason": reason}, ip)
    return database.get_refund(conn, refund_id)


def cancel_order(conn, provider, config, order_id, *, actor, ip=None):
    """
    Cancel an order that has not yet shipped and refund it if it was paid.
    If the automatic refund fails, the cancellation is kept and a High-priority ticket is opened
    for the finance team, so money is never silently left behind.
    """
    order = database.get_order(conn, order_id)
    if not order:
        raise PaymentError("Order not found.", 404, "not_found")
    if order["status"] != "Processing":
        raise PaymentError(f"Order is '{order['status']}' and cannot be cancelled automatically.", 409,
                           "not_cancellable")
    database.set_order_status(conn, order["order_id"], "Cancelled")
    database.add_audit(conn, actor, "order.cancelled", "order", order["order_id"], None, ip)

    payment = database.get_succeeded_payment(conn, order["order_id"])
    if not payment or database.refunded_total_cents(conn, payment["id"]) >= payment["amount_cents"]:
        return {"outcome": "cancelled_no_charge" if not payment else "cancelled_already_refunded",
                "refund": None, "ticket_id": None}
    try:
        refund = refund_order(conn, provider, config, order["order_id"],
                              reason="Order cancelled before dispatch", actor=actor, is_admin=True, ip=ip)
    except PaymentError as exc:
        ticket_id = new_ticket_id()
        database.create_support_ticket(
            conn, ticket_id=ticket_id, conversation_id=None,
            customer_name=order["customer_name"], customer_email=order["customer_email"],
            issue_description=(f"Automatic refund failed for cancelled order {order['order_id']} "
                               f"({format_cents(order['amount_cents'], order['currency'])}). "
                               f"Reason: {exc.message}"),
            predicted_category="refund_request", priority="High", status="Open", order_id=order["order_id"],
        )
        database.add_ticket_note(conn, ticket_id, "System", "Opened automatically: refund failed after cancellation.")
        return {"outcome": "cancelled_refund_failed", "refund": None, "ticket_id": ticket_id}
    outcome = "cancelled_refund_succeeded" if refund["status"] == "succeeded" else "cancelled_refund_pending"
    return {"outcome": outcome, "refund": refund, "ticket_id": None}
