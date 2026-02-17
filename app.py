import os
import json
import re
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

import streamlit as st
import pandas as pd

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


# ============================
# Secrets / Config
# ============================
APP_TITLE = "Offertly"

def sget(key: str, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return os.environ.get(key, default)

def sbool(key: str, default: bool = False) -> bool:
    val = str(sget(key, str(default))).strip().lower()
    return val in ("1", "true", "yes", "y", "on")

SHOW_DEBUG = sbool("SHOW_DEBUG", False)  # debug OFF default

BACKEND_BASE_URL = (sget("BACKEND_BASE_URL") or "").rstrip("/")
APP_API_TOKEN = ((sget("APP_API_TOKEN") or "").strip() or (sget("APP_WEBHOOK_TOKEN") or "").strip())

APP_BASE_URL = (sget("APP_BASE_URL") or "").rstrip("/")

STRIPE_PRICE_ID_STARTER = (sget("STRIPE_PRICE_ID_STARTER") or "").strip()
STRIPE_PRICE_ID_PRO = (sget("STRIPE_PRICE_ID_PRO") or "").strip()
STRIPE_PRICE_ID_TEAM = (sget("STRIPE_PRICE_ID_TEAM") or "").strip()

OPENAI_API_KEY = (sget("OPENAI_API_KEY") or "").strip()

ROT_RATE_DEFAULT = float(sget("ROT_RATE", 0.30))  # 30% default


# ============================
# Safe casting (never crash)
# ============================
def as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False, indent=2)
        except Exception:
            return str(v)
    return str(v)

def to_float(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, bool):
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip().replace(" ", "").replace(",", ".")
        if s == "" or s.lower() == "none":
            return default
        return float(s)
    except Exception:
        return default

def to_int(x, default=0) -> int:
    try:
        return int(round(to_float(x, float(default))))
    except Exception:
        return default

def as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, dict):
        return [as_text(v)]
    return [as_text(v)]


# ============================
# HTTP helpers
# ============================
def backend_get(path: str, params: dict | None = None):
    if not BACKEND_BASE_URL:
        raise RuntimeError("BACKEND_BASE_URL saknas")

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
        raise RuntimeError("BACKEND_BASE_URL saknas")

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


