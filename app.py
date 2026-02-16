import os
import time
import json
import requests
import streamlit as st
import stripe
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from io import BytesIO

# OpenAI (nya SDK)
from openai import OpenAI

# -----------------------
# Config / Env
# -----------------------
st.set_page_config(page_title="Offertly", layout="wide")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")  # valfri, men bra att ha
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")  # din streamlit app URL
WEBHOOK_STATUS_URL = os.environ.get("WEBHOOK_STATUS_URL", "").rstrip("/")  # t.ex. https://offertly-webbhook...railway.app/status
APP_WEBHOOK_TOKEN = os.environ.get("APP_WEBHOOK_TOKEN", "")

PRICE_STARTER = os.environ.get("STRIPE_PRICE_ID_STARTER", "")
PRICE_PRO = os.environ.get("STRIPE_PRICE_ID_PRO", "")
PRICE_TEAM = os.environ.get("STRIPE_PRICE_ID_TEAM", "")

stripe.api_key = STRIPE_SECRET_KEY
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# -----------------------
# Helpers
# -----------------------
def env_ok():
    checks = {
        "OpenAI": bool(OPENAI_API_KEY),
        "Stripe Secret": bool(STRIPE_SECRET_KEY),
        "APP_BASE_URL": bool(APP_BASE_URL),
        "WEBHOOK_STATUS_URL": bool(WEBHOOK_STATUS_URL),
        "Price IDs": bool(PRICE_STARTER and PRICE_PRO and PRICE_TEAM),
        "APP_WEBHOOK_TOKEN": bool(APP_WEBHOOK_TOKEN),
    }
    return checks


def get_plan_status(email: str):
    """Fråga Railway-webhook-servicen vilken plan användaren har."""
    if not WEBHOOK_STATUS_URL or not APP_WEBHOOK_TOKEN:
        return {"plan": "starter", "status": "inactive"}

    try:
        r = requests.get(
            WEBHOOK_STATUS_URL,
            params={"email": email, "token": APP_WEBHOOK_TOKEN},
            timeout=10,
        )
        if r.status_code != 200:
            return {"plan": "starter", "status": "inactive"}
        return r.json()
    except Exception:
        return {"plan": "starter", "status": "inactive"}


def create_checkout_session(email: str, price_id: str):
    if not APP_BASE_URL:
        raise RuntimeError("APP_BASE_URL saknas")

    # Stripe Checkout för subscription
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{APP_BASE_URL}/?success=1",
        cancel_url=f"{APP_BASE_URL}/?canceled=1",
        allow_promotion_codes=True,
    )
    return session.url


