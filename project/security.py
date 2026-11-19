"""
Security primitives: CSRF tokens, per-client rate limiting, password policy,
response security headers and JSON error helpers.
"""
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque

from flask import current_app, jsonify, request, session

from utils import USERNAME_RE

_PASSWORD_LETTER = re.compile(r"[A-Za-z]")
_PASSWORD_DIGIT = re.compile(r"\d")


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------

def json_error(status, error, message, **extra):
    body = {"success": False, "error": error, "message": message}
    body.update(extra)
    response = jsonify(body)
    response.status_code = status
    return response


# ----------------------------------------------------------------------
# CSRF protection (synchroniser token pattern)
# ----------------------------------------------------------------------

def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_is_valid():
    expected = session.get("csrf_token")
    provided = request.headers.get("X-CSRF-Token", "")
    return bool(expected) and bool(provided) and hmac.compare_digest(expected, provided)


# ----------------------------------------------------------------------
# Rate limiting (sliding window, in-process)
# ----------------------------------------------------------------------

class RateLimiter:
    """
    Sliding-window limiter. It is process-local: with several gunicorn workers or
    several servers, use a shared store (Redis) instead. See README.
    """

    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key, limit, window_seconds):
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            while bucket and now - bucket[0] > window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = int(window_seconds - (now - bucket[0])) + 1
                return False, retry_after
            bucket.append(now)
            if len(self._hits) > 50_000:  # bound memory under attack
                self._hits.clear()
            return True, 0

    def reset(self):
        with self._lock:
            self._hits.clear()


rate_limiter = RateLimiter()


def client_ip():
    if current_app.config.get("TRUST_PROXY_HEADERS"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return (request.remote_addr or "unknown")[:64]


def check_rate_limit(bucket):
    """Return a 429 response if the caller exceeded the bucket's limit, else None."""
    limit, window = current_app.config["RATE_LIMITS"][bucket]
    allowed, retry_after = rate_limiter.hit(f"{bucket}:{client_ip()}", limit, window)
    if allowed:
        return None
    response = json_error(429, "Too Many Requests",
                          "You are sending requests too quickly. Please wait a moment and try again.")
    response.headers["Retry-After"] = str(retry_after)
    return response


# ----------------------------------------------------------------------
# Passwords
# ----------------------------------------------------------------------

def validate_password(password, username=None, min_length=10):
    """Return a list of human-readable policy violations (empty list means acceptable)."""
    errors = []
    if not isinstance(password, str) or len(password) < min_length:
        errors.append(f"Password must be at least {min_length} characters long.")
    if password and len(password) > 256:
        errors.append("Password must be at most 256 characters long.")
    if password and not _PASSWORD_LETTER.search(password):
        errors.append("Password must contain at least one letter.")
    if password and not _PASSWORD_DIGIT.search(password):
        errors.append("Password must contain at least one digit.")
    if username and password and password.lower() == username.lower():
        errors.append("Password must not be the same as the username.")
    return errors


def generate_temporary_password(length=16):
    """Random password that satisfies the policy (used for resets and bootstrap)."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if _PASSWORD_LETTER.search(candidate) and _PASSWORD_DIGIT.search(candidate):
            return candidate


def valid_username(username):
    return bool(username) and bool(USERNAME_RE.match(username))


# ----------------------------------------------------------------------
# Headers
# ----------------------------------------------------------------------

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def apply_security_headers(response, secure_cookies):
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if secure_cookies:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path.startswith(("/api/", "/agent", "/admin")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response

