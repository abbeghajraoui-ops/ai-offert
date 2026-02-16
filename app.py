import os
import json
import time
import re
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

import streamlit as st

# PDF
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle

# AI (OpenAI) – robust import (nya + gamla SDK)
OPENAI_AVAILABLE = False
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
    _OPENAI_MODE = "new"
except Exception:
    try:
        import openai
        OPENAI_AVAILABLE = True
        _OPENAI_MODE = "old"
    except Exception:
        OPENAI_AVAILABLE = False
        _OPENAI_MODE = None


# ----------------------------
# Config / Secrets
# ----------------------------
APP_TITLE = "Offertly – offertmotor för bygg & VVS"

def sget(key: str, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return os.environ.get(key, default)

STRIPE_PRICE_ID_STARTER = sget("STRIPE_PRICE_ID_STARTER")
STRIPE_PRICE_ID_PRO = sget("STRIPE_PRICE_ID_PRO")
STRIPE_PRICE_ID_TEAM = sget("STRIPE_PRICE_ID_TEAM")

BACKEND_BASE_URL = (sget("BACKEND_BASE_URL") or "").rstrip("/")
APP_WEBHOOK_TOKEN = (sget("APP_WEBHOOK_TOKEN") or "").strip()

APP_BASE_URL = (sget("APP_BASE_URL") or "").rstrip("/")  # rekommenderas i secrets

OPENAI_API_KEY = (sget("OPENAI_API_KEY") or "").strip()


# ----------------------------
# Helpers (HTTP)
# ----------------------------
def backend_get(path: str, params: dict | None = None):
    if not BACKEND_BASE_URL:
        raise RuntimeError("BACKEND_BASE_URL saknas i secrets")

    url = BACKEND_BASE_URL + path
    if params:
        qs = urllib.parse.urlencode(params)
        url = url + ("&" if "?" in url else "?") + qs

    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {APP_WEBHOOK_TOKEN}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))

def backend_post(path: str, payload: dict):
    if not BACKEND_BASE_URL:
        raise RuntimeError("BACKEND_BASE_URL saknas i secrets")

    url = BACKEND_BASE_URL + path
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {APP_WEBHOOK_TOKEN}")

    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))

def ok_or_err(fn, *args, **kwargs):
    try:
        return True, fn(*args, **kwargs)
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8")
        except Exception:
            msg = str(e)
        return False, f"HTTP Error {e.code}: {msg}"
    except Exception as e:
        return False, str(e)


# ----------------------------
# AI
# ----------------------------
def _extract_json(text: str) -> dict | None:
    # Försök hitta JSON-objekt i texten
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def generate_offer_ai(company: str, customer: str, description: str) -> dict:
    """
    Returnerar strukturerad offert som dict.
    Om OpenAI saknas eller fel: returnerar fallback.
    """
    fallback = {
        "title": "Offert",
        "company": company,
        "customer": customer,
        "summary": "Offertutkast baserat på din beskrivning.",
        "scope": [
            "Planering och genomgång på plats",
            "Material och arbete enligt överenskommelse",
            "Städning och bortforsling (om tillämpligt)"
        ],
        "timeline": "Start enligt överenskommelse. Beräknad tidsåtgång: 1–3 veckor (beroende på omfattning).",
        "pricing_note": "Pris lämnas efter eventuell platsbesök/kompletterande underlag.",
        "terms": [
            "Offerten är giltig i 30 dagar",
            "Betalningsvillkor: 30 dagar",
            "ÄTA (ändring/tillägg) offereras separat"
        ]
    }

    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        return fallback

    prompt = f"""
Du är en svensk offertassistent för bygg & VVS. Svara ENDAST som JSON (utan ```).
Skapa en professionell offert baserat på input.

Returnera JSON med nycklar:
title, company, customer, summary, scope (lista), exclusions (lista), timeline,
pricing (lista med {{"item","qty","unit","unit_price_sek","total_sek"}}), total_sek,
rot_note, terms (lista), contact

Input:
Företag: {company}
Kund: {customer}
Beskrivning: {description}

Regler:
- Sätt rimliga placeholder-priser om pris saknas (men tydligt).
- Validera att total_sek = summan av pricing.total_sek.
- Skriv på svenska.
""".strip()

    try:
        if _OPENAI_MODE == "new":
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Du skriver svenska offerter som JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            text = resp.choices[0].message.content or ""
        else:
            openai.api_key = OPENAI_API_KEY
            resp = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Du skriver svenska offerter som JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            text = resp["choices"][0]["message"]["content"] or ""

        data = _extract_json(text)
        if not isinstance(data, dict):
            return fallback

        # Minimal sanity defaults
        data.setdefault("title", "Offert")
        data.setdefault("company", company)
        data.setdefault("customer", customer)
        data.setdefault("scope", [])
        data.setdefault("exclusions", [])
        data.setdefault("terms", [])
        return data

    except Exception:
        return fallback