# ============================
# Industry profiles
# ============================
INDUSTRIES = {
    "VVS": {
        "scope_defaults": [
            "Förberedelse och skydd av ytor",
            "Demontering vid behov",
            "Installation/byte av VVS-komponenter enligt överenskommelse",
            "Funktionskontroll",
            "Städning av arbetsområdet"
        ],
        "exclusions_defaults": [
            "Dolda fel i väggar/golv (t.ex. fuktskador/rördragning som inte syns)",
            "Åtgärder utanför överenskommet arbetsområde",
            "Tillval/ändringar efter start (ÄTA) utan skriftlig överenskommelse"
        ],
        "trust_points": [
            "Tydlig omfattning – du vet vad du får",
            "ÄTA hanteras skriftligt innan arbete utförs",
            "Vi håller dig uppdaterad under arbetets gång"
        ],
    },
    "El": {
        "scope_defaults": [
            "Planering och genomgång vid behov",
            "Installation/byte av elkomponenter enligt överenskommelse",
            "Mätning/funktionskontroll",
            "Enkel återställning av arbetsområde"
        ],
        "exclusions_defaults": [
            "Felsökning utöver överenskommelse",
            "Dolda fel i befintlig anläggning",
            "Tillval/ändringar efter start (ÄTA) utan skriftlig överenskommelse"
        ],
        "trust_points": [
            "Tydlig offert – inga överraskningar",
            "ÄTA hanteras skriftligt innan arbete utförs",
            "Säkerhet och kvalitet i fokus"
        ],
    },
    "Snickeri": {
        "scope_defaults": [
            "Förberedelse och skydd av ytor",
            "Rivning/montering enligt överenskommelse",
            "Material och montage",
            "Finjustering och genomgång",
            "Städning av arbetsområdet"
        ],
        "exclusions_defaults": [
            "Dolda skador i bärande konstruktioner",
            "Arbeten som kräver bygglov/extra ritningar utöver överenskommelse",
            "Tillval/ändringar efter start (ÄTA) utan skriftlig överenskommelse"
        ],
        "trust_points": [
            "Tydlig plan och kommunikation",
            "ÄTA hanteras skriftligt innan arbete utförs",
            "Noggrann slutgenomgång innan avslut"
        ],
    },
    "Murning": {
        "scope_defaults": [
            "Förberedelse av underlag",
            "Murning/putsning enligt överenskommelse",
            "Avjämning och kontroll av ytor",
            "Städning av arbetsområdet"
        ],
        "exclusions_defaults": [
            "Dolda skador/fuktproblem i underlag",
            "Extra armering/åtgärder utöver överenskommelse",
            "Tillval/ändringar efter start (ÄTA) utan skriftlig överenskommelse"
        ],
        "trust_points": [
            "Tydlig omfattning och materialval",
            "ÄTA hanteras skriftligt innan arbete utförs",
            "Slutkontroll av ytor innan avslut"
        ],
    },
    "Plattsättning": {
        "scope_defaults": [
            "Förberedelse och skydd av ytor",
            "Underarbete/avjämning vid behov",
            "Sättning av kakel/klinker enligt överenskommelse",
            "Fogning och genomgång",
            "Städning av arbetsområdet"
        ],
        "exclusions_defaults": [
            "Dolda fel i underlag/konstruktion",
            "Tätskikt/extra underarbete utöver överenskommelse",
            "Tillval/ändringar efter start (ÄTA) utan skriftlig överenskommelse"
        ],
        "trust_points": [
            "Tydlig offert med omfattning och val",
            "ÄTA hanteras skriftligt innan arbete utförs",
            "Vi stämmer av vid eventuella avvikelser"
        ],
    },
    "Golv": {
        "scope_defaults": [
            "Förberedelse och skydd av ytor",
            "Rivning av befintligt golv vid behov",
            "Läggning av nytt golv enligt överenskommelse",
            "Lister/avslut vid behov",
            "Städning av arbetsområdet"
        ],
        "exclusions_defaults": [
            "Dolda fel i undergolv/konstruktion",
            "Extra spackling/avjämning utöver överenskommelse",
            "Tillval/ändringar efter start (ÄTA) utan skriftlig överenskommelse"
        ],
        "trust_points": [
            "Tydlig plan för utförande",
            "ÄTA hanteras skriftligt innan arbete utförs",
            "Slutgenomgång innan avslut"
        ],
    },
    "Städ": {
        "scope_defaults": [
            "Genomgång av önskemål och ytor",
            "Städning enligt överenskommen checklista",
            "Kvalitetskontroll efter utfört arbete"
        ],
        "exclusions_defaults": [
            "Sanering/specialrengöring om inte avtalat",
            "Skador i underlag/material som påverkar resultatet",
            "Extra tillval utöver checklistan (offereras separat)"
        ],
        "trust_points": [
            "Tydlig checklista – du vet vad som ingår",
            "Vi stämmer av efteråt",
            "Extra önskemål hanteras separat och tydligt"
        ],
    },
    "Arkitekt/Ingenjör/Konstruktör": {
        "scope_defaults": [
            "Behovsanalys och genomgång av underlag",
            "Förslag/ritningar/beräkningar enligt överenskommelse",
            "Avstämning och revidering (antal enligt offert)",
            "Leverans av slutunderlag"
        ],
        "exclusions_defaults": [
            "Myndighetskontakter/extra handlingar utöver överenskommelse",
            "Extra revisioner utöver överenskommen omfattning",
            "Ändringar i projektets grundförutsättningar (offereras separat)"
        ],
        "trust_points": [
            "Tydliga leveranser och avstämningspunkter",
            "Ändringar hanteras strukturerat och skriftligt",
            "Du får underlag som är lätt att gå vidare med"
        ],
    },
}

def industry_options():
    return list(INDUSTRIES.keys())


# ============================
# Pricing (NO guessing)
# ============================
DEFAULT_PRICE_ROWS = [
    {"item": "Arbete", "qty": 1, "unit": "st", "unit_price_sek": 0, "kind": "arbete"},
    {"item": "Material", "qty": 1, "unit": "st", "unit_price_sek": 0, "kind": "material"},
    {"item": "Avfallshantering & bortforsling", "qty": 1, "unit": "st", "unit_price_sek": 0, "kind": "övrigt"},
]

