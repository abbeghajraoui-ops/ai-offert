import os
import re
import uuid
from datetime import datetime
from io import BytesIO
from typing import Optional

import streamlit as st

# OpenAI (nya klienten)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # hanteras i UI

# PDF (ReportLab)
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader


# =============================
# Konfiguration / Branding
# =============================
APP_NAME = "Offertly"
APP_TITLE = "Offertly – AI-offertgenerator"
APP_TAGLINE = "Fyll i uppgifterna → generera offert → ladda ner som premium-PDF eller .md"


# =============================
# Helpers
# =============================
def safe_filename(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_\-]", "", s)
    return (s[:60] or "offert")


def get_api_key() -> Optional[str]:
    """
    Försöker läsa OPENAI_API_KEY från:
      1) Streamlit Secrets (utan att krascha om secrets saknas)
      2) Miljövariabel
    """
    # 1) Streamlit Secrets
    try:
        # st.secrets kan kasta StreamlitSecretNotFoundError om secrets.toml saknas
        if "OPENAI_API_KEY" in st.secrets:
            v = str(st.secrets["OPENAI_API_KEY"]).strip()
            return v or None
    except Exception:
        pass

    # 2) Miljövariabel
    v = os.getenv("OPENAI_API_KEY", "").strip()
    return v or None


def generate_offer_id() -> str:
    # Kort, snyggt offert-ID
    return "OFF-" + uuid.uuid4().hex[:8].upper()


def build_prompt(d: dict) -> str:
    return f"""
Du är en professionell offertskrivare för byggrelaterade tjänster (B2B). Skriv på svenska.

Skapa en tydlig och proffsig offert baserat på:

Företag: {d['company']}
Kontakt: {d['contact']}
Datum: {d['date']}
Kund: {d['customer']}
Plats/ort: {d['location']}

Typ av arbete/tjänst: {d['job_type']}
Omfattning/storlek: {d['size']}
Material: {d['material']}
Kommentar/önskemål: {d['comment']}

Prisuppgifter (använd dessa exakt):
- Arbete: {d['price_work']} SEK
- Material: {d['price_material']} SEK
- Övrigt: {d['price_other']} SEK
- Totalpris inkl. moms: {d['price_total']} SEK

Krav:
- Använd rubriker: Projektbeskrivning, Arbetsmoment, Material, Tidsplan, Pris, Villkor, Kontakt
- Arbetsmoment: punktlista
- Materiallista: punktlista
- Tidsplan: realistisk
- Pris: visa uppdelning + total inkl moms
- 4–6 korta villkor (giltighetstid, betalning, tillägg, startdatum “enl. överenskommelse”, ROT om relevant)
- Datum ska vara exakt: {d['date']} (skriv inte "[Dagens datum]")
- Avsluta med vänlig hälsning + kontakt

Skriv kortfattat, tydligt och professionellt.
"""


def draw_wrapped_text(c: canvas.Canvas, text: str, x: float, y: float, max_chars: int, line_h: float):
    """
    Enkel radbrytning för PDF.
    """
    for raw in (text or "").splitlines():
        line = raw.replace("\t", "    ")
        if not line.strip():
            y -= line_h
            continue

        while len(line) > max_chars:
            c.drawString(x, y, line[:max_chars])
            y -= line_h
            line = line[max_chars:]
        c.drawString(x, y, line)
        y -= line_h
    return y


