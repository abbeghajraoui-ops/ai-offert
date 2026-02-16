import os
import sqlite3
import stripe
from flask import Flask, request, abort

app = Flask(__name__)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
DB_PATH = os.environ.get("DB_PATH", "offertly.db")

stripe.api_key = STRIPE_SECRET_KEY


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception:
        abort(400)

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type in [
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ]:
        customer_id = data["customer"]

        status = data["status"]
        period_start = data["current_period_start"]
        period_end = data["current_period_end"]
        cancel_at_period_end = int(data["cancel_at_period_end"])

        price_id = data["items"]["data"][0]["price"]["id"]

        conn = db()
        cur = conn.execute(
            "SELECT id FROM users WHERE stripe_customer_id = ?",
            (customer_id,),
        )
        row = cur.fetchone()
        if row:
            user_id = row["id"]
            conn.execute(
                """
                UPDATE users
                SET
                    stripe_subscription_status = ?,
                    stripe_price_id = ?,
                    stripe_current_period_start = ?,
                    stripe_current_period_end = ?,
                    stripe_cancel_at_period_end = ?
                WHERE id = ?
                """,
                (
                    status,
                    price_id,
                    period_start,
                    period_end,
                    cancel_at_period_end,
                    user_id,
                ),
            )
            conn.commit()
        conn.close()

    return "", 200


@app.route("/health")
def health():
    return {"ok": True}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
