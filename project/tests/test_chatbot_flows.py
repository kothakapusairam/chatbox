from conftest import add_order


def chat(client, message, conversation_id=None, **extra):
    body = {"message": message, **extra}
    if conversation_id:
        body["conversation_id"] = conversation_id
    res = client.post("/api/chat", json=body)
    assert res.status_code == 200, res.get_json()
    return res.get_json()


def test_greeting_is_answered(client):
    data = chat(client, "Hello, good morning!")
    assert data["intent"] == "greeting"
    assert data["response"]


def test_order_status_requires_order_id_then_email(client):
    first = chat(client, "Where is my order?")
    cid = first["conversation_id"]
    second = chat(client, "ORD1025", cid)
    assert "email address" in second["response"].lower()
    assert "Order Status" not in second["response"] and "In Transit" not in second["response"]
    third = chat(client, "john.doe@example.com", cid)
    assert "ORD1025" in third["response"]
    assert "In Transit" in third["response"]
    assert "FedEx Express" in third["response"]


def test_order_details_are_never_returned_without_verification(client):
    data = chat(client, "What is the status of ORD1025?")
    body = str(data)
    assert "John Doe" not in body
    assert "FDX-9988231" not in body
    assert "order_data" not in data


def test_wrong_email_and_unknown_order_get_identical_replies(client):
    cid = chat(client, "Where is my order?")["conversation_id"]
    chat(client, "ORD1025", cid)
    wrong_email = chat(client, "wrong@example.com", cid)["response"]
    unknown = chat(client, "Where is my order ORD9999 and my email is nobody@example.com")["response"]
    assert "couldn't verify" in wrong_email.lower()
    assert "couldn't verify" in unknown.lower()


def test_session_email_is_used_when_supplied(client):
    data = chat(client, "Where is ORD1025 right now?", customer_email="john.doe@example.com")
    assert "In Transit" in data["response"]


def test_cancelling_paid_processing_order_refunds_and_updates_records(client, db):
    data = chat(client, "Cancel order ORD1027, michael.brown@example.com")
    assert "cancelled" in data["response"].lower()
    assert "refund" in data["response"].lower()
    order = db.execute("SELECT status, payment_status FROM orders WHERE order_id='ORD1027'").fetchone()
    assert order["status"] == "Cancelled"
    assert order["payment_status"] == "refunded"
    refund = db.execute("SELECT status, amount_cents FROM refunds WHERE order_id='ORD1027'").fetchone()
    assert refund["status"] == "succeeded" and refund["amount_cents"] == 8900


def test_cancelling_in_transit_order_is_refused(client, db):
    data = chat(client, "cancel ORD1025 john.doe@example.com")
    assert "already in transit" in data["response"].lower()
    status = db.execute("SELECT status FROM orders WHERE order_id='ORD1025'").fetchone()["status"]
    assert status == "In Transit"


def test_return_for_recent_delivery_opens_linked_ticket(client, db):
    data = chat(client, "I want to return ORD1031, priya.raman@example.com")
    assert data["escalated"] is True and data["ticket_id"]
    ticket = db.execute("SELECT order_id, predicted_category FROM support_tickets WHERE ticket_id=?",
                        (data["ticket_id"],)).fetchone()
    assert ticket["order_id"] == "ORD1031"
    assert ticket["predicted_category"] == "return_request"


def test_return_outside_window_is_refused(client):
    data = chat(client, "return ORD1029 david.wilson@example.com")
    assert data["escalated"] is False
    assert "outside" in data["response"].lower() or "not eligible" in data["response"].lower()


def test_unpaid_order_gets_secure_checkout_link(client, db):
    add_order(db, "ORD2001", status="Processing", payment_status="unpaid", amount_cents=4999)
    data = chat(client, "I want to pay for my order ORD2001 buyer@example.com")
    assert data["intent"] == "make_payment"
    assert "/payments/sandbox/cs_sandbox_" in data["response"]
    assert "$49.99" in data["response"]


def test_already_paid_order_is_not_charged_again(client, db):
    add_order(db, "ORD2002", status="Processing", payment_status="paid")
    data = chat(client, "pay for ORD2002 buyer@example.com")
    assert "already been paid" in data["response"].lower()
    assert "/payments/sandbox/" not in data["response"]


def test_cancelled_order_is_not_payable(client):
    data = chat(client, "pay for ORD1028 emily.davis@example.com")
    assert "no longer payable" in data["response"].lower()


def test_explicit_human_request_creates_ticket(client, db):
    data = chat(client, "I would like to speak to a human support agent")
    assert data["escalated"] is True
    assert db.execute("SELECT COUNT(*) FROM support_tickets WHERE ticket_id=?", (data["ticket_id"],)).fetchone()[0] == 1


def test_complaint_creates_high_priority_ticket(client, db):
    data = chat(client, "This is unacceptable, my package arrived broken and I'm furious")
    assert data["escalated"] is True
    priority = db.execute("SELECT priority FROM support_tickets WHERE ticket_id=?",
                          (data["ticket_id"],)).fetchone()["priority"]
    assert priority in ("High", "Urgent")


def test_urgent_keyword_raises_priority(client, db):
    data = chat(client, "URGENT: I think my account has fraud on it, please help immediately")
    priority = db.execute("SELECT priority FROM support_tickets WHERE ticket_id=?",
                          (data["ticket_id"],)).fetchone()["priority"]
    assert priority == "Urgent"


def test_topic_change_clears_pending_order_request(client):
    cid = chat(client, "Where is my order?")["conversation_id"]
    chat(client, "ORD1025", cid)  # now waiting for an email
    data = chat(client, "hello there", cid)
    assert data["intent"] == "greeting"


def test_cancel_word_exits_pending_request(client):
    cid = chat(client, "I want to cancel my order")["conversation_id"]
    data = chat(client, "nevermind", cid)
    assert "cleared" in data["response"].lower()


def test_empty_and_long_messages_rejected(client):
    assert client.post("/api/chat", json={"message": "   "}).status_code == 400
    assert client.post("/api/chat", json={"message": "a" * 501}).status_code == 400
    assert client.post("/api/chat", data="not json", content_type="text/plain").status_code == 400


def test_feedback_accepts_real_message_ids_only(client):
    data = chat(client, "Hello")
    cid, mid = data["conversation_id"], data["message_id"]
    assert mid
    ok = client.post("/api/feedback", json={"conversation_id": cid, "message_id": mid, "rating": "helpful"})
    assert ok.status_code == 200
    other = client.post("/api/conversations", json={}).get_json()["conversation_id"]
    bad = client.post("/api/feedback", json={"conversation_id": other, "message_id": mid, "rating": "helpful"})
    assert bad.status_code == 400
    bad_rating = client.post("/api/feedback", json={"conversation_id": cid, "message_id": mid, "rating": "meh"})
    assert bad_rating.status_code == 400