# ----------------------------
# PDF
# ----------------------------
def build_offer_pdf(offer: dict) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    margin = 18 * mm
    x = margin
    y = height - margin

    # Header
    c.setFont("Helvetica-Bold", 18)
    c.drawString(x, y, offer.get("title", "Offert"))
    y -= 10 * mm

    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Datum: {datetime.now().strftime('%Y-%m-%d')}")
    y -= 5 * mm

    company = offer.get("company", "")
    customer = offer.get("customer", "")
    if company:
        c.drawString(x, y, f"Företag: {company}")
        y -= 5 * mm
    if customer:
        c.drawString(x, y, f"Kund: {customer}")
        y -= 8 * mm

    # Summary
    summary = offer.get("summary", "")
    if summary:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Sammanfattning")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, summary, x, y, width - 2*margin)
        y -= 4 * mm

    # Scope
    scope = offer.get("scope") or []
    if scope:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Omfattning")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        for item in scope[:25]:
            y = _draw_bullet(c, str(item), x, y, width - 2*margin)
        y -= 2 * mm

    # Exclusions
    exclusions = offer.get("exclusions") or []
    if exclusions:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Ingår ej")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        for item in exclusions[:20]:
            y = _draw_bullet(c, str(item), x, y, width - 2*margin)
        y -= 2 * mm

    # Timeline
    timeline = offer.get("timeline", "")
    if timeline:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Tidsplan")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, timeline, x, y, width - 2*margin)
        y -= 4 * mm

    # Pricing table
    pricing = offer.get("pricing") or []
    total_sek = offer.get("total_sek")

    if pricing:
        if y < 70*mm:
            c.showPage()
            y = height - margin

        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Pris")
        y -= 8 * mm

        rows = [["Post", "Antal", "Enhet", "á-pris (SEK)", "Summa (SEK)"]]
        for p in pricing[:30]:
            rows.append([
                str(p.get("item", ""))[:40],
                str(p.get("qty", "")),
                str(p.get("unit", "")),
                str(p.get("unit_price_sek", "")),
                str(p.get("total_sek", "")),
            ])

        tbl = Table(rows, colWidths=[70*mm, 18*mm, 18*mm, 28*mm, 28*mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
            ("FONTNAME", (0,1), (-1,-1), "Helvetica"),
            ("FONTSIZE", (0,0), (-1,-1), 9),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
        ]))
        w, h = tbl.wrapOn(c, width - 2*margin, y)
        tbl.drawOn(c, x, y - h)
        y = y - h - 6*mm

        if total_sek is not None:
            c.setFont("Helvetica-Bold", 11)
            c.drawString(x, y, f"Totalt: {total_sek} SEK")
            y -= 6*mm

    pricing_note = offer.get("pricing_note", "")
    if pricing_note:
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, pricing_note, x, y, width - 2*margin)
        y -= 4 * mm

    rot_note = offer.get("rot_note", "")
    if rot_note:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "ROT")
        y -= 5*mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, rot_note, x, y, width - 2*margin)
        y -= 4*mm

    # Terms
    terms = offer.get("terms") or []
    if terms:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Villkor")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        for t in terms[:25]:
            y = _draw_bullet(c, str(t), x, y, width - 2*margin)
        y -= 2*mm

    contact = offer.get("contact", "")
    if contact:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Kontakt")
        y -= 5*mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, contact, x, y, width - 2*margin)

    c.showPage()
    c.save()
    return buf.getvalue()


def _draw_paragraph(c, text, x, y, max_width, line_height=12):
    # enkel word-wrap
    words = (text or "").split()
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        if c.stringWidth(test, "Helvetica", 10) <= max_width:
            line = test
        else:
            c.drawString(x, y, line)
            y -= line_height
            line = w
            if y < 25*mm:
                c.showPage()
                y = A4[1] - 18*mm
                c.setFont("Helvetica", 10)
    if line:
        c.drawString(x, y, line)
        y -= line_height
    return y

def _draw_bullet(c, text, x, y, max_width):
    bullet = "• "
    indent = 10
    c.drawString(x, y, bullet)
    return _draw_paragraph(c, text, x + indent, y, max_width - indent)


# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="Offertly", layout="wide")
st.title(APP_TITLE)
st.caption("Välj paket, betala och skapa offerter (PDF).")

