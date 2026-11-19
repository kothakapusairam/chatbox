# AI Customer Support

A customer support platform: an ML-powered chatbot for customers, a support console for agents,
an admin console, and hosted payments with refunds. Built with Flask, scikit-learn and SQLite.

## Features

- **Chatbot** (scikit-learn TF-IDF word + character n-grams → logistic regression, 21 intents) with a
  knowledge base, multi-turn conversations, and automatic escalation to human tickets.
- **Verified order lookups.** Order details are shared only after the customer gives the order ID *and*
  the email used at checkout. Wrong and unknown orders receive identical replies, so order IDs cannot be
  enumerated.
- **Payments:** hosted checkout links (Stripe Checkout, so card data never touches this app), webhook
  processing with signature verification and idempotency, full and partial refunds, and automatic refunds
  when a paid order is cancelled before dispatch. If an automatic refund fails, a High-priority ticket is
  opened so money is never silently left behind.
- **Agent console:** ticket queue with filters and pagination, transcripts, internal notes, assignment,
  status and priority, plus an orders panel (payments, refunds, cancellation, linked tickets).
- **Admin console:** overview metrics, user management (create, role, deactivate, password reset), payments
  list, and an audit log of every sensitive action.
- **Security:** CSRF tokens on state-changing requests, account lockout after repeated failures, forced
  password change for new and reset accounts, idle session timeout, rate limits, strict Content Security
  Policy, and output escaping throughout the front end.
- **Operations:** versioned schema with in-place upgrades, health endpoint, request IDs, consistent online
  backups, a CLI for creating agents, Dockerfile, and CI.

## Quick start (development)

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

On first start an administrator account `admin` is created. Its random initial password is written to
`instance/initial_admin_password.txt` (readable only by you). Sign in at <http://127.0.0.1:5000/agent/login>.
You will be asked to choose a new password. Delete the file afterwards.

- Customer chat: <http://127.0.0.1:5000/>
- Agent console: <http://127.0.0.1:5000/agent/login>
- Admin console: <http://127.0.0.1:5000/admin> (administrators only)

Demo orders (seeded in development): `ORD1025`–`ORD1031`. Use the matching customer email, for example
`ORD1025` with `john.doe@example.com`. Payments run in the **sandbox** provider, which simulates the
checkout page locally.

## Configuration

All settings are environment variables. See `.env.example`.

| Variable | Purpose |
|---|---|
| `FLASK_ENV` | `development`, `testing` or `production` |
| `SECRET_KEY` | Required in production (32+ random characters) |
| `PUBLIC_BASE_URL` | Public URL, used in payment return links (`https://` in production) |
| `DATABASE_PATH` | SQLite file location |
| `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` | First admin (required in production) |
| `PAYMENT_PROVIDER` | `sandbox` (dev/test only) or `stripe` |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Stripe credentials |
| `AGENT_REFUND_LIMIT_CENTS` | Largest refund an agent may issue (admins are unlimited) |
| `TRUST_PROXY_HEADERS` | Set `true` only behind a proxy that sets `X-Forwarded-For` |
| `SESSION_COOKIE_SECURE`, `SESSION_HOURS`, `AGENT_IDLE_TIMEOUT_MINUTES` | Session behaviour |
| `CONFIDENCE_THRESHOLD`, `MAX_MESSAGE_LENGTH` | Chatbot tuning |

The app refuses to start in production if the secret key, HTTPS base URL, first admin password, or
Stripe keys are missing, or if the sandbox provider is selected.

## Production deployment

1. Copy `.env.example` to `.env` and set the values. Keep it out of version control.
2. Build and run the container:
   ```bash
   docker build -t ai-support .
   docker run -d --name ai-support -p 8000:8000 --env-file .env -v support-data:/data ai-support
   ```
   Put it behind a TLS-terminating reverse proxy (nginx, Caddy, a cloud load balancer). Set
   `TRUST_PROXY_HEADERS=true` only in that case.
3. Create agents from the command line (password is prompted, not passed as an argument):
   ```bash
   docker exec -it ai-support python scripts/create_agent.py --username jdoe --name "Jane Doe" --admin
   ```
4. Back up regularly. The backup uses SQLite's online backup API and verifies integrity:
   ```bash
   python scripts/backup_db.py --dest /backups --keep 14
   ```
5. Monitor `GET /health`. It returns 503 if the database is unreachable.

### Connecting Stripe

1. In the Stripe dashboard, create a webhook endpoint at `https://<your-domain>/api/payments/webhook`.
   Subscribe to `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
   `checkout.session.async_payment_failed`, `checkout.session.expired`, `refund.created` and `refund.updated`.
2. Set `PAYMENT_PROVIDER=stripe`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` and `PUBLIC_BASE_URL`.
3. Test with Stripe's test keys first. Stripe's CLI (`stripe listen --forward-to localhost:5000/api/payments/webhook`)
   forwards test events to a dev server.

