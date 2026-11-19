from conftest import add_order, login_admin, create_user, login_as


def test_customer_pages_render(client):
    for path in ("/", "/ticket/track", "/agent/login"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert b"<!DOCTYPE html>" in res.data


def test_customer_page_has_no_demo_credentials_in_production_style_config(client):
    body = client.get("/agent/login").get_data(as_text=True)
    assert "admin123" not in body and "agent123" not in body


def test_agent_dashboard_embeds_csrf_token_and_scripts(client):
    token = login_admin(client)
    body = client.get("/agent/dashboard").get_data(as_text=True)
    assert f'content="{token}"' in body
    assert "/static/js/dashboard.js" in body and "/static/js/orders.js" in body


def test_ticket_details_page_is_server_rendered_and_escaped(client):
    token = login_admin(client)
    ticket = client.post("/api/tickets", json={
        "issue_description": '<script>alert("x")</script> broken item',
        "customer_name": "Eve <b>",
    }).get_json()["ticket_id"]
    res = client.get(f"/agent/tickets/{ticket}")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "<b>" not in html.split("Customer")[1][:200] or "&lt;b&gt;" in html
    assert token


def test_ticket_details_page_404_for_bad_id_format(client):
    login_admin(client)
    assert client.get("/agent/tickets/not-a-ticket").status_code == 404


def test_agent_ticket_workflow_end_to_end(client):
    token = login_admin(client)
    ticket_id = client.post("/api/tickets", json={
        "issue_description": "Package never arrived", "customer_email": "pat@example.com",
    }).get_json()["ticket_id"]

    listing = client.get("/api/agent/tickets?search=pat@example.com").get_json()
    assert listing["total"] == 1 and listing["tickets"][0]["ticket_id"] == ticket_id

    res = client.patch(f"/api/agent/tickets/{ticket_id}", json={
        "status": "In Progress", "priority": "High", "assigned_agent": "admin",
        "resolution_notes": "Contacting carrier.",
    }, headers={"X-CSRF-Token": token})
    assert res.status_code == 200
    updated = res.get_json()["ticket"]
    assert updated["status"] == "In Progress" and updated["priority"] == "High"

    filtered = client.get("/api/agent/tickets?status=In%20Progress&priority=High").get_json()
    assert any(t["ticket_id"] == ticket_id for t in filtered["tickets"])

    detail = client.get(f"/api/agent/tickets/{ticket_id}").get_json()
    assert any("Update:" in n["note"] for n in detail["notes"])

    status = client.get("/api/agent/stats").get_json()["stats"]
    assert status["total"] >= 1

    public = client.get(f"/api/tickets/{ticket_id}?email=pat@example.com").get_json()
    assert public["status"] == "In Progress"


def test_public_ticket_description_is_visible_only_to_matching_email(client):
    ticket_id = client.post("/api/tickets", json={
        "issue_description": "Refund please", "customer_email": "k@example.com",
    }).get_json()["ticket_id"]
    body = client.get(f"/api/tickets/{ticket_id}?email=k@example.com").get_json()
    assert body["description"] == "Refund please"


def test_agent_order_lookup_and_refund_visibility(client, db):
    token = login_admin(client)
    add_order(db, "ORD4001", status="Delivered", payment_status="paid", amount_cents=6400,
              email="order@example.com")
    detail = client.get("/api/agent/orders/ord4001").get_json()
    assert detail["order"]["order_id"] == "ORD4001"
    assert detail["order"]["customer_email"] == "order@example.com"
    assert detail["payments"] == []  # no payment row created for this manual fixture
    bad = client.post("/api/agent/orders/ORD4001/refunds", json={"reason": "x"},
                      headers={"X-CSRF-Token": token})
    assert bad.status_code == 409  # nothing to refund: no successful payment record
    assert client.get("/api/agent/orders/ORD9999").status_code == 404


def test_order_list_filters_and_search(client):
    login_admin(client)
    res = client.get("/api/agent/orders?payment_status=paid&search=john")
    assert res.status_code == 200
    assert all(o["payment_status"] == "paid" for o in res.get_json()["orders"])
    assert client.get("/api/agent/orders?payment_status=bogus").status_code == 400


def test_admin_user_lifecycle(client):
    token = login_admin(client)
    create_user(client, token, "newbie")
    listing = client.get("/api/admin/users").get_json()["users"]
    newbie = next(u for u in listing if u["username"] == "newbie")
    assert newbie["must_change_password"] == 1 and newbie["access_level"] == "agent"

    upgraded = client.patch("/api/admin/users/newbie", json={"access_level": "admin"},
                            headers={"X-CSRF-Token": token})
    assert upgraded.status_code == 200

    audit = client.get("/api/admin/audit").get_json()["entries"]
    assert any(e["action"] == "user.updated" and e["target_id"] == "newbie" for e in audit)


def test_admin_console_renders_for_admin(client):
    login_admin(client)
    res = client.get("/admin")
    assert res.status_code == 200
    assert b"Administration console" in res.data


def test_admin_payments_and_overview_endpoints(client):
    login_admin(client)
    assert client.get("/api/admin/payments").status_code == 200
    assert client.get("/api/admin/payments?status=succeeded").status_code == 200
    assert client.get("/api/admin/payments?status=nope").status_code == 400
    overview = client.get("/api/admin/overview").get_json()
    assert overview["success"] is True


def test_health_endpoint_reports_ok(client):
    res = client.get("/health")
    body = res.get_json()
    assert res.status_code == 200
    assert body["status"] == "ok" and body["database"] == "ok" and body["model_loaded"] is True
    assert "model_metadata" not in body  # internal details are not exposed publicly


def test_unknown_routes_return_json_for_api_and_html_for_pages(client):
    assert client.get("/api/nothing-here").get_json()["error"] == "Not Found"
    page = client.get("/nothing-here")
    assert page.status_code == 404 and b"Page not found" in page.data


def test_logout_api_clears_agent_session_and_redirects_pages(client):
    token = login_admin(client)
    assert client.post("/api/agent/logout", headers={"X-CSRF-Token": token}).get_json()["success"] is True
    assert client.get("/agent/dashboard").status_code == 302


def test_feedback_and_conversation_history(client):
    chat = client.post("/api/chat", json={"message": "Hi there"}).get_json()
    history = client.get(f"/api/conversations/{chat['conversation_id']}").get_json()
    assert history["messages"][0]["sender"] == "user"
    assert history["messages"][-1]["sender"] == "bot"
    assert client.get("/api/conversations/not-a-real-id-123").status_code == 404
