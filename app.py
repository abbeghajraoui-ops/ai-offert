import os
import json
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
APP_TITLE = "Offertly"

def sget(key: str, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return os.environ.get(key, default)

def sbool(key: str, default: bool = False) -> bool:
    val = str(sget(key, str(default))).strip().lower()
    return val in ("1", "true", "yes", "y", "on")

# ✅ Debug är AV som standard. Slå på med SHOW_DEBUG=true i secrets.
SHOW_DEBUG = sbool("SHOW_DEBUG", False)

STRIPE_PRICE_ID_STARTER = (sget("STRIPE_PRICE_ID_STARTER") or "").strip()
STRIPE_PRICE_ID_PRO = (sget("STRIPE_PRICE_ID_PRO") or "").strip()
STRIPE_PRICE_ID_TEAM = (sget("STRIPE_PRICE_ID_TEAM") or "").strip()

BACKEND_BASE_URL = (sget("BACKEND_BASE_URL") or "").rstrip("/")
APP_API_TOKEN = ((sget("APP_API_TOKEN") or "").strip() or (sget("APP_WEBHOOK_TOKEN") or "").strip())
APP_BASE_URL = (sget("APP_BASE_URL") or "").rstrip("/")
OPENAI_API_KEY = (sget("OPENAI_API_KEY") or "").strip()


# ----------------------------
# Safe casting helpers
# ----------------------------
def as_text(v) -> str:
    """Always return a safe string (handles dict/list/None)."""
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

def as_list(v) -> list:
    """Ensure list type. If string -> [string]. If dict -> [json]."""
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

def as_int(v, default=0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return default
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip().replace(" ", "")
        if s == "":
            return default
        return int(float(s))
    except Exception:
        return default

def normalize_offer(offer: dict, company: str, customer: str, industry: str, include_rot: bool, profile: dict) -> dict:
    """Normalize AI output so PDF/UI never crashes."""
    scope_defaults = profile.get("scope_defaults", [])
    exclusions_defaults = profile.get("exclusions_defaults", [])
    trust_points_defaults = profile.get("trust_points", [])
    rot_note_default = profile.get("rot_note", "ROT/RUT kan vara möjligt beroende på arbete. Slutligt avdrag beslutas av Skatteverket.")

    out = dict(offer or {})

    out["title"] = as_text(out.get("title")) or f"Offert – {industry}"
    out["company"] = as_text(out.get("company")) or company
    out["customer"] = as_text(out.get("customer")) or customer

    out["summary"] = as_text(out.get("summary"))
    out["timeline"] = as_text(out.get("timeline"))
    out["rot_note"] = as_text(out.get("rot_note")) if include_rot else ""

    # Lists
    out["scope"] = as_list(out.get("scope")) or list(scope_defaults)
    out["exclusions"] = as_list(out.get("exclusions")) or list(exclusions_defaults)
    out["trust_points"] = as_list(out.get("trust_points")) or list(trust_points_defaults)
    out["terms"] = as_list(out.get("terms")) or [
        "Offerten är giltig i 30 dagar",
        "Betalningsvillkor: 10 dagar efter slutfört arbete (om inget annat avtalas)",
        "ÄTA (ändring/tillägg) offereras separat och bekräftas skriftligt"
    ]

    out["next_steps"] = as_text(out.get("next_steps")) or "Om ni vill gå vidare: svara och bekräfta offerten. Vi återkommer för att boka startdatum."

    # Contact must always be text
    contact = out.get("contact")
    if not contact:
        out["contact"] = f"{out['company']}\nTelefon: \nE-post: "
    else:
        out["contact"] = as_text(contact)

    # Pricing normalization
    pricing = out.get("pricing")
    if not isinstance(pricing, list):
        pricing = []
    cleaned = []
    for p in pricing[:40]:
        if not isinstance(p, dict):
            continue
        item = as_text(p.get("item"))
        qty = p.get("qty")
        unit = as_text(p.get("unit"))
        unit_price = p.get("unit_price_sek")
        total = p.get("total_sek")

        # keep numeric-ish
        qty_s = as_text(qty) if qty is not None else ""
        unit_price_i = as_int(unit_price, 0)
        total_i = as_int(total, 0)

        cleaned.append({
            "item": item,
            "qty": qty_s,
            "unit": unit,
            "unit_price_sek": unit_price_i,
            "total_sek": total_i
        })

    if not cleaned:
        cleaned = [
            {"item": "Arbete", "qty": "1", "unit": "st", "unit_price_sek": 0, "total_sek": 0},
            {"item": "Material", "qty": "1", "unit": "st", "unit_price_sek": 0, "total_sek": 0},
        ]
    out["pricing"] = cleaned

    # total_sek: always int; if missing, compute sum
    computed_total = sum(as_int(r.get("total_sek"), 0) for r in out["pricing"])
    total_in = out.get("total_sek", None)
    out["total_sek"] = as_int(total_in, computed_total)

    # Fill rot note if include_rot but AI omitted
    if include_rot and not out["rot_note"]:
        out["rot_note"] = rot_note_default

    # Ensure strings
    for k in ("summary", "timeline", "rot_note", "next_steps", "contact"):
        out[k] = as_text(out.get(k))

    return out


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
# Branschprofiler
# ----------------------------
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
            "Ändringar/tillägg (ÄTA) hanteras skriftligt",
            "Vi går igenom allt innan start och svarar på frågor löpande"
        ],
        "rot_note": "ROT-avdrag kan vara möjligt för arbetskostnaden beroende på typ av arbete och din situation. Slutligt avdrag fastställs av Skatteverket."
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
            "Vi dokumenterar förändringar och tillägg",
            "Säkerhet och kvalitet i fokus"
        ],
        "rot_note": "ROT kan vara möjligt för vissa elarbeten i bostad. Slutligt avdrag beslutas av Skatteverket."
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
            "Vi håller dig uppdaterad under arbetets gång",
            "ÄTA hanteras skriftligt så du har kontroll på kostnaden",
            "Noggrann slutgenomgång innan avslut"
        ],
        "rot_note": "ROT-avdrag kan vara möjligt för arbetskostnaden vid renovering/ombyggnation i bostad. Slutligt avdrag beslutas av Skatteverket."
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
            "Extra armering/åtgärder som inte ingår i underlaget",
            "Tillval/ändringar efter start (ÄTA) utan skriftlig överenskommelse"
        ],
        "trust_points": [
            "Tydlig omfattning och materialval",
            "Skriftlig hantering av tillägg",
            "Slutkontroll av ytor innan avslut"
        ],
        "rot_note": "ROT kan vara möjligt vid vissa renoveringsarbeten i bostad. Slutligt avdrag beslutas av Skatteverket."
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
            "Vi stämmer av vid eventuella avvikelser",
            "ÄTA hanteras skriftligt"
        ],
        "rot_note": "ROT kan vara möjligt för arbetskostnaden vid renovering i bostad. Slutligt avdrag beslutas av Skatteverket."
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
            "Skriftlig hantering av tillägg",
            "Slutgenomgång innan avslut"
        ],
        "rot_note": "ROT kan vara möjligt för arbetskostnaden i bostad vid renovering. Slutligt avdrag beslutas av Skatteverket."
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
        "rot_note": "RUT-avdrag kan vara möjligt för hushållsnära tjänster. Slutligt avdrag beslutas av Skatteverket."
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
        "rot_note": "ROT/RUT gäller normalt inte för projekteringstjänster. Kontrollera din situation. Slutliga regler avgörs av Skatteverket."
    },
}

