import os
import time
import json
import sqlite3
from functools import wraps

import stripe
from flask import Flask, request, jsonify

# ----------------------------
# App / Config
# ----------------------------
app = Flask(__name__)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

# Token mellan Streamlit <-> backend (stödjer båda namn)
APP_API_TOKEN = (
    os.environ.get("APP_API_TOKEN", "").strip()
    or os.environ.get("APP_WEBHOOK_TOKEN", "").strip()
)

# Price IDs i backend (för plan mapping)
STRIPE_PRICE_ID_STARTER = os.environ.get("STRIPE_PRICE_ID_STARTER", "").strip()
STRIPE_PRICE_ID_PRO = os.environ.get("STRIPE_PRICE_ID_PRO", "").strip()
STRIPE_PRICE_ID_TEAM = os.environ.get("STRIPE_PRICE_ID_TEAM", "").strip()

DB_PATH = os.environ.get("DB_PATH", "offertly.db")

if not STRIPE_SECRET_KEY:
    print("WARN: STRIPE_SECRET_KEY missing")
stripe.api_key = STRIPE_SECRET_KEY


# ----------------------------
# DB
# ----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                email TEXT PRIMARY KEY,
                customer_id TEXT,
                subscription_id TEXT,
                status TEXT,
                current_period_end INTEGER,
                plan TEXT,
                price_id TEXT,
                updated_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage (
                email TEXT PRIMARY KEY,
                free_quotes_used INTEGER DEFAULT 0,
                created_at INTEGER,
                updated_at INTEGER
            )
            """
        )
        conn.commit()


init_db()


def now_ts() -> int:
    return int(time.time())


def plan_from_price(price_id: str | None) -> str | None:
    if not price_id:
        return None
    if STRIPE_PRICE_ID_STARTER and price_id == STRIPE_PRICE_ID_STARTER:
        return "starter"
    if STRIPE_PRICE_ID_PRO and price_id == STRIPE_PRICE_ID_PRO:
        return "pro"
    if STRIPE_PRICE_ID_TEAM and price_id == STRIPE_PRICE_ID_TEAM:
        return "team"
    return None


def upsert_subscription(
    email: str,
    customer_id: str | None,
    subscription_id: str | None,
    status: str | None,
    current_period_end: int | None,
    plan: str | None,
    price_id: str | None,
):
    email = (email or "").strip().lower()
    t = now_ts()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions(email, customer_id, subscription_id, status, current_period_end, plan, price_id, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                customer_id=excluded.customer_id,
                subscription_id=excluded.subscription_id,
                status=excluded.status,
                current_period_end=excluded.current_period_end,
                plan=excluded.plan,
                price_id=excluded.price_id,
                updated_at=excluded.updated_at
            """,
            (email, customer_id, subscription_id, status, current_period_end, plan, price_id, t),
        )
        conn.commit()


