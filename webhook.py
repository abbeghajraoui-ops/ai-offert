import os
import time
import json
import sqlite3
from functools import wraps

import stripe
from flask import Flask, request, jsonify

app = Flask(__name__)

# ----------------------------
# Config
# ----------------------------
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

APP_API_TOKEN = (
    os.environ.get("APP_API_TOKEN", "").strip()
    or os.environ.get("APP_WEBHOOK_TOKEN", "").strip()
)

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
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                free_quotes_used INTEGER DEFAULT 0,
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
        conn.commit()


init_db()


def now_ts() -> int:
    return int(time.time())


def plan_from_price(price_id: str | None) -> str | None:
    if not price_id:
        return None
    if price_id == STRIPE_PRICE_ID_STARTER:
        return "starter"
    if price_id == STRIPE_PRICE_ID_PRO:
        return "pro"
    if price_id == STRIPE_PRICE_ID_TEAM:
        return "team"
    return None


def ensure_user(email: str):
    email = (email or "").strip().lower()
    if not email:
        return
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users(email, free_quotes_used, updated_at)
            VALUES(?, 0, ?)
            """,
            (email, now_ts()),
        )
        conn.commit()


def get_user(email: str):
    email = (email or "").strip().lower()
    if not email:
        return None
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def increment_free_quote(email: str):
    email = (email or "").strip().lower()
    if not email:
        return
    ensure_user(email)
    with db() as conn:
        conn.execute(
            """
            UPDATE users
            SET free_quotes_used = free_quotes_used + 1,
                updated_at = ?
            WHERE email = ?
            """,
            (now_ts(), email),
        )
        conn.commit()


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
    if not email:
        return
    ensure_user(email)

    with db() as conn:
        conn.execute(
            """
            UPDATE users
            SET customer_id = ?,
                subscription_id = ?,
                status = ?,
                current_period_end = ?,
                plan = ?,
                price_id = ?,
                updated_at = ?
            WHERE email = ?
            """,
            (
                customer_id,
                subscription_id,
                status,
                int(current_period_end or 0),
                plan,
                price_id,
                now_ts(),
                email,
            ),
        )
        conn.commit()


def is_active(user: dict | None) -> bool:
    if not user:
        return False

    status = (user.get("status") or "").lower().strip()
    cpe = int(user.get("current_period_end") or 0)
    now = now_ts()

    # Active/trialing räknas som aktiv.
    # Om cpe=0 (saknas) => räknas som aktiv, annars måste den ligga i framtiden.
    if status in ("active", "trialing"):
        return True if cpe == 0 else (cpe > now)

    return False


# ----------------------------
# Auth
# ----------------------------
def require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not APP_API_TOKEN:
            return jsonify({"error": "APP_API_TOKEN missing"}), 500

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401

        token = auth.replace("Bearer ", "", 1).strip()
        if token != APP_API_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401

        return fn(*args, **kwargs)

    return wrapper


# ----------------------------
# API
# ----------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/status")
@require_token
def api_status():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    ensure_user(email)
    user = get_user(email) or {}

    active = is_active(user)
    free_used = int(user.get("free_quotes_used") or 0)

    return jsonify(
        {
            "email": email,
            "active": active,
            "plan": user.get("plan"),
            "free_used": free_used,
            "free_remaining": max(0, 3 - free_used),
        }
    )


# Bakåtkompatibilitet om du råkar ha kvar gammal app någonstans:
@app.get("/api/subscription")
@require_token
def api_subscription():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    ensure_user(email)
    user = get_user(email)

    return jsonify(
        {
            "email": email,
            "found": bool(user),
            "active": is_active(user),
            "subscription": user,
        }
    )


@app.post("/api/use-free-quote")
@require_token
def api_use_free_quote():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    if not email:
        return jsonify({"error": "email required"}), 400

    ensure_user(email)
    user = get_user(email) or {}

    # Har kunden aktiv plan -> ingen begränsning
    if is_active(user):
        return jsonify({"ok": True, "active": True})

    free_used = int(user.get("free_quotes_used") or 0)
    if free_used >= 3:
        # 402 = Payment Required (praktiskt för frontend)
        return jsonify({"error": "free_limit_reached"}), 402

    increment_free_quote(email)
    return jsonify({"ok": True, "free_used": free_used + 1, "free_remaining": max(0, 3 - (free_used + 1))})


@app.post("/api/create-checkout-session")
@require_token
def create_checkout_session():
    data = request.get_json(force=True, silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    price_id = (data.get("price_id") or "").strip()
    success_url = (data.get("success_url") or "").strip()
    cancel_url = (data.get("cancel_url") or "").strip()

    if not (email and price_id and success_url and cancel_url):
        return jsonify({"error": "missing fields"}), 400

    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "STRIPE_SECRET_KEY missing"}), 500

    # säkerställ user
    ensure_user(email)

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer_email=email,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=email,
            allow_promotion_codes=True,
            metadata={"email": email, "price_id": price_id},
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ----------------------------
# Stripe webhook
# ----------------------------
@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    # Webhook secret måste finnas i prod. (Om du kör dev kan du sätta en riktig secret i Stripe.)
    if not STRIPE_WEBHOOK_SECRET:
        return "STRIPE_WEBHOOK_SECRET missing", 500

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print("Webhook verify failed:", e)
        return "Bad Request", 400

    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    def upsert_from_subscription(subscription_id: str, email_hint: str | None = None, customer_id_hint: str | None = None):
        sub = stripe.Subscription.retrieve(subscription_id)
        status = sub.get("status")
        cpe = sub.get("current_period_end")
        customer_id = sub.get("customer") or customer_id_hint

        # price_id från subscription items
        items = (sub.get("items") or {}).get("data") or []
        price_id = None
        if items and isinstance(items, list):
            try:
                price_id = items[0].get("price", {}).get("id")
            except Exception:
                price_id = None

        plan = plan_from_price(price_id)

        # robust email resolve via customer
        email = None
        if customer_id:
            cust = stripe.Customer.retrieve(customer_id)
            email = cust.get("email")

        email = (email or email_hint or "").strip().lower()
        if email:
            upsert_subscription(
                email=email,
                customer_id=customer_id,
                subscription_id=subscription_id,
                status=status,
                current_period_end=int(cpe or 0),
                plan=plan,
                price_id=price_id,
            )

    try:
        if event_type == "checkout.session.completed":
            email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
            subscription_id = obj.get("subscription")
            customer_id = obj.get("customer")

            if subscription_id:
                upsert_from_subscription(subscription_id, email_hint=email, customer_id_hint=customer_id)

        elif event_type.startswith("customer.subscription."):
            subscription_id = obj.get("id")
            if subscription_id:
                upsert_from_subscription(subscription_id)

        elif event_type == "invoice.paid":
            subscription_id = obj.get("subscription")
            if subscription_id:
                upsert_from_subscription(subscription_id)

        # Ignorera annat
        return "", 200

    except Exception as e:
        print("Webhook error:", e)
        return "Internal Server Error", 500









