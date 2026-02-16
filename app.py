import os
import json
import time
import urllib.request
import urllib.parse
import streamlit as st
import stripe
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

# ----------------------------
# Secrets / config
# ----------------------------
def sget(key: str, default: str = "") -> str:
    # Streamlit Cloud: st.secrets
    try:
        return str(st.secrets.get(key, default)).strip()
    except Exception:
        return str(os.environ.get(key, default)).strip()

STRIPE_SECRET_KEY = sget("STRIPE_SECRET_KEY")
APP_WEBHOOK_TOKEN = sget("APP_WEBHOOK_TOKEN")  # same as Railway
BACKEND_BASE_URL = sget("BACKEND_BASE_URL")    # e.g. https://offertly-webbhook-production.up.railway.app
APP_BASE_URL = sget("APP_BASE_URL")            # your Streamlit app URL (used in success/cancel)
PRICE_STARTER = sget("STRIPE_PRICE_ID_STARTER")
PRICE_PRO = sget("STRIPE_PRICE_ID_PRO")
PRICE_TEAM = sget("STRIPE_PRICE_ID_TEAM")

if not STRIPE_SECRET_KEY:
    st.error("Missing STRIPE_SECRET_KEY in Streamlit Secrets")
    st.stop()

stripe.api_key = STRIPE_SECRET_KEY

# ----------------------------
# Helper: backend calls
# ----------------------------
def backend_get_subscription(email: str) -> dict:
    if not BACKEND_BASE_URL:
        return {"ok": False, "error": "BACKEND_BASE_URL missing"}
    if not APP_WEBHOOK_TOKEN:
        return {"ok": False, "error": "APP_WEBHOOK_TOKEN missing"}

    qs = urllib.parse.urlencode({"email": email})
    url = f"{BACKEND_BASE_URL.rstrip('/')}/api/subscription?{qs}"
    req = urllib.request.Request(
        url,
        headers={"X-APP-TOKEN": APP_WEBHOOK_TOKEN},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except Exception as e:
        return {"ok": False, "error": f"Backend error: {e}"}

def create_customer_portal(customer_id: str) -> str | None:
    if not customer_id:
        return None
    try:
        sess = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=APP_BASE_URL or "https://streamlit.io",
        )
        return sess.url
    except Exception:
        return None

# ----------------------------
# Offer / PDF helpers
# ----------------------------
def generate_offer_text(company: str, customer: str, description: str) -> str:
    # Robust, no external dependency required
    # (You can later plug in OpenAI, but this always works.)
    now = time.strftime("%Y-%m-%d")
    return f"""OFFERT – {now}

Företag: {company}
Kund: {customer}

Projektbeskrivning:
{description}

Förslag på upplägg:
- Planering & genomgång på plats
- Material & etablering
- Utförande enligt överenskommelse
- Avstämning och slutkontroll

Pris:
Pris fastställs efter platsbesök / kompletterande underlag.

Villkor:
- Offerten gäller i 14 dagar
- Betalningsvillkor: 10 dagar
- Eventuella tillägg debiteras enligt godkännande

Kontakt:
{company}
"""

def pdf_bytes_from_text(title: str, text: str) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    x = 50
    y = height - 60

    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y, title)
    y -= 30

    c.setFont("Helvetica", 11)

    for line in text.splitlines():
        if y < 60:
            c.showPage()
            c.setFont("Helvetica", 11)
            y = height - 60
        c.drawString(x, y, line[:120])
        y -= 16

    c.save()
    buffer.seek(0)
    return buffer.read()

# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="Offertly", layout="wide")

st.title("Offertly – offertmotor för bygg & VVS")
st.caption("Välj paket, betala och skapa offerter (PDF).")

with st.sidebar:
    st.subheader("Inloggning")
    email = st.text_input("Email", placeholder="din@email.se").strip().lower()
    st.divider()

    # Basic diagnostics
    st.caption("Status (debug)")
    st.write("Stripe:", "✅" if STRIPE_SECRET_KEY else "❌")
    st.write("BACKEND_BASE_URL:", "✅" if BACKEND_BASE_URL else "❌")
    st.write("APP_BASE_URL:", "✅" if APP_BASE_URL else "❌")
    st.write("Price IDs:", "✅" if (PRICE_STARTER and PRICE_PRO and PRICE_TEAM) else "❌")

if not email:
    st.info("Skriv din email för att fortsätta.")
    st.stop()

sub = backend_get_subscription(email)
if not sub.get("ok"):
    st.error(sub.get("error", "Okänt fel när appen försökte kontakta backend."))
    st.stop()

active = bool(sub.get("active"))
status = sub.get("status", "")

col1, col2 = st.columns([2, 1], gap="large")

with col2:
    st.subheader("Abonnemang")
    if active:
        st.success(f"Aktiv ({status})")
        portal_url = create_customer_portal(sub.get("customer_id", ""))
        if portal_url:
            st.link_button("Hantera abonnemang", portal_url)
    else:
        st.warning("Inte aktivt")

with col1:
    if not active:
        st.subheader("Välj paket & betala")

        plans = {
            "Starter": PRICE_STARTER,
            "Pro": PRICE_PRO,
            "Team": PRICE_TEAM,
        }
        plan_name = st.radio("Paket", list(plans.keys()), horizontal=True)
        chosen_price = plans[plan_name]

        if not chosen_price:
            st.error("Saknar Price ID i Secrets för valt paket.")
            st.stop()

        if st.button("Gå till Stripe Checkout", type="primary"):
            if not APP_BASE_URL:
                st.error("APP_BASE_URL saknas i Streamlit Secrets (behövs för success/cancel).")
                st.stop()

            try:
                checkout = stripe.checkout.Session.create(
                    mode="subscription",
                    customer_email=email,
                    line_items=[{"price": chosen_price, "quantity": 1}],
                    success_url=f"{APP_BASE_URL}?success=1&session_id={{CHECKOUT_SESSION_ID}}",
                    cancel_url=f"{APP_BASE_URL}?canceled=1",
                    allow_promotion_codes=True,
                )
                st.success("Checkout skapad!")
                st.link_button("Öppna betalning", checkout.url)
                st.caption("Efter betalning uppdateras din status via webhook (kan ta några sekunder).")
            except Exception as e:
                st.error(f"Kunde inte skapa Checkout: {e}")

        st.divider()
        st.subheader("Efter betalning")
        st.write("Tryck här om du vill uppdatera status manuellt:")
        if st.button("Uppdatera abonnemangstatus"):
            st.rerun()

    else:
        st.subheader("Offertgenerator")

        company = st.text_input("Företagsnamn", value="", placeholder="Ex: Bygg & VVS AB")
        customer = st.text_input("Kundens namn", value="", placeholder="Ex: Anna Andersson")
        description = st.text_area("Beskrivning", value="", height=160, placeholder="Ex: totalrenovering badrum...")

        if st.button("Generera offert (PDF)", type="primary"):
            if not company or not customer or not description:
                st.error("Fyll i företagsnamn, kundens namn och beskrivning.")
                st.stop()

            text = generate_offer_text(company, customer, description)
            pdf = pdf_bytes_from_text("Offertly – Offert", text)

            st.success("Offert klar!")
            st.text_area("Förhandsvisning (text)", value=text, height=220)
            st.download_button(
                "Ladda ner PDF",
                data=pdf,
                file_name=f"offert_{customer.replace(' ', '_')}.pdf",
                mime="application/pdf",
            )











 






    





