def industry_options():
    return list(INDUSTRIES.keys())


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


def generate_offer_ai(company: str, customer: str, description: str, industry: str, include_rot: bool) -> dict:
    profile = INDUSTRIES.get(industry, {})
    scope_defaults = profile.get("scope_defaults", [])
    exclusions_defaults = profile.get("exclusions_defaults", [])
    trust_points_defaults = profile.get("trust_points", [])
    rot_note_default = profile.get("rot_note", "ROT/RUT kan vara möjligt beroende på arbete. Slutligt avdrag beslutas av Skatteverket.")

    fallback = {
        "title": f"Offert – {industry}",
        "company": company,
        "customer": customer,
        "summary": "Tack för er förfrågan. Nedan följer vårt förslag baserat på era önskemål. Vi har lagt fokus på tydlighet, omfattning och trygghet.",
        "scope": scope_defaults[:],
        "exclusions": exclusions_defaults[:],
        "timeline": "Start enligt överenskommelse. Beräknad tid beror på omfattning och tillgänglighet på material.",
        "pricing": [
            {"item": "Arbete", "qty": 1, "unit": "st", "unit_price_sek": 0, "total_sek": 0},
            {"item": "Material", "qty": 1, "unit": "st", "unit_price_sek": 0, "total_sek": 0},
        ],
        "total_sek": 0,
        "rot_note": rot_note_default if include_rot else "",
        "trust_points": trust_points_defaults[:],
        "terms": [
            "Offerten är giltig i 30 dagar",
            "Betalningsvillkor: 10 dagar efter slutfört arbete (om inget annat avtalas)",
            "ÄTA (ändring/tillägg) offereras separat och bekräftas skriftligt"
        ],
        "next_steps": "Om ni vill gå vidare: svara och bekräfta offerten. Vi återkommer för att boka startdatum och gå igenom eventuella val/tillval.",
        "contact": f"{company}\nTelefon: \nE-post: ",
    }

    if not (OPENAI_AVAILABLE and OPENAI_API_KEY):
        return fallback

    prompt = f"""
Du är en svensk offertassistent. Du skriver offerter som ska skickas från en firma till en privatkund.
Svara ENDAST som JSON (utan ```). Skriv tydligt, professionellt och tryggt. Undvik onödigt fackspråk.

BRANSCH: {industry}

Returnera JSON med nycklar:
title, company, customer,
summary, scope (lista), exclusions (lista), timeline,
pricing (lista av {{"item","qty","unit","unit_price_sek","total_sek"}}),
total_sek,
rot_note,
trust_points (lista),
terms (lista),
next_steps,
contact (TEXTBLOCK som sträng).

Input:
Företag: {company}
Kund: {customer}
Beskrivning: {description}

Riktlinjer:
- Utgå från standardpunkter men anpassa efter beskrivningen.
  scope_defaults: {json.dumps(scope_defaults, ensure_ascii=False)}
  exclusions_defaults: {json.dumps(exclusions_defaults, ensure_ascii=False)}
  trust_points_defaults: {json.dumps(trust_points_defaults, ensure_ascii=False)}
- Om pris inte framgår: skapa en enkel prisöversikt med 2–6 rader och använd 0 SEK om du inte kan uppskatta.
- total_sek måste vara exakt summan av pricing.total_sek.
- ROT/RUT: {"inkludera en tydlig rot_note" if include_rot else "sätt rot_note till tom sträng"}.
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
                temperature=0.25,
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
                temperature=0.25,
            )
            text = resp["choices"][0]["message"]["content"] or ""

        data = _extract_json(text)
        if not isinstance(data, dict):
            return fallback

        return data

    except Exception:
        return fallback


# ----------------------------
# PDF
# ----------------------------
def build_offer_pdf(offer: dict, industry: str) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    margin = 18 * mm
    x = margin
    y = height - margin

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

    summary = as_text(offer.get("summary"))
    if summary:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Sammanfattning")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, summary, x, y, width - 2 * margin)
        y -= 4 * mm

    scope = offer.get("scope") or []
    if scope:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Detta ingår")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        for item in scope[:28]:
            y = _draw_bullet(c, as_text(item), x, y, width - 2 * margin)
        y -= 2 * mm

    exclusions = offer.get("exclusions") or []
    if exclusions:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Ingår inte")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        for item in exclusions[:24]:
            y = _draw_bullet(c, as_text(item), x, y, width - 2 * margin)
        y -= 2 * mm

    timeline = as_text(offer.get("timeline"))
    if timeline:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Tidsplan")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, timeline, x, y, width - 2 * margin)
        y -= 4 * mm

    pricing = offer.get("pricing") or []
    total_sek = offer.get("total_sek")

    if pricing:
        if y < 75 * mm:
            c.showPage()
            y = height - margin

        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Prisöversikt")
        y -= 8 * mm

        rows = [["Post", "Antal", "Enhet", "á-pris (SEK)", "Summa (SEK)"]]
        for p in pricing[:30]:
            if not isinstance(p, dict):
                continue
            rows.append([
                as_text(p.get("item"))[:45],
                as_text(p.get("qty")),
                as_text(p.get("unit")),
                as_text(p.get("unit_price_sek")),
                as_text(p.get("total_sek")),
            ])

        tbl = Table(rows, colWidths=[75 * mm, 18 * mm, 18 * mm, 26 * mm, 26 * mm])
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

        if total_sek is not None:
            c.setFont("Helvetica-Bold", 11)
            c.drawString(x, y, f"Totalt: {as_text(total_sek)} SEK")
            y -= 6 * mm

    rot_note = as_text(offer.get("rot_note"))
    if rot_note:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "ROT/RUT (information)")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, rot_note, x, y, width - 2 * margin)
        y -= 4 * mm

    trust_points = offer.get("trust_points") or []
    if trust_points:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Trygghet")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        for t in trust_points[:18]:
            y = _draw_bullet(c, as_text(t), x, y, width - 2 * margin)
        y -= 2 * mm

    terms = offer.get("terms") or []
    if terms:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Villkor (kort)")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        for t in terms[:22]:
            y = _draw_bullet(c, as_text(t), x, y, width - 2 * margin)
        y -= 2 * mm

    next_steps = as_text(offer.get("next_steps"))
    if next_steps:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Nästa steg")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, next_steps, x, y, width - 2 * margin)
        y -= 4 * mm

    contact = as_text(offer.get("contact"))
    if contact:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, "Kontakt")
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        y = _draw_paragraph(c, contact, x, y, width - 2 * margin)
        y -= 4 * mm

    if y < 55 * mm:
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

    c.showPage()
    c.save()
    return buf.getvalue()


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


# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="Offertly", layout="wide")
st.title(APP_TITLE)
st.caption("Skapa säljande och tydliga offerter till privatkunder – med AI + proffsig PDF.")

# Query params feedback (utan debug)
try:
    qp = st.query_params
    if qp.get("success"):
        st.success("Betalning genomförd ✅ Om du inte ser din plan direkt, vänta 5–10 sek och uppdatera sidan.")
    if qp.get("cancel"):
        st.info("Betalning avbruten. Du kan prova gratis eller välja paket igen.")
except Exception:
    pass

# Debug i sidebar endast om SHOW_DEBUG=true
if SHOW_DEBUG:
    with st.sidebar:
        st.subheader("Systemstatus (debug)")
        st.write("BACKEND_BASE_URL:", "✅" if BACKEND_BASE_URL else "❌")
        st.write("APP_API_TOKEN:", "✅" if APP_API_TOKEN else "❌")
        st.write("Price IDs:", "✅" if all([STRIPE_PRICE_ID_STARTER, STRIPE_PRICE_ID_PRO, STRIPE_PRICE_ID_TEAM]) else "❌")
        st.write("OpenAI:", "✅" if (OPENAI_API_KEY and OPENAI_AVAILABLE) else "⚠️ (fallback)")
        st.divider()
        if st.button("Nollställ (session)"):
            st.session_state.clear()
            st.rerun()

# Session state
if "selected_plan" not in st.session_state:
    st.session_state["selected_plan"] = None
if "email" not in st.session_state:
    st.session_state["email"] = ""
if "industry" not in st.session_state:
    st.session_state["industry"] = "VVS"
if "include_rot" not in st.session_state:
    st.session_state["include_rot"] = True
if "offer_data" not in st.session_state:
    st.session_state["offer_data"] = None
if "offer_pdf" not in st.session_state:
    st.session_state["offer_pdf"] = None


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

    payload = {
        "email": email,
        "price_id": plan_key_to_price_id(plan_key),
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

def get_status(email: str):
    ok, resp = ok_or_err(backend_get, "/api/status", {"email": email})
    if not ok:
        return False, None, resp
    return True, resp, None

def use_free_quote(email: str):
    ok, resp = ok_or_err(backend_post, "/api/use-free-quote", {"email": email})
    if not ok:
        return False, resp
    return True, resp


# ----------------------------
# LANDNINGSSIDA
# ----------------------------
st.markdown("## Skicka proffsiga offerter som privatkunder förstår")
st.write(
    "Offertly hjälper dig skapa en tydlig och säljande offert med omfattning, trygghet, ROT/RUT-information och proffsig PDF. "
    "Perfekt för hantverkare och konsulter som vill få fler ‘ja’ och minska missförstånd."
)

b1, b2, b3 = st.columns(3)
b1.write("✅ Tydlig omfattning (ingår/ingår inte)")
b2.write("✅ Trygghet & nästa steg (kunden vet hur de tackar ja)")
b3.write("✅ PDF med godkännande längst ner")

st.divider()

left, right = st.columns([1.15, 0.85], gap="large")

with left:
    st.markdown("### 1) Välj bransch")
    options = industry_options()
    current = st.session_state["industry"] if st.session_state["industry"] in options else options[0]
    st.session_state["industry"] = st.selectbox("Bransch", options=options, index=options.index(current))

    st.session_state["include_rot"] = st.toggle("Visa ROT/RUT-information i offerten", value=st.session_state["include_rot"])

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
    st.write("Betalning via Stripe. Kvitto skickas av Stripe till din email (enligt dina Stripe-inställningar).")

email = st.session_state.get("email", "").strip().lower()
industry = st.session_state.get("industry", "VVS")
include_rot = bool(st.session_state.get("include_rot", True))

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
free_remaining = int(status.get("free_remaining") or 0)

st.divider()

s1, s2, s3 = st.columns(3)
s1.metric("Gratis offerter kvar", free_remaining)
s2.metric("Din plan", (plan or "Ingen (testläge)") if active else "Ingen (testläge)")
s3.metric("Status", "Aktiv ✅" if active else "Testläge 🧪")

# ----------------------------
# Betalvägg när gratis är slut och ingen aktiv plan
# ----------------------------
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

# ----------------------------
# Offertgenerator (testläge eller aktiv plan)
# ----------------------------
st.markdown("## Offertgenerator")

company = st.text_input("Företagsnamn", value="")
customer = st.text_input("Kundens namn", value="")
desc = st.text_area(
    "Beskrivning (vad ska göras?)",
    height=160,
    placeholder="t.ex. badrumsrenovering 6 kvm: rivning, tätskikt, kakel/klinker, montering WC & dusch, bortforsling…"
)

col1, col2 = st.columns([1, 1], gap="large")

with col1:
    st.markdown("### Skapa offert")
    st.caption("Offerten blir privatkundvänlig: tydligt ingår/ingår inte, trygghet, ROT/RUT, nästa steg och godkännande.")

    if st.button("Generera offert (AI) + PDF", type="primary", use_container_width=True):
        if not (company and customer and desc):
            st.error("Fyll i företagsnamn, kundnamn och beskrivning.")
        else:
            if not active:
                ok_free, resp_free = use_free_quote(email)
                if not ok_free:
                    st.error("Du har nått gränsen för testofferter. Välj paket för att fortsätta.")
                    if SHOW_DEBUG:
                        st.code(str(resp_free))
                    st.stop()

            profile = INDUSTRIES.get(industry, {})
            with st.spinner("Genererar offert..."):
                raw_offer = generate_offer_ai(company, customer, desc, industry=industry, include_rot=include_rot)
                offer = normalize_offer(raw_offer, company, customer, industry, include_rot, profile)

                st.session_state["offer_data"] = offer
                st.session_state["offer_pdf"] = build_offer_pdf(offer, industry=industry)

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

# Utkast visas endast om debug är på
if SHOW_DEBUG and st.session_state.get("offer_data"):
    st.markdown("### Utkast (granskning)")
    st.json(st.session_state["offer_data"])

if (not active) and free_remaining > 0:
    st.info(f"Du är i testläge. Du har {free_remaining} gratis offerter kvar innan betalning krävs.")

















 






    














