def calc_pricing(rows: list[dict], rot_enabled: bool, rot_rate: float):
    cleaned = []
    labor_sum = 0
    material_sum = 0
    other_sum = 0

    for r in rows or []:
        r = r or {}
        item = str(r.get("item", "")).strip()
        if not item:
            continue

        qty = to_float(r.get("qty", 0), 0.0)
        unit = str(r.get("unit", "")).strip() or "st"
        unit_price = to_int(r.get("unit_price_sek", 0), 0)

        kind = str(r.get("kind", "övrigt")).strip().lower()
        if kind not in ("arbete", "material", "övrigt"):
            kind = "övrigt"

        total = int(round(qty * unit_price))

        cleaned.append({
            "item": item,
            "qty": qty,
            "unit": unit,
            "unit_price_sek": unit_price,
            "total_sek": total,
            "kind": kind,
        })

        if kind == "arbete":
            labor_sum += total
        elif kind == "material":
            material_sum += total
        else:
            other_sum += total

    rot_amount = int(round(labor_sum * rot_rate)) if rot_enabled else 0
    total_before = labor_sum + material_sum + other_sum
    total_after = max(0, total_before - rot_amount)

    return cleaned, labor_sum, material_sum, other_sum, rot_amount, total_before, total_after


# ============================
# AI (text only — no prices)
# ============================
def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def generate_offer_ai(company: str, customer: str, description: str, industry: str, include_rot: bool) -> dict:
    profile = INDUSTRIES.get(industry, {})
    scope_defaults = profile.get("scope_defaults", [])
    exclusions_defaults = profile.get("exclusions_defaults", [])
    trust_points_defaults = profile.get("trust_points", [])

    fallback = {
        "title": f"Offert – {industry}",
        "summary": "Tack för er förfrågan. Nedan följer vårt förslag baserat på era önskemål. Vi har lagt fokus på tydlighet, omfattning och trygghet.",
        "scope": scope_defaults[:],
        "exclusions": exclusions_defaults[:],
        "timeline": "Start enligt överenskommelse. Beräknad tid beror på omfattning och tillgänglighet på material.",
        "trust_points": trust_points_defaults[:],
        "terms": [
            "Offerten är giltig i 30 dagar",
            "Betalningsvillkor: 10 dagar efter slutfört arbete (om inget annat avtalas)",
            "ÄTA (ändring/tillägg) offereras separat och bekräftas skriftligt"
        ],
        "next_steps": "Om ni vill gå vidare: svara och bekräfta offerten. Vi återkommer för att boka startdatum och gå igenom val/tillval.",
        "contact": f"{company}\nTelefon: \nE-post: ",
        "rot_note": "" if not include_rot else "ROT/RUT är preliminärt inräknat på arbetskostnaden. Slutligt avdrag fastställs av Skatteverket.",
    }

    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        out = dict(fallback)
        out["company"] = company
        out["customer"] = customer
        return out

    prompt = f"""
Du är en svensk offertassistent. Du skriver offerter som ska skickas från en firma till en privatkund.
Svara ENDAST som JSON (utan ```). Skriv tydligt, professionellt och tryggt. Undvik onödigt fackspråk.

VIKTIGT: Du får INTE hitta på priser, totalsummor eller prisrader. Priser fylls i av användaren.

BRANSCH: {industry}

Returnera JSON med nycklar:
title, summary, scope (lista), exclusions (lista), timeline,
trust_points (lista), terms (lista), next_steps, rot_note, contact (sträng).

Input:
Företag: {company}
Kund: {customer}
Beskrivning: {description}

Standardpunkter (anpassa):
scope_defaults: {json.dumps(scope_defaults, ensure_ascii=False)}
exclusions_defaults: {json.dumps(exclusions_defaults, ensure_ascii=False)}
trust_points_defaults: {json.dumps(trust_points_defaults, ensure_ascii=False)}

ROT/RUT: {"rot_note ska förklara att det är preliminärt inräknat" if include_rot else "rot_note ska vara tom sträng"}.
""".strip()

    try:
        if _OPENAI_MODE == "new":
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Du skriver svenska offerter som strikt JSON. Inga priser."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.25,
            )
            text = resp.choices[0].message.content or ""
        else:
            openai.api_key = OPENAI_API_KEY
            resp = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Du skriver svenska offerter som strikt JSON. Inga priser."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.25,
            )
            text = resp["choices"][0]["message"]["content"] or ""

        data = _extract_json(text)
        if not isinstance(data, dict):
            data = fallback

        # Hard normalize
        out = dict(fallback)
        out.update(data)

        out["title"] = as_text(out.get("title")) or fallback["title"]
        out["summary"] = as_text(out.get("summary")) or fallback["summary"]
        out["scope"] = as_list(out.get("scope")) or fallback["scope"]
        out["exclusions"] = as_list(out.get("exclusions")) or fallback["exclusions"]
        out["timeline"] = as_text(out.get("timeline")) or fallback["timeline"]
        out["trust_points"] = as_list(out.get("trust_points")) or fallback["trust_points"]
        out["terms"] = as_list(out.get("terms")) or fallback["terms"]
        out["next_steps"] = as_text(out.get("next_steps")) or fallback["next_steps"]
        out["rot_note"] = as_text(out.get("rot_note"))
        out["contact"] = as_text(out.get("contact")) or fallback["contact"]

        out["company"] = company
        out["customer"] = customer

        return out

    except Exception:
        out = dict(fallback)
        out["company"] = company
        out["customer"] = customer
        return out


