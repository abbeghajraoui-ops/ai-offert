import os
import time
import json
import sqlite3
import stripe
from flask import Flask, request, jsonify, abort

# ----------------------------
# Config
# ----------------------------
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
DB_PATH = os.environ.get("DB_PATH", "offertly.db")
APP_WEBHOOK_TOKEN = os.environ.get("APP_WEBHOOK_TOKEN", "").strip()  # shared with Streamlit secrets

if not STRIPE_SECRET_KEY:
    raise RuntimeError("Missing STRIPE_SECRET_KEY env var")

stripe.api_key = STRIPE_SECRET_KEY

app = Flask(__name__)

# ----------------------------
# DB helpers
# ----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            email TEXT PRIMARY KEY,
            customer_id TEXT,
            subscription_id TEXT,
            price_id TEXT,
            status TEXT,
            current_period_end INTEGER,
            created_at INTEGER,
            updated_at INTEGER
        )
        """
    )
    conn.commit()
    conn.close()

def upsert_subscription(email: str, customer_id: str, subscription_id: str, price_id: str,
                        status: str, current_period_end: int):
    now = int(time.time())
    conn = db()
    conn.execute(
        """
        INSERT INTO subscriptions(email, customer_id, subscription_id, price_id, status, current_period_end, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(email) DO UPDATE SET
            customer_id=excluded.customer_id,
            subscription_id=excluded.subscription_id,
            price_id=excluded.price_id,
            status=excluded.status,
            current_period_end=excluded.current_period_end,
            updated_at=excluded.updated_at
        """,
        (email, customer_id, subscription_id, price_id, status, int(current_period_end or 0), now, now),
    )
    conn.commit()
    conn.close()

def get_subscription(email: str):
    conn = db()
    row = conn.execute("SELECT * FROM subscriptions WHERE email=?", (email,)).fetchone()
    conn.close()
    return row

init_db()

# ----------------------------
# Small utils
# ----------------------------
def require_token():
    if not APP_WEBHOOK_TOKEN:
        # If you forgot to set it, fail closed
        abort(500, "APP_WEBHOOK_TOKEN not configured on server")
    token = request.headers.get("X-APP-TOKEN", "")
    if token != APP_WEBHOOK_TOKEN:
        abort(401, "Invalid token")

def safe_email_from_checkout_session(session: dict) -> str | None:
    # Stripe can put email in different places
    cd = session.get("customer_details") or {}
    return (cd.get("email") or session.get("customer_email") or "").strip() or None

def get_customer_email(customer_id: str) -> str | None:
    if not customer_id:
        return None
    try:
        cust = stripe.Customer.retrieve(customer_id)
        email = (cust.get("email") or "").strip()
        return email or None
    except Exception:
        return None

def normalize_subscription_data(sub: dict) -> tuple[str | None, str | None]:
    """
    Returns (price_id, current_period_end)
    """
    price_id = None
    try:
        items = (sub.get("items") or {}).get("data") or []
        if items:
            price = items[0].get("price") or {}
            price_id = price.get("id")
    except Exception:
        pass
    cpe = sub.get("current_period_end")
    return price_id, cpe

# ----------------------------
# Routes
# ----------------------------
@app.get("/health")
def health():
    return "ok", 200

@app.get("/api/subscription")
def api_subscription():
    """
    Streamlit calls this:
      GET /api/subscription?email=...
      Header: X-APP-TOKEN: <APP_WEBHOOK_TOKEN>
    """
    require_token()
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "missing email"}), 400

    row = get_subscription(email)
    if not row:
        return jsonify({"ok": True, "active": False, "email": email}), 200

    status = row["status"] or ""
    active = status in ("active", "trialing")  # Stripe statuses that should unlock app
    return jsonify({
        "ok": True,
        "active": active,
        "email": row["email"],
        "status": status,
        "price_id": row["price_id"],
        "customer_id": row["customer_id"],
        "subscription_id": row["subscription_id"],
        "current_period_end": row["current_period_end"],
        "updated_at": row["updated_at"],
    }), 200

@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    if not STRIPE_WEBHOOK_SECRET:
        return "Missing STRIPE_WEBHOOK_SECRET", 500

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
            tolerance=300,
        )
    except ValueError:
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400

    event_type = event["type"]
    obj = event["data"]["object"]

    try:
        # 1) Checkout completed (best place to provision)
        if event_type == "checkout.session.completed":
            session = obj

            email = safe_email_from_checkout_session(session)
            customer_id = session.get("customer")
            subscription_id = session.get("subscription")

            # If email missing, try customer lookup
            if not email and customer_id:
                email = get_customer_email(customer_id)

            # If we got a subscription, fetch full subscription to get price + period
            price_id = None
            current_period_end = 0
            status = "unknown"

            if subscription_id:
                sub = stripe.Subscription.retrieve(
                    subscription_id,
                    expand=["items.data.price"]
                )
                status = sub.get("status") or "unknown"
                price_id, current_period_end = normalize_subscription_data(sub)

            if email:
                upsert_subscription(
                    email=email.lower(),
                    customer_id=customer_id or "",
                    subscription_id=subscription_id or "",
                    price_id=price_id or "",
                    status=status,
                    current_period_end=int(current_period_end or 0),
                )

        # 2) Subscription lifecycle events (keeps DB in sync)
        elif event_type in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
            sub = obj
            customer_id = sub.get("customer")
            subscription_id = sub.get("id")
            status = sub.get("status") or "unknown"
            price_id, current_period_end = normalize_subscription_data(sub)

            email = get_customer_email(customer_id) if customer_id else None
            if email:
                upsert_subscription(
                    email=email.lower(),
                    customer_id=customer_id or "",
                    subscription_id=subscription_id or "",
                    price_id=price_id or "",
                    status=status,
                    current_period_end=int(current_period_end or 0),
                )

        # 3) Invoice paid (often happens each cycle)
        elif event_type == "invoice.paid":
            invoice = obj
            customer_id = invoice.get("customer")
            subscription_id = invoice.get("subscription")

            email = get_customer_email(customer_id) if customer_id else None
            if subscription_id:
                sub = stripe.Subscription.retrieve(
                    subscription_id,
                    expand=["items.data.price"]
                )
                status = sub.get("status") or "unknown"
                price_id, current_period_end = normalize_subscription_data(sub)

                if email:
                    upsert_subscription(
                        email=email.lower(),
                        customer_id=customer_id or "",
                        subscription_id=subscription_id or "",
                        price_id=price_id or "",
                        status=status,
                        current_period_end=int(current_period_end or 0),
                    )

        # Ignore other event types
    except Exception as e:
        # Important: if we return 500, Stripe retries (good when transient, bad when bug)
        # For now we return 200 after logging, but in Railway logs you will see error.
        app.logger.exception(f"Webhook processing error for {event_type}: {e}")

    return "", 200