def generate_pdf_premium(
    offer_md: str,
    data: dict,
    customer_logo_bytes: Optional[bytes] = None,
) -> bytes:
    """
    Premium PDF: header, metadata, kundlogo (om uppladdad), och offert-text.
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    margin = 18 * mm
    x = margin
    y = height - margin

    # Header
    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y, "OFFERT")
    c.setFont("Helvetica", 10)
    c.drawRightString(width - margin, y, f"{APP_NAME}")
    y -= 10 * mm

    # Kundens logga (uppladdad)
    if customer_logo_bytes:
        try:
            img = ImageReader(BytesIO(customer_logo_bytes))
            # placera uppe till höger, lagom stor
            logo_w = 38 * mm
            logo_h = 22 * mm
            c.drawImage(img, width - margin - logo_w, height - margin - logo_h - 6 * mm, logo_w, logo_h, mask='auto')
        except Exception:
            pass

    # Meta-rad
    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Offert-ID: {data.get('offer_id','')}")
    c.drawRightString(width - margin, y, f"Datum: {data.get('date','')}")
    y -= 8 * mm

    # Företagsblock
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x, y, data.get("company", ""))
    y -= 5.5 * mm
    c.setFont("Helvetica", 10)
    y = draw_wrapped_text(c, f"Kontakt: {data.get('contact','')}", x, y, 95, 5.2 * mm)
    y -= 2 * mm

    # Kundblock
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x, y, f"Kund: {data.get('customer','')}")
    y -= 5.5 * mm
    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Plats/ort: {data.get('location','')}")
    y -= 8 * mm

    # Tjänstinfo
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x, y, f"Tjänst: {data.get('job_type','')}")
    y -= 5.5 * mm
    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Omfattning: {data.get('size','')}")
    y -= 5.5 * mm
    c.drawString(x, y, f"Material: {data.get('material','')}")
    y -= 8 * mm

    # Prisruta
    box_w = width - 2 * margin
    box_h = 26 * mm
    c.roundRect(x, y - box_h + 6 * mm, box_w, box_h, 6, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x + 6 * mm, y, "Prisöversikt")
    c.setFont("Helvetica", 10)
    c.drawRightString(x + box_w - 6 * mm, y, "SEK (inkl. moms)")
    y -= 6.5 * mm
    c.drawString(x + 6 * mm, y, f"Arbete: {data.get('price_work','')}")
    c.drawRightString(x + box_w - 6 * mm, y, f"Material: {data.get('price_material','')}")
    y -= 5.5 * mm
    c.drawString(x + 6 * mm, y, f"Övrigt: {data.get('price_other','')}")
    c.drawRightString(x + box_w - 6 * mm, y, f"Total: {data.get('price_total','')}")
    y -= 12 * mm

    # Offerttext (markdown-ish, vi skriver som ren text)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, "Offerttext")
    y -= 7 * mm
    c.setFont("Helvetica", 10)

    line_h = 5.2 * mm

    def new_page():
        nonlocal y
        c.showPage()
        y = height - margin
        c.setFont("Helvetica", 10)

    # skriv offerttext
    for raw in (offer_md or "").splitlines():
        line = raw.replace("\t", "    ").strip()
        # gör rubriker lite tydligare
        if line.startswith("#"):
            line = line.lstrip("#").strip()
            y -= 2 * mm
            c.setFont("Helvetica-Bold", 11)
            c.drawString(x, y, line)
            c.setFont("Helvetica", 10)
            y -= 6 * mm
            if y < margin:
                new_page()
            continue

        # bullets
        if line.startswith(("-", "•")):
            line = "• " + line.lstrip("-• ").strip()

        # wrap
        while len(line) > 110:
            c.drawString(x, y, line[:110])
            y -= line_h
            line = line[110:]
            if y < margin:
                new_page()

        c.drawString(x, y, line)
        y -= line_h
        if y < margin:
            new_page()

    c.save()
    buf.seek(0)
    return buf.read()


# =============================
# UI
# =============================
st.set_page_config(page_title=APP_NAME, page_icon="📄", layout="wide")

st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
      .card {
        border: 1px solid rgba(0,0,0,0.08);
        border-radius: 18px;
        padding: 18px;
        background: rgba(255,255,255,0.75);
      }
      .muted { opacity: 0.75; }
      .stButton button, .stDownloadButton button {
        border-radius: 12px !important;
        padding: 0.65rem 1rem !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

api_key = get_api_key()

with st.sidebar:
    st.markdown(f"## {APP_NAME}")
    st.caption("Skapa en offert på sekunder.")

    # Om du har en app-logga i projektmappen (t.ex. logo.png)
    if os.path.exists("logo.png"):
        st.image("logo.png", use_container_width=True)

    st.divider()
    st.markdown("### Inställningar")

    if api_key:
        st.success("OPENAI_API_KEY hittad")
    else:
        st.warning("Ingen OPENAI_API_KEY hittad (fallback-mall används).")
        st.caption('Lägg nyckeln i Streamlit Secrets som:\n\nOPENAI_API_KEY = "sk-..."')

    st.divider()
    st.markdown("### Kundens logo (valfritt)")
    st.caption("Ladda upp logo för PDF (PNG/JPG)")
    customer_logo_file = st.file_uploader(" ", type=["png", "jpg", "jpeg"], label_visibility="collapsed")

    st.divider()
    st.markdown("### Tips")
    st.caption("Det är kundens logo som ska synas i PDF-offerten.")


st.markdown(f"# {APP_TITLE}")
st.markdown(f'<div class="muted">{APP_TAGLINE}</div>', unsafe_allow_html=True)
st.write("")

# session state
if "offertext" not in st.session_state:
    st.session_state.offertext = ""
if "meta" not in st.session_state:
    st.session_state.meta = {}
if "offer_id" not in st.session_state:
    st.session_state.offer_id = generate_offer_id()

form_col, out_col = st.columns([1.05, 1.25], gap="large")

with form_col:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Projektdata")

    c1, c2 = st.columns(2)
    with c1:
        company = st.text_input("Företagsnamn", value="")
        contact = st.text_input("Kontaktinfo (tel/mejl)", value="")
    with c2:
        date_str = st.date_input("Datum", value=datetime.now()).strftime("%Y-%m-%d")
        location = st.text_input("Plats/ort", value="")

    customer = st.text_input("Kundens namn", value="")
    job_type = st.text_input("Typ av jobb", value="")
    size = st.text_input("Omfattning / storlek", value="")
    material = st.text_input("Material", value="")

    st.write("")
    st.markdown("#### Pris (SEK)")
    p1, p2, p3 = st.columns(3)
    with p1:
        price_work = st.number_input("Arbete", min_value=0, value=0, step=500)
    with p2:
        price_material = st.number_input("Material", min_value=0, value=0, step=500)
    with p3:
        price_other = st.number_input("Övrigt", min_value=0, value=0, step=500)

    total_price = int(price_work + price_material + price_other)

    comment = st.text_area(
        "Kommentar / önskemål (valfritt)",
        height=110,
        placeholder="T.ex. ROT, tidsönskemål, specifika material, budget…",
    )

    st.write("")
    gen = st.button("Generera offert", use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

# Generera text
if gen:
    missing = []
    for val, label in [
        (company, "Företagsnamn"),
        (contact, "Kontaktinfo"),
        (customer, "Kundens namn"),
        (location, "Plats/ort"),
        (job_type, "Typ av jobb"),
        (size, "Omfattning / storlek"),
    ]:
        if not str(val).strip():
            missing.append(label)

    if missing:
        st.error("Fyll i: " + ", ".join(missing))
    else:
        d = {
            "company": company.strip(),
            "contact": contact.strip(),
            "date": date_str,
            "customer": customer.strip(),
            "location": location.strip(),
            "job_type": job_type.strip(),
            "size": size.strip(),
            "material": material.strip(),
            "comment": comment.strip(),
            "offer_id": st.session_state.offer_id,
            "price_work": int(price_work),
            "price_material": int(price_material),
            "price_other": int(price_other),
            "price_total": total_price,
        }

        # Om ingen nyckel / ingen OpenAI-klient => fallback-text
        if (not api_key) or (OpenAI is None):
            st.session_state.offertext = f"""# Offert för {d['job_type']}