Payment status changes only through webhooks. The browser return page is informational. Duplicate webhook
deliveries are ignored, and amount or currency mismatches are refused and audited.

## Security model

| Area | Control |
|---|---|
| Authentication | Salted scrypt password hashes; lockout after `LOGIN_MAX_ATTEMPTS` failures; generic error messages |
| Passwords | Minimum 10 characters with a letter and a digit; cannot equal the username; forced change for new and reset accounts |
| Sessions | HTTP-only, SameSite=Lax cookies; regenerated at sign-in; idle timeout; deactivated accounts lose access immediately |
| Authorisation | Agent and admin roles; last-admin and self-lockout safeguards; refund limits for agents |
| CSRF | Synchroniser token on every state-changing agent and admin request |
| Abuse | Per-IP sliding-window rate limits on chat, ticket creation, lookups, feedback, login and checkout |
| Data exposure | Order and payment data only after identity verification; ticket lookups require the ticket's email; internal notes never public |
| Browser | Strict CSP (`script-src 'self'`, no inline scripts), frame-ancestors none, nosniff, referrer policy; customer text rendered with DOM APIs only |
| Payments | Card data handled by the provider (hosted checkout); signed webhooks with timestamp tolerance; idempotency keys on every provider write |
| Audit | Logins, failures, ticket changes, refunds, cancellations, payments, and user administration are logged with actor, target and IP |

## Architecture

```
app.py            Flask factory, pages, JSON APIs, auth decorator, error handling
chatbot.py        Intent classification, order verification flows, escalation
payments.py       Provider adapters (Stripe, sandbox), checkout, webhooks, refunds, cancellation
database.py       SQLite schema, in-place migrations, data access (no commits; request-scoped)
security.py       CSRF, rate limiting, password policy, security headers
utils.py          Validation and formatting helpers
config.py         Environment-driven configuration and production checks
train_model.py    Trains and evaluates the intent model; writes model_metadata.json
intents.json      Training phrasings     knowledge_base.json  Approved answers
templates/        Pages               static/js/        Front-end scripts (no inline JavaScript)
scripts/          create_agent.py, backup_db.py
tests/            Pytest suite         verify_all.py      End-to-end smoke check
```

Each request uses one database connection. It commits when the request completes and rolls back on an
unhandled error, so a multi-step change (for example a refund and its order update) is all-or-nothing.

## Model

The intent model is trained from `intents.json`. Retrain after editing the data:

```bash
python train_model.py
```

Current quality (see `models/model_metadata.json`): 5-fold cross-validated accuracy about **83.6%**
(±2.5), held-out macro F1 about **0.85**. The model is good at common phrasings and weaker on short,
ambiguous messages, which is why the bot asks clarifying questions and escalates after repeated
misunderstandings. Improve accuracy by adding real customer phrasings to `intents.json`, then retrain.

## Tests and verification

```bash
pytest                       # 109 unit and integration tests
python verify_all.py         # end-to-end journey check (prints PASS/FAIL per step)
for f in static/js/*.js; do node --check "$f"; done
```

CI (`.github/workflows/ci.yml`) runs all three on every push.

The suite covers chatbot journeys, authentication and lockout, CSRF, authorisation, rate limits, input
validation, webhook signatures and idempotency, checkout, partial and full refunds, refund limits, failed
refund escalation, legacy database upgrades, and model quality.

## Upgrading from version 1

Point the new version at your existing `DATABASE_PATH`. The schema is upgraded in place; no rows are
deleted. On first start:

- Missing columns are added and existing orders get amounts in cents, a payment status, and a contact email.
- Any account still using the published defaults (`admin123`, `agent123`) is flagged to change its password
  at next sign-in. Remove the demo accounts you no longer need from the admin console.
- Version 1 reported cancellations as refunded, but no payment system existed. Orders cancelled under
  version 1 have no payment record, so there is nothing to reconcile automatically. Check them in the
  orders panel.

`ACADEMIC_DOCUMENTATION.md` describes version 1 and has not been updated.

## Known limitations

- **Rate limits are per process.** With several servers or workers, use a shared store (Redis) for the
  limiter. The included gunicorn setup runs two workers on one host.
- **SQLite** suits one host with moderate traffic. Move to PostgreSQL for multiple hosts (the data layer
  uses plain SQL, so the port is contained to `database.py`).
- **Notifications** (email on ticket updates, return labels) are not implemented. The data model and
  ticket notes are ready for them.
- **Customer accounts** do not exist. Customers verify with the order ID and checkout email each time.
- **Demo data** (orders, emails) is for development only. Set `SEED_DEMO_DATA=false` in production
  (the default there).
- **Order and return rules** are simplified: returns are accepted for 30 days after `estimated_delivery`
  when the order's `return_eligible` flag is set. Connect your order system to replace the demo table.
- Stripe integration was tested against the documented request and webhook formats with simulated
  responses. Run it end to end with Stripe test mode before going live.
