import os
import re
import uuid
from datetime import datetime
from io import BytesIO
from typing import Optional, Dict, Any

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
# Branding
# =============================
APP_NAME = "Offertly"
APP_TITLE = "Offertly – offertmotor för bygg & VVS"
APP_TAGLINE = (
    "För byggfirmor och VVS-firmor som skickar offerter till privatkunder. "
    "Skapa en proffsig offert på under 60 sekunder."
)


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
      1) Streamlit Secrets (utan att krascha om secrets saknas
         eller om appen körs utan secrets.toml)
      2) Miljövariabel
    """
    try:
        if "OPENAI_API_KEY" in st.secrets:
            v = str(st.secrets["OPENAI_API_KEY"]).strip()
            return v or None
    except Exception:
        pass

    v = os.getenv("OPENAI_API_KEY", "").strip()
    return v or None


def generate_offer_id() -> str:
    return "OFF-" + uuid.uuid4().hex[:8].upper()


def format_sek(n: int) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return str(n)


def build_prompt(d: Dict[str, Any]) -> str:
    # Bygg + VVS, privatkund, tydligt och proffsigt
    return f"""
Du är en professionell offertskrivare för bygg- och VVS-uppdrag till privatkunder i Sverige. Skriv på svenska.

Skapa en tydlig, kortfattad och proffsig offert baserat på:

Utförare (företag): {d['company']}
Kontakt: {d['contact']}
Datum: {d['date']}
Kund: {d['customer']}
Plats/ort: {d['location']}

Tjänst / typ av jobb: {d['job_type']}
Omfattning / storlek: {d['size']}
Material / specifikation: {d['material']}
Kommentar / önskemål: {d['comment']}

Pris (SEK, inkl. moms) – använd dessa exakt:
- Arbete: {d['price_work']} SEK
- Material: {d['price_material']} SEK
- Övrigt: {d['price_other']} SEK
- Total: {d['price_total']} SEK

Krav:
- Rubriker i denna ordning: Projektbeskrivning, Arbetsmoment, Material, Tidsplan, Pris, Villkor, Kontakt
- Arbetsmoment: punktlista
- Material: punktlista (om relevant)
- Tidsplan: realistisk uppskattning
- Pris: visa uppdelning + total (inkl. moms)
- 4–6 korta villkor: giltighetstid, betalning, tilläggsarbete, avbokning/ändringar, startdatum “enl. överenskommelse”
- Avsluta med vänlig hälsning och kontakt

