import os
import json
import sqlite3
from datetime import datetime, timezone

import stripe
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

# ----------------------------
# Config
# ----------------------------
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
APP_WEBHOOK_TOKEN = os.environ.get("APP_WEBHOOK_TOKEN", "").strip()  # shared secret between Streamlit and this backend
DB_PATH = os.environ.get("DB_PATH", "offertly.db")

if not STRIPE_SECRET_KEY:
    print("WARNING: STRIPE_SECRET_KEY missing")
stripe.api_key = STRIPE_SECRET_KEY


# ----------------------------
# DB helpers
# ----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            customer_id TEXT,
            subscription_id TEXT,
            price_id TEXT,
            plan TEXT,
            status TEXT,
            current_period_start INTEGER,
            current_period_end INTEGER,
            created_at INTEGER,
            updated_at INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS checkout_sessions (
            id TEXT PRIMARY KEY,
            email TEXT,
            customer_id TEXT,
            subscription_id TEXT,
            price_id TEXT,
            created_at INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def plan_from_price(price_id: str) -> str:
    # You can name these whatever you want
    mapping = {
        os.environ.get("STRIPE_PRICE_ID_STARTER", ""): "starter",
        os.environ.get("STRIPE_PRICE_ID_PRO", ""): "pro",
        os.environ.get("STRIPE_PRICE_ID_TEAM", ""): "team",
    }
    return mapping.get(price_id, "unknown")


def upsert_user(
    email: str,
    customer_id: str | None = None,
    subscription_id: str | None = None,
    price_id: str | None = None,
    status: str | None = None,
    current_period_start: int | None = None,
    current_period_end: int | None = None,
):
    conn = db()
    cur = conn.cursor()
    existing = cur.execute("SELECT email FROM users WHERE email = ?", (email,)).fetchone()
    ts = now_ts()

    plan = plan_from_price(price_id) if price_id else None

    if existing:
        cur.execute(
            """
            UPDATE users SET
              customer_id = COALESCE(?, customer_id),
              subscription_id = COALESCE(?, subscription_id),
              price_id = COALESCE(?, price_id),
              plan = COALESCE(?, plan),
              status = COALESCE(?, status),
              current_period_start = COALESCE(?, current_period_start),
              current_period_end = COALESCE(?, current_period_end),
              updated_at = ?
            WHERE email = ?
            """,
            (
                customer_id,
                subscription_id,
                price_id,
                plan,
                status,
                current_period_start,
                current_period_end,
                ts,
                email,
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO users (
              email, customer_id, subscription_id, price_id, plan, status,
              current_period_start, current_period_end, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email,
                customer_id,
                subscription_id,
                price_id,
                plan,
                status or "incomplete",
                current_period_start,
                current_period_end,
                ts,
                ts,
            ),
        )

    conn.commit()
    conn.close()


def require_token():
    # Streamlit -> Backend auth
    # Header: Authorization: Bearer <APP_WEBHOOK_TOKEN>
    auth = request.headers.get("Authorization", "")
    if not APP_WEBHOOK_TOKEN:
        abort(500, "APP_WEBHOOK_TOKEN not configured on backend")
    if not auth.startswith("Bearer "):
        abort(401)
    token = auth.replace("Bearer ", "", 1).strip()
    if token != APP_WEBHOOK_TOKEN:
        abort(401)


# ----------------------------
# Health
# ----------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True})


# ----------------------------
# API: Streamlit uses these
# ----------------------------
@app.post("/api/create-checkout-session")
def create_checkout_session():
    require_token()
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    price_id = (data.get("price_id") or "").strip()
    success_url = (data.get("success_url") or "").strip()
    cancel_url = (data.get("cancel_url") or "").strip()

    if not email or not price_id or not success_url or not cancel_url:
        return jsonify({"error": "Missing email/price_id/success_url/cancel_url"}), 400

    # Create Stripe Checkout Session for SUBSCRIPTION
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{success_url}?success=1&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{cancel_url}?canceled=1",
        allow_promotion_codes=True,
    )

    # Save session (optional but useful)
    conn = db()
    conn.execute(
        """
        INSERT OR REPLACE INTO checkout_sessions (id, email, customer_id, subscription_id, price_id, created_at)
        VALUES (?, ?, NULL, NULL, ?, ?)
        """,
        (session["id"], email, price_id, now_ts()),
    )
    conn.commit()
    conn.close()

    return jsonify({"checkout_url": session["url"], "id": session["id"]})


@app.get("/api/status")
def api_status():
    require_token()
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Missing email"}), 400

    conn = db()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    if not row:
        return jsonify({"email": email, "status": "none", "plan": None})

    return jsonify(
        {
            "email": row["email"],
            "status": row["status"],
            "plan": row["plan"],
            "price_id": row["price_id"],
            "subscription_id": row["subscription_id"],
            "current_period_start": row["current_period_start"],
            "current_period_end": row["current_period_end"],
        }
    )


# ----------------------------
# Stripe Webhook
# ----------------------------
@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print("Webhook signature verification failed:", str(e))
        return "bad", 400

    event_type = event["type"]
    obj = event["data"]["object"]

    try:
        if event_type == "checkout.session.completed":
            # obj is a Checkout Session
            email = (obj.get("customer_email") or "").strip().lower()
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")

            price_id = None
            period_start = None
            period_end = None
            status = "active"

            # Retrieve subscription to get price_id + period dates
            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id, expand=["items.data.price"])
                status = sub.get("status") or status
                period_start = sub.get("current_period_start")
                period_end = sub.get("current_period_end")
                items = (sub.get("items") or {}).get("data") or []
                if items and items[0].get("price"):
                    price_id = items[0]["price"]["id"]

            if email:
                upsert_user(
                    email=email,
                    customer_id=customer_id,
                    subscription_id=subscription_id,
                    price_id=price_id,
                    status=status,
                    current_period_start=period_start,
                    current_period_end=period_end,
                )

        elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
            # obj is a Subscription
            sub = obj
            customer_id = sub.get("customer")
            status = sub.get("status") or "unknown"
            period_start = sub.get("current_period_start")
            period_end = sub.get("current_period_end")

            # Find email via customer
            email = None
            if customer_id:
                cust = stripe.Customer.retrieve(customer_id)
                email = (cust.get("email") or "").strip().lower()

            price_id = None
            items = (sub.get("items") or {}).get("data") or []
            if items and items[0].get("price"):
                price_id = items[0]["price"]["id"]

            if email:
                upsert_user(
                    email=email,
                    customer_id=customer_id,
                    subscription_id=sub.get("id"),
                    price_id=price_id,
                    status=status,
                    current_period_start=period_start,
                    current_period_end=period_end,
                )

        elif event_type in ("customer.subscription.deleted",):
            customer_id = obj.get("customer")
            email = None
            if customer_id:
                cust = stripe.Customer.retrieve(customer_id)
                email = (cust.get("email") or "").strip().lower()
            if email:
                upsert_user(email=email, status="canceled")

        elif event_type == "invoice.paid":
            # optional: keep status active
            customer_id = obj.get("customer")
            email = None
            if customer_id:
                cust = stripe.Customer.retrieve(customer_id)
                email = (cust.get("email") or "").strip().lower()
            if email:
                upsert_user(email=email, status="active")

        else:
            # ignore others
            pass

    except Exception as e:
        print("Webhook handler error:", str(e))
        return "error", 500

    return "ok", 200






