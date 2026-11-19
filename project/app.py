"""
AI Customer Support application: Flask factory, pages, JSON APIs, auth and payments routes.
"""
import logging
import os
import re
import secrets
import time
import uuid
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import (Flask, g, jsonify, redirect, render_template, request, session, url_for)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import database
import payments
from chatbot import ChatbotEngine
from config import config_by_name, validate_config
from security import (apply_security_headers, check_rate_limit, client_ip, csrf_is_valid, get_csrf_token,
                      generate_temporary_password, json_error, validate_password, valid_username)
from flask import current_app
from utils import format_cents
from utils import (CONVERSATION_ID_RE, clamp_int, emails_match, is_valid_email, new_ticket_id,
                   normalize_order_id, sanitize_text, utc_now_iso)

logger = logging.getLogger(__name__)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
TICKET_ID_PATTERN = re.compile(r"^TKT-[A-Z0-9]{6,12}$")
TICKET_STATUSES = database.TICKET_STATUSES
TICKET_PRIORITIES = database.TICKET_PRIORITIES
CommerceContext = namedtuple("CommerceContext", "provider config")
APP_VERSION = "2.0.0"


# ----------------------------------------------------------------------
# Application factory
# ----------------------------------------------------------------------

def create_app(config_name=None, overrides=None):
    config_name = config_name or os.environ.get("FLASK_ENV", "development")
    if config_name not in config_by_name:
        config_name = "development"
    config_class = config_by_name[config_name]

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(config_class)
    if overrides:
        app.config.update(overrides)

    problems = validate_config(_ConfigView(app.config))
    if problems and app.config["ENV_NAME"] == "production":
        raise RuntimeError("Invalid production configuration: " + " ".join(problems))
    for problem in problems:
        logger.warning("Configuration warning: %s", problem)

    _resolve_secret_key(app)
    instance_dir = Path(app.config["DATABASE_PATH"]).parent
    instance_dir.mkdir(parents=True, exist_ok=True)

    _initialise_database(app)

    app.chatbot = ChatbotEngine(
        model_path=app.config["MODEL_PATH"],
        knowledge_base_path=app.config["KNOWLEDGE_BASE_PATH"],
        confidence_threshold=app.config["CONFIDENCE_THRESHOLD"],
        metadata_path=app.config["METADATA_PATH"],
    )
    app.payment_provider = payments.build_provider(app.config)
    app.commerce = CommerceContext(provider=app.payment_provider, config=app.config)
    app.jinja_env.globals["csrf_token"] = get_csrf_token

    _register_request_hooks(app)
    _register_error_handlers(app)
    _register_pages(app)
    _register_customer_api(app)
    _register_agent_api(app)
    _register_admin_api(app)
    _register_payment_routes(app)
    return app


class _ConfigView:
    """Adapter so validate_config can read Flask's config mapping."""

    def __init__(self, mapping):
        self._m = mapping

    def __getattr__(self, item):
        try:
            return self._m[item]
        except KeyError as exc:
            raise AttributeError(item) from exc


def _resolve_secret_key(app):
    if app.config.get("SECRET_KEY"):
        return
    if app.config["ENV_NAME"] == "production":
        raise RuntimeError("SECRET_KEY must be set in production.")
    key_file = Path(app.config["DATABASE_PATH"]).parent / ".secret_key"
    try:  # atomic create: when several workers start together, exactly one key wins
        with open(key_file, "x", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(48))
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        logger.info("Generated development SECRET_KEY at %s", key_file)
    except FileExistsError:
        pass
    app.config["SECRET_KEY"] = key_file.read_text(encoding="utf-8").strip()


def _initialise_database(app):
    db_path = app.config["DATABASE_PATH"]
    username = app.config["BOOTSTRAP_ADMIN_USERNAME"]
    password = app.config.get("BOOTSTRAP_ADMIN_PASSWORD")
    generated = False
    if not password:
        if app.config["ENV_NAME"] == "production":
            raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD is required to create the first admin in production.")
        password = generate_temporary_password()
        generated = True

    def announce(created_username):
        if not generated:
            return
        pw_file = Path(db_path).parent / "initial_admin_password.txt"
        pw_file.write_text(f"username: {created_username}\npassword: {password}\n"
                           f"Change this password at first login. Delete this file afterwards.\n",
                           encoding="utf-8")
        try:
            os.chmod(pw_file, 0o600)
        except OSError:
            pass
        logger.warning("Created first admin '%s'. Initial password written to %s", created_username, pw_file)

    conn = database.init_db(db_path, seed_data=app.config["SEED_DEMO_DATA"], bootstrap_username=username,
                            bootstrap_password=password, on_bootstrap_created=announce)
    logger.info("Database ready (schema v%s).", database.current_schema_version(conn))
    conn.close()


# ----------------------------------------------------------------------
# Request plumbing
# ----------------------------------------------------------------------