**Offert-ID:** {d['offer_id']}  
**Datum:** {d['date']}  
**Företag:** {d['company']}  
**Kontakt:** {d['contact']}  
**Kund:** {d['customer']}  
**Plats/ort:** {d['location']}

## Projektbeskrivning
Vi lämnar härmed offert för {d['job_type']} enligt angivna uppgifter.

## Arbetsmoment
- Genomgång och planering
- Utförande enligt överenskommelse
- Avstämning och slutbesiktning

## Material
- Enligt överenskommelse: {d['material']}

## Tidsplan
Startdatum: enligt överenskommelse. Leverans: 2–6 veckor beroende på omfattning.

## Pris
- Arbete: {d['price_work']} SEK  
- Material: {d['price_material']} SEK  
- Övrigt: {d['price_other']} SEK  
**Totalpris inkl. moms:** {d['price_total']} SEK

## Villkor
1. Offerten gäller i 30 dagar.
2. Betalningsvillkor: 30 dagar.
3. Tilläggsarbete debiteras enligt överenskommelse.
4. Startdatum enligt överenskommelse.

## Kontakt
{d['company']} – {d['contact']}

Vänliga hälsningar,  
{d['company']}
"""
        else:
            client = OpenAI(api_key=api_key)
            prompt = build_prompt(d)

            try:
                with st.spinner("AI skriver offerten…"):
                    resp = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {"role": "system", "content": "Du skriver professionella svenska offerter för byggrelaterade tjänster (B2B)."},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.3,
                        max_tokens=900,
                    )
                st.session_state.offertext = resp.choices[0].message.content
            except Exception as e:
                st.session_state.offertext = ""
                st.error(f"Kunde inte generera offert: {e}")

        st.session_state.meta = {"jobb": d["job_type"], "kund": d["customer"], "datum": d["date"]}

with out_col:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Färdig offert")

    if not st.session_state.offertext:
        st.info("Generera en offert så dyker den upp här.")
    else:
        offertext = st.session_state.offertext
        st.markdown(offertext)

        st.write("")
        st.markdown("### Ladda ner")

        meta = st.session_state.meta or {}
        fname_base = f"offert_{safe_filename(meta.get('jobb','jobb'))}_{safe_filename(meta.get('kund','kund'))}_{meta.get('datum','')}"

        customer_logo_bytes = customer_logo_file.read() if customer_logo_file else None

        # Premium PDF
        pdf_buffer = generate_pdf_premium(
            offer_md=offertext,
            data={
                "company": company.strip(),
                "contact": contact.strip(),
                "customer": customer.strip(),
                "location": location.strip(),
                "job_type": job_type.strip(),
                "size": size.strip(),
                "material": material.strip(),
                "date": date_str,
                "offer_id": st.session_state.offer_id,
                "price_work": int(price_work),
                "price_material": int(price_material),
                "price_other": int(price_other),
                "price_total": int(total_price),
            },
            customer_logo_bytes=customer_logo_bytes,
        )

        st.download_button(
            "📄 Ladda ner premium-PDF",
            data=pdf_buffer,
            file_name=f"{fname_base}_premium.pdf",
            mime="application/pdf",
            use_container_width=True,
        )

        st.download_button(
            "⬇️ Ladda ner som .md",
            data=offertext,
            file_name=f"{fname_base}.md",
            mime="text/markdown; charset=utf-8",
            use_container_width=True,
        )

    st.markdown("</div>", unsafe_allow_html=True)