# ============================
# PDF
# ============================
def _draw_paragraph(c, text, x, y, max_width, line_height=12):
    text = as_text(text)
    words = text.split()
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        if c.stringWidth(test, "Helvetica", 10) <= max_width:
            line = test
        else:
            if line:
                c.drawString(x, y, line)
                y -= line_height
            line = w
            if y < 25 * mm:
                c.showPage()
                y = A4[1] - 18 * mm
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

def build_offer_pdf(offer: dict, industry: str, include_rot: bool) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    margin = 18 * mm
    x = margin
    y = height - margin

    # Header
    c.setFont("Helvetica-Bold", 18)
    c.drawString(x, y, as_text(offer.get("title")) or "Offert")
    y -= 7 * mm

    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Datum: {datetime.now().strftime('%Y-%m-%d')}   •   Bransch: {industry}")
    y -= 7 * mm

    company = as_text(offer.get("company"))
    customer = as_text(offer.get("customer"))
    if company:
        c.drawString(x, y, f"Företag: {company}")
        y -= 5 * mm
    if customer:
        c.drawString(x, y, f"Kund: {customer}")
        y -= 8 * mm

    # Summary
    summary = as_text(offer.get("summary"))
    if summary:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Sammanfattning")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, summary, x, y, width - 2 * margin)
        y -= 4 * mm

    # Scope
    scope = offer.get("scope") or []
    if scope:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Detta ingår")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        for item in scope[:28]:
            y = _draw_bullet(c, as_text(item), x, y, width - 2 * margin)
        y -= 2 * mm

    # Materials included
    materials_included = as_text(offer.get("materials_included"))
    if materials_included.strip():
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Material som ingår")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, materials_included, x, y, width - 2 * margin)
        y -= 4 * mm

    # Exclusions
    exclusions = offer.get("exclusions") or []
    if exclusions:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Ingår inte")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        for item in exclusions[:24]:
            y = _draw_bullet(c, as_text(item), x, y, width - 2 * margin)
        y -= 2 * mm

    # Timeline
    timeline = as_text(offer.get("timeline"))
    if timeline:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Tidsplan")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, timeline, x, y, width - 2 * margin)
        y -= 4 * mm

    # Pricing table
    pricing = offer.get("pricing") or []
    total_before = offer.get("total_before_rot_sek")
    rot_amount = offer.get("rot_amount_sek", 0)
    total_after = offer.get("total_sek")

    if pricing:
        if y < 85 * mm:
            c.showPage()
            y = height - margin

        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Prisöversikt")
        y -= 8 * mm

        rows = [["Post", "Antal", "Enhet", "á-pris (SEK)", "Summa (SEK)"]]
        for p in pricing[:40]:
            p = p or {}
            rows.append([
                as_text(p.get("item"))[:50],
                as_text(p.get("qty")),
                as_text(p.get("unit")),
                as_text(p.get("unit_price_sek")),
                as_text(p.get("total_sek")),
            ])

        tbl = Table(rows, colWidths=[78 * mm, 18 * mm, 18 * mm, 25 * mm, 25 * mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        w, h = tbl.wrapOn(c, width - 2 * margin, y)
        tbl.drawOn(c, x, y - h)
        y = y - h - 6 * mm

        # Totals (ROT included)
        if total_before is not None:
            c.setFont("Helvetica", 10)
            c.drawString(x, y, f"Totalt före ROT/RUT: {as_text(total_before)} SEK")
            y -= 6 * mm

        if include_rot and rot_amount:
            c.setFont("Helvetica", 10)
            c.drawString(x, y, f"ROT/RUT (preliminärt): -{as_text(rot_amount)} SEK")
            y -= 6 * mm

        if total_after is not None:
            c.setFont("Helvetica-Bold", 11)
            label = "Att betala (efter ROT/RUT):" if include_rot else "Att betala:"
            c.drawString(x, y, f"{label} {as_text(total_after)} SEK")
            y -= 6 * mm

    # ROT note
    rot_note = as_text(offer.get("rot_note"))
    if include_rot and rot_note:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "ROT/RUT (information)")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, rot_note, x, y, width - 2 * margin)
        y -= 4 * mm

    # Trust
    trust_points = offer.get("trust_points") or []
    if trust_points:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Trygghet")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        for t in trust_points[:20]:
            y = _draw_bullet(c, as_text(t), x, y, width - 2 * margin)
        y -= 2 * mm

    # Terms
    terms = offer.get("terms") or []
    if terms:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Villkor (kort)")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        for t in terms[:25]:
            y = _draw_bullet(c, as_text(t), x, y, width - 2 * margin)
        y -= 2 * mm

    # Next steps
    next_steps = as_text(offer.get("next_steps"))
    if next_steps:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Nästa steg")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, next_steps, x, y, width - 2 * margin)
        y -= 4 * mm

    # Contact
    contact = as_text(offer.get("contact"))
    if contact:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Kontakt")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, contact, x, y, width - 2 * margin)
        y -= 4 * mm

    # Acceptance
    if y < 60 * mm:
        c.showPage()
        y = height - margin

    c.setFont("Helvetica-Bold", 11)
    c.drawString(x, y, "Godkännande")
    y -= 6 * mm
    c.setFont("Helvetica", 10)
    c.drawString(x, y, "Jag/vi godkänner offerten enligt ovan.")
    y -= 10 * mm
    c.drawString(x, y, "Namn: ________________________________")
    y -= 8 * mm
    c.drawString(x, y, "Underskrift: ___________________________")
    y -= 8 * mm
    c.drawString(x, y, "Datum: ________________________________")
    y -= 10 * mm
    c.setFont("Helvetica", 9)
    c.drawString(x, y, "Digital signering (BankID) kan aktiveras i nästa steg i Offertly.")

    c.showPage()
    c.save()
    return buf.getvalue()