def _register_request_hooks(app):
    def get_db():
        if "db" not in g:
            g.db = database.get_db_connection(app.config["DATABASE_PATH"])
        return g.db

    app.get_db = get_db

    @app.teardown_appcontext
    def close_db(exc):
        db = g.pop("db", None)
        if db is None:
            return
        try:
            if exc is None:
                db.commit()
            else:
                db.rollback()
        except Exception:
            logger.exception("Commit failed; rolling back")
            db.rollback()
        finally:
            db.close()

    @app.before_request
    def assign_request_id():
        g.request_id = uuid.uuid4().hex[:12]

    @app.after_request
    def finish(response):
        response.headers["X-Request-ID"] = getattr(g, "request_id", "-")
        return apply_security_headers(response, app.config["SESSION_COOKIE_SECURE"])


def _register_error_handlers(app):
    def wants_json():
        return request.path.startswith("/api/") or (request.path.startswith("/payments/") and request.method == "POST")

    @app.errorhandler(404)
    def not_found(_e):
        if wants_json():
            return json_error(404, "Not Found", "The requested resource was not found.")
        return render_template("error.html", title="Page not found",
                               message="The page you are looking for does not exist."), 404

    @app.errorhandler(405)
    def method_not_allowed(_e):
        return json_error(405, "Method Not Allowed", "This action is not supported.")

    @app.errorhandler(413)
    def too_large(_e):
        return json_error(413, "Payload Too Large", "The request body is too large.")

    @app.errorhandler(Exception)
    def unhandled(exc):
        if isinstance(exc, HTTPException):
            if wants_json():
                return json_error(exc.code or 500, exc.name, exc.description or exc.name)
            return render_template("error.html", title=exc.name, message=exc.description), exc.code
        logger.exception("Unhandled error [request %s]", getattr(g, "request_id", "-"))
        if wants_json():
            return json_error(500, "Internal Server Error", "Something went wrong. Please try again.")
        return render_template("error.html", title="Something went wrong",
                               message="An unexpected error occurred. Please try again shortly."), 500


def get_json_body():
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else None


def current_agent():
    """Return the logged-in, active agent for this request (cached), or None."""
    if "agent" in g:
        return g.agent
    g.agent = None
    username = session.get("agent_user")
    if not username:
        return None
    agent = database.get_agent_by_username(_db(), username)
    idle_limit = current_app.config["AGENT_IDLE_TIMEOUT_MINUTES"] * 60
    last_seen = session.get("last_seen", 0)
    if not agent or not agent["is_active"] or (time.time() - last_seen > idle_limit):
        session.clear()
        return None
    session["last_seen"] = time.time()
    g.agent = agent
    return agent


def _db():
    return current_app.get_db()


def agent_required(admin=False, allow_password_change=False):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            is_api = request.path.startswith("/api/")
            agent = current_agent()
            if agent is None:
                if is_api:
                    return json_error(401, "Unauthorized", "Agent authentication required.")
                return redirect(url_for("agent_login", next=request.path))
            if request.method not in SAFE_METHODS and not csrf_is_valid():
                return json_error(403, "CSRF", "Your session token is missing or expired. Reload the page and try again.")
            if agent["must_change_password"] and not allow_password_change:
                if is_api:
                    return json_error(403, "password_change_required", "You must change your password first.")
                return redirect(url_for("change_password_page"))
            if admin and agent["access_level"] != "admin":
                if is_api:
                    return json_error(403, "Forbidden", "Administrator access is required.")
                return render_template("error.html", title="Forbidden",
                                       message="Administrator access is required for this page."), 403
            return view(*args, **kwargs)
        return wrapper
    return decorator


def _agent_public(agent):
    return {"username": agent["username"], "full_name": agent["full_name"], "role": agent["role"],
            "access_level": agent["access_level"]}


def _client_ip():
    return client_ip()


def _commerce():
    return current_app.commerce


def _service_error(exc):
    return json_error(exc.status, exc.code, exc.message)


# ----------------------------------------------------------------------
# HTML pages
# ----------------------------------------------------------------------

def _register_pages(app):
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/ticket/track")
    def ticket_track():
        return render_template("ticket_track.html")

    @app.route("/agent/login")
    def agent_login():
        if current_agent():
            return redirect(url_for("agent_dashboard"))
        return render_template("login.html", demo_mode=app.config["DEMO_MODE"])

    @app.route("/agent")
    @app.route("/agent/dashboard")
    @agent_required()
    def agent_dashboard():
        return render_template("agent_dashboard.html", agent=g.agent)

    @app.route("/agent/tickets/<ticket_id>")
    @agent_required()
    def ticket_details_view(ticket_id):
        if not TICKET_ID_PATTERN.match(ticket_id.upper()):
            return render_template("error.html", title="Not found", message="Ticket not found."), 404
        ticket = database.get_ticket(_db(), ticket_id)
        if not ticket:
            return render_template("error.html", title="Not found", message="Ticket not found."), 404
        return render_template("ticket_details.html", ticket=ticket, agent=g.agent)

    @app.route("/agent/change-password")
    @agent_required(allow_password_change=True)
    def change_password_page():
        return render_template("change_password.html", agent=g.agent, forced=bool(g.agent["must_change_password"]))

    @app.route("/admin")
    @agent_required(admin=True)
    def admin_page():
        return render_template("admin.html", agent=g.agent)

    @app.route("/payments/return")
    def payment_return():
        status = request.args.get("status", "")
        order_id = normalize_order_id(request.args.get("order", ""))
        return render_template("payment_return.html", status=status if status in ("success", "cancelled") else "",
                               order_id=order_id)

    @app.route("/payments/sandbox/<session_id>")
    def sandbox_checkout(session_id):
        if app.payment_provider.name != "sandbox":
            return render_template("error.html", title="Not found", message="Page not found."), 404
        payment = database.get_payment_by_session(_db(), session_id)
        if not payment:
            return render_template("error.html", title="Not found", message="Checkout session not found."), 404
        return render_template("sandbox_checkout.html", payment=payment,
                               amount=format_cents(payment["amount_cents"], payment["currency"]))


