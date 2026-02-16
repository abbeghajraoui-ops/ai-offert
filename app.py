import os
import io
import base64
from datetime import datetime

import requests
import streamlit as st
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# OpenAI (supports newer client)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None


st.set_page_config(page_title="Offertly", page_icon="✅", layout="wide")

# ----------------------------
# Secrets / Env
# ----------------------------
def secret(name: str, default: str = "") -> str:
    # Streamlit Cloud: st.secrets
    if name in st.secrets:
        return str(st.secrets[name])
    # fallback: env
    return os.environ.get(name, default)


STRIPE_PRICE_ID_STARTER = secret("STRIPE_PRICE_ID_STARTER")
STRIPE_PRICE_ID_PRO = secret("STRIPE_PRICE_ID_PRO")
STRIPE_PRICE_ID_TEAM = secret("STRIPE_PRICE_ID_TEAM")

BACKEND_BASE_URL = secret("BACKEND_BASE_URL")  # ex: https://offertly-webbhook-production.up.railway.app
APP_WEBHOOK_TOKEN = secret("APP_WEBHOOK_TOKEN")

OPENAI_API_KEY = secret("OPENAI_API_KEY")

# ----------------------------
# Helpers
# ----------------------------
def backend_headers():
    return {"Authorization": f"Bearer {APP_WEBHOOK_TOKEN}"}


def backend_get_status(email: str):
    r = requests.get(
        f"{BACKEND_BASE_URL}/api/status",
        params={"email": email},
        headers=backend_headers(),
        timeout=20,
    )
    if r.status_code == 401:
        raise RuntimeError("Backend error: HTTP 401 Unauthorized (fel APP_WEBHOOK_TOKEN)")
    r.raise_for_status()
    return r.json()


def backend_create_checkout(email: str, price_id: str):
    app_url = secret("APP_BASE_URL", "").rstrip("/")  # set to your streamlit public URL
    if not app_url:
        # fallback to current app url is not reliable in streamlit; require secret ideally
        app_url = ""

    payload = {
        "email": email,
        "price_id": price_id,
        "success_url": app_url or st.experimental_get_query_params().get("app_url", [""])[0] or "https://example.com",
        "cancel_url": app_url or st.experimental_get_query_params().get("app_url", [""])[0] or "https://example.com",
    }

    # Best: set APP_BASE_URL in secrets to your streamlit URL (https://xxxxx.streamlit.app)
    if payload["success_url"] == "https://example.com":
        # still works, but redirect won't return to your app
        pass

    r = requests.post(
        f"{BACKEND_BASE_URL}/api/create-checkout-session",
        json=payload,
        headers=backend_headers(),
        timeout=20,
    )
    if r.status_code == 401:
        raise RuntimeError("Backend error: HTTP 401 Unauthorized (fel APP_WEBHOOK_TOKEN)")
    r.raise_for_status()
    return r.json()


def generate_offer_text(company: str, customer: str, description: str) -> str:
    if not OPENAI_API_KEY or OpenAI is None:
        return (
            f"OFFERT\n\nFöretag: {company}\nKund: {customer}\n\n"
            f"Beskrivning:\n{description}\n\n"
            "AI-nyckel saknas – detta är en placeholder-offert."
        )

    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = f"""
Du är en svensk offert-skrivare för bygg/VVS.
Skriv en professionell offert på svenska med:
- Rubrik
- Kort sammanfattning
- Arbetsmoment (punktlista)
- Material (punktlista, om relevant)
- Tidsplan
- Prisupplägg (utan exakta priser om okänt, men struktur)
- Villkor (betalning, giltighetstid 14 dagar, ROT nämn som rad "ROT kan tillämpas vid behov")
- Kontaktuppgifter plats för företag

Företag: {company}
Kund: {customer}
Jobbbeskrivning: {description}
""".strip()

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
    )
    return resp.choices[0].message.content.strip()