# ============================
# Backend endpoints wrappers
# ============================
def get_status(email: str):
    # expected: {"active": bool, "plan": "...", "free_remaining": int}
    ok, resp = ok_or_err(backend_get, "/api/status", {"email": email})
    if not ok:
        return False, None, resp
    return True, resp, None

def use_free_quote(email: str):
    ok, resp = ok_or_err(backend_post, "/api/use-free-quote", {"email": email})
    if not ok:
        return False, resp
    return True, resp

def plan_key_to_price_id(plan_key: str) -> str:
    return {
        "starter": STRIPE_PRICE_ID_STARTER,
        "pro": STRIPE_PRICE_ID_PRO,
        "team": STRIPE_PRICE_ID_TEAM,
    }[plan_key]

def go_checkout(email: str, plan_key: str):
    if not APP_BASE_URL:
        st.error("Saknar APP_BASE_URL i secrets.")
        return

    price_id = plan_key_to_price_id(plan_key)
    if not price_id:
        st.error("Saknar Stripe price_id för detta paket i secrets.")
        return

    payload = {
        "email": email,
        "price_id": price_id,
        "success_url": f"{APP_BASE_URL}?success=1",
        "cancel_url": f"{APP_BASE_URL}?cancel=1",
    }
    ok2, resp2 = ok_or_err(backend_post, "/api/create-checkout-session", payload)
    if not ok2:
        st.error("Kunde inte skapa betalning. Försök igen.")
        if SHOW_DEBUG:
            st.code(str(resp2))
        return

    url = (resp2 or {}).get("url")
    if not url:
        st.error("Kunde inte starta betalning.")
        if SHOW_DEBUG:
            st.json(resp2)
        return

    st.success("Öppna Stripe Checkout för att betala.")
    st.link_button("Öppna Stripe Checkout", url, use_container_width=True)