# ----------------------------------------------------------------------
# Customer API
# ----------------------------------------------------------------------

def _register_customer_api(app):

    @app.route("/health", methods=["GET"])
    def health_check():
        db_ok = True
        try:
            database.ping(_db())
        except Exception:
            db_ok = False
            logger.exception("Health check: database unreachable")
        status = "ok" if db_ok and app.chatbot.pipeline is not None else "degraded"
        return jsonify({"status": status, "database": "ok" if db_ok else "error",
                        "model_loaded": app.chatbot.pipeline is not None, "version": APP_VERSION}), \
            (200 if db_ok else 503)

    @app.route("/api/conversations", methods=["POST"])
    def start_conversation():
        limited = check_rate_limit("chat")
        if limited:
            return limited
        payload = get_json_body() or {}
        name = sanitize_text(payload.get("customer_name") or "Customer", 100) or "Customer"
        conv_id = str(uuid.uuid4())
        conv = database.create_conversation(_db(), conv_id, name)
        return jsonify({"success": True, "conversation_id": conv["id"], "customer_name": conv["customer_name"],
                        "created_at": conv["created_at"]}), 201

    @app.route("/api/conversations/<conversation_id>", methods=["GET"])
    def get_conversation_history(conversation_id):
        if not CONVERSATION_ID_RE.match(conversation_id or ""):
            return json_error(404, "Not Found", "Conversation session not found.")
        conv = database.get_conversation(_db(), conversation_id)
        if not conv:
            return json_error(404, "Not Found", "Conversation session not found.")
        return jsonify({"success": True, "conversation_id": conversation_id,
                        "customer_name": conv["customer_name"],
                        "messages": database.get_conversation_messages(_db(), conversation_id)})

    @app.route("/api/chat", methods=["POST"])
    def chat():
        limited = check_rate_limit("chat")
        if limited:
            return limited
        payload = get_json_body()
        if payload is None:
            return json_error(400, "Bad Request", "Invalid JSON payload.")
        raw = payload.get("message")
        if not isinstance(raw, str) or not raw.strip():
            return json_error(400, "Bad Request", "Message text cannot be empty.")
        if len(raw) > app.config["MAX_MESSAGE_LENGTH"]:
            return json_error(400, "Bad Request",
                              f"Message exceeds limit of {app.config['MAX_MESSAGE_LENGTH']} characters.")
        message = sanitize_text(raw, app.config["MAX_MESSAGE_LENGTH"], multiline=True)
        name = sanitize_text(payload.get("customer_name") or "Customer", 100) or "Customer"
        email = payload.get("customer_email")
        email = email.strip().lower() if isinstance(email, str) and is_valid_email(email.strip()) else None

        conn = _db()
        conversation_id = payload.get("conversation_id")
        if not (isinstance(conversation_id, str) and CONVERSATION_ID_RE.match(conversation_id)
                and database.get_conversation(conn, conversation_id)):
            conversation_id = str(uuid.uuid4())
            database.create_conversation(conn, conversation_id, name)

        result = app.chatbot.process_message(conn, conversation_id, message, customer_name=name,
                                             customer_email=email, commerce=_commerce())
        return jsonify({
            "success": True,
            "conversation_id": conversation_id,
            "response": result["response"],
            "intent": result["intent"],
            "confidence": result["confidence"],
            "escalated": result["escalated"],
            "ticket_id": result.get("ticket_id"),
            "message_id": result.get("message_id"),
        })

    @app.route("/api/tickets", methods=["POST"])
    def create_ticket():
        limited = check_rate_limit("ticket_create")
        if limited:
            return limited
        payload = get_json_body()
        if payload is None:
            return json_error(400, "Bad Request", "Invalid JSON payload.")
        description = sanitize_text(payload.get("issue_description"), app.config["MAX_DESCRIPTION_LENGTH"],
                                    multiline=True)
        if len(description) < 5:
            return json_error(400, "Bad Request", "Please describe your issue (at least 5 characters).")
        name = sanitize_text(payload.get("customer_name") or "Customer", 100) or "Customer"
        email_raw = (payload.get("customer_email") or "")
        email = email_raw.strip().lower() if isinstance(email_raw, str) else ""
        if email and not is_valid_email(email):
            return json_error(400, "Bad Request", "Please enter a valid email address.")
        category = payload.get("predicted_category") or "human_support"
        if not isinstance(category, str) or category not in _known_categories(app):
            category = "human_support"
        conv_id = payload.get("conversation_id")
        conn = _db()
        if not (isinstance(conv_id, str) and CONVERSATION_ID_RE.match(conv_id)
                and database.get_conversation(conn, conv_id)):
            conv_id = None
        ticket_id = app.chatbot.generate_ticket_id(conn)
        ticket = database.create_support_ticket(
            conn, ticket_id=ticket_id, conversation_id=conv_id, customer_name=name,
            customer_email=email or None, issue_description=description, predicted_category=category,
            priority="Medium", status="Open",
        )
        database.add_ticket_note(conn, ticket_id, "System", "Support ticket opened by customer via web form.")
        database.add_audit(conn, "customer", "ticket.created", "ticket", ticket_id, {"category": category},
                           _client_ip())
        return jsonify({"success": True, "ticket_id": ticket["ticket_id"], "status": ticket["status"],
                        "priority": ticket["priority"], "created_at": ticket["created_at"],
                        "message": "Support ticket created successfully."}), 201

    @app.route("/api/tickets/<ticket_id>", methods=["GET"])
    def get_public_ticket_status(ticket_id):
        """Customer-visible ticket status. Requires the email address used on the ticket."""
        limited = check_rate_limit("ticket_lookup")
        if limited:
            return limited
        email = (request.args.get("email") or "").strip().lower()
        if not is_valid_email(email):
            return json_error(400, "Bad Request", "A valid email address is required to look up a ticket.")
        ticket = database.get_ticket(_db(), ticket_id)
        if not ticket or not emails_match(ticket.get("customer_email"), email):
            return json_error(404, "Not Found", "No ticket matches those details.")
        return jsonify({
            "success": True,
            "ticket_id": ticket["ticket_id"],
            "status": ticket["status"],
            "priority": ticket["priority"],
            "category": ticket["predicted_category"],
            "description": ticket["issue_description"],
            "created_at": ticket["created_at"],
            "updated_at": ticket["updated_at"],
        })

    @app.route("/api/feedback", methods=["POST"])
    def submit_feedback():
        limited = check_rate_limit("feedback")
        if limited:
            return limited
        payload = get_json_body() or {}
        rating = payload.get("rating")
        if rating not in ("helpful", "unhelpful"):
            return json_error(400, "Bad Request", "Rating must be 'helpful' or 'unhelpful'.")
        conv_id = payload.get("conversation_id")
        if not (isinstance(conv_id, str) and CONVERSATION_ID_RE.match(conv_id)):
            return json_error(400, "Bad Request", "A valid conversation_id is required.")
        message_id = payload.get("message_id")
        if message_id is not None:
            if not isinstance(message_id, int) or isinstance(message_id, bool):
                return json_error(400, "Bad Request", "message_id must be an integer.")
            owner = _db().execute("SELECT conversation_id FROM messages WHERE id = ?", (message_id,)).fetchone()
            if not owner or owner["conversation_id"] != conv_id:
                return json_error(400, "Bad Request", "That message does not belong to this conversation.")
        comment = sanitize_text(payload.get("comment") or "", 500, multiline=True)
        fid = database.save_feedback(_db(), conv_id, message_id, rating, comment)
        return jsonify({"success": True, "feedback_id": fid})

    @app.route("/api/orders/<order_id>/checkout", methods=["POST"])
    def order_checkout(order_id):
        limited = check_rate_limit("checkout")
        if limited:
            return limited
        payload = get_json_body() or {}
        email = (payload.get("email") or "").strip().lower() if isinstance(payload.get("email"), str) else ""
        normalized = normalize_order_id(order_id)
        order = database.get_order(_db(), normalized) if normalized else None
        if not order or not is_valid_email(email) or not emails_match(order.get("customer_email"), email):
            return json_error(404, "Not Found", "We couldn't verify that order and email address.")
        try:
            result = payments.start_checkout(_db(), app.payment_provider, app.config, order, email,
                                             actor="customer", ip=_client_ip())
        except payments.PaymentError as exc:
            return _service_error(exc)
        return jsonify({"success": True, "checkout_url": result["checkout_url"],
                        "amount_cents": result["amount_cents"], "currency": result["currency"],
                        "expires_in_minutes": result["expires_in_minutes"]}), 201


