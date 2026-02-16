import os
import json
import requests
import streamlit as st
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from io import BytesIO

# --- ENV (Streamlit secrets) ---
BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "").rstrip("/")  # ex: https://offertly-webbhook-production.up.railway.app
SUB_PRICE_ID = os.environ.get("SUB_PRICE_ID", "")                      # samma som på Railway
CREDITS_PRICE_ID = os.environ.get("CREDITS_PRICE_ID", "")              # samma som på Railway


st.set_page_config(page_title="Offertly", layout="wide")

st.title("Offertly – offertmotor för bygg & VVS")
st.caption("Skapa en proffsig offert på under 60 sekunder.")

if not BACKEND_BASE_URL:
    st.error("BACKEND_BASE_URL saknas i Streamlit secrets. Lägg till den först.")
    st.stop()


def backend_get_user(email: str):
    r = requests.get(f"{BACKEND_BASE_URL}/api/user", params={"email": email}, timeout=20)
    r.raise_for_status()
    return r.json()


def backend_create_checkout(email: str, mode: str, price_id: str, credits_to_add: int = 0):
    payload = {
        "email": email,
        "mode": mode,
        "price_id": price_id,
        "credits_to_add": credits_to_add
    }
    r = requests.post(f"{BACKEND_BASE_URL}/api/create-checkout-session", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["url"]


def backend_consume_credit(email: str, amount: int = 1):
    r = requests.post(f"{BACKEND_BASE_URL}/api/consume-credit", json={"email": email, "amount": amount}, timeout=20)
    if r.status_code == 200:
        return r.json()
    return r.json()


def make_simple_pdf(company: str, customer: str, description: str, total_sek: int = 0) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, h - 60, "OFFERT")

    c.setFont("Helvetica", 11)
    c.drawString(50, h - 95, f"Företag: {company}")
    c.drawString(50, h - 115, f"Kund: {customer}")
    c.drawString(50, h - 135, f"Datum: {datetime.now().strftime('%Y-%m-%d')}")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, h - 170, "Beskrivning")
    c.setFont("Helvetica", 11)
    text = c.beginText(50, h - 190)
    for line in (description or "").splitlines():
        text.textLine(line)
    c.drawText(text)

    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, 120, "Summa (SEK)")
    c.setFont("Helvetica", 12)
    c.drawString(50, 100, f"{total_sek:,}".replace(",", " "))

    c.setFont("Helvetica", 9)
    c.drawString(50, 60, "Offertly – genererad offert (MVP).")
    c.showPage()
    c.save()
    return buf.getvalue()


# --- Sidebar login ---
with st.sidebar:
    st.image("logo.png", width=160) if os.path.exists("logo.png") else st.write("Offertly")
    st.write("---")
    email = st.text_input("Logga in med din email", placeholder="du@firma.se").strip().lower()

    if email:
        try:
            user = backend_get_user(email)
        except Exception as e:
            st.error(f"Kunde inte läsa status från backend: {e}")
            st.stop()

        st.write(f"Inloggad som: **{email}**")
        st.write(f"Plan: **{'pro' if user.get('subscription_active') else 'credits' if user.get('credits',0)>0 else 'free'}**")
        st.write(f"Credits: **{user.get('credits',0)}**")
        st.write(f"Abonnemang aktivt: **{user.get('subscription_active')}**")

        st.write("---")
        st.subheader("Köp")

        colA, colB = st.columns(2)

        with colA:
            st.caption("Pro (abonnemang) – obegränsat")
            if st.button("Starta Pro-abonnemang", use_container_width=True, disabled=not bool(SUB_PRICE_ID)):
                url = backend_create_checkout(email, mode="subscription", price_id=SUB_PRICE_ID)
                st.link_button("Öppna betalning", url, use_container_width=True)

        with colB:
            st.caption("Credits (engångsköp) – t.ex. 10 offerter")
            if st.button("Köp 10 credits", use_container_width=True, disabled=not bool(CREDITS_PRICE_ID)):
                url = backend_create_checkout(email, mode="payment", price_id=CREDITS_PRICE_ID, credits_to_add=10)
                st.link_button("Öppna betalning", url, use_container_width=True)

        st.write("---")
        st.caption("Status-check")
        if st.button("Uppdatera status", use_container_width=True):
            st.rerun()
    else:
        st.info("Skriv din email för att fortsätta.")
        st.stop()


# --- Main app (offertgenerator) ---
st.header("Offertgenerator")

company = st.text_input("Företagsnamn", value="")
customer = st.text_input("Kundens namn", value="")
desc = st.text_area("Beskrivning", height=140, placeholder="Ex: totalrenovering badrum, 6 kvm...")

# Refresh status in main too
user = backend_get_user(email)
sub_active = bool(user.get("subscription_active"))
credits = int(user.get("credits") or 0)
can_generate = sub_active or credits > 0

if not can_generate:
    st.warning("Du behöver **Pro-abonnemang** eller **credits** för att generera offerter.")
    st.stop()

if st.button("Generera offert (AI)", use_container_width=True):
    # 1) If credits mode => consume 1 credit
    if not sub_active:
        res = backend_consume_credit(email, amount=1)
        if not res.get("ok"):
            st.error("Inga credits kvar. Köp credits eller starta Pro.")
            st.stop()

    # 2) Generate a simple, stable PDF (du kan byta till AI senare)
    pdf_bytes = make_simple_pdf(
        company=company or "Ditt företag",
        customer=customer or "Kund",
        description=desc or "Beskrivning saknas.",
        total_sek=0
    )

    st.success("Offert skapad!")
    st.download_button(
        "Ladda ner PDF",
        data=pdf_bytes,
        file_name="offertly-offert.pdf",
        mime="application/pdf",
        use_container_width=True
    )










 






    




































