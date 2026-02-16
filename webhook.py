import os
import time
import sqlite3
from typing import Optional, Dict, Any

import stripe
from flask import Flask, request, jsonify, abort

# -----------------------
# Config
# -----------------------
app = Flask(__name__)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DB_PATH = os.environ.get("DB_PATH", "offertly.db")

# En enkel "token" så bara din Streamlit-app kan fråga /status
APP_WEBHOOK_TOKEN = os.environ.get("APP_WEBHOOK_TOKEN", "")

# Price IDs för att mappa plan
PRICE_STARTER = os.environ.get("STRIPE_PRICE_ID_STARTER", "")
PRICE_PRO = os.environ.get("STRIPE_PRICE_ID_PRO", "")
PRICE_TEAM = os.environ.get("STRIPE_PRICE_ID_TEAM", "")

stripe.api_key = STRIPE_SECRET_KEY


# -----------------------
# DB helpers
# -----------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            plan TEXT DEFAULT 'starter',
            status TEXT DEFAULT 'inactive', -- active / inactive / past_due / canceled
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            current_period_end INTEGER,
            updated_at INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def plan_from_price_id(price_id: Optional[str]) -> str:
    if not price_id:
        return "starter"
    if price_id == PRICE_TEAM:
        return "team"
    if price_id == PRICE_PRO:
        return "pro"
    if price_id == PRICE_STARTER:
        return "starter"
    # okänt price_id: spara som starter men du kan ändra här om du vill
    return "starter"