def _known_categories(app):
    categories = set(app.chatbot.knowledge_base.keys()) | {"human_support", "unknown_query"}
    return categories


# ----------------------------------------------------------------------
# Payment webhooks & sandbox
# ----------------------------------------------------------------------

def _register_payment_routes(app):

    @app.route("/api/payments/webhook", methods=["POST"])
    def payment_webhook():
        if app.payment_provider.name != "stripe":
            return json_error(404, "Not Found", "Webhooks are not enabled for this provider.")
        payload = request.get_data(cache=False)
        try:
            event = app.payment_provider.parse_webhook(payload, request.headers.get("Stripe-Signature", ""))
        except payments.PaymentError as exc:
            logger.warning("Rejected webhook: %s", exc.message)
            return _service_error(exc)
        event_id = event.get("id")
        if not event_id:
            return json_error(400, "Bad Request", "Event has no id.")
        conn = _db()
        if not database.record_payment_event(conn, app.payment_provider.name, event_id, event.get("type", ""),
                                             payload.decode("utf-8", errors="replace")[:20000]):
            return jsonify({"success": True, "status": "duplicate"})
        try:
            outcome = payments.handle_event(conn, event)
        except Exception:
            conn.rollback()  # undo partial writes so the provider retries this event
            logger.exception("Webhook processing failed for event %s", event_id)
            return json_error(500, "Internal Server Error", "Event processing failed; it will be retried.")
        return jsonify({"success": True, "status": outcome})

    @app.route("/payments/sandbox/<session_id>/complete", methods=["POST"])
    def sandbox_complete(session_id):
        if app.payment_provider.name != "sandbox":
            return json_error(404, "Not Found", "Not found.")
        outcome = "success" if request.form.get("outcome") == "success" else "cancelled"
        try:
            payments.sandbox_complete(_db(), session_id, outcome)
        except payments.PaymentError as exc:
            return render_template("error.html", title="Payment", message=exc.message), exc.status
        payment = database.get_payment_by_session(_db(), session_id)
        return redirect(url_for("payment_return", status="success" if outcome == "success" else "cancelled",
                                order=payment["order_id"] if payment else ""))


