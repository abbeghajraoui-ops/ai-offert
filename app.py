import os
import json
import re
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

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
# Secrets
# ----------------------------
APP_TITLE = "Offertly"

def sget(key: str, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return os.environ.get(key, default)

STRIPE_PRICE_ID_STARTER = (sget("STRIPE_PRICE_ID_STARTER") or "").strip()
STRIPE_PRICE_ID_PRO = (sget("STRIPE_PRICE_ID_PRO") or "").strip()
STRIPE_PRICE_ID_TEAM = (sget("STRIPE_PRICE_ID_TEAM") or "").strip()

BACKEND_BASE_URL = (sget("BACKEND_BASE_URL") or "").rstrip("/")
APP_BASE_URL = (sget("APP_BASE_URL") or "").rstrip("/")

# Token mellan Streamlit <-> backend (stödjer båda namn)
APP_API_TOKEN = ((sget("APP_API_TOKEN") or "").strip() or (sget("APP_WEBHOOK_TOKEN") or "").strip())

OPENAI_API_KEY = (sget("OPENAI_API_KEY") or "").strip()

# ----------------------------
# HTTP helpers
# ----------------------------
def backend_get(path: str, params: dict | None = None):
    if not BACKEND_BASE_URL:
        raise RuntimeError("BACKEND_BASE_URL saknas i secrets")
    url = BACKEND_BASE_URL + path
    if params:
        qs = urllib.parse.urlencode(params)
        url = url + ("&" if "?" in url else "?") + qs
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {APP_API_TOKEN}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))

def backend_post(path: str, payload: dict):
    if not BACKEND_BASE_URL:
        raise RuntimeError("BACKEND_BASE_URL saknas i secrets")
    url = BACKEND_BASE_URL + path
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {APP_API_TOKEN}")
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
# Branschprofiler (för “känns som för mig” + bättre offert)
# ----------------------------
INDUSTRIES = {
    "VVS": {
        "scope_defaults": ["Genomgång på plats", "Material och montering enligt överenskommelse", "Trycktest och funktionskontroll", "Städning av arbetsyta"],
        "excl_defaults": ["Dolda fel i befintliga rör/installationer", "Åtgärder utanför överenskommen omfattning", "Tillval efter start (offereras separat)"],
        "trust": ["Tydlig omfattning – inga överraskningar", "Ändringar/tillägg (ÄTA) hanteras skriftligt", "Vi återkopplar löpande under projektet"],
    },
    "El": {
        "scope_defaults": ["Genomgång av behov på plats", "Installation enligt gällande regler", "Funktionstest och dokumentation", "Städning av arbetsyta"],
        "excl_defaults": ["Åtgärder i befintliga dolda fel", "Extra uttag/armaturer som tillkommer efter start", "Byggarbete utöver elinstallationen"],
        "trust": ["Tydlig offert – du vet vad du betalar för", "Säkerhetsfokus och ordning på arbetsplatsen", "ÄTA offereras innan utförande"],
    },
    "Snickare/Bygg": {
        "scope_defaults": ["Etablering och skydd av ytor", "Rivning/bygg enligt överenskommelse", "Montering och färdigställande", "Städning av arbetsyta"],
        "excl_defaults": ["Dolda fel i konstruktion", "Tillval efter start", "Myndighetsavgifter (om tillämpligt)"],
        "trust": ["Tydlig tidsplan och avstämningar", "ÄTA hanteras skriftligt", "Vi lämnar ett snyggt slutresultat"],
    },
    "Plattsättning/Badrum": {
        "scope_defaults": ["Rivning av befintligt (om avtalat)", "Tätskikt enligt branschregler", "Kakel/klinker montering", "Fogning och silikon", "Städning av arbetsyta"],
        "excl_defaults": ["Dolda fel i underlag/konstruktion", "Extra tillval (nischer, specialmönster) efter start", "El/VVS utanför badrummet"],
        "trust": ["Tydlig omfattning och materialval", "ÄTA offereras innan utförande", "Fokus på ett hållbart resultat"],
    },
    "Målning/Golv": {
        "scope_defaults": ["Täckning och skydd", "Förarbete", "Målning/läggning enligt överenskommelse", "Städning av arbetsyta"],
        "excl_defaults": ["Dolda fel i underlag", "Extra ytor som tillkommer", "Flytt/storstäd om ej avtalat"],
        "trust": ["Tydlig offert och avstämningar", "ÄTA hanteras skriftligt", "Snyggt och rent avslut"],
    },
    "Städ": {
        "scope_defaults": ["Genomgång av ytor och behov", "Städning enligt checklista", "Kvalitetskontroll", "Återkoppling efter utfört jobb"],
        "excl_defaults": ["Specialrengöring som ej avtalats (t.ex. sanering)", "Extra utrymmen som tillkommer", "Material/kem utöver standard om ej avtalat"],
        "trust": ["Tydlig checklista", "Vi följer upp kvalitet", "Enkelt att lägga till/ta bort moment"],
    },
    "Arkitekt/Ingenjör/Konstruktör": {
        "scope_defaults": ["Startmöte och behovsanalys", "Underlag/ritningar enligt överenskommelse", "Revideringar (antal enligt paket)", "Leverans i överenskommet format"],
        "excl_defaults": ["Bygglovsavgifter/myndighetskostnader", "Extra revideringar utöver avtal", "Konsultation utanför omfattning"],
        "trust": ["Tydliga leveranser och deadlines", "Transparens i arbetstid", "Ändringar offereras innan arbete"],
    },
}