# ============================
# Streamlit UI
# ============================
st.set_page_config(page_title="Offertly", layout="wide")
st.title(APP_TITLE)
st.caption("Skapa säljande och tydliga offerter till privatkunder – med AI + proffsig PDF.")

# Query params feedback
try:
    qp = st.query_params
    if qp.get("success"):
        st.success("Betalning genomförd ✅ Om du inte ser din plan direkt, vänta 5–10 sek och uppdatera sidan.")
    if qp.get("cancel"):
        st.info("Betalning avbruten. Du kan prova gratis eller välja paket igen.")
except Exception:
    pass

# Session defaults
st.session_state.setdefault("email", "")
st.session_state.setdefault("industry", "VVS")
st.session_state.setdefault("include_rot", True)
st.session_state.setdefault("price_rows", DEFAULT_PRICE_ROWS)
st.session_state.setdefault("materials_included", "")
st.session_state.setdefault("offer_pdf", None)
st.session_state.setdefault("offer_data", None)
st.session_state.setdefault("selected_plan", None)

# Debug sidebar
if SHOW_DEBUG:
    with st.sidebar:
        st.subheader("Systemstatus (debug)")
        st.write("BACKEND_BASE_URL:", "✅" if BACKEND_BASE_URL else "❌")
        st.write("APP_API_TOKEN:", "✅" if APP_API_TOKEN else "❌")
        st.write("Stripe Price IDs:", "✅" if all([STRIPE_PRICE_ID_STARTER, STRIPE_PRICE_ID_PRO, STRIPE_PRICE_ID_TEAM]) else "❌")
        st.write("OpenAI:", "✅" if (OPENAI_AVAILABLE and OPENAI_API_KEY) else "⚠️ (fallback)")
        st.divider()
        if st.button("Nollställ session"):
            st.session_state.clear()
            st.rerun()

# Landing copy
st.markdown("## Skicka proffsiga offerter som privatkunder förstår")
st.write(
    "Offertly hjälper dig skapa en tydlig och säljande offert med omfattning, trygghet, ROT/RUT-information och proffsig PDF. "
    "Du fyller alltid i dina egna priser – Offertly gissar aldrig."
)

b1, b2, b3 = st.columns(3)
b1.write("✅ Tydlig omfattning (ingår/ingår inte)")
b2.write("✅ ROT/RUT kan räknas in (preliminärt) på arbetet")
b3.write("✅ PDF med godkännande längst ner")

st.divider()

left, right = st.columns([1.15, 0.85], gap="large")

with left:
    st.markdown("### 1) Välj bransch")
    opts = industry_options()
    idx = opts.index(st.session_state["industry"]) if st.session_state["industry"] in opts else 0
    st.session_state["industry"] = st.selectbox("Bransch", options=opts, index=idx)

    st.session_state["include_rot"] = st.toggle(
        "Räkna in ROT/RUT (preliminärt) i totalsumman",
        value=st.session_state["include_rot"]
    )
    st.caption(f"ROT/RUT beräknas som {int(ROT_RATE_DEFAULT*100)}% på rader markerade som **arbete**.")

    st.markdown("### 2) Skriv din email")
    st.session_state["email"] = st.text_input("Email", value=st.session_state["email"], placeholder="din@email.se").strip().lower()
    st.caption("Du kan skapa **3 testofferter gratis**. Därefter behöver du välja paket och betala.")