# ----------------------------------------------------------------------
# Agent API
# ----------------------------------------------------------------------

def _register_agent_api(app):

    @app.route("/api/agent/login", methods=["POST"])
    def agent_login_api():
        limited = check_rate_limit("login")
        if limited:
            return limited
        payload = get_json_body() or {}
        username = sanitize_text(payload.get("username") or "", 64).lower()
        password = payload.get("password") if isinstance(payload.get("password"), str) else ""
        if not username or not password:
            return json_error(400, "Bad Request", "Username and password are required.")
        conn = _db()
        now = datetime.now(timezone.utc)
        agent = database.get_agent_by_username(conn, username)

        if agent and agent["locked_until"] and datetime.fromisoformat(agent["locked_until"]) > now:
            database.add_audit(conn, username, "login.locked", ip_address=_client_ip())
            return json_error(423, "Locked", "This account is temporarily locked after failed sign-in attempts. "
                                             "Please try again later.")
        if not agent or not agent["is_active"] or not check_password_hash(agent["password_hash"], password):
            if agent:
                failures = int(agent["failed_login_count"] or 0) + 1
                updates = {"failed_login_count": failures}
                if failures >= app.config["LOGIN_MAX_ATTEMPTS"]:
                    updates["locked_until"] = (now + timedelta(minutes=app.config["LOGIN_LOCKOUT_MINUTES"])).isoformat()
                    updates["failed_login_count"] = 0
                database.update_agent(conn, username, **updates)
            database.add_audit(conn, username, "login.failed", ip_address=_client_ip())
            logger.warning("Failed agent login for '%s'", username)
            return json_error(401, "Unauthorized", "Invalid username or password.")

        session.clear()
        session.permanent = True
        session["agent_user"] = agent["username"]
        session["last_seen"] = time.time()
        database.update_agent(conn, agent["username"], failed_login_count=0, locked_until=None,
                              last_login_at=utc_now_iso())
        database.add_audit(conn, agent["username"], "login.success", ip_address=_client_ip())
        g.agent = None
        return jsonify({
            "success": True,
            "agent": _agent_public(agent),
            "must_change_password": bool(agent["must_change_password"]),
            "csrf_token": get_csrf_token(),
        })

    @app.route("/api/agent/logout", methods=["POST"])
    @agent_required(allow_password_change=True)
    def agent_logout_api():
        database.add_audit(_db(), g.agent["username"], "logout", ip_address=_client_ip())
        session.clear()
        return jsonify({"success": True, "message": "Logged out successfully."})

    @app.route("/api/agent/status", methods=["GET"])
    def agent_session_status():
        agent = current_agent()
        if not agent:
            return jsonify({"logged_in": False})
        return jsonify({"logged_in": True, "agent": _agent_public(agent),
                        "must_change_password": bool(agent["must_change_password"]),
                        "csrf_token": get_csrf_token()})

    @app.route("/api/agent/change-password", methods=["POST"])
    @agent_required(allow_password_change=True)
    def agent_change_password():
        payload = get_json_body() or {}
        current = payload.get("current_password") if isinstance(payload.get("current_password"), str) else ""
        new = payload.get("new_password") if isinstance(payload.get("new_password"), str) else ""
        agent = g.agent
        conn = _db()
        if not check_password_hash(agent["password_hash"], current):
            return json_error(400, "Bad Request", "Your current password is incorrect.")
        errors = validate_password(new, agent["username"], app.config["PASSWORD_MIN_LENGTH"])
        if errors:
            return json_error(400, "Bad Request", " ".join(errors))
        if check_password_hash(agent["password_hash"], new):
            return json_error(400, "Bad Request", "Choose a password you have not used before.")
        database.update_agent(conn, agent["username"], password_hash=generate_password_hash(new),
                              must_change_password=0)
        database.add_audit(conn, agent["username"], "password.changed", "agent", agent["username"],
                           ip_address=_client_ip())
        session["csrf_token"] = secrets.token_urlsafe(32)
        return jsonify({"success": True, "message": "Password updated.", "csrf_token": session["csrf_token"]})

    @app.route("/api/agent/tickets", methods=["GET"])
    @agent_required()
    def agent_list_tickets():
        status = request.args.get("status") or None
        priority = request.args.get("priority") or None
        if status not in (None, "All") and status not in TICKET_STATUSES:
            return json_error(400, "Bad Request", "Unknown status filter.")
        if priority not in (None, "All") and priority not in TICKET_PRIORITIES:
            return json_error(400, "Bad Request", "Unknown priority filter.")
        search = sanitize_text(request.args.get("search") or "", 100) or None
        page = clamp_int(request.args.get("page"), 1, 1, 10_000)
        limit = clamp_int(request.args.get("limit"), 20, 1, 100)
        offset = (page - 1) * limit
        conn = _db()
        tickets = database.get_tickets(conn, status=status, priority=priority, search=search, limit=limit,
                                       offset=offset)
        total = database.count_tickets(conn, status=status, priority=priority, search=search)
        return jsonify({
            "success": True, "tickets": tickets, "total": total, "page": page, "limit": limit,
            "total_pages": max(1, (total + limit - 1) // limit), "stats": database.get_ticket_stats(conn),
        })

    @app.route("/api/agent/tickets/<ticket_id>", methods=["GET"])
    @agent_required()
    def agent_get_ticket(ticket_id):
        conn = _db()
        ticket = database.get_ticket(conn, ticket_id)
        if not ticket:
            return json_error(404, "Not Found", "Ticket not found.")
        messages = database.get_conversation_messages(conn, ticket["conversation_id"]) \
            if ticket.get("conversation_id") else []
        return jsonify({"success": True, "ticket": ticket,
                        "notes": database.get_ticket_notes(conn, ticket["ticket_id"]),
                        "messages": messages})

    @app.route("/api/agent/tickets/<ticket_id>", methods=["PATCH"])
    @agent_required()
    def agent_update_ticket(ticket_id):
        payload = get_json_body()
        if payload is None:
            return json_error(400, "Bad Request", "Invalid JSON payload.")
        conn = _db()
        if not database.get_ticket(conn, ticket_id):
            return json_error(404, "Not Found", "Ticket not found.")

        status = payload.get("status")
        priority = payload.get("priority")
        if status is not None and status not in TICKET_STATUSES:
            return json_error(400, "Bad Request", f"Invalid status. Must be one of: {list(TICKET_STATUSES)}")
        if priority is not None and priority not in TICKET_PRIORITIES:
            return json_error(400, "Bad Request", f"Invalid priority. Must be one of: {list(TICKET_PRIORITIES)}")

        clear_assignee = False
        assigned = None
        if "assigned_agent" in payload:
            value = payload.get("assigned_agent")
            if value in (None, ""):
                clear_assignee = True
            else:
                if not isinstance(value, str) or value.strip().lower() not in database.list_active_agent_usernames(conn):
                    return json_error(400, "Bad Request", "Assign the ticket to an active agent username.")
                assigned = value.strip().lower()

        notes_text = payload.get("resolution_notes")
        if notes_text is not None:
            if not isinstance(notes_text, str) or len(notes_text) > 5000:
                return json_error(400, "Bad Request", "Resolution notes must be text up to 5000 characters.")

        updated = database.update_ticket(conn, ticket_id, status=status, priority=priority,
                                         assigned_agent=assigned, resolution_notes=notes_text,
                                         clear_assignee=clear_assignee)
        changes = []
        if status is not None:
            changes.append(f"status changed to '{status}'")
        if priority is not None:
            changes.append(f"priority changed to '{priority}'")
        if clear_assignee:
            changes.append("unassigned")
        elif assigned is not None:
            changes.append(f"assigned to '{assigned}'")
        if notes_text is not None:
            changes.append("resolution notes updated")
        if changes:
            database.add_ticket_note(conn, ticket_id, g.agent["username"], f"Update: {', '.join(changes)}.")
            database.add_audit(conn, g.agent["username"], "ticket.updated", "ticket", ticket_id,
                               {"changes": changes}, _client_ip())
        return jsonify({"success": True, "ticket": updated, "message": "Ticket updated successfully."})

    @app.route("/api/agent/tickets/<ticket_id>/notes", methods=["POST"])
    @agent_required()
    def agent_add_note(ticket_id):
        payload = get_json_body() or {}
        note = sanitize_text(payload.get("note") or "", 2000, multiline=True)
        if not note:
            return json_error(400, "Bad Request", "Note text cannot be empty.")
        conn = _db()
        if not database.get_ticket(conn, ticket_id):
            return json_error(404, "Not Found", "Ticket not found.")
        note_id = database.add_ticket_note(conn, ticket_id, g.agent["username"], note)
        return jsonify({"success": True, "note_id": note_id, "message": "Note added successfully."}), 201

    @app.route("/api/agent/stats", methods=["GET"])
    @agent_required()
    def agent_stats():
        return jsonify({"success": True, "stats": database.get_ticket_stats(_db())})

    @app.route("/api/agent/orders", methods=["GET"])
    @agent_required()
    def agent_list_orders():
        search = sanitize_text(request.args.get("search") or "", 100) or None
        payment_status = request.args.get("payment_status") or None
        if payment_status not in (None, "All") and payment_status not in database.PAYMENT_STATUSES:
            return json_error(400, "Bad Request", "Unknown payment status filter.")
        page = clamp_int(request.args.get("page"), 1, 1, 10_000)
        limit = clamp_int(request.args.get("limit"), 20, 1, 100)
        conn = _db()
        orders = database.list_orders(conn, search=search, payment_status=payment_status, limit=limit,
                                      offset=(page - 1) * limit)
        total = database.count_orders(conn, search=search, payment_status=payment_status)
        return jsonify({"success": True, "orders": orders, "total": total, "page": page, "limit": limit,
                        "total_pages": max(1, (total + limit - 1) // limit)})

    @app.route("/api/agent/orders/<order_id>", methods=["GET"])
    @agent_required()
    def agent_get_order(order_id):
        conn = _db()
        order = database.get_order(conn, normalize_order_id(order_id) or "")
        if not order:
            return json_error(404, "Not Found", "Order not found.")
        return jsonify({
            "success": True,
            "order": order,
            "payments": database.list_payments_for_order(conn, order["order_id"]),
            "refunds": database.list_refunds_for_order(conn, order["order_id"]),
            "tickets": database.list_tickets_for_order(conn, order["order_id"]),
        })

    @app.route("/api/agent/orders/<order_id>/refunds", methods=["POST"])
    @agent_required()
    def agent_refund_order(order_id):
        payload = get_json_body() or {}
        amount = payload.get("amount_cents")
        if amount is not None and (not isinstance(amount, int) or isinstance(amount, bool)):
            return json_error(400, "Bad Request", "amount_cents must be an integer number of cents.")
        reason = sanitize_text(payload.get("reason") or "", 300)
        if not reason:
            return json_error(400, "Bad Request", "A reason is required for every refund.")
        key = sanitize_text(request.headers.get("Idempotency-Key", ""), 200) or None
        normalized = normalize_order_id(order_id) or ""
        try:
            refund = payments.refund_order(
                _db(), app.payment_provider, app.config, normalized, amount_cents=amount, reason=reason,
                actor=g.agent["username"], is_admin=g.agent["access_level"] == "admin",
                idempotency_key=(f"agent-{key}" if key else None), ip=_client_ip())
        except payments.PaymentError as exc:
            return _service_error(exc)
        return jsonify({"success": True, "refund": refund, "message": "Refund submitted."}), 201

    @app.route("/api/agent/orders/<order_id>/cancel", methods=["POST"])
    @agent_required()
    def agent_cancel_order(order_id):
        try:
            result = payments.cancel_order(_db(), app.payment_provider, app.config,
                                           normalize_order_id(order_id) or "", actor=g.agent["username"],
                                           ip=_client_ip())
        except payments.PaymentError as exc:
            return _service_error(exc)
        return jsonify({"success": True, "result": result})


# ----------------------------------------------------------------------
# Admin API
# ----------------------------------------------------------------------

def _register_admin_api(app):

    @app.route("/api/admin/overview", methods=["GET"])
    @agent_required(admin=True)
    def admin_overview():
        conn = _db()
        totals = conn.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents FROM payments WHERE status = 'succeeded'
        """).fetchone()
        refunded = conn.execute("""
            SELECT COALESCE(SUM(amount_cents), 0) AS cents FROM refunds WHERE status = 'succeeded'
        """).fetchone()
        failed_refunds = conn.execute("SELECT COUNT(*) AS n FROM refunds WHERE status = 'failed'").fetchone()["n"]
        orders_by_payment = {row["payment_status"]: row["n"] for row in conn.execute(
            "SELECT payment_status, COUNT(*) AS n FROM orders GROUP BY payment_status").fetchall()}
        return jsonify({
            "success": True,
            "tickets": database.get_ticket_stats(conn),
            "payments": {
                "succeeded_count": totals["n"],
                "collected_cents": totals["cents"],
                "refunded_cents": refunded["cents"],
                "failed_refunds": failed_refunds,
                "provider": app.payment_provider.name,
                "last_webhook_at": database.last_payment_event_time(conn),
            },
            "orders_by_payment_status": orders_by_payment,
            "system": {
                "version": APP_VERSION,
                "environment": app.config["ENV_NAME"],
                "model_loaded": app.chatbot.pipeline is not None,
                "model_metadata": app.chatbot.model_metadata,
                "agents": len(database.list_agents(conn)),
                "audit_events": database.count_audit(conn),
            },
        })

    @app.route("/api/admin/users", methods=["GET"])
    @agent_required(admin=True)
    def admin_list_users():
        return jsonify({"success": True, "users": database.list_agents(_db())})

    @app.route("/api/admin/users", methods=["POST"])
    @agent_required(admin=True)
    def admin_create_user():
        payload = get_json_body() or {}
        username = sanitize_text(payload.get("username") or "", 32).lower()
        full_name = sanitize_text(payload.get("full_name") or "", 100)
        access = payload.get("access_level", "agent")
        email = (payload.get("email") or "").strip().lower() or None
        password = payload.get("password") if isinstance(payload.get("password"), str) else ""
        if not valid_username(username):
            return json_error(400, "Bad Request", "Username must be 3-32 characters: lowercase letters, digits, . _ -")
        if not full_name:
            return json_error(400, "Bad Request", "Full name is required.")
        if access not in ("admin", "agent"):
            return json_error(400, "Bad Request", "access_level must be 'admin' or 'agent'.")
        if email and not is_valid_email(email):
            return json_error(400, "Bad Request", "Please enter a valid email address.")
        errors = validate_password(password, username, app.config["PASSWORD_MIN_LENGTH"])
        if errors:
            return json_error(400, "Bad Request", " ".join(errors))
        conn = _db()
        if database.get_agent_by_username(conn, username):
            return json_error(409, "Conflict", "That username is already in use.")
        database.create_agent(conn, username, generate_password_hash(password), full_name,
                              access_level=access, role="Administrator" if access == "admin" else "Support Specialist",
                              must_change_password=True, email=email)
        database.add_audit(conn, g.agent["username"], "user.created", "agent", username,
                           {"access_level": access}, _client_ip())
        return jsonify({"success": True, "message": f"User '{username}' created. They must change the password "
                                                    f"at first sign-in."}), 201

    @app.route("/api/admin/users/<username>", methods=["PATCH"])
    @agent_required(admin=True)
    def admin_update_user(username):
        payload = get_json_body() or {}
        conn = _db()
        target = database.get_agent_by_username(conn, username)
        if not target:
            return json_error(404, "Not Found", "User not found.")
        is_self = target["username"] == g.agent["username"]
        updates = {}
        if "full_name" in payload:
            name = sanitize_text(payload.get("full_name") or "", 100)
            if not name:
                return json_error(400, "Bad Request", "Full name cannot be empty.")
            updates["full_name"] = name
        if "email" in payload:
            email = (payload.get("email") or "").strip().lower()
            if email and not is_valid_email(email):
                return json_error(400, "Bad Request", "Please enter a valid email address.")
            updates["email"] = email or None
        if "access_level" in payload:
            if payload["access_level"] not in ("admin", "agent"):
                return json_error(400, "Bad Request", "access_level must be 'admin' or 'agent'.")
            if is_self:
                return json_error(409, "Conflict", "You cannot change your own access level.")
            updates["access_level"] = payload["access_level"]
        if "is_active" in payload:
            if not isinstance(payload["is_active"], bool):
                return json_error(400, "Bad Request", "is_active must be true or false.")
            if is_self and not payload["is_active"]:
                return json_error(409, "Conflict", "You cannot deactivate your own account.")
            updates["is_active"] = 1 if payload["is_active"] else 0
        losing_admin = target["access_level"] == "admin" and target["is_active"] and (
            updates.get("access_level") == "agent" or updates.get("is_active") == 0)
        if losing_admin and database.count_active_admins(conn) <= 1:
            return json_error(409, "Conflict", "At least one active administrator must remain.")
        if not updates:
            return json_error(400, "Bad Request", "No changes were provided.")
        database.update_agent(conn, target["username"], **updates)
        database.add_audit(conn, g.agent["username"], "user.updated", "agent", target["username"],
                           {k: v for k, v in updates.items()}, _client_ip())
        return jsonify({"success": True, "message": "User updated."})

    @app.route("/api/admin/users/<username>/reset-password", methods=["POST"])
    @agent_required(admin=True)
    def admin_reset_password(username):
        conn = _db()
        target = database.get_agent_by_username(conn, username)
        if not target:
            return json_error(404, "Not Found", "User not found.")
        temporary = generate_temporary_password()
        database.update_agent(conn, target["username"], password_hash=generate_password_hash(temporary),
                              must_change_password=1, failed_login_count=0, locked_until=None)
        database.add_audit(conn, g.agent["username"], "user.password_reset", "agent", target["username"],
                           ip_address=_client_ip())
        return jsonify({"success": True, "temporary_password": temporary,
                        "message": "Share this temporary password securely. It is shown only once."})

    @app.route("/api/admin/payments", methods=["GET"])
    @agent_required(admin=True)
    def admin_list_payments():
        status = request.args.get("status") or None
        if status not in (None, "All") and status not in ("pending", "succeeded", "failed", "expired"):
            return json_error(400, "Bad Request", "Unknown payment status filter.")
        page = clamp_int(request.args.get("page"), 1, 1, 10_000)
        limit = clamp_int(request.args.get("limit"), 20, 1, 100)
        conn = _db()
        rows = database.list_payments(conn, status=status, limit=limit, offset=(page - 1) * limit)
        total = database.count_payments(conn, status=status)
        return jsonify({"success": True, "payments": rows, "total": total, "page": page, "limit": limit,
                        "total_pages": max(1, (total + limit - 1) // limit)})

    @app.route("/api/admin/audit", methods=["GET"])
    @agent_required(admin=True)
    def admin_audit_log():
        page = clamp_int(request.args.get("page"), 1, 1, 10_000)
        limit = clamp_int(request.args.get("limit"), 50, 1, 200)
        conn = _db()
        return jsonify({"success": True, "entries": database.list_audit(conn, limit=limit, offset=(page - 1) * limit),
                        "total": database.count_audit(conn), "page": page, "limit": limit})


if __name__ == "__main__":
    flask_env = os.environ.get("FLASK_ENV", "development")
    application = create_app(flask_env)
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n* Customer portal:  http://{host}:{port}")
    print(f"* Agent portal:     http://{host}:{port}/agent/login")
    print(f"* Admin console:    http://{host}:{port}/admin\n")
    application.run(host=host, port=port, debug=application.config["DEBUG"])
