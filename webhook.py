import os
import sqlite3
import stripe
from flask import Flask, request, abort

app = Flask(__name__)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DB_PATH = os.environ.get("DB_PATH", "offertly.db")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            stripe_subscription_status TEXT,
            plan TEXT,
            created_at INTEGER
        );
        """
    )
    conn.commit()
    conn.close()


def update_user_by_customer(customer_id: str, **fields):
    """
    Uppdaterar användare via stripe_customer_id (säkrast i webhook-världen).
    """
    if not customer_id:
        return
    keys = []
    vals = []
    for k, v in fields.items():
        if v is None:
            continue
        keys.append(f"{k} = ?")
        vals.append(v)

    if not keys:
        return

    vals.append(customer_id)

    conn = db()
    conn.execute(
        f"UPDATE users SET {', '.join(keys)} WHERE stripe_customer_id = ?",
        tuple(vals),
    )
    conn.commit()
    conn.close()


def update_user_by_email(email: str, **fields):
    if not email:
        return
    keys = []
    vals = []
    for k, v in fields.items():
        if v is None:
            continue
        keys.append(f"{k} = ?")
        vals.append(v)
    if not keys:
        return
    vals.append(email.lower().strip())

    conn = db()
    conn.execute(
        f"UPDATE users SET {', '.join(keys)} WHERE email = ?",
        tuple(vals),
    )
    conn.commit()
    conn.close()


@app.route("/health", methods=["GET"])
def health():
    return {"ok": True}, 200


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    ensure_schema()

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    # 1) Verifiera signatur (viktigt)
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception:
        abort(400)

    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    # 2) Hantera event utan att krascha om fält saknas
    try:
        # A) Checkout klar → bra ställe att få email + customer + subscription + plan
        if event_type == "checkout.session.completed":
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")

            email = None
            cd = obj.get("customer_details") or {}
            if isinstance(cd, dict):
                email = cd.get("email")
            if not email:
                email = obj.get("customer_email")

            plan_key = None
            md = obj.get("metadata") or {}
            if isinstance(md, dict):
                plan_key = md.get("plan_key")

            # Spara på user (om du matchar på email i din Streamlit-app)
            if email:
                update_user_by_email(
                    email,
                    stripe_customer_id=str(customer_id) if customer_id else None,
                    stripe_subscription_id=str(subscription_id) if subscription_id else None,
                    plan=plan_key,
                )

        # B) Subscription events → uppdatera status
        elif event_type in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            customer_id = obj.get("customer")
            subscription_id = obj.get("id")
            status = (obj.get("status") or "").lower()

            update_user_by_customer(
                str(customer_id) if customer_id else "",
                stripe_subscription_id=str(subscription_id) if subscription_id else None,
                stripe_subscription_status=status if status else None,
            )

        # C) Invoice paid/failed → ofta bra “sanning” om betalning
        elif event_type in ("invoice.paid", "invoice.payment_failed"):
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")

            status = "active" if event_type == "invoice.paid" else "past_due"

            update_user_by_customer(
                str(customer_id) if customer_id else "",
                stripe_subscription_id=str(subscription_id) if subscription_id else None,
                stripe_subscription_status=status,
            )

        # Annars: ignorera event vi inte bryr oss om (men svara 200!)
        return {"received": True, "type": event_type}, 200

    except Exception as e:
        # Viktigt: returnera 200 eller 400? Här är det bättre att INTE krascha med 500.
        # Om något går fel i vår hantering, svara 200 så Stripe inte spammar retries.
        return {"received": True, "type": event_type, "handled_error": str(e)}, 200