def generate_offer_ai(company: str, customer: str, description: str, plan: str) -> str:
    if not client:
        raise RuntimeError("OPENAI_API_KEY saknas")

    # Lite “plan gating”: pro/team kan få mer detaljer
    detail_level = "normal"
    if plan == "pro":
        detail_level = "hög"
    if plan == "team":
        detail_level = "mycket hög (inkl. risker, tidplan och tydliga antaganden)"

    prompt = f"""
Du är en svensk offert-assistent för bygg & VVS.
Skriv en professionell offert på svenska.
Detaljnivå: {detail_level}.

Företag: {company}
Kund: {customer}
Projektbeskrivning: {description}

Krav:
- Rubrik
- Kort sammanfattning
- Omfattning (punktlista)
- Material/arbete (punktlista)
- Tidplan (ungefärlig)
- Pris (ange intervall + vad som påverkar)
- Betalningsvillkor
- Giltighetstid för offert
- Ansvars-/förbehåll
- Kontaktuppgifter (lämna plats för telefon/mejl)
Svara som ren text med tydliga rubriker.
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Du skriver tydliga, seriösa offerter på svenska."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
    )
    return resp.choices[0].message.content.strip()


def offer_to_pdf_bytes(title: str, text: str) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    x = 50
    y = height - 60

    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y, title)
    y -= 30

    c.setFont("Helvetica", 10)

    # enkel radbrytning
    max_chars = 100
    for paragraph in text.split("\n"):
        lines = []
        p = paragraph.strip()
        if not p:
            lines.append("")
        else:
            while len(p) > max_chars:
                lines.append(p[:max_chars])
                p = p[max_chars:]
            lines.append(p)

        for line in lines:
            if y < 60:
                c.showPage()
                c.setFont("Helvetica", 10)
                y = height - 60
            c.drawString(x, y, line)
            y -= 14

    c.save()
    buffer.seek(0)
    return buffer.read()


# -----------------------
# UI
# -----------------------
checks = env_ok()

with st.sidebar:
    st.image("logo.png", width=160) if os.path.exists("logo.png") else st.write("Offertly")
    st.markdown("---")

    st.caption("Status")
    st.write("OpenAI:", "✅" if checks["OpenAI"] else "❌")
    st.write("Stripe:", "✅" if checks["Stripe Secret"] else "❌")
    st.write("APP_BASE_URL:", "✅" if checks["APP_BASE_URL"] else "❌")
    st.write("WEBHOOK_STATUS_URL:", "✅" if checks["WEBHOOK_STATUS_URL"] else "❌")
    st.write("Price IDs:", "✅" if checks["Price IDs"] else "❌")
    st.write("APP_WEBHOOK_TOKEN:", "✅" if checks["APP_WEBHOOK_TOKEN"] else "❌")

    st.markdown("---")

# Enkel “login”: email
st.title("Offertly – offertmotor för bygg & VVS")
st.write("Skapa en proffsig offert på under 60 sekunder.")

params = st.query_params
if params.get("success"):
    st.success("Betalning klar! Det kan ta 10–60 sek innan din plan blir aktiv (Stripe webhook).")
if params.get("canceled"):
    st.warning("Betalning avbruten.")

if "email" not in st.session_state:
    st.session_state.email = ""

email = st.text_input("Logga in med din email", value=st.session_state.email).strip().lower()
if email:
    st.session_state.email = email

if not email:
    st.info("Skriv din email för att fortsätta.")
    st.stop()

plan_info = get_plan_status(email)
plan = plan_info.get("plan", "starter")
status = plan_info.get("status", "inactive")

cols = st.columns([2, 1])
with cols[0]:
    st.subheader("Offertgenerator")
with cols[1]:
    st.markdown(f"**Inloggad som:** {email}")
    st.markdown(f"**Plan:** {plan}")
    st.markdown(f"**Status:** {status}")

# Om inte aktiv → visa betalplaner
if status != "active":
    st.warning("Din plan är inte aktiv ännu. Välj paket och betala för att låsa upp offertgeneratorn.")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("### Starter")
        st.caption("Grundfunktioner")
        if st.button("Välj Starter", use_container_width=True):
            try:
                url = create_checkout_session(email, PRICE_STARTER)
                st.link_button("Öppna betalning", url, use_container_width=True)
            except Exception as e:
                st.error(f"Stripe-fel: {e}")

    with c2:
        st.markdown("### Pro")
        st.caption("Mer detaljerade offerter")
        if st.button("Välj Pro", use_container_width=True):
            try:
                url = create_checkout_session(email, PRICE_PRO)
                st.link_button("Öppna betalning", url, use_container_width=True)
            except Exception as e:
                st.error(f"Stripe-fel: {e}")

    with c3:
        st.markdown("### Team")
        st.caption("Bäst för team / fler offerter")
        if st.button("Välj Team", use_container_width=True):
            try:
                url = create_checkout_session(email, PRICE_TEAM)
                st.link_button("Öppna betalning", url, use_container_width=True)
            except Exception as e:
                st.error(f"Stripe-fel: {e}")

    st.info("När du betalat: uppdatera sidan (F5).")
    st.stop()

# Aktiv plan → visa formulär
company = st.text_input("Företagsnamn", value="")
customer = st.text_input("Kundens namn", value="")
description = st.text_area("Beskrivning", value="", height=140)

btn = st.button("Generera offert (AI)", use_container_width=True)

if btn:
    if not checks["OpenAI"]:
        st.error("OPENAI_API_KEY saknas i Streamlit.")
        st.stop()

    if not company or not customer or not description:
        st.error("Fyll i företagsnamn, kundens namn och beskrivning.")
        st.stop()

    with st.spinner("Skapar offert..."):
        try:
            offer_text = generate_offer_ai(company, customer, description, plan)
            st.session_state.offer_text = offer_text
        except Exception as e:
            st.error(f"Fel vid AI: {e}")
            st.stop()

if "offer_text" in st.session_state and st.session_state.offer_text:
    st.markdown("## Förhandsvisning")
    st.text(st.session_state.offer_text)

    pdf_bytes = offer_to_pdf_bytes(
        title=f"Offert – {customer}",
        text=st.session_state.offer_text,
    )

    st.download_button(
        "Ladda ner PDF",
        data=pdf_bytes,
        file_name=f"offert_{customer.replace(' ', '_')}.pdf",
        mime="application/pdf",
        use_container_width=True,
    )









 






    



































