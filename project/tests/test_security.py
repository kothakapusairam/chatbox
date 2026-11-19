import re
from pathlib import Path

import pytest

import security
from conftest import ADMIN_PASSWORD, NEW_ADMIN_PASSWORD, create_user, login_admin, login_as, _make_app
from config import BASE_DIR

TEMPLATES = Path(BASE_DIR) / "templates"
STATIC_JS = Path(BASE_DIR) / "static" / "js"


# ---------------- authentication & sessions ----------------

def test_agent_api_requires_authentication(client):
    assert client.get("/api/agent/tickets").status_code == 401
    assert client.get("/api/admin/users").status_code == 401
    assert client.patch("/api/agent/tickets/TKT-000000", json={"status": "Closed"}).status_code == 401


def test_agent_pages_redirect_anonymous_users_to_login(client):
    for path in ("/agent/dashboard", "/agent/tickets/TKT-000000", "/admin"):
        res = client.get(path)
        assert res.status_code == 302, path
        assert "/agent/login" in res.headers["Location"]


def test_factory_default_credentials_are_rejected(client):
    assert client.post("/api/agent/login", json={"username": "admin", "password": "admin123"}).status_code == 401
    assert client.post("/api/agent/login", json={"username": "agent1", "password": "agent123"}).status_code == 401


def test_wrong_password_is_rejected_generically(client):
    res = client.post("/api/agent/login", json={"username": "nobody", "password": "whatever-123"})
    assert res.status_code == 401
    assert res.get_json()["message"] == "Invalid username or password."