def offer_to_pdf_bytes(title: str, text: str) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    y = height - 60
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, title)
    y -= 30

    c.setFont("Helvetica", 11)
    for line in text.splitlines():
        if y < 60:
            c.showPage()
            c.setFont("Helvetica", 11)
            y = height - 60
        c.drawString(50, y, line[:110])
        y -= 16

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.read()


# ----------------------------
# UI
# ----------------------------
left, right = st.columns([1, 3], gap="large")

with left:
    st.markdown("### Inloggning")
    email = st.text_input("Email", placeholder="din@email.se").strip().lower()

    st.divider()
    st.markdown("### Status (debug)")

    checks = {
        "Stripe": bool(STRIPE_PRICE_ID_STARTER and STRIPE_PRICE_ID_PRO and STRIPE_PRICE_ID_TEAM),
        "BACKEND_BASE_URL": bool(BACKEND_BASE_URL),
        "APP_WEBHOOK_TOKEN": bool(APP_WEBHOOK_TOKEN),
        "Price IDs": bool(STRIPE_PRICE_ID_STARTER and STRIPE_PRICE_ID_PRO and STRIPE_PRICE_ID_TEAM),
    }
    for k, ok in checks.items():
        st.write(f"{k}: {'✅' if ok else '❌'}")

    st.divider()
    # logo (optional)
    if os.path.exists("logo.png"):
        st.image("logo.png", width=140)


with right:
    st.title("Offertly – offertmotor för bygg & VVS")
    st.caption("Välj paket, betala och skapa offerter (PDF).")

    if not email:
        st.info("Skriv din email för att fortsätta.")
        st.stop()

    # Fetch status
    status = None
    try:
        status = backend_get_status(email)
    except Exception as e:
        st.error(f"Backend error: {e}")
        st.stop()

    is_active = status.get("status") in ("active", "trialing")
    plan = status.get("plan")

    if not is_active:
        st.subheader("Välj paket")
        cols = st.columns(3)

        def plan_card(col, name, price_id, bullets):
            with col:
                st.markdown(f"#### {name}")
                for b in bullets:
                    st.write(f"• {b}")
                if st.button(f"Välj {name}", use_container_width=True):
                    try:
                        res = backend_create_checkout(email, price_id)
                        st.success("Öppnar Stripe Checkout…")
                        st.link_button("Gå till betalning", res["checkout_url"], use_container_width=True)
                    except Exception as e:
                        st.error(str(e))

        plan_card(cols[0], "Starter", STRIPE_PRICE_ID_STARTER, ["AI-offert", "PDF-export", "Grundmallar"])
        plan_card(cols[1], "Pro", STRIPE_PRICE_ID_PRO, ["Allt i Starter", "Bättre mallar", "Mer proffsigt flöde"])
        plan_card(cols[2], "Team", STRIPE_PRICE_ID_TEAM, ["Allt i Pro", "Flera användare (senare)", "Prioritet (senare)"])

        st.warning("Efter betalning kan det ta 10–30 sekunder innan webhooken uppdaterat status.")
        st.stop()

    # Active UI
    st.success(f"Plan aktiv: **{plan or 'ok'}** ✅")

    st.subheader("Offertgenerator")
    company = st.text_input("Företagsnamn", value="")
    customer = st.text_input("Kundens namn", value="")
    description = st.text_area("Beskrivning", height=140, placeholder="Ex: totalrenovering badrum, 6 kvm, kakel, golvvärme...")

    if st.button("Generera offert (AI)", use_container_width=True):
        if not company or not customer or not description:
            st.error("Fyll i företagsnamn, kund och beskrivning.")
            st.stop()

        with st.spinner("Skapar offert..."):
            offer_text = generate_offer_text(company, customer, description)

        st.text_area("Offert (text)", value=offer_text, height=300)

        pdf_bytes = offer_to_pdf_bytes(
            title=f"Offert – {customer} – {datetime.now().strftime('%Y-%m-%d')}",
            text=offer_text,
        )

        st.download_button(
            "Ladda ner PDF",
            data=pdf_bytes,
            file_name=f"offert_{customer}_{datetime.now().strftime('%Y%m%d')}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )











 






    







































