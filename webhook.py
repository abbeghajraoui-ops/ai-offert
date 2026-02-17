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


def get_user(email: str):
    email = email.strip().lower()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    return dict(row) if row else None


def ensure_user(email: str):
    email = email.strip().lower()
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users(email, free_quotes_used, updated_at)
            VALUES(?, 0, ?)
            """,
            (email, int(time.time())),
        )
        conn.commit()


def increment_free_quote(email: str):
    email = email.strip().lower()
    ensure_user(email)
    with db() as conn:
        conn.execute(
            """
            UPDATE users
            SET free_quotes_used = free_quotes_used + 1,
                updated_at = ?
            WHERE email = ?
            """,
            (int(time.time()), email),
        )
        conn.commit()


def is_active(user: dict | None) -> bool:
    if not user:
        return False
    status = (user.get("status") or "").lower()
    cpe = int(user.get("current_period_end") or 0)
    now = int(time.time())

    if status in ("active", "trialing"):
        return True if cpe == 0 else (cpe > now)
    return False


def upsert_subscription(
    email: str,
    customer_id: str | None,
    subscription_id: str | None,
    status: str | None,
    current_period_end: int | None,
    plan: str | None,
    price_id: str | None,
):
    now = int(time.time())
    email = email.strip().lower()
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
                current_period_end,
                plan,
                price_id,
                now,
                email,
            ),
        )
        conn.commit()


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
    user = get_user(email)

    active = is_active(user)
    free_used = user.get("free_quotes_used", 0)

    return jsonify(
        {
            "email": email,
            "active": active,
            "plan": user.get("plan"),
            "free_used": free_used,
            "free_remaining": max(0, 3 - free_used),
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
    user = get_user(email)

    if is_active(user):
        return jsonify({"ok": True, "active": True})

    free_used = user.get("free_quotes_used", 0)
    if free_used >= 3:
        return jsonify({"error": "free_limit_reached"}), 402

    increment_free_quote(email)
    return jsonify({"ok": True, "free_used": free_used + 1})


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

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=email,
        metadata={"email": email, "price_id": price_id},
    )
    return jsonify({"url": session.url})


# ----------------------------
# Stripe webhook
# ----------------------------
@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        print("Webhook verify failed:", e)
        return "Bad Request", 400

    event_type = event["type"]
    obj = event["data"]["object"]

    def upsert_from_subscription(subscription_id, email_hint=None, customer_id_hint=None):
        sub = stripe.Subscription.retrieve(subscription_id)
        status = sub.get("status")
        cpe = sub.get("current_period_end")
        customer_id = sub.get("customer") or customer_id_hint

        items = (sub.get("items") or {}).get("data") or []
        price_id = None
        if items:
            price_id = items[0]["price"]["id"]

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
            subscription_id = obj.get("subscription")
            customer_id = obj.get("customer")

            if subscription_id:
                upsert_from_subscription(subscription_id, email, customer_id)

        elif event_type.startswith("customer.subscription."):
            subscription_id = obj.get("id")
            if subscription_id:
                upsert_from_subscription(subscription_id)

        elif event_type == "invoice.paid":
            subscription_id = obj.get("subscription")
            if subscription_id:
                upsert_from_subscription(subscription_id)

        return "", 200

    except Exception as e:
        print("Webhook error:", e)
        return "Internal Server Error", 500









