import os
import json
import sqlite3
from datetime import datetime

import stripe
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- ENV ---
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "")  # ex: https://ai-offert-xxxx.streamlit.app
DB_PATH = os.environ.get("DB_PATH", "offertly.db")

# Price IDs (rekommenderat att sätta dessa som ENV)
SUB_PRICE_ID = os.environ.get("SUB_PRICE_ID", "")          # ex: price_...
CREDITS_PRICE_ID = os.environ.get("CREDITS_PRICE_ID", "")  # ex: price_...

stripe.api_key = STRIPE_SECRET_KEY


# --- DB ---
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            stripe_customer_id TEXT,
            subscription_status TEXT,
            subscription_id TEXT,
            plan TEXT,
            credits INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def now_iso():
    return datetime.utcnow().isoformat()


def upsert_user(email: str, **fields):
    if not email:
        return
    conn = db()
    cur = conn.cursor()

    # Ensure row exists
    cur.execute("INSERT OR IGNORE INTO users (email, updated_at) VALUES (?, ?)", (email, now_iso()))

    # Update fields
    cols = []
    vals = []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(v)
    cols.append("updated_at=?")
    vals.append(now_iso())
    vals.append(email)

    cur.execute(f"UPDATE users SET {', '.join(cols)} WHERE email=?", vals)
    conn.commit()
    conn.close()


