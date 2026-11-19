"""Small, dependency-free helpers shared across the application."""
import re
import secrets
from datetime import datetime, timezone

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}$")
EMAIL_IN_TEXT_RE = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}\b")
ORDER_ID_RE = re.compile(r"\bORD-?(\d{3,8})\b", re.IGNORECASE)
ORDER_ID_BARE_RE = re.compile(r"^\s*(?:ORD-?)?(\d{3,8})\s*$", re.IGNORECASE)
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
CURRENCY_SYMBOLS = {"usd": "$", "eur": "€", "gbp": "£", "inr": "₹"}


def utc_now_iso():
    """Current UTC time as an ISO-8601 string (stored as TEXT in SQLite)."""
    return datetime.now(timezone.utc).isoformat()


def new_ticket_id():
    """Human-readable, unguessable ticket identifier such as TKT-3F9A1C."""
    return f"TKT-{secrets.token_hex(3).upper()}"


def extract_order_id(text):
    """Return a normalised order id (e.g. ORD1025) found in free text, or None."""
    match = ORDER_ID_RE.search(text or "")
    return f"ORD{match.group(1)}" if match else None


def parse_bare_order_id(text):
    """Accept a message that is only an order number, such as '1025' or 'ORD-1025'."""
    match = ORDER_ID_BARE_RE.match(text or "")
    return f"ORD{match.group(1)}" if match else None


def normalize_order_id(raw):
    """Normalise a user-supplied order id to the canonical ORDnnnn form."""
    if not raw or not isinstance(raw, str):
        return None
    match = re.fullmatch(r"\s*(?:ORD)?-?(\d{3,8})\s*", raw, re.IGNORECASE)
    return f"ORD{match.group(1)}" if match else None


def extract_email(text):
    """Return the first email address found in free text (lower-cased), or None."""
    match = EMAIL_IN_TEXT_RE.search(text or "")
    return match.group(0).lower() if match else None


def is_valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and bool(EMAIL_RE.match(value.strip()))


def emails_match(a, b):
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


def clamp_int(value, default, minimum, maximum):
    """Parse an integer query/body parameter safely, clamped to a range."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def sanitize_text(value, max_length, multiline=False):
    """Strip control characters and enforce a length limit on user text."""
    if not isinstance(value, str):
        return ""
    allowed_control = "\n\t" if multiline else ""
    cleaned = "".join(ch for ch in value if ch.isprintable() or ch in allowed_control)
    return cleaned.strip()[:max_length]


def format_cents(cents, currency="usd"):
    """Format an integer amount in minor units (cents) for display."""
    symbol = CURRENCY_SYMBOLS.get((currency or "usd").lower())
    amount = (cents or 0) / 100
    if symbol:
        return f"{symbol}{amount:,.2f}"
    return f"{amount:,.2f} {(currency or 'usd').upper()}"