Skriv tydligt utan onödigt fluff. Använd gärna fetstil för viktiga rader (som totalpris).
Datum ska vara exakt: {d['date']}.
Offert-ID ska vara exakt: {d['offer_id']}.
"""


def draw_wrapped_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    max_chars: int,
    line_h: float,
    margin_bottom: float,
    new_page_fn,
):
    for raw in (text or "").splitlines():
        line = raw.replace("\t", "    ").rstrip()

        # tomrad
        if not line.strip():
            y -= line_h
            if y < margin_bottom:
                y = new_page_fn()
            continue

        while len(line) > max_chars:
            c.drawString(x, y, line[:max_chars])
            y -= line_h
            line = line[max_chars:]
            if y < margin_bottom:
                y = new_page_fn()

        c.drawString(x, y, line)
        y -= line_h
        if y < margin_bottom:
            y = new_page_fn()

    return y


def generate_pdf_premium(
    offer_text: str,
    data: Dict[str, Any],
    customer_logo_bytes: Optional[bytes] = None,
) -> bytes:
    """
    Premium PDF: header + kundlogo (valfri) + metadata + prisruta + offerttext.
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    margin = 18 * mm
    x = margin
    y = height - margin

    def new_page():
        c.showPage()
        c.setFont("Helvetica", 10)
        return height - margin

    # Header
    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y, "OFFERT")
    c.setFont("Helvetica", 10)
    c.drawRightString(width - margin, y, APP_NAME)
    y -= 10 * mm

    # Kundlogo (uppe höger)
    if customer_logo_bytes:
        try:
            img = ImageReader(BytesIO(customer_logo_bytes))
            logo_w = 42 * mm
            logo_h = 24 * mm
            c.drawImage(
                img,
                width - margin - logo_w,
                height - margin - logo_h - 6 * mm,
                logo_w,
                logo_h,
                mask="auto",
                preserveAspectRatio=True,
                anchor="c",
            )
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
    y = draw_wrapped_text(
        c,
        f"Kontakt: {data.get('contact','')}",
        x,
        y,
        max_chars=95,
        line_h=5.2 * mm,
        margin_bottom=margin,
        new_page_fn=new_page,
    )
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
    c.drawString(x + 6 * mm, y, f"Arbete: {format_sek(int(data.get('price_work', 0)))}")
    c.drawRightString(x + box_w - 6 * mm, y, f"Material: {format_sek(int(data.get('price_material', 0)))}")
    y -= 5.5 * mm
    c.drawString(x + 6 * mm, y, f"Övrigt: {format_sek(int(data.get('price_other', 0)))}")
    c.drawRightString(x + box_w - 6 * mm, y, f"Total: {format_sek(int(data.get('price_total', 0)))}")
    y -= 12 * mm

    # Offerttext
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, "Offerttext")
    y -= 7 * mm
    c.setFont("Helvetica", 10)

    # “Markdown-ish” till ren text
    line_h = 5.2 * mm

    for raw in (offer_text or "").splitlines():
        line = raw.replace("\t", "    ").rstrip()

        if not line.strip():
            y -= line_h
            if y < margin:
                y = new_page()
            continue

        # rubriker
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            y -= 2 * mm
            c.setFont("Helvetica-Bold", 11)
            c.drawString(x, y, title)
            c.setFont("Helvetica", 10)
            y -= 6 * mm
            if y < margin:
                y = new_page()
            continue

        # bullets
        if stripped.startswith(("-", "•")):
            line = "• " + stripped.lstrip("-• ").strip()

        # wrap
        while len(line) > 110:
            c.drawString(x, y, line[:110])
            y -= line_h
            line = line[110:]
            if y < margin:
                y = new_page()

        c.drawString(x, y, line)
        y -= line_h
        if y < margin:
            y = new_page()

    c.save()
    buf.seek(0)
    return buf.read()