def get_user(email: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email=?", (email,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def add_credits(email: str, amount: int):
    if not email or amount <= 0:
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (email, updated_at) VALUES (?, ?)", (email, now_iso()))
    cur.execute("UPDATE users SET credits = COALESCE(credits,0) + ?, updated_at=? WHERE email=?",
                (amount, now_iso(), email))
    conn.commit()
    conn.close()


def consume_credit(email: str, amount: int = 1) -> bool:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT credits FROM users WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    credits = int(row["credits"] or 0)
    if credits < amount:
        conn.close()
        return False
    cur.execute("UPDATE users SET credits = credits - ?, updated_at=? WHERE email=?",
                (amount, now_iso(), email))
    conn.commit()
    conn.close()
    return True


def is_sub_active(user: dict) -> bool:
    if not user:
        return False
    status = (user.get("subscription_status") or "").lower()
    return status in ("active", "trialing")


# --- HEALTH ---
@app.get("/health")
def health():
    return jsonify({"ok": True})


# --- API: user status (Streamlit -> Railway) ---
@app.get("/api/user")
def api_user():
    email = (request.args.get("email") or "").strip().lower()
    user = get_user(email) if email else None
    if not user:
        return jsonify({
            "email": email,
            "exists": False,
            "subscription_active": False,
            "credits": 0,
            "can_generate": False,
            "plan": "free"
        })

    sub_active = is_sub_active(user)
    credits = int(user.get("credits") or 0)
    can_generate = sub_active or credits > 0

    return jsonify({
        "email": email,
        "exists": True,
        "subscription_active": sub_active,
        "subscription_status": user.get("subscription_status"),
        "credits": credits,
        "can_generate": can_generate,
        "plan": user.get("plan") or ("sub" if sub_active else "credits")
    })


# --- API: create checkout session ---
@app.post("/api/create-checkout-session")
def api_create_checkout_session():
    """
    Body JSON:
    {
      "email": "kund@firma.se",
      "mode": "subscription" | "payment",
      "price_id": "price_...",
      "credits_to_add": 10   (bara för payment)
    }
    """
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "STRIPE_SECRET_KEY saknas"}), 500
    if not APP_BASE_URL:
        return jsonify({"error": "APP_BASE_URL saknas"}), 500

    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    mode = (data.get("mode") or "").strip().lower()
    price_id = (data.get("price_id") or "").strip()
    credits_to_add = int(data.get("credits_to_add") or 0)

    if not email:
        return jsonify({"error": "email saknas"}), 400
    if mode not in ("subscription", "payment"):
        return jsonify({"error": "mode måste vara subscription eller payment"}), 400
    if not price_id:
        return jsonify({"error": "price_id saknas"}), 400

    # Create / reuse customer
    user = get_user(email)
    customer_id = user.get("stripe_customer_id") if user else None
    if not customer_id:
        customer = stripe.Customer.create(email=email)
        customer_id = customer["id"]
        upsert_user(email, stripe_customer_id=customer_id)

    success_url = f"{APP_BASE_URL}/?checkout=success"
    cancel_url = f"{APP_BASE_URL}/?checkout=cancel"

    metadata = {
        "email": email,
        "mode": mode,
    }
    if mode == "payment":
        metadata["credits_to_add"] = str(max(0, credits_to_add))

    try:
        session = stripe.checkout.Session.create(
            mode=mode,
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            allow_promotion_codes=True,
        )
        return jsonify({"url": session["url"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- API: consume credit when generating offer ---
@app.post("/api/consume-credit")
def api_consume_credit():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    amount = int(data.get("amount") or 1)

    if not email:
        return jsonify({"error": "email saknas"}), 400

    user = get_user(email)
    if is_sub_active(user):
        # abonnemang = unlimited => ingen credit dras
        return jsonify({"ok": True, "used_credits": 0, "subscription_active": True})

    ok = consume_credit(email, amount)
    if not ok:
        return jsonify({"ok": False, "error": "Inga credits kvar"}), 402

    user2 = get_user(email)
    return jsonify({"ok": True, "used_credits": amount, "credits_left": int(user2.get("credits") or 0)})


# --- STRIPE WEBHOOK ---
@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return ("Bad signature", 400)

    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    # Helper: get email safely
    def guess_email():
        # common places
        email = None
        if isinstance(obj, dict):
            email = obj.get("customer_email")
            if not email:
                details = obj.get("customer_details") or {}
                if isinstance(details, dict):
                    email = details.get("email")
            if not email:
                metadata = obj.get("metadata") or {}
                if isinstance(metadata, dict):
                    email = metadata.get("email")
        return (email or "").strip().lower()

    email = guess_email()

    try:
        # 1) Checkout completed (subscription or one-time payment)
        if event_type == "checkout.session.completed":
            mode = (obj.get("mode") or "").lower()
            metadata = obj.get("metadata") or {}
            if not email and isinstance(metadata, dict):
                email = (metadata.get("email") or "").strip().lower()

            customer_id = obj.get("customer")
            if email and customer_id:
                upsert_user(email, stripe_customer_id=customer_id)

            if mode == "subscription":
                # subscription is created; status will come via subscription events too
                upsert_user(email, plan="sub")
            elif mode == "payment":
                # credits pack
                credits_to_add = 0
                if isinstance(metadata, dict):
                    try:
                        credits_to_add = int(metadata.get("credits_to_add") or 0)
                    except Exception:
                        credits_to_add = 0

                # If not provided, try to infer from price id (optional)
                if credits_to_add <= 0:
                    # default fallback
                    credits_to_add = 10

                add_credits(email, credits_to_add)
                upsert_user(email, plan="credits")

        # 2) Subscription updates
        elif event_type in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
            sub_id = obj.get("id")
            status = (obj.get("status") or "").lower()
            customer_id = obj.get("customer")

            # Try to resolve email if missing by pulling customer
            if (not email) and customer_id:
                try:
                    cust = stripe.Customer.retrieve(customer_id)
                    email = (cust.get("email") or "").strip().lower()
                except Exception:
                    pass

            if email:
                upsert_user(
                    email,
                    stripe_customer_id=customer_id,
                    subscription_id=sub_id,
                    subscription_status=status,
                    plan="sub" if status in ("active", "trialing") else "credits"
                )

        # 3) Invoice paid (useful for renewals)
        elif event_type == "invoice.paid":
            customer_id = obj.get("customer")
            if (not email) and customer_id:
                try:
                    cust = stripe.Customer.retrieve(customer_id)
                    email = (cust.get("email") or "").strip().lower()
                except Exception:
                    pass
            if email:
                # Keep subscription alive if exists
                upsert_user(email, subscription_status="active", plan="sub")

        # 4) Payment failed (optional)
        elif event_type == "invoice.payment_failed":
            customer_id = obj.get("customer")
            if (not email) and customer_id:
                try:
                    cust = stripe.Customer.retrieve(customer_id)
                    email = (cust.get("email") or "").strip().lower()
                except Exception:
                    pass
            if email:
                upsert_user(email, subscription_status="past_due")

    except Exception as e:
        # Never crash webhook -> return 200 so Stripe doesn't hammer you
        print("Webhook handling error:", str(e))

    return ("ok", 200)



