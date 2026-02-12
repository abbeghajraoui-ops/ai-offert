import os
import re
from datetime import date
from io import BytesIO

import streamlit as st

# OpenAI (nya klienten) - valfri, appen funkar även utan
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm


# -----------------------------
# Helpers
# -----------------------------
def load_api_key_from_env_file(path=".env"):
    """Minimal .env-läsare (key=value)"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_api_key():
    """Läs från Streamlit Secrets (Cloud) eller från miljö/.env (lokalt)."""
    # 1) Streamlit secrets
    try:
        if hasattr(st, "secrets") and "OPENAI_API_KEY" in st.secrets:
            v = str(st.secrets["OPENAI_API_KEY"]).strip()
            return v or None
    except Exception:
        pass

    # 2) .env + env var
    load_api_key_from_env_file()
    v = os.getenv("OPENAI_API_KEY", "").strip()
    return v or None


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

    # Enkel radbrytning & sidbryt
    for raw_line in body.splitlines():
        line = raw_line.replace("\t", "    ")
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


def fallback_offer(d: dict) -> str:
    """Om API-nyckel saknas eller är fel, generera en enkel men proffsig offert utan AI."""
    return f"""# Offert – {d['jobb']}

**Företag:** {d['foretagsnamn']}  
**Kontakt:** {d['kontaktinfo']}  
**Datum:** {d['datum']}  
**Kund:** {d['kund']}  
**Plats/ort:** {d['plats']}  

---

## Projektbeskrivning
Denna offert avser **{d['jobb']}** i **{d['plats']}**. Omfattning: **{d['storlek']}**.  
Material: **{d['material']}**.  
Kommentar/önskemål: {d['kommentar'] or "—"}

## Arbetsmoment
- Förberedelse och skydd av ytor
- Rivning / demontering (vid behov)
- Byggnation / montering enligt överenskommelse
- Finjustering, kontroll och städning
- Slutbesiktning med kund

## Material
- Standardmaterial enligt överenskommelse
- Skruv/fästelement
- Eventuella tillbehör enligt behov

## Tidsplan
- Uppskattad tid: **enl. överenskommelse** (påverkas av tillgång på material och eventuella tillägg)

## Pris
- Arbete: **— SEK**
- Material: **— SEK**
- Övrigt (transport/avfall): **— SEK**
- **Totalpris inkl. moms:** **— SEK**

## Villkor
1. Offerten gäller i 30 dagar från datumet ovan.  
2. Betalning 30 dagar efter slutfört arbete, om inget annat avtalats.  
3. Tilläggsarbete debiteras enligt överenskommelse.  
4. Startdatum enligt överenskommelse.  
5. Ändringar kan påverka pris och tidsplan.  

## Kontakt
Vid frågor eller ändringar, kontakta oss.

Vänliga hälsningar,  
**{d['foretagsnamn']}**  
{d['kontaktinfo']}
"""


# -----------------------------
# App setup
# -----------------------------
st.set_page_config(page_title="Offertly", page_icon="📄", layout="wide")

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

api_key = get_api_key()

# init OpenAI client only if possible
client = None
if api_key and OpenAI is not None:
    try:
        client = OpenAI(api_key=api_key)
    except Exception:
        client = None

# -----------------------------
# Sidebar
# -----------------------------
with st.sidebar:
    st.title("📄 Offertly")
    st.caption("Skapa en offert på sekunder.")

    if os.path.exists("logo.png"):
        st.image("logo.png", use_container_width=True)

    st.divider()

    if api_key:
        st.success("OPENAI_API_KEY hittad")
    else:
        st.warning("Ingen OPENAI_API_KEY hittad (fallback-mall används).")
        st.caption('Lägg nyckeln i Streamlit Secrets som:\n\nOPENAI_API_KEY = "sk-..."')

    st.divider()
    st.caption("Tips: Kundens logo kan laddas upp och användas i PDF (steg 2).")


# -----------------------------
# State
# -----------------------------
if "offertext" not in st.session_state:
    st.session_state.offertext = ""
if "meta" not in st.session_state:
    st.session_state.meta = {}


# -----------------------------
# UI
# -----------------------------
st.markdown("## Offertly – AI-offertgenerator")
st.markdown(
    '<div class="muted">Fyll i uppgifterna → generera offert → ladda ner som PDF/.md</div>',
    unsafe_allow_html=True,
)
st.write("")

form_col, out_col = st.columns([1.05, 1.25], gap="large")

with form_col:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Projektdata")

    c1, c2 = st.columns(2)
    with c1:
        foretagsnamn = st.text_input("Företagsnamn", value="")
        kontaktinfo = st.text_input("Kontaktinfo (tel/mejl)", value="")
    with c2:
        datum_val = st.date_input("Datum", value=date.today())
        datum = datum_val.strftime("%Y-%m-%d")
        plats = st.text_input("Plats/ort", value="")

    kund = st.text_input("Kundens namn", value="")
    jobb = st.text_input("Typ av jobb", value="")
    storlek = st.text_input("Omfattning / storlek", value="")
    material = st.text_input("Material", value="")
    kommentar = st.text_area(
        "Kommentar / önskemål (valfritt)",
        height=90,
        placeholder="T.ex. ROT, tidsönskemål, specifika material…",
    )

    st.write("")
    gen = st.button("Generera offert", use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

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

        # Prova AI om vi har client, annars fallback
        if client is None:
            st.session_state.offertext = fallback_offer(d)
        else:
            prompt = build_prompt(d)
            try:
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
            except Exception as e:
                st.warning("Kunde inte använda AI-nyckeln just nu – fallback-mall används.")
                st.session_state.offertext = fallback_offer(d)

        st.session_state.meta = {"jobb": d["jobb"], "kund": d["kund"], "datum": d["datum"]}

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






