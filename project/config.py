"""
Application configuration.

Every secret and deployment-specific value is read from environment variables.
ProductionConfig is validated at start-up (see validate_config) so the app will
refuse to run with insecure defaults.
"""
import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    ENV_NAME = "development"
    APP_NAME = "AI Customer Support"
    DEBUG = False
    TESTING = False

    # Sessions & cookies
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SESSION_COOKIE_NAME = "cs_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=_env_int("SESSION_HOURS", 8))
    AGENT_IDLE_TIMEOUT_MINUTES = _env_int("AGENT_IDLE_TIMEOUT_MINUTES", 60)
    MAX_CONTENT_LENGTH = 64 * 1024  # reject request bodies larger than 64 KB

    # Storage & model artefacts
    DATABASE_PATH = os.environ.get("DATABASE_PATH", str(INSTANCE_DIR / "customer_support.db"))
    MODEL_PATH = os.environ.get("MODEL_PATH", str(BASE_DIR / "models" / "intent_model.joblib"))
    METADATA_PATH = os.environ.get("METADATA_PATH", str(BASE_DIR / "models" / "model_metadata.json"))
    INTENTS_PATH = os.environ.get("INTENTS_PATH", str(BASE_DIR / "intents.json"))
    KNOWLEDGE_BASE_PATH = os.environ.get("KNOWLEDGE_BASE_PATH", str(BASE_DIR / "knowledge_base.json"))

    # Chatbot behaviour
    CONFIDENCE_THRESHOLD = _env_float("CONFIDENCE_THRESHOLD", 0.40)
    MAX_MESSAGE_LENGTH = _env_int("MAX_MESSAGE_LENGTH", 500)
    MAX_DESCRIPTION_LENGTH = 2000
    RETURN_WINDOW_DAYS = 30

    # Bootstrap admin. If BOOTSTRAP_ADMIN_PASSWORD is unset in development, a random
    # password is generated and written to instance/initial_admin_password.txt.
    BOOTSTRAP_ADMIN_USERNAME = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "admin")
    BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD")
    SEED_DEMO_DATA = _env_bool("SEED_DEMO_DATA", True)
    DEMO_MODE = _env_bool("DEMO_MODE", True)  # shows demo hints on the login page
    PASSWORD_MIN_LENGTH = 10

    # Authentication hardening
    LOGIN_MAX_ATTEMPTS = _env_int("LOGIN_MAX_ATTEMPTS", 5)
    LOGIN_LOCKOUT_MINUTES = _env_int("LOGIN_LOCKOUT_MINUTES", 15)
    TRUST_PROXY_HEADERS = _env_bool("TRUST_PROXY_HEADERS", False)
    AGENT_REFUND_LIMIT_CENTS = _env_int("AGENT_REFUND_LIMIT_CENTS", 10000)

    # Rate limits: bucket -> (max requests, window in seconds), per client IP
    RATE_LIMITS = {
        "chat": (60, 60),
        "ticket_create": (10, 600),
        "ticket_lookup": (30, 60),
        "feedback": (30, 60),
        "login": (20, 300),
        "checkout": (10, 600),
    }

    # Payments
    PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "sandbox").strip().lower()
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
    STRIPE_API_BASE = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com")
    CURRENCY = os.environ.get("CURRENCY", "usd").strip().lower()
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
    CHECKOUT_TTL_MINUTES = _env_int("CHECKOUT_TTL_MINUTES", 60)


class DevelopmentConfig(Config):
    ENV_NAME = "development"
    DEBUG = True


class TestingConfig(Config):
    ENV_NAME = "testing"
    TESTING = True
    DEBUG = True
    DEMO_MODE = False
    CONFIDENCE_THRESHOLD = 0.35
    BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "TestAdmin-2026")


class ProductionConfig(Config):
    ENV_NAME = "production"
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    DEMO_MODE = False
    SEED_DEMO_DATA = _env_bool("SEED_DEMO_DATA", False)


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}


def validate_config(cfg):
    """
    Return a list of configuration problems. In production, any problem is fatal.
    """
    problems = []
    if cfg.ENV_NAME == "production":
        if not cfg.SECRET_KEY or len(cfg.SECRET_KEY) < 32:
            problems.append("SECRET_KEY must be set to at least 32 random characters in production.")
        if not cfg.PUBLIC_BASE_URL.startswith("https://"):
            problems.append("PUBLIC_BASE_URL must use https:// in production.")
        if cfg.PAYMENT_PROVIDER == "sandbox":
            problems.append("PAYMENT_PROVIDER=sandbox is not allowed in production; use 'stripe'.")
        if cfg.PAYMENT_PROVIDER == "stripe" and (not cfg.STRIPE_SECRET_KEY or not cfg.STRIPE_WEBHOOK_SECRET):
            problems.append("STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET are required for Stripe.")
    if cfg.PAYMENT_PROVIDER not in ("sandbox", "stripe"):
        problems.append(f"Unknown PAYMENT_PROVIDER '{cfg.PAYMENT_PROVIDER}'. Use 'sandbox' or 'stripe'.")
    if not (0.0 < cfg.CONFIDENCE_THRESHOLD < 1.0):
        problems.append("CONFIDENCE_THRESHOLD must be between 0 and 1.")
    return problems
