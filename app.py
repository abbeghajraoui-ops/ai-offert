st.image("logo.png", width=180)
import os
import re
from datetime import datetime
from io import BytesIO

import streamlit as st
from openai import OpenAI

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm


# -----------------------------
# Helpers
# -----------------------------
def load_api_key_from_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def safe_filename(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_\-]", "", s)
    return (s[:60] or "offert")


def offer_to_pdf_bytes(title: str, body: str) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    x = 18 * mm
    y = height - 18 * mm
    line_height = 5.2 * mm

    c.setFont("Helvetica-Bold", 14)
    c.drawString(x, y, title)
    y -= 10 * mm

    c.setFont("Helvetica", 10)

    def new_page():
        nonlocal y
        c.showPage()
        c.setFont("Helvetica", 10)
        y = height - 18 * mm

    # Enkel radbrytning och sidbryt
    for raw_line in body.splitlines():
        line = raw_line.replace("\t", "    ")

        # wrap lite mjukt
        while len(line) > 110:
            c.drawString(x, y, line[:110])
            y -= line_height
            line = line[110:]
            if y < 18 * mm:
                new_page()

        c.drawString(x, y, line)
        y -= line_height

        if y < 18 * mm:
            new_page()

    c.save()
    buffer.seek(0)
    return buffer.read()


def build_prompt(d: dict) -> str:
    return f"""
Du är en professionell byggkalkylator som skriver offerter på svenska.

Skapa en tydlig och proffsig offert baserat på:

Företag: {d['foretagsnamn']}
Kontakt: {d['kontaktinfo']}
Datum: {d['datum']}
Kund: {d['kund']}
Plats/ort: {d['plats']}

Typ av arbete: {d['jobb']}
Omfattning/storlek: {d['storlek']}
Material: {d['material']}
Kommentar/önskemål: {d['kommentar']}

Krav:
- Använd rubriker: Projektbeskrivning, Arbetsmoment, Material, Tidsplan, Pris, Villkor, Kontakt
- Arbetsmoment: punktlista
- Materiallista: punktlista
- Tidsuppskattning (realistisk)
- Prisuppdelning (arbete, material, övrigt) + totalpris inkl. moms
- 4–6 korta villkor (giltighetstid, betalning, tilläggsarbete, startdatum “enl. överenskommelse”)
- Datum ska vara exakt: {d['datum']} (skriv inte “[Dagens datum]”)
- Avsluta med vänlig hälsning och företagets kontakt

Skriv kortfattat och tydligt.
"""


# -----------------------------
# App setup
# -----------------------------
st.set_page_config(page_title="AI-offertgenerator", page_icon="🧱", layout="wide")

st.markdown(
    """
    <style>
      .block-container { padding-top: 1.4rem; padding-bottom: 2rem; }
      .stButton button { border-radius: 12px; padding: 0.6rem 1rem; }
      .stDownloadButton button { border-radius: 12px; padding: 0.6rem 1rem; }
      .card {
        border: 1px solid rgba(0,0,0,0.08);
        border-radius: 18px;
        padding: 18px;
        background: rgba(255,255,255,0.7);
      }
      .muted { opacity: 0.75; }
    </style>
    """,
    unsafe_allow_html=True,
)

load_api_key_from_env_file()
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    st.error("Hittar ingen OPENAI_API_KEY i .env i C:\\ai-offert. Lägg in nyckeln och starta om appen.")
    st.stop()

client = OpenAI(api_key=api_key)

# -----------------------------
# UI
# -----------------------------
with st.sidebar:
    st.title("🧱 AI-offertgenerator")
    st.caption("Skapa en offert på sekunder.")
    if os.path.exists("logo.png"):
        st.image("logo.png", use_container_width=True)
    st.divider()
    st.caption("Tips: Lägg in företagets logga som logo.png i C:\\ai-offert")

st.markdown("## AI-offertgenerator för byggföretag")
st.markdown('<div class="muted">Fyll i uppgifterna → generera offert → ladda ner som PDF.</div>', unsafe_allow_html=True)
st.write("")

form_col, out_col = st.columns([1.05, 1.25], gap="large")

with form_col:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Projektdata")

    c1, c2 = st.columns(2)
    with c1:
        foretagsnamn = st.text_input("Företagsnamn", value="RivoBygg")
        kontaktinfo = st.text_input("Kontaktinfo (tel/mejl)", value="070-000 00 00 • info@rivobygg.se")
    with c2:
        datum = st.date_input("Datum", value=datetime.now()).strftime("%Y-%m-%d")
        plats = st.text_input("Plats/ort", value="Landskrona")

    kund = st.text_input("Kundens namn", value="Hamid")
    jobb = st.text_input("Typ av jobb", value="Ombyggnation")
    storlek = st.text_input("Omfattning / storlek", value="145 kvm")
    material = st.text_input("Material", value="Standardmaterial enligt överenskommelse")
    kommentar = st.text_area("Kommentar / önskemål (valfritt)", height=90, placeholder="T.ex. ROT, tidsönskemål, specifika material…")

    st.write("")
    gen = st.button("Generera offert", use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

if "offertext" not in st.session_state:
    st.session_state.offertext = ""
if "meta" not in st.session_state:
    st.session_state.meta = {}

if gen:
    missing = []
    for val, label in [
        (foretagsnamn, "Företagsnamn"),
        (kontaktinfo, "Kontaktinfo"),
        (kund, "Kundens namn"),
        (plats, "Plats/ort"),
        (jobb, "Typ av jobb"),
        (storlek, "Storlek"),
    ]:
        if not str(val).strip():
            missing.append(label)

    if missing:
        st.error("Fyll i: " + ", ".join(missing))
    else:
        d = {
            "foretagsnamn": foretagsnamn.strip(),
            "kontaktinfo": kontaktinfo.strip(),
            "datum": datum,
            "kund": kund.strip(),
            "plats": plats.strip(),
            "jobb": jobb.strip(),
            "storlek": storlek.strip(),
            "material": material.strip(),
            "kommentar": kommentar.strip(),
        }

        prompt = build_prompt(d)

        with out_col:
            st.markdown('<div class="card">', unsafe_allow_html=True)
            st.subheader("Genererar…")
            with st.spinner("AI skriver offerten…"):
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "Du skriver professionella svenska bygg-offerter."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=900,
                )
                st.session_state.offertext = resp.choices[0].message.content
                st.session_state.meta = {"jobb": d["jobb"], "kund": d["kund"], "datum": d["datum"]}
            st.success("Klart!")
            st.markdown("</div>", unsafe_allow_html=True)

with out_col:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Färdig offert")

    if not st.session_state.offertext:
        st.info("Generera en offert så dyker den upp här.")
    else:
        offertext = st.session_state.offertext

        # Snygg preview
        st.markdown(offertext)

        st.write("")
        st.markdown("### Ladda ner")

        meta = st.session_state.meta or {}
        fname_base = f"offert_{safe_filename(meta.get('jobb','jobb'))}_{safe_filename(meta.get('kund','kund'))}_{meta.get('datum','')}"
        pdf_bytes = offer_to_pdf_bytes("Offert", offertext)

        st.download_button(
            "⬇️ Ladda ner som PDF",
            data=pdf_bytes,
            file_name=f"{fname_base}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )

        st.download_button(
            "⬇️ Ladda ner som .txt",
            data=offertext,
            file_name=f"{fname_base}.txt",
            mime="text/plain; charset=utf-8",
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