with st.sidebar:
    st.header("Inloggning")
    email = st.text_input("Email", placeholder="din@email.se").strip().lower()

    st.divider()
    st.subheader("Status (debug)")
    stripe_ok = all([STRIPE_PRICE_ID_STARTER, STRIPE_PRICE_ID_PRO, STRIPE_PRICE_ID_TEAM])
    st.write("Stripe:", "✅" if stripe_ok else "❌")
    st.write("BACKEND_BASE_URL:", "✅" if BACKEND_BASE_URL else "❌")
    st.write("APP_WEBHOOK_TOKEN:", "✅" if APP_WEBHOOK_TOKEN else "❌")
    st.write("Price IDs:", "✅" if stripe_ok else "❌")
    st.write("OpenAI API:", "✅" if (OPENAI_API_KEY and OPENAI_AVAILABLE) else "⚠️ (fallback)")

    st.divider()
    if st.button("Logga ut"):
        st.session_state.clear()
        st.rerun()

# Visa logo om fil finns
try:
    if os.path.exists("logo.png"):
        st.sidebar.image("logo.png", width=160)
except Exception:
    pass

if not email:
    st.info("Skriv din email för att fortsätta.")
    st.stop()

if not APP_WEBHOOK_TOKEN:
    st.error("APP_WEBHOOK_TOKEN saknas i Streamlit secrets.")
    st.stop()

ok, sub_resp = ok_or_err(backend_get, "/api/subscription", {"email": email})
if not ok:
    st.error(f"Backend error: {sub_resp}")
    st.stop()

active = bool(sub_resp.get("active"))

def go_checkout(price_id: str, plan_name: str):
    base = APP_BASE_URL or ""
    if not base:
        st.error("Sätt APP_BASE_URL i Streamlit secrets (t.ex. https://din-app.streamlit.app).")
        return

    payload = {
        "email": email,
        "price_id": price_id,
        "success_url": f"{base}?success=1",
        "cancel_url": f"{base}?cancel=1",
    }
    ok2, resp2 = ok_or_err(backend_post, "/api/create-checkout-session", payload)
    if not ok2:
        st.error(f"Kunde inte skapa checkout: {resp2}")
        return
    url = resp2.get("url")
    if not url:
        st.error("Ingen checkout-URL returnerades.")
        return

    st.success(f"Skickar dig till Stripe Checkout för {plan_name}…")
    st.link_button("Öppna Stripe Checkout", url)

# Om ej aktiv: visa paket
if not active:
    st.warning("Din plan är inte aktiv ännu. Välj ett paket för att fortsätta.")
    cols = st.columns(3)

    with cols[0]:
        st.subheader("Starter")
        st.write("✅ Offertgenerator")
        st.write("✅ PDF")
        if st.button("Välj Starter", use_container_width=True):
            go_checkout(STRIPE_PRICE_ID_STARTER, "Starter")

    with cols[1]:
        st.subheader("Pro")
        st.write("✅ Allt i Starter")
        st.write("✅ Mer kapacitet")
        if st.button("Välj Pro", use_container_width=True):
            go_checkout(STRIPE_PRICE_ID_PRO, "Pro")

    with cols[2]:
        st.subheader("Team")
        st.write("✅ Allt i Pro")
        st.write("✅ Flera användare")
        if st.button("Välj Team", use_container_width=True):
            go_checkout(STRIPE_PRICE_ID_TEAM, "Team")

    st.info("Efter betalning: uppdatera sidan om det tar några sekunder innan webbhooken slår igenom.")
    st.stop()

# ----------------------------
# Offertgenerator
# ----------------------------
st.success("Plan aktiv ✅ Du kan skapa offerter.")

st.header("Offertgenerator")

company = st.text_input("Företagsnamn", value="")
customer = st.text_input("Kundens namn", value="")
desc = st.text_area("Beskrivning", height=140, placeholder="t.ex. totalrenovering badrum, 6 kvm, rivning, tätskikt, kakel...")

col1, col2 = st.columns([1, 1], gap="large")

with col1:
    if st.button("Generera offert (AI)", type="primary", use_container_width=True):
        if not (company and customer and desc):
            st.error("Fyll i företagsnamn, kundnamn och beskrivning.")
        else:
            with st.spinner("Genererar offert..."):
                offer = generate_offer_ai(company, customer, desc)
                st.session_state["offer_data"] = offer
                # Bygg PDF direkt
                pdf_bytes = build_offer_pdf(offer)
                st.session_state["offer_pdf"] = pdf_bytes

offer_data = st.session_state.get("offer_data")
offer_pdf = st.session_state.get("offer_pdf")

with col2:
    st.subheader("PDF")
    if offer_pdf:
        filename = f"offert_{customer or 'kund'}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        st.download_button(
            "Ladda ner offert (PDF)",
            data=offer_pdf,
            file_name=filename,
            mime="application/pdf",
            use_container_width=True
        )
    else:
        st.info("Generera en offert först så får du PDF-knappen här.")

if offer_data:
    st.subheader("Utkast (granskning)")
    st.json(offer_data)
else:
    st.caption("När du genererar en offert visas utkastet här (och du får PDF-download).")













 






    










