JOB_TYPES = [
    "Servicejobb",
    "Badrum",
    "Kök",
    "Renovering",
    "Ombyggnation",
    "Tillbyggnad",
    "Nyproduktion",
    "Felsökning",
    "Projektering/Ritning",
    "Städning",
    "Annat",
]

# ----------------------------
# AI
# ----------------------------
def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def generate_offer_ai(company: str, customer: str, description: str, industry: str, job_type: str) -> dict:
    prof = INDUSTRIES.get(industry, {})
    fallback = {
        "title": f"Offert – {job_type}",
        "company": company,
        "customer": customer,
        "summary": "Tack för er förfrågan. Nedan följer vårt förslag baserat på era önskemål.",
        "scope": prof.get("scope_defaults", [])[:],
        "exclusions": prof.get("excl_defaults", [])[:],
        "timeline": "Start enligt överenskommelse. Beräknad tidsåtgång: 1–3 veckor (beroende på omfattning).",
        "pricing": [
            {"item": "Arbete", "qty": 1, "unit": "st", "unit_price_sek": 0, "total_sek": 0},
            {"item": "Material", "qty": 1, "unit": "st", "unit_price_sek": 0, "total_sek": 0},
        ],
        "total_sek": 0,
        "rot_note": "ROT-avdrag kan vara möjligt (om arbetet uppfyller villkoren). Slutligt avdrag fastställs av Skatteverket.",
        "trust_points": prof.get("trust", [])[:],
        "terms": [
            "Offerten är giltig i 30 dagar",
            "Betalningsvillkor: 10 dagar efter slutfört arbete (om inget annat avtalas)",
            "ÄTA (ändringar/tillägg) offereras och godkänns innan utförande",
        ],
        "next_steps": "Om ni vill gå vidare: svara och bekräfta offerten. Vi återkommer för att boka startdatum och praktiska detaljer.",
        "contact": f"{company}\nTelefon: \nE-post: ",
    }

    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        return fallback

    prompt = f"""
Du är en svensk offertassistent. Skriv för privatperson: tryggt, tydligt och säljande utan överdrifter.
Svara ENDAST som JSON (utan ```).

Returnera JSON med nycklar:
title, company, customer,
summary,
scope (lista),
exclusions (lista),
timeline,
pricing (lista av {{"item","qty","unit","unit_price_sek","total_sek"}}),
total_sek,
rot_note,
trust_points (lista),
terms (lista),
next_steps,
contact.

Bransch: {industry}
Typ av jobb: {job_type}

Input:
Företag: {company}
Kund: {customer}
Beskrivning: {description}

Regler:
- Offerten ska kännas proffsig och lätt att förstå.
- Om pris inte finns i texten: skapa relevanta prisrader men låt beloppen vara 0.
- total_sek ska vara summan av pricing.total_sek.
- Lägg till “Trygghet”-punkter (trust_points) som ökar förtroendet.
""".strip()

    try:
        if _OPENAI_MODE == "new":
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Du skriver svenska offerter som strikt JSON."},
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
                    {"role": "system", "content": "Du skriver svenska offerter som strikt JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            text = resp["choices"][0]["message"]["content"] or ""

        data = _extract_json(text)
        if not isinstance(data, dict):
            return fallback

        # Defaults
        data.setdefault("title", fallback["title"])
        data.setdefault("company", company)
        data.setdefault("customer", customer)
        data.setdefault("scope", fallback["scope"])
        data.setdefault("exclusions", fallback["exclusions"])
        data.setdefault("pricing", fallback["pricing"])
        data.setdefault("terms", fallback["terms"])
        data.setdefault("trust_points", fallback["trust_points"])
        data.setdefault("rot_note", fallback["rot_note"])
        data.setdefault("next_steps", fallback["next_steps"])
        data.setdefault("contact", fallback["contact"])
        return data

    except Exception:
        return fallback


# ----------------------------
# PDF builders: Offert / Kontrakt / Faktura
# ----------------------------
def _draw_paragraph(c, text, x, y, max_width, font="Helvetica", size=10, line_height=12):
    c.setFont(font, size)
    words = (text or "").split()
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        if c.stringWidth(test, font, size) <= max_width:
            line = test
        else:
            c.drawString(x, y, line)
            y -= line_height
            line = w
            if y < 25 * mm:
                c.showPage()
                y = A4[1] - 18 * mm
                c.setFont(font, size)
    if line:
        c.drawString(x, y, line)
        y -= line_height
    return y

def _draw_bullet(c, text, x, y, max_width):
    bullet = "• "
    indent = 10
    c.setFont("Helvetica", 10)
    c.drawString(x, y, bullet)
    return _draw_paragraph(c, text, x + indent, y, max_width - indent)

def _pricing_table(c, x, y, width, pricing):
    rows = [["Post", "Antal", "Enhet", "á-pris (SEK)", "Summa (SEK)"]]
    for p in pricing[:30]:
        rows.append([
            str(p.get("item", ""))[:50],
            str(p.get("qty", "")),
            str(p.get("unit", "")),
            str(p.get("unit_price_sek", "")),
            str(p.get("total_sek", "")),
        ])
    tbl = Table(rows, colWidths=[78 * mm, 16 * mm, 16 * mm, 26 * mm, 26 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    w, h = tbl.wrapOn(c, width, y)
    tbl.drawOn(c, x, y - h)
    return y - h - 6 * mm

def build_offer_pdf(offer: dict) -> bytes:
    return _build_document_pdf(doc_type="offert", offer=offer)

def build_contract_pdf(offer: dict) -> bytes:
    return _build_document_pdf(doc_type="kontrakt", offer=offer)

def build_invoice_pdf(offer: dict) -> bytes:
    return _build_document_pdf(doc_type="faktura", offer=offer)

def _build_document_pdf(doc_type: str, offer: dict) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    margin = 18 * mm
    x = margin
    y = height - margin
    maxw = width - 2 * margin

    company = offer.get("company", "")
    customer = offer.get("customer", "")

    # Title
    title = offer.get("title", "Offert")
    if doc_type == "kontrakt":
        title = "Kontrakt (baserat på offert)"
    if doc_type == "faktura":
        title = "Faktura (baserat på offert)"

    c














 






    











































