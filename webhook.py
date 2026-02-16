import os
import sqlite3
import time
from typing import Optional, Dict, Any

import stripe
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

# =========================
# ENV
# =========================
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_WEBHOOK_TOKEN = os.environ.get("APP_WEBHOOK_TOKEN", "")  # shared secret between Streamlit and this backend
DB_PATH = os.environ.get("DB_PATH", "offertly.db")

PRICE_ID_STARTER = os.environ.get("STRIPE_PRICE_ID_STARTER", "")
PRICE_ID_PRO = os.environ.get("STRIPE_PRICE_ID_PRO", "")
PRICE_ID_TEAM = os.environ.get("STRIPE_PRICE_ID_TEAM", "")

APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")  # Streamlit base url (optional)
PORT = int(os.environ.get("PORT", "8080"))

if not STRIPE_SECRET_KEY:
    print("WARNING: STRIPE_SECRET_KEY is missing")
stripe.api_key = STRIPE_SECRET_KEY


# =========================
# DB helpers
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                stripe_customer_id TEXT,
                subscription_id TEXT,
                plan TEXT DEFAULT 'free',
                status TEXT DEFAULT 'inactive',
                current_period_end INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                updated_at INTEGER DEFAULT (strftime('%s','now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stripe_event_id TEXT UNIQUE,
                type TEXT,
                email TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
            """
        )
        conn.commit()


def upsert_user(
    email: str,
    plan: Optional[str] = None,
    status: Optional[str] = None,
    stripe_customer_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
    current_period_end: Optional[int] = None,
) -> None:
    now = int(time.time())
    with db() as conn:
        # Create if not exists
        conn.execute(
            """
            INSERT INTO users(email, created_at, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (email.lower().strip(), now, now),
        )
        # Update fields if provided
        if plan is not None:
            conn.execute("UPDATE users SET plan=?, updated_at=? WHERE email=?", (plan, now, email))
        if status is not None:
            conn.execute("UPDATE users SET status=?, updated_at=? WHERE email=?", (status, now, email))
        if stripe_customer_id is not None:
            conn.execute(
                "UPDATE users SET stripe_customer_id=?, updated_at=? WHERE email=?",
                (stripe_customer_id, now, email),
            )
        if subscription_id is not None:
            conn.execute(
                "UPDATE users SET subscription_id=?, updated_at=? WHERE email=?",
                (subscription_id, now, email),
            )
        if current_period_end is not None:
            conn.execute(
                "UPDATE users SET current_period_end=?, updated_at=? WHERE email=?",
                (int(current_period_end), now, email),
            )
        conn.commit()


def get_user(email: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()
        return dict(row) if row else None


def record_event(stripe_event_id: str, event_type: str, email: Optional[str]) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO events(stripe_event_id, type, email) VALUES(?,?,?)",
            (stripe_event_id, event_type, email),
        )
        conn.commit()


# =========================
# Auth for internal API
# =========================
def require_internal_token() -> None:
    if not APP_WEBHOOK_TOKEN:
        # If you forget to set it, we should fail closed.
        abort(401, "APP_WEBHOOK_TOKEN missing on backend")
    auth = request.headers.get("Authorization", "")
    # Expected: "Bearer <token>"
    parts = auth.split(" ", 1)
    token = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
    if token != APP_WEBHOOK_TOKEN:
        abort(401, "Unauthorized")


@app.before_request
def guard_api() -> None:
    if request.path.startswith("/api/"):
        require_internal_token()


# =========================
# Utils
# =========================
def plan_from_price(price_id: str) -> str:
    if price_id == PRICE_ID_STARTER:
        return "starter"
    if price_id == PRICE_ID_PRO:
        return "pro"
    if price_id == PRICE_ID_TEAM:
        return "team"
    return "unknown"


# =========================
# Public endpoints
# =========================
@app.get("/health")
def health():
    return jsonify({"ok": True})


# =========================
# Internal API for Streamlit
# =========================
@app.get("/api/prices")
def api_prices():
    # Streamlit uses this to render packages
    return jsonify(
        {
            "starter": {"price_id": PRICE_ID_STARTER, "label": "Starter"},
            "pro": {"price_id": PRICE_ID_PRO, "label": "Pro"},
            "team": {"price_id": PRICE_ID_TEAM, "label": "Team"},
        }
    )


@app.get("/api/status")
def api_status():
    email = request.args.get("email", "").strip().lower()
    if not email:
        return jsonify({"error": "missing email"}), 400
    user = get_user(email)
    if not user:
        return jsonify({"email": email, "plan": "free", "status": "inactive"})
    return jsonify(
        {
            "email": user["email"],
            "plan": user["plan"],
            "status": user["status"],
            "current_period_end": user["current_period_end"],
        }
    )


@app.post("/api/create-checkout-session")
def api_create_checkout_session():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    price_id = (data.get("price_id") or "").strip()

    if not email:
        return jsonify({"error": "missing email"}), 400
    if not price_id:
        return jsonify({"error": "missing price_id"}), 400
    if not APP_BASE_URL:
        return jsonify({"error": "APP_BASE_URL missing (Streamlit URL)"}), 500

    # create checkout session for subscription
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer_email=email,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{APP_BASE_URL}/?success=1",
            cancel_url=f"{APP_BASE_URL}/?canceled=1",
            allow_promotion_codes=True,
        )
        # pre-create user row so status page works
        upsert_user(email=email, status="pending")
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================
# Stripe Webhook
# =========================
@app.post("/stripe/webhook")
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return "STRIPE_WEBHOOK_SECRET missing", 500

    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print("Webhook signature verify failed:", e)
        return "bad signature", 400

    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    # We try to find email safely:
    email = None
    customer_id = obj.get("customer")

    # checkout.session.completed contains customer_email
    if event_type == "checkout.session.completed":
        email = obj.get("customer_details", {}).get("email") or obj.get("customer_email")

    # subscription events might not contain email; fetch customer if needed
    if not email and customer_id:
        try:
            cust = stripe.Customer.retrieve(customer_id)
            email = (cust.get("email") or "").strip().lower() or None
        except Exception:
            email = None

    # idempotency: store event id
    record_event(event.get("id", ""), event_type, email)

    try:
        if event_type == "checkout.session.completed":
            # subscription should be created after this; but we can mark pending -> active if subscription already attached
            if email:
                upsert_user(email=email, stripe_customer_id=customer_id, status="active")

        elif event_type in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
            # obj is a Subscription
            subscription_id = obj.get("id")
            status = obj.get("status")  # active, trialing, past_due, canceled, unpaid...
            current_period_end = obj.get("current_period_end")  # <-- ONLY exists on subscription object
            items = (obj.get("items") or {}).get("data") or []
            price_id = None
            if items and isinstance(items, list):
                price_id = ((items[0].get("price") or {}).get("id")) if items[0] else None

            plan = plan_from_price(price_id or "")

            if email:
                # set active only if status indicates it
                normalized_status = "active" if status in ("active", "trialing") else status
                upsert_user(
                    email=email,
                    stripe_customer_id=customer_id,
                    subscription_id=subscription_id,
                    plan=plan if plan != "unknown" else None,
                    status=normalized_status,
                    current_period_end=current_period_end if current_period_end else None,
                )

        elif event_type == "invoice.paid":
            # invoice paid means subscription should be active
            if email:
                upsert_user(email=email, status="active")

        elif event_type == "invoice.payment_failed":
            if email:
                upsert_user(email=email, status="past_due")

        # else: ignore other events

    except Exception as e:
        print("Webhook handler error:", e)
        return "handler error", 500

    return "ok", 200


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT)