with right:
    st.markdown("### Paket")
    st.write("**Starter** – 199 kr/mån")
    st.write("**Pro** – 499 kr/mån (mest populär)")
    st.write("**Team** – 1 200 kr/mån (3–10 användare)")
    st.divider()
    st.markdown("### Trygg betalning")
    st.write("Betalning via Stripe. Kvitto skickas av Stripe till din email (enligt Stripe receipts).")

email = st.session_state["email"]
industry = st.session_state["industry"]
include_rot = bool(st.session_state["include_rot"])

if not email:
    st.info("Skriv din email för att prova Offertly eller välja paket.")
    st.stop()

if not APP_API_TOKEN:
    st.error("Saknar APP_API_TOKEN i secrets.")
    st.stop()

ok, status, err = get_status(email)
if not ok:
    st.error("Kan inte kontakta servern just nu. Försök igen strax.")
    if SHOW_DEBUG:
        st.code(str(err))
    st.stop()

active = bool(status.get("active"))
plan = status.get("plan")
free_remaining = to_int(status.get("free_remaining"), 0)

st.divider()

s1, s2, s3 = st.columns(3)
s1.metric("Gratis offerter kvar", free_remaining)
s2.metric("Din plan", (plan or "Ingen (testläge)") if active else "Ingen (testläge)")
s3.metric("Status", "Aktiv ✅" if active else "Testläge 🧪")

# Paywall when trials exhausted
if (not active) and free_remaining <= 0:
    st.warning("Du har använt dina 3 gratis testofferter. Välj paket för att fortsätta.")
    cols = st.columns(3)

    with cols[0]:
        st.subheader("Starter")
        st.write("199 kr/mån")
        st.write("För mindre jobb och enmansfirma.")
        if st.button("Välj Starter", use_container_width=True):
            st.session_state["selected_plan"] = "starter"

    with cols[1]:
        st.subheader("Pro ⭐ Mest populär")
        st.write("499 kr/mån")
        st.write("För firmor som lämnar offerter varje vecka.")
        if st.button("Välj Pro", use_container_width=True):
            st.session_state["selected_plan"] = "pro"

    with cols[2]:
        st.subheader("Team")
        st.write("1 200 kr/mån")
        st.write("3–10 användare.")
        if st.button("Välj Team", use_container_width=True):
            st.session_state["selected_plan"] = "team"

    plan_sel = st.session_state.get("selected_plan")
    if plan_sel:
        st.markdown("### Fortsätt till betalning")
        if st.button("Öppna Stripe Checkout", type="primary", use_container_width=True):
            go_checkout(email, plan_sel)

    st.stop()

# ============================
# Offer builder
# ============================
st.markdown("## Offertgenerator")

company = st.text_input("Företagsnamn", value="")
customer = st.text_input("Kundens namn", value="")
desc = st.text_area("Beskrivning (vad ska göras?)", height=140)

st.markdown("### Prisrader (du fyller i – Offertly gissar inte)")
st.caption("Rader kan lämnas tomma. ROT/RUT räknas bara på rader som är **arbete**. Avfallshantering finns som standardrad.")

# Always ensure defaults exist
if not isinstance(st.session_state["price_rows"], list) or len(st.session_state["price_rows"]) == 0:
    st.session_state["price_rows"] = list(DEFAULT_PRICE_ROWS)

df = pd.DataFrame(st.session_state["price_rows"])
# Ensure columns exist even if df is empty
for col in ["item", "qty", "unit", "unit_price_sek", "kind"]:
    if col not in df.columns:
        df[col] = None

df = st.data_editor(
    df[["item", "qty", "unit", "unit_price_sek", "kind"]],
    use_container_width=True,
    num_rows="dynamic",
    column_config={
        "item": st.column_config.TextColumn("Post"),
        "qty": st.column_config.NumberColumn("Antal", min_value=0.0, step=1.0),
        "unit": st.column_config.TextColumn("Enhet"),
        "unit_price_sek": st.column_config.NumberColumn("á-pris (SEK)", min_value=0, step=100),
        "kind": st.column_config.SelectboxColumn("Typ", options=["arbete", "material", "övrigt"]),
    },
    hide_index=True,
)
st.session_state["price_rows"] = df.fillna("").to_dict(orient="records")