def test_account_locks_after_repeated_failures(app, client):
    max_attempts = app.config["LOGIN_MAX_ATTEMPTS"]
    for _ in range(max_attempts):
        assert client.post("/api/agent/login", json={"username": "admin", "password": "wrong-pass-1"}).status_code == 401
    locked = client.post("/api/agent/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert locked.status_code == 423


def test_login_rate_limit_returns_429_with_retry_after(tmp_path):
    application = _make_app(tmp_path, RATE_LIMITS={"login": (3, 60), "chat": (100000, 60),
                                                    "ticket_create": (100000, 60), "ticket_lookup": (100000, 60),
                                                    "feedback": (100000, 60), "checkout": (100000, 60)})
    client = application.test_client()
    statuses = [client.post("/api/agent/login", json={"username": "x", "password": "y"}).status_code
                for _ in range(4)]
    assert statuses[:3] == [401, 401, 401] and statuses[3] == 429
    res = client.post("/api/agent/login", json={"username": "x", "password": "y"})
    assert res.headers.get("Retry-After")


def test_forced_password_change_blocks_api_until_changed(client):
    res = client.post("/api/agent/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert res.get_json()["must_change_password"] is True
    assert client.get("/api/agent/tickets").status_code == 403
    assert client.get("/api/agent/tickets").get_json()["error"] == "password_change_required"
    assert client.get("/agent/dashboard").status_code == 302  # redirected to change-password


def test_password_policy_is_enforced_on_change(client):
    token = login_admin(client)  # now on the new password
    weak = client.post("/api/agent/change-password",
                       json={"current_password": NEW_ADMIN_PASSWORD, "new_password": "short1"},
                       headers={"X-CSRF-Token": token})
    assert weak.status_code == 400
    no_digit = client.post("/api/agent/change-password",
                           json={"current_password": NEW_ADMIN_PASSWORD, "new_password": "onlyletters"},
                           headers={"X-CSRF-Token": token})
    assert no_digit.status_code == 400


def test_validate_password_rules():
    assert security.validate_password("short1") != []
    assert security.validate_password("alllettersonly") != []
    assert security.validate_password("12345678901") != []
    assert security.validate_password("Good-Pass-123") == []
    gen = security.generate_temporary_password()
    assert security.validate_password(gen, "x") == [] and len(gen) >= 16


def test_logout_invalidates_session(client):
    token = login_admin(client)
    assert client.post("/api/agent/logout", headers={"X-CSRF-Token": token}).status_code == 200
    assert client.get("/api/agent/tickets").status_code == 401


def test_idle_timeout_ends_session(app, client):
    token = login_admin(client)
    with client.session_transaction() as sess:
        sess["last_seen"] = 0  # far in the past
    assert client.get("/api/agent/tickets").status_code == 401
    assert token  # session existed before the timeout


# ---------------- CSRF ----------------

def test_state_changing_request_without_csrf_token_is_rejected(client):
    login_admin(client)
    res = client.patch("/api/agent/tickets/TKT-000000", json={"status": "Closed"})
    assert res.status_code == 403
    assert res.get_json()["error"] == "CSRF"


def test_wrong_csrf_token_is_rejected(client):
    login_admin(client)
    res = client.post("/api/agent/tickets/TKT-000000/notes", json={"note": "x"},
                      headers={"X-CSRF-Token": "forged-token"})
    assert res.status_code == 403


def test_reads_do_not_need_csrf(client):
    login_admin(client)
    assert client.get("/api/agent/tickets").status_code == 200


# ---------------- authorisation ----------------

def test_agent_cannot_use_admin_endpoints(client):
    admin_token = login_admin(client)
    password = create_user(client, admin_token, "rookie")
    client.post("/api/agent/logout", headers={"X-CSRF-Token": admin_token})
    login_as(client, "rookie", password)
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/admin/audit").status_code == 403
    page = client.get("/admin")
    assert page.status_code == 403


def test_admin_safeguards_protect_the_last_admin_and_self(client):
    token = login_admin(client)
    create_user(client, token, "second", access="admin")
    res = client.patch("/api/admin/users/second", json={"access_level": "agent"},
                       headers={"X-CSRF-Token": token})
    assert res.status_code == 200  # allowed: still one admin left (the caller)
    res = client.patch("/api/admin/users/admin", json={"is_active": False}, headers={"X-CSRF-Token": token})
    assert res.status_code == 409  # cannot deactivate yourself


def test_deactivated_agent_loses_access_immediately(app, client):
    token = login_admin(client)
    password = create_user(client, token, "leaver")
    agent_client = app.test_client()
    login_as(agent_client, "leaver", password)
    assert agent_client.get("/api/agent/tickets").status_code == 200
    res = client.patch("/api/admin/users/leaver", json={"is_active": False}, headers={"X-CSRF-Token": token})
    assert res.status_code == 200
    assert agent_client.get("/api/agent/tickets").status_code == 401


def test_admin_reset_password_forces_change_and_returns_secret_once(client):
    token = login_admin(client)
    password = create_user(client, token, "forgetful")
    res = client.post("/api/admin/users/forgetful/reset-password", headers={"X-CSRF-Token": token})
    temp = res.get_json()["temporary_password"]
    assert temp and temp != password
    login = client.post("/api/agent/login", json={"username": "forgetful", "password": temp}).get_json()
    assert login["must_change_password"] is True


def test_admin_user_creation_validates_input(client):
    token = login_admin(client)
    base = {"username": "good.name", "full_name": "Good", "password": "Good-Pass-123", "access_level": "agent"}
    for bad in ({"username": "A B"}, {"full_name": ""}, {"access_level": "root"},
                {"password": "weak"}, {"email": "not-an-email"}):
        body = {**base, **bad}
        res = client.post("/api/admin/users", json=body, headers={"X-CSRF-Token": token})
        assert res.status_code == 400, bad
    dup = client.post("/api/admin/users", json={**base, "username": "admin"}, headers={"X-CSRF-Token": token})
    assert dup.status_code == 409


# ---------------- input handling & robustness ----------------

def test_bad_pagination_parameters_do_not_crash(client):
    login_admin(client)
    res = client.get("/api/agent/tickets?page=abc&limit=-5")
    assert res.status_code == 200
    assert res.get_json()["page"] == 1 and res.get_json()["limit"] == 1


def test_unknown_filter_values_are_rejected(client):
    login_admin(client)
    assert client.get("/api/agent/tickets?status=Hacked").status_code == 400
    assert client.get("/api/agent/tickets?priority=Ultra").status_code == 400


def test_missing_ticket_page_returns_404_not_500(client):
    login_admin(client)
    res = client.get("/agent/tickets/TKT-NOPE00")
    assert res.status_code == 404
    assert b"Ticket not found" in res.data


def test_null_and_wrong_types_return_400_not_500(client):
    assert client.post("/api/tickets", json={"issue_description": None}).status_code == 400
    assert client.post("/api/tickets", json={"issue_description": ["x"]}).status_code == 400
    assert client.post("/api/tickets", json={"issue_description": "Broken item", "customer_email": "bad"}).status_code == 400
    assert client.post("/api/tickets", json={"issue_description": "Broken item",
                                             "predicted_category": ["list"]}).status_code == 201


def test_oversized_request_body_is_rejected(client):
    res = client.post("/api/chat", json={"message": "x" * 70_000})
    assert res.status_code == 413
    assert res.get_json()["error"] == "Payload Too Large"


def test_customer_cannot_choose_ticket_priority(client, db):
    res = client.post("/api/tickets", json={"issue_description": "Please escalate", "priority": "Urgent"})
    assert res.status_code == 201
    assert res.get_json()["priority"] == "Medium"


def test_assignment_must_be_an_active_agent(client):
    token = login_admin(client)
    ticket = client.post("/api/tickets", json={"issue_description": "Help me please"}).get_json()["ticket_id"]
    bad = client.patch(f"/api/agent/tickets/{ticket}", json={"assigned_agent": "ghost"},
                       headers={"X-CSRF-Token": token})
    assert bad.status_code == 400
    good = client.patch(f"/api/agent/tickets/{ticket}", json={"assigned_agent": "admin"},
                        headers={"X-CSRF-Token": token})
    assert good.status_code == 200 and good.get_json()["ticket"]["assigned_agent"] == "admin"
    cleared = client.patch(f"/api/agent/tickets/{ticket}", json={"assigned_agent": None},
                           headers={"X-CSRF-Token": token})
    assert cleared.get_json()["ticket"]["assigned_agent"] is None


def test_customer_ticket_lookup_requires_matching_email(client):
    ticket = client.post("/api/tickets", json={"issue_description": "Where is my refund",
                                               "customer_email": "owner@example.com"}).get_json()["ticket_id"]
    assert client.get(f"/api/tickets/{ticket}").status_code == 400
    assert client.get(f"/api/tickets/{ticket}?email=other@example.com").status_code == 404
    ok = client.get(f"/api/tickets/{ticket}?email=owner@example.com")
    assert ok.status_code == 200
    body = ok.get_json()
    assert "notes" not in body and "resolution_notes" not in body and "assigned_agent" not in body


def test_ticket_lookup_is_rate_limited(tmp_path):
    application = _make_app(tmp_path, RATE_LIMITS={"ticket_lookup": (2, 60), "chat": (100000, 60),
                                                    "ticket_create": (100000, 60), "login": (100000, 60),
                                                    "feedback": (100000, 60), "checkout": (100000, 60)})
    client = application.test_client()
    codes = [client.get("/api/tickets/TKT-000000?email=a@b.co").status_code for _ in range(3)]
    assert codes[-1] == 429


def test_agent_ticket_notes_and_audit_trail(client, db):
    token = login_admin(client)
    ticket = client.post("/api/tickets", json={"issue_description": "Audit me please"}).get_json()["ticket_id"]
    client.post(f"/api/agent/tickets/{ticket}/notes", json={"note": "Looked into it"},
                headers={"X-CSRF-Token": token})
    client.patch(f"/api/agent/tickets/{ticket}", json={"status": "Resolved"}, headers={"X-CSRF-Token": token})
    res = client.get("/api/admin/audit")
    assert res.status_code == 200
    actions = [e["action"] for e in res.get_json()["entries"]]
    assert "login.success" in actions
    assert "ticket.updated" in actions
    assert "ticket.created" in actions


def test_notes_are_stored_as_text_not_markup(client):
    token = login_admin(client)
    ticket = client.post("/api/tickets", json={"issue_description": "Payload test"}).get_json()["ticket_id"]
    payload = '<img src=x onerror=alert(1)>'
    client.post(f"/api/agent/tickets/{ticket}/notes", json={"note": payload}, headers={"X-CSRF-Token": token})
    notes = client.get(f"/api/agent/tickets/{ticket}").get_json()["notes"]
    assert any(n["note"] == payload for n in notes)  # stored verbatim; rendering must escape it


# ---------------- headers & front-end safety ----------------

def test_security_headers_on_pages_and_api(client):
    for path in ("/", "/agent/login", "/api/conversations/x", "/health"):
        res = client.get(path)
        assert res.headers.get("X-Frame-Options") == "DENY", path
        assert res.headers.get("X-Content-Type-Options") == "nosniff", path
        csp = res.headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp, path
        assert res.headers.get("X-Request-ID"), path


def test_responses_are_not_cached_for_agent_areas(client):
    assert client.get("/api/agent/status").headers.get("Cache-Control") == "no-store"


def test_templates_have_no_inline_scripts_or_handlers():
    inline_script = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
    inline_handler = re.compile(r"\son(click|change|submit|keydown|input|load)\s*=", re.IGNORECASE)
    for template in TEMPLATES.glob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert not inline_script.search(text), f"inline <script> in {template.name}"
        assert not inline_handler.search(text), f"inline event handler in {template.name}"


def test_javascript_never_assigns_server_text_as_html():
    for script in STATIC_JS.glob("*.js"):
        code = "\n".join(line for line in script.read_text(encoding="utf-8").splitlines()
                         if not line.strip().startswith(("*", "/*", "//")))
        assert "innerHTML" not in code, f"innerHTML used in {script.name}"
        assert "insertAdjacentHTML" not in code, script.name
        assert "document.write" not in code, script.name


def test_error_responses_do_not_leak_tracebacks(client):
    res = client.get("/api/does-not-exist")
    assert res.status_code == 404
    assert b"Traceback" not in res.data