def upsert_user(
    email: str,
    plan: str,
    status: str,
    customer_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
    current_period_end: Optional[int] = None,
) -> None:
    now = int(time.time())
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (email, plan, status, stripe_customer_id, stripe_subscription_id, current_period_end, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            plan=excluded.plan,
            status=excluded.status,
            stripe_customer_id=COALESCE(excluded.stripe_customer_id, users.stripe_customer_id),
            stripe_subscription_id=COALESCE(excluded.stripe_subscription_id, users.stripe_subscription_id),
            current_period_end=COALESCE(excluded.current_period_end, users.current_period_end),
            updated_at=excluded.updated_at
        """,
        (email, plan, status, customer_id, subscription_id, current_period_end, now),
    )
    conn.commit()
    conn.close()


def get_user(email: str) -> Optional[Dict[str, Any]]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email = ?", (email,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


def token_ok(req) -> bool:
    if not APP_WEBHOOK_TOKEN:
        return False
    # Tillåt token via header eller query param
    header = req.headers.get("X-Webhook-Token", "")
    q = req.args.get("token", "")
    return header == APP_WEBHOOK_TOKEN or q == APP_WEBHOOK_TOKEN


# -----------------------
# Stripe helpers
# -----------------------
def safe_customer_email_from_checkout_session(session: Dict[str, Any]) -> Optional[str]:
    # Stripe kan lägga email på olika fält beroende på mode/inställningar
    email = session.get("customer_email")
    if email:
        return email
    cd = session.get("customer_details") or {}
    return cd.get("email")


def safe_get_subscription_fields(sub: Dict[str, Any]) -> Dict[str, Any]:
    # Subscription → plan/status/period_end
    items = (sub.get("items") or {}).get("data") or []
    price_id = None
    if items and items[0].get("price"):
        price_id = items[0]["price"].get("id")

    return {
        "subscription_id": sub.get("id"),
        "customer_id": sub.get("customer"),
        "status": sub.get("status") or "inactive",
        "plan": plan_from_price_id(price_id),
        "current_period_end": sub.get("current_period_end"),
    }


def email_from_customer_id(customer_id: str) -> Optional[str]:
    try:
        cust = stripe.Customer.retrieve(customer_id)
        return cust.get("email")
    except Exception:
        return None


# -----------------------
# Routes
# -----------------------
@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/status")
def status():
    # Din Streamlit-app ska kalla: /status?email=...&token=...
    if not token_ok(request):
        return jsonify({"error": "unauthorized"}), 401

    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "missing email"}), 400

    u = get_user(email)
    if not u:
        return jsonify({"email": email, "plan": "starter", "status": "inactive"}), 200

    return jsonify(
        {
            "email": u["email"],
            "plan": u["plan"],
            "status": u["status"],
            "current_period_end": u.get("current_period_end"),
        }
    )


@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "missing STRIPE_WEBHOOK_SECRET"}), 500

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        # fel signatur/format
        return jsonify({"error": f"webhook verify failed: {str(e)}"}), 400

    event_type = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    try:
        # -----------------------
        # 1) checkout.session.completed
        # -----------------------
        if event_type == "checkout.session.completed":
            # obj = Checkout Session
            session = obj
            email = safe_customer_email_from_checkout_session(session)
            customer_id = session.get("customer")
            mode = session.get("mode")

            if not email and customer_id:
                email = email_from_customer_id(customer_id)

            if not email:
                # Kan inte koppla till användare → returnera 200 så Stripe inte spammar
                return jsonify({"ok": True, "note": "no email found"}), 200

            email = email.strip().lower()

            if mode == "subscription":
                sub_id = session.get("subscription")
                if sub_id:
                    sub = stripe.Subscription.retrieve(sub_id)
                    fields = safe_get_subscription_fields(sub)
                    upsert_user(
                        email=email,
                        plan=fields["plan"],
                        status=fields["status"],
                        customer_id=fields["customer_id"],
                        subscription_id=fields["subscription_id"],
                        current_period_end=fields["current_period_end"],
                    )
                else:
                    # subscription saknas (ovanligt) → markera aktiv ändå
                    upsert_user(email=email, plan="starter", status="active", customer_id=customer_id)

            else:
                # Om du senare använder engångsbetalning (mode=payment)
                upsert_user(email=email, plan="starter", status="active", customer_id=customer_id)

            return jsonify({"ok": True}), 200

        # -----------------------
        # 2) customer.subscription.* (created/updated/deleted)
        # -----------------------
        if event_type.startswith("customer.subscription."):
            sub = obj  # Subscription
            fields = safe_get_subscription_fields(sub)
            customer_id = fields["customer_id"]
            email = email_from_customer_id(customer_id) if customer_id else None

            if email:
                email = email.strip().lower()

                # deleted → sätt canceled/inactive
                if event_type == "customer.subscription.deleted":
                    upsert_user(
                        email=email,
                        plan=fields["plan"],
                        status="canceled",
                        customer_id=customer_id,
                        subscription_id=fields["subscription_id"],
                        current_period_end=fields["current_period_end"],
                    )
                else:
                    upsert_user(
                        email=email,
                        plan=fields["plan"],
                        status=fields["status"],
                        customer_id=customer_id,
                        subscription_id=fields["subscription_id"],
                        current_period_end=fields["current_period_end"],
                    )
            return jsonify({"ok": True}), 200

        # -----------------------
        # 3) invoice.paid / invoice.payment_failed
        # -----------------------
        if event_type in ("invoice.paid", "invoice.payment_failed"):
            invoice = obj  # Invoice
            customer_id = invoice.get("customer")
            sub_id = invoice.get("subscription")
            email = email_from_customer_id(customer_id) if customer_id else None

            if email:
                email = email.strip().lower()

                # Hämta subscription för korrekt plan & period_end
                if sub_id:
                    sub = stripe.Subscription.retrieve(sub_id)
                    fields = safe_get_subscription_fields(sub)
                    status = fields["status"]
                    if event_type == "invoice.payment_failed":
                        status = "past_due"
                    upsert_user(
                        email=email,
                        plan=fields["plan"],
                        status=status,
                        customer_id=fields["customer_id"],
                        subscription_id=fields["subscription_id"],
                        current_period_end=fields["current_period_end"],
                    )
                else:
                    # ingen subscription → ändå uppdatera status
                    status = "active" if event_type == "invoice.paid" else "past_due"
                    upsert_user(email=email, plan="starter", status=status, customer_id=customer_id)

            return jsonify({"ok": True}), 200

        # Okänd event → svara 200 så Stripe inte retry:ar i onödan
        return jsonify({"ok": True, "ignored": event_type}), 200

    except Exception as e:
        # Här vill vi se felet i Railway logs + Stripe får 500 → den retry:ar
        # Men vi vill hellre att du märker fel.
        return jsonify({"error": str(e), "event_type": event_type}), 500


# Init DB vid start
init_db()