def get_subscription(email: str):
    email = (email or "").strip().lower()
    with db() as conn:
        row = conn.execute("SELECT * FROM subscriptions WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def is_active(sub: dict | None) -> bool:
    if not sub:
        return False
    status = (sub.get("status") or "").lower()
    cpe = int(sub.get("current_period_end") or 0)
    n = now_ts()
    # Om cpe saknas men status är active, räkna ändå som aktiv (Stripe kan vara inkonsekvent beroende på event)
    if status in ("active", "trialing"):
        return True if cpe == 0 else (cpe > n)
    return False


def get_or_create_usage(email: str):
    email = (email or "").strip().lower()
    t = now_ts()
    with db() as conn:
        row = conn.execute("SELECT * FROM usage WHERE email = ?", (email,)).fetchone()
        if row:
            return dict(row)
        conn.execute(
            "INSERT INTO usage(email, free_quotes_used, created_at, updated_at) VALUES(?, ?, ?, ?)",
            (email, 0, t, t),
        )
        conn.commit()
    return {"email": email, "free_quotes_used": 0, "created_at": t, "updated_at": t}


def increment_free_quotes(email: str, max_free: int = 3):
    email = (email or "").strip().lower()
    t = now_ts()
    with db() as conn:
        row = conn.execute("SELECT free_quotes_used FROM usage WHERE email = ?", (email,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO usage(email, free_quotes_used, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (email, 0, t, t),
            )
            current = 0
        else:
            current = int(row["free_quotes_used"] or 0)

        if current >= max_free:
            return current, False

        new_val = current + 1
        conn.execute(
            "UPDATE usage SET free_quotes_used = ?, updated_at = ? WHERE email = ?",
            (new_val, t, email),
        )
        conn.commit()

    return new_val, True


# ----------------------------
# Auth: Streamlit <-> Backend
# ----------------------------
def require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not APP_API_TOKEN:
            return jsonify({"error": "Server not configured (APP_API_TOKEN missing)"}), 500

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized (missing bearer token)"}), 401

        token = auth.replace("Bearer ", "", 1).strip()
        if token != APP_API_TOKEN:
            return jsonify({"error": "Unauthorized (bad token)"}), 401

        return fn(*args, **kwargs)

    return wrapper


# ----------------------------
# Health
# ----------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True})


# ----------------------------
# API: subscription status
# ----------------------------
@app.get("/api/subscription")
@require_token
def api_subscription():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    sub = get_subscription(email)
    return jsonify(
        {
            "email": email,
            "found": bool(sub),
            "active": is_active(sub),
            "plan": (sub or {}).get("plan"),
            "subscription": sub,
        }
    )


# ----------------------------
# API: usage (3 free quotes)
# ----------------------------
@app.get("/api/usage")
@require_token
def api_usage():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    u = get_or_create_usage(email)
    return jsonify({"email": email, "free_quotes_used": int(u.get("free_quotes_used") or 0), "free_quotes_max": 3})


@app.post("/api/usage/increment")
@require_token
def api_usage_increment():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    # Om kunden är aktiv betalande → vi incrementar inte, men returnerar "allowed"
    sub = get_subscription(email)
    if is_active(sub):
        return jsonify({"email": email, "allowed": True, "free_quotes_used": None, "free_quotes_max": 3, "reason": "subscription_active"})

    used, ok = increment_free_quotes(email, max_free=3)
    return jsonify({"email": email, "allowed": ok, "free_quotes_used": used, "free_quotes_max": 3})


# ----------------------------
# API: create checkout session
# ----------------------------
@app.post("/api/create-checkout-session")
@require_token
def create_checkout_session():
    """
    Body JSON:
      {
        "email": "din@email.se",
        "price_id": "price_...",
        "success_url": "https://din-streamlit-app?success=1",
        "cancel_url": "https://din-streamlit-app?cancel=1"
      }
    """
    data = request.get_json(force=True, silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    price_id = (data.get("price_id") or "").strip()
    success_url = (data.get("success_url") or "").strip()
    cancel_url = (data.get("cancel_url") or "").strip()

    if not (email and price_id and success_url and cancel_url):
        return jsonify({"error": "email, price_id, success_url, cancel_url required"}), 400

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer_email=email,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
            client_reference_id=email,
            metadata={"email": email, "price_id": price_id},
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ----------------------------
# Stripe Webhook
# ----------------------------
@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            # DEV fallback (inte rekommenderat i prod)
            event = json.loads(payload.decode("utf-8"))
    except Exception as e:
        print("Webhook signature verify failed:", e)
        return "Bad Request", 400

    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    def upsert_from_subscription_id(subscription_id: str, email_hint: str | None = None, customer_id_hint: str | None = None):
        sub = stripe.Subscription.retrieve(subscription_id)
        status = sub.get("status")
        cpe = sub.get("current_period_end")
        customer_id = sub.get("customer") or customer_id_hint

        items = (sub.get("items") or {}).get("data") or []
        price_id = None
        if items and isinstance(items, list):
            price = (items[0] or {}).get("price") or {}
            price_id = price.get("id")

        plan = plan_from_price(price_id)

        email = None
        if customer_id:
            cust = stripe.Customer.retrieve(customer_id)
            email = cust.get("email")

        email = (email or email_hint or "").strip().lower()
        if email:
            upsert_subscription(email, customer_id, subscription_id, status, cpe, plan, price_id)

    try:
        if event_type == "checkout.session.completed":
            email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")
            price_id = (obj.get("metadata") or {}).get("price_id")

            if subscription_id:
                upsert_from_subscription_id(subscription_id, email_hint=email, customer_id_hint=customer_id)
            else:
                plan = plan_from_price(price_id)
                if email:
                    upsert_subscription(email.strip().lower(), customer_id, None, "active", None, plan, price_id)

        elif event_type.startswith("customer.subscription."):
            subscription_id = obj.get("id")
            if subscription_id:
                upsert_from_subscription_id(subscription_id)

        elif event_type == "invoice.paid":
            subscription_id = obj.get("subscription")
            if subscription_id:
                upsert_from_subscription_id(subscription_id)

        return "", 200

    except Exception as e:
        print("Webhook handler error:", e)
        return "Internal Server Error", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))