st.markdown("### Material som ingår (visa tydligt för privatkunden)")
st.session_state["materials_included"] = st.text_area(
    "Lista material / produktval",
    value=st.session_state["materials_included"],
    height=120,
    placeholder="Exempel:\n- Gipsskivor\n- Regelvirke\n- Skruv/spackel\n- Tätskikt (vid våtrum)\n- Kakel/klinker (om valt)\n- Fog/lim\n\nSkriv 'Kundens val' om kunden står för vissa produkter."
)

# Show live totals preview (nice + safe)
pricing_preview, labor_sum, material_sum, other_sum, rot_amount, total_before, total_after = calc_pricing(
    st.session_state["price_rows"],
    rot_enabled=include_rot,
    rot_rate=ROT_RATE_DEFAULT
)

p1, p2, p3, p4 = st.columns(4)
p1.metric("Arbete", f"{labor_sum} SEK")
p2.metric("Material", f"{material_sum} SEK")
p3.metric("ROT/RUT (prel.)", f"-{rot_amount} SEK" if include_rot else "0 SEK")
p4.metric("Att betala", f"{total_after} SEK")

col1, col2 = st.columns([1, 1], gap="large")

with col1:
    st.markdown("### Skapa offert")
    st.caption("AI skriver texten. Priserna kommer från dina rader. ROT/RUT räknas in preliminärt om du valt det.")

    if st.button("Generera offert (AI) + PDF", type="primary", use_container_width=True):
        if not (company and customer and desc):
            st.error("Fyll i företagsnamn, kundnamn och beskrivning.")
        else:
            # consume free quote if not active
            if not active:
                ok_free, resp_free = use_free_quote(email)
                if not ok_free:
                    st.error("Du har nått gränsen för testofferter. Välj paket för att fortsätta.")
                    if SHOW_DEBUG:
                        st.code(str(resp_free))
                    st.stop()

            # Always calc from current rows
            pricing, labor_sum, material_sum, other_sum, rot_amount, total_before, total_after = calc_pricing(
                st.session_state["price_rows"],
                rot_enabled=include_rot,
                rot_rate=ROT_RATE_DEFAULT
            )

            # AI offer (text only)
            with st.spinner("Genererar offert..."):
                ai = generate_offer_ai(company, customer, desc, industry=industry, include_rot=include_rot)

            # Build final offer dict
            offer = dict(ai)
            offer["company"] = company
            offer["customer"] = customer
            offer["pricing"] = pricing
            offer["labor_sum_sek"] = labor_sum
            offer["material_sum_sek"] = material_sum
            offer["other_sum_sek"] = other_sum
            offer["rot_amount_sek"] = rot_amount
            offer["total_before_rot_sek"] = total_before
            offer["total_sek"] = total_after
            offer["materials_included"] = as_text(st.session_state["materials_included"])

            if include_rot:
                offer["rot_note"] = (
                    f"ROT/RUT är preliminärt inräknat med {int(ROT_RATE_DEFAULT*100)}% på arbetskostnaden. "
                    "Slutligt avdrag fastställs av Skatteverket och kan påverka slutsumman."
                )
            else:
                offer["rot_note"] = ""

            # PDF
            st.session_state["offer_data"] = offer
            st.session_state["offer_pdf"] = build_offer_pdf(offer, industry=industry, include_rot=include_rot)

with col2:
    st.markdown("### PDF")
    if st.session_state.get("offer_pdf"):
        filename = f"offert_{(customer or 'kund').replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        st.download_button(
            "Ladda ner offert (PDF)",
            data=st.session_state["offer_pdf"],
            file_name=filename,
            mime="application/pdf",
            use_container_width=True
        )
    else:
        st.info("Generera en offert så dyker PDF-knappen upp här.")

# Debug-only preview
if SHOW_DEBUG and st.session_state.get("offer_data"):
    st.markdown("### Utkast (debug)")
    st.json(st.session_state["offer_data"])

if (not active) and free_remaining > 0:
    st.info(f"Du är i testläge. Du har {free_remaining} gratis offerter kvar innan betalning krävs.")



















 






    
















