# =============================
# App
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
      .pill {
        display: inline-block;
        padding: 6px 10px;
        border: 1px solid rgba(0,0,0,0.08);
        border-radius: 999px;
        margin-right: 6px;
        margin-bottom: 8px;
        font-size: 0.9rem;
        background: rgba(255,255,255,0.7);
      }
    </style>
    """,
    unsafe_allow_html=True,
)

api_key = get_api_key()

# Session state
if "offertext" not in st.session_state:
    st.session_state.offertext = ""
if "meta" not in st.session_state:
    st.session_state.meta = {}
if "offer_id" not in st.session_state:
    st.session_state.offer_id = generate_offer_id()

# Sidebar
with st.sidebar:
    st.markdown(f"## {APP_NAME}")
    st.caption("Automatisera offerter och vinn fler jobb.")

    # App-logga i projektmappen (valfritt)
    if os.path.exists("logo.png"):
        st.image("logo.png", use_container_width=True)

    st.divider()
    st.markdown("### Inställningar")
    if api_key and (OpenAI is not None):
        st.success("OPENAI_API_KEY hittad")
    elif api_key and (OpenAI is None):
        st.warning("Nyckel hittad men OpenAI-bibliotek saknas.")
        st.caption("Installera paketet `openai` i din miljö/requirements.")
    else:
        st.warning("Ingen OPENAI_API_KEY hittad (fallback-mall används).")
        st.caption('Lägg nyckeln i Streamlit Secrets som:\n\nOPENAI_API_KEY = "sk-..."')

    st.divider()
    st.markdown("### Kundens logo (valfritt)")
    st.caption("Loggan som syns i PDF-offerten (PNG/JPG).")
    customer_logo_file = st.file_uploader(" ", type=["png", "jpg", "jpeg"], label_visibility="collapsed")

    st.divider()
    st.markdown("### Målgrupp")
    st.markdown(
        """
        <span class="pill">Byggfirmor</span>
        <span class="pill">Snickare</span>
        <span class="pill">VVS-firmor</span>
        <span class="pill">Plattsättare</span>
        <span class="pill">Elektriker</span>
        <span class="pill">Målare</span>
        """,
        unsafe_allow_html=True,
    )

# Header
st.markdown(f"# {APP_TITLE}")
st.markdown(f'<div class="muted">{APP_TAGLINE}</div>', unsafe_allow_html=True)
st.write("")

form_col, out_col = st.columns([1.05, 1.25], gap="large")

# Form
with form_col:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Projektdata")

    c1, c2 = st.columns(2)
    with c1:
        company = st.text_input("Företagsnamn (utförare)", value="")
        contact = st.text_input("Kontaktinfo (tel/mejl)", value="")
    with c2:
        date_str = st.date_input("Datum", value=datetime.now()).strftime("%Y-%m-%d")
        location = st.text_input("Plats/ort", value="")

    customer = st.text_input("Beställare / kundens namn", value="")
    job_type = st.text_input("Tjänst / typ av jobb", value="")
    size = st.text_input("Omfattning / storlek", value="")
    material = st.text_input("Material", value="")

    st.write("")
    st.markdown("#### Pris (SEK, inkl. moms)")
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

# Generate offer
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
        # nytt offert-ID per generering
        st.session_state.offer_id = generate_offer_id()

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
            "price_total": int(total_price),
        }

        # AI eller fallback
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
- Avstämning och slutgenomgång

## Material
- {d['material'] or "Enligt överenskommelse"}

## Tidsplan
Startdatum: enligt överenskommelse. Utförandetid beror på omfattning.

## Pris
- Arbete: {format_sek(d['price_work'])} kr  
- Material: {format_sek(d['price_material'])} kr  
- Övrigt: {format_sek(d['price_other'])} kr  
**Totalpris inkl. moms: {format_sek(d['price_total'])} kr**

## Villkor
1. Offerten gäller i 30 dagar.
2. Betalningsvillkor: 10–30 dagar (enligt överenskommelse).
3. Tilläggsarbete debiteras separat efter godkännande.
4. Startdatum enligt överenskommelse.
5. Avbokning/ändring ska meddelas snarast möjligt.

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
                            {
                                "role": "system",
                                "content": "Du skriver professionella svenska offerter för bygg & VVS till privatkunder.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.25,
                        max_tokens=950,
                    )
                st.session_state.offertext = resp.choices[0].message.content or ""
            except Exception as e:
                st.session_state.offertext = ""
                st.error(f"Kunde inte generera offert: {e}")

        st.session_state.meta = {
            "jobb": d["job_type"],
            "kund": d["customer"],
            "datum": d["date"],
            "offer_id": d["offer_id"],
        }

# Output
with out_col:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Färdig offert")

    if not st.session_state.offertext:
        st.info("Fyll i projektdata och klicka 'Generera offert' så dyker den upp här.")
    else:
        offertext = st.session_state.offertext
        st.markdown(offertext)

        st.write("")
        st.markdown("### Ladda ner")

        meta = st.session_state.meta or {}
        fname_base = (
            f"offert_{safe_filename(meta.get('jobb','jobb'))}_"
            f"{safe_filename(meta.get('kund','kund'))}_{meta.get('datum','')}_{meta.get('offer_id','')}"
        )

        customer_logo_bytes = customer_logo_file.read() if customer_logo_file else None

        pdf_bytes = generate_pdf_premium(
            offer_text=offertext,
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
            data=pdf_bytes,
            file_name=f"{fname_base}.pdf",
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




    





















