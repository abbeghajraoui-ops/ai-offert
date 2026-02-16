import os
import time
import json
import sqlite3
from functools import wraps

import stripe
from flask import Flask, request, jsonify, abort

# ----------------------------
# Config
# ----------------------------
app = Flask(__name__)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_WEBHOOK_TOKEN = os.environ.get("APP_WEBHOOK_TOKEN", "")
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
                updated_at INTEGER
            )
            """
        )
        conn.commit()


init_db()


def upsert_subscription(email: str, customer_id: str | None, subscription_id: str | None,
                        status: str | None, current_period_end: int | None):
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions(email, customer_id, subscription_id, status, current_period_end, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                customer_id=excluded.customer_id,
                subscription_id=excluded.subscription_id,
                status=excluded.status,
                current_period_end=excluded.current_period_end,
                updated_at=excluded.updated_at
            """,
            (email, customer_id, subscription_id, status, current_period_end, now),
        )
        conn.commit()


def get_subscription(email: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
    return dict(row) if row else None


def is_active(sub: dict | None) -> bool:
    if not sub:
        return False
    status = (sub.get("status") or "").lower()
    cpe = sub.get("current_period_end") or 0
    now = int(time.time())
    # Stripe "active" är aktiv. "trialing" kan också räknas som aktiv om du vill.
    return status in ("active", "trialing") and cpe > now


# ----------------------------
# Auth between Streamlit <-> Backend
# ----------------------------
def require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not APP_WEBHOOK_TOKEN:
            return jsonify({"error": "Server not configured (APP_WEBHOOK_TOKEN missing)"}), 500

        auth = request.headers.get("Authorization", "")
        # Expect: Authorization: Bearer <token>
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized (missing bearer token)"}), 401

        token = auth.replace("Bearer ", "", 1).strip()
        if token != APP_WEBHOOK_TOKEN:
            return jsonify({"error": "Unauthorized (bad token)"}), 401

        return fn(*args, **kwargs)
    return wrapper


# ----------------------------
# Basic endpoints
# ----------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/subscription")
@require_token
def api_subscription():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    sub = get_subscription(email)
    return jsonify({
        "email": email,
        "found": bool(sub),
        "active": is_active(sub),
        "subscription": sub,
    })


@app.post("/api/create-checkout-session")
@require_token
def create_checkout_session():
    """
    Body JSON:
      {
        "email": "kund@firma.se",
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

    # 1) Verify signature (om du har STRIPE_WEBHOOK_SECRET)
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            # Fallback (inte rekommenderat): acceptera utan signatur i dev
            event = json.loads(payload.decode("utf-8"))
    except Exception as e:
        print("Webhook signature verify failed:", e)
        return "Bad Request", 400

    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    try:
        # checkout.session.completed (bra för att få email + customer + subscription)
        if event_type == "checkout.session.completed":
            email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")

            cpe = None
            status = None
            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                status = sub.get("status")
                cpe = sub.get("current_period_end")

            if email:
                upsert_subscription(email.strip().lower(), customer_id, subscription_id, status, cpe)

        # customer.subscription.created / updated / deleted
        elif event_type.startswith("customer.subscription."):
            subscription_id = obj.get("id")
            customer_id = obj.get("customer")
            status = obj.get("status")
            cpe = obj.get("current_period_end")

            # Hämta email via customer (robust)
            email = None
            if customer_id:
                cust = stripe.Customer.retrieve(customer_id)
                email = cust.get("email")

            if email:
                upsert_subscription(email.strip().lower(), customer_id, subscription_id, status, cpe)

        # invoice.paid (ofta bra som extra “bekräftelse”)
        elif event_type == "invoice.paid":
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")

            email = None
            if customer_id:
                cust = stripe.Customer.retrieve(customer_id)
                email = cust.get("email")

            cpe = None
            status = None
            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                status = sub.get("status")
                cpe = sub.get("current_period_end")

            if email:
                upsert_subscription(email.strip().lower(), customer_id, subscription_id, status, cpe)

        # Ignorera allt annat
        return "", 200

    except Exception as e:
        print("Webhook handler error:", e)
        # Stripe vill ofta ha 2xx. Men om du vill retry:a vid fel, returnera 500.
        return "Internal Server Error", 500








