"""
End-to-end smoke check. Runs the main customer, agent, admin and payment journeys against a
throw-away database and prints PASS/FAIL per step. Exit code 1 if anything fails.

  python verify_all.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FLASK_ENV", "testing")

from app import create_app  # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition)))
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not condition else ""))


def main():
    workdir = tempfile.mkdtemp(prefix="cs-verify-")
    app = create_app("testing", overrides={
        "DATABASE_PATH": os.path.join(workdir, "verify.db"),
        "BOOTSTRAP_ADMIN_PASSWORD": "VerifyAdmin-2026",
        "PUBLIC_BASE_URL": "http://localhost",
    })
    client = app.test_client()

    print("\n[1] Model & health")
    health = client.get("/health").get_json()
    check("health endpoint reports ok", health["status"] == "ok", health)

    print("\n[2] Customer chat: greeting, FAQ, verified order lookup")
    check("greeting answered", client.post("/api/chat", json={"message": "hello"}).get_json()["intent"] == "greeting")
    first = client.post("/api/chat", json={"message": "Where is my order?"}).get_json()
    cid = first["conversation_id"]
    second = client.post("/api/chat", json={"conversation_id": cid, "message": "ORD1025"}).get_json()
    check("order id triggers email verification", "email" in second["response"].lower())
    third = client.post("/api/chat", json={"conversation_id": cid, "message": "john.doe@example.com"}).get_json()
    check("verified order shows status", "In Transit" in third["response"])

    print("\n[3] Human escalation and ticket tracking")
    esc = client.post("/api/chat", json={"message": "I want to speak to a human agent"}).get_json()
    check("escalation creates ticket", esc["escalated"] and esc["ticket_id"])
    ticket_id = esc["ticket_id"]
    check("ticket hidden without email", client.get(f"/api/tickets/{ticket_id}").status_code == 400)

    print("\n[4] Agent login (forced password change) and ticket handling")
    login = client.post("/api/agent/login", json={"username": "admin", "password": "VerifyAdmin-2026"}).get_json()
    check("first login requires password change", login.get("must_change_password") is True)
    token = client.post("/api/agent/change-password", json={
        "current_password": "VerifyAdmin-2026", "new_password": "VerifyAdmin-2027"},
        headers={"X-CSRF-Token": login["csrf_token"]}).get_json()["csrf_token"]
    patch = client.patch(f"/api/agent/tickets/{ticket_id}", json={"status": "In Progress"},
                         headers={"X-CSRF-Token": token})
    check("agent updates ticket with CSRF token", patch.status_code == 200)

    print("\n[5] Payments: checkout, sandbox completion, refund")
    client_order = client.post("/api/orders/ORD1027/checkout", json={"email": "michael.brown@example.com"})
    check("paid order cannot be checked out again", client_order.status_code == 409)
    cancel = client.post("/api/chat", json={"message": "Cancel order ORD1027 michael.brown@example.com"}).get_json()
    check("cancellation refunds payment", "refund" in cancel["response"].lower())
    overview = client.get("/api/admin/overview").get_json()
    check("admin overview reports payments", overview["payments"]["provider"] == "sandbox")

    print("\n[6] Security boundaries")
    anonymous = app.test_client()
    check("agent API needs auth", anonymous.get("/api/agent/tickets").status_code == 401)
    check("admin default password rejected",
          anonymous.post("/api/agent/login", json={"username": "admin", "password": "admin123"}).status_code == 401)
    check("security headers present",
          "frame-ancestors 'none'" in anonymous.get("/").headers.get("Content-Security-Policy", ""))

    failed = [name for name, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
