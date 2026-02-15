import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional, Dict, Any, List

import streamlit as st
import stripe
import bcrypt

# OpenAI (nya klienten)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# PDF (ReportLab)
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader


# =========================================================
# Branding / Planer
# =========================================================
APP_NAME = "Offertly"
APP_TITLE = "Offertly – offertmotor för bygg & VVS"
APP_TAGLINE = "För byggfirmor och VVS-firmor som skickar offerter till privatkunder. Skapa en proffsig offert på under 60 sekunder."

TARGET_CHIPS = ["Byggfirmor", "Snickare", "VVS-firmor", "Plattsättare", "Elektriker", "Målare"]

PLANS: Dict[str, Dict[str, Any]] = {
    "starter": {
        "label": "Starter",
        "price_text": "199 kr/mån",
        "limit_per_month": 50,
        "features": ["50 offerter/mån", "Premium-PDF", "Kundlogo i PDF", "Standardmallar"],
        "stripe_price_id_secret": "STRIPE_PRICE_ID_STARTER",
    },
    "pro": {
        "label": "Pro (populär)",
        "price_text": "499 kr/mån",
        "limit_per_month": 300,
        "features": ["300 offerter/mån", "Premium-PDF", "Mallar (altan, badrum, VVS)", "Offert-historik"],
        "stripe_price_id_secret": "STRIPE_PRICE_ID_PRO",
    },
    "team": {
        "label": "Team",
        "price_text": "1 199 kr/mån",
        "limit_per_month": 1000,
        "features": ["1 000 offerter/mån", "Flera användare (senare)", "Offert-historik", "Företagsanpassat (senare)"],
        "stripe_price_id_secret": "STRIPE_PRICE_ID_TEAM",
    },
}

# Mallar (Fas 4): enkla prompt-varianter
TEMPLATES = {
    "Standard": "STANDARD",
    "Altan": "ALTAN",
    "Badrum": "BADRUM",
    "VVS": "VVS",
}


# =========================================================
# Secrets / Config
# =========================================================
def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        if name in st.secrets:
            v = str(st.secrets[name]).strip()
            return v or default
    except Exception:
        pass
    return (os.getenv(name) or default)


def app_base_url() -> str:
    url = get_secret("APP_BASE_URL", "")
    return (url or "").rstrip("/")


def stripe_setup() -> bool:
    key = get_secret("STRIPE_SECRET_KEY")
    if not key:
        return False
    stripe.api_key = key
    return True


def get_price_id(plan_key: str) -> Optional[str]:
    secret_name = PLANS[plan_key]["stripe_price_id_secret"]
    return get_secret(secret_name)


def get_openai_client() -> Optional["OpenAI"]:
    api_key = get_secret("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


def get_openai_model() -> str:
    return get_secret("OPENAI_MODEL", "gpt-4o-mini") or "gpt-4o-mini"


# =========================================================
# DB (SQLite)
# =========================================================
DB_PATH = "offertly.db"


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash BLOB NOT NULL,
            created_at INTEGER NOT NULL,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            stripe_subscription_status TEXT,
            plan TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            offer_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            template TEXT,
            company TEXT,
            contact TEXT,
            customer TEXT,
            location TEXT,
            job_type TEXT,
            size TEXT,
            material TEXT,
            price_work INTEGER,
            price_material INTEGER,
            price_other INTEGER,
            price_total INTEGER,
            md TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )
    conn.commit()
    conn.close()


def get_user_by_email(email: str) -> Optional[dict]:
    conn = db()
    cur = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),))
    row = cur.fetchone()
    cols = [d[0] for d in cur.description] if cur.description else []
    conn.close()
    return dict(zip(cols, row)) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    conn = db()
    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    cols = [d[0] for d in cur.description] if cur.description else []
    conn.close()
    return dict(zip(cols, row)) if row else None


def create_user(email: str, password: str) -> Optional[int]:
    email = email.lower().strip()
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    conn = db()
    try:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
            (email, password_hash, int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def update_user_subscription(
    user_id: int,
    *,
    customer_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
    status: Optional[str] = None,
    plan: Optional[str] = None,
):
    conn = db()
    conn.execute(
        """
        UPDATE users
        SET
            stripe_customer_id = COALESCE(?, stripe_customer_id),
            stripe_subscription_id = COALESCE(?, stripe_subscription_id),
            stripe_subscription_status = COALESCE(?, stripe_subscription_status),
            plan = COALESCE(?, plan)
        WHERE id = ?
        """,
        (customer_id, subscription_id, status, plan, user_id),
    )
    conn.commit()
    conn.close()


def insert_offer(user_id: int, offer: dict, md: str):
    conn = db()
    conn.execute(
        """
        INSERT INTO offers (
            user_id, offer_id, created_at, template,
            company, contact, customer, location,
            job_type, size, material,
            price_work, price_material, price_other, price_total,
            md
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            user_id,
            offer["offer_id"],
            int(time.time()),
            offer.get("template") or "Standard",
            offer.get("company") or "",
            offer.get("contact") or "",
            offer.get("customer") or "",
            offer.get("location") or "",
            offer.get("job_type") or "",
            offer.get("size") or "",
            offer.get("material") or "",
            int(offer.get("price_work") or 0),
            int(offer.get("price_material") or 0),
            int(offer.get("price_other") or 0),
            int(offer.get("price_total") or 0),
            md,
        ),
    )
    conn.commit()
    conn.close()


def month_start_ts_utc() -> int:
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return int(month_start.timestamp())


def count_offers_current_month(user_id: int) -> int:
    start_ts = month_start_ts_utc()
    conn = db()
    cur = conn.execute(
        "SELECT COUNT(*) FROM offers WHERE user_id = ? AND created_at >= ?",
        (user_id, start_ts),
    )
    n = int(cur.fetchone()[0])
    conn.close()
    return n


def get_recent_offers(user_id: int, limit: int = 10) -> List[dict]:
    conn = db()
    cur = conn.execute(
        """
        SELECT offer_id, created_at, template, job_type, customer, location, price_total
        FROM offers
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        offer_id, created_at, template, job_type, customer, location, price_total = r
        dt = datetime.fromtimestamp(int(created_at), tz=timezone.utc).strftime("%Y-%m-%d")
        out.append(
            {
                "Offert-ID": offer_id,
                "Datum": dt,
                "Mall": template or "",
                "Tjänst": job_type or "",
                "Kund": customer or "",
                "Ort": location or "",
                "Total (SEK)": int(price_total or 0),
            }
        )
    return out


def get_offer_md_by_offer_id(user_id: int, offer_id: str) -> Optional[dict]:
    conn = db()
    cur = conn.execute(
        """
        SELECT template, company, contact, customer, location, job_type, size, material,
               price_work, price_material, price_other, price_total, md
        FROM offers
        WHERE user_id = ? AND offer_id = ?
        """,
        (user_id, offer_id),
    )
    row = cur.fetchone()
    cols = [d[0] for d in cur.description] if cur.description else []
    conn.close()
    if not row:
        return None
    return dict(zip(cols, row))


# =========================================================
# Auth
# =========================================================
def valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (email or "").strip().lower()))


def verify_password(password: str, password_hash: bytes) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash)
    except Exception:
        return False


def login_user(user_id: int):
    st.session_state["user_id"] = user_id


def logout_user():
    st.session_state.pop("user_id", None)


def current_user() -> Optional[dict]:
    uid = st.session_state.get("user_id")
    if not uid:
        return None
    return get_user_by_id(int(uid))


def has_active_subscription(u: dict) -> bool:
    status = (u.get("stripe_subscription_status") or "").lower()
    return status in ("active", "trialing")


# =========================================================
# Stripe checkout + success redirect
# =========================================================
def create_checkout_session(user: dict, plan_key: str) -> str:
    base = app_base_url()
    if not base:
        raise RuntimeError('APP_BASE_URL saknas i Secrets (ex: APP_BASE_URL="https://din-app.streamlit.app")')

    if not stripe_setup():
        raise RuntimeError("STRIPE_SECRET_KEY saknas i Secrets.")

    price_id = get_price_id(plan_key)
    if not price_id:
        raise RuntimeError(f"Saknar Stripe Price ID i Secrets: {PLANS[plan_key]['stripe_price_id_secret']}")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{base}?success=1&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base}?canceled=1",
        customer_email=user["email"],
        allow_promotion_codes=True,
        metadata={"user_id": str(user["id"]), "plan_key": plan_key, "app": APP_NAME},
    )
    return session.url  # type: ignore


def handle_stripe_success_callback(user: dict):
    if not stripe_setup():
        return
    params = st.query_params
    success = params.get("success")
    session_id = params.get("session_id")
    if not success or not session_id:
        return
    try:
        sess = stripe.checkout.Session.retrieve(session_id)
        customer_id = sess.get("customer")
        subscription_id = sess.get("subscription")

        plan_key = None
        md = sess.get("metadata") or {}
        if isinstance(md, dict):
            plan_key = md.get("plan_key")

        status = None
        if subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            status = (sub.get("status") or "").lower()

        update_user_subscription(
            user["id"],
            customer_id=str(customer_id) if customer_id else None,
            subscription_id=str(subscription_id) if subscription_id else None,
            status=status,
            plan=plan_key,
        )
        st.query_params.clear()
        st.success("✅ Betalning klar! Ditt konto är nu aktivt.")
    except Exception as e:
        st.error(f"Kunde inte verifiera betalningen: {e}")


def sync_subscription_from_stripe(user: dict):
    if not stripe_setup():
        return
    sub_id = user.get("stripe_subscription_id")
    if not sub_id:
        return
    try:
        sub = stripe.Subscription.retrieve(sub_id)
        status = (sub.get("status") or "").lower()
        update_user_subscription(user["id"], status=status)
    except Exception:
        return


# =========================================================
# Offer generator helpers
# =========================================================
def safe_filename(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_\-]", "", s)
    return (s[:60] or "offert")


def generate_offer_id() -> str:
    return "OFF-" + uuid.uuid4().hex[:8].upper()


def template_instructions(template_key: str) -> str:
    t = (template_key or "Standard").strip()
    if t == "Altan":
        return "Fokusera på altan/uteplats: grund, stomme, räcke, trapp, impregnerat virke, tidplan i dagar."
    if t == "Badrum":
        return "Fokusera på badrum: rivning, tätskikt, plattsättning, VVS, el (om relevant), kvalitet, garanti."
    if t == "VVS":
        return "Fokusera på VVS: installation/byte, material (rör, kopplingar), provtryckning, dokumentation."
    return "Standard offert för privatkund, tydlig och kort."


def build_prompt(d: dict) -> str:
    return f"""
Du är en professionell offertskrivare för bygg- och VVS-tjänster till privatkunder. Skriv på svenska.

Mall/typ: {d['template']}
Extra instruktion: {template_instructions(d['template'])}

Skapa en tydlig, proffsig offert baserat på:

Företag: {d['company']}
Kontakt: {d['contact']}
Datum: {d['date']}
Kund: {d['customer']}
Plats/ort: {d['location']}

Tjänst / typ av jobb: {d['job_type']}
Omfattning/storlek: {d['size']}
Material: {d['material']}
Kommentar/önskemål: {d['comment']}

Prisuppgifter (använd exakt dessa siffror):
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
- 4–6 villkor: giltighetstid, betalning, tillägg/ändringar, startdatum
- Datum ska vara exakt: {d['date']}

Skriv kortfattat, tydligt och professionellt.
""".strip()


def generate_offer_text(d: dict) -> str:
    client = get_openai_client()
    if client is None:
        # Fallback om OpenAI saknas
        return f"""# Offert för {d['job_type']}

**Offert-ID:** {d['offer_id']}  
**Datum:** {d['date']}  
**Företag:** {d['company']}  
**Kontakt:** {d['contact']}  
**Kund:** {d['customer']}  
**Plats/ort:** {d['location']}

## Projektbeskrivning
Vi lämnar härmed offert för **{d['job_type']}** enligt angivna uppgifter.

## Arbetsmoment
- Genomgång och planering
- Utförande enligt överenskommelse
- Avstämning och slutbesiktning

## Material
- {d['material'] or "Enligt överenskommelse"}

## Tidsplan
Startdatum: enligt överenskommelse.

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
""".strip()

    prompt = build_prompt(d)
    resp = client.chat.completions.create(
        model=get_openai_model(),
        messages=[
            {"role": "system", "content": "Du skriver professionella svenska offerter för bygg- och VVS-tjänster till privatkunder."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=900,
    )
    return (resp.choices[0].message.content or "").strip()


# =========================================================
# PDF (Premium)
# =========================================================
def draw_wrapped_text(c: canvas.Canvas, text: str, x: float, y: float, max_chars: int, line_h: float):
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


def generate_pdf_premium(offer_md: str, data: dict, customer_logo_bytes: Optional[bytes] = None) -> bytes:
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
    c.drawRightString(width - margin, y, APP_NAME)
    y -= 10 * mm

    # Kundens logga (valfritt)
    if customer_logo_bytes:
        try:
            img = ImageReader(BytesIO(customer_logo_bytes))
            logo_w = 38 * mm
            logo_h = 22 * mm
            c.drawImage(
                img,
                width - margin - logo_w,
                height - margin - logo_h - 6 * mm,
                logo_w,
                logo_h,
                mask="auto",
            )
        except Exception:
            pass

    # Meta
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

    # Jobbinfo
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

    # Offerttext
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

    for raw in (offer_md or "").splitlines():
        line = raw.replace("\t", "    ").strip()

        # rubriker
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

        if line.startswith(("-", "•")):
            line = "• " + line.lstrip("-• ").strip()

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


# =========================================================
# UI Components
# =========================================================
def pricing_cards(show_cta: bool = False, user: Optional[dict] = None):
    st.markdown("### Prisplaner")
    cols = st.columns(3)
    for i, (key, plan) in enumerate(PLANS.items()):
        with cols[i]:
            st.markdown(
                f"""
                <div style="
                    border: 1px solid rgba(0,0,0,0.10);
                    border-radius: 16px;
                    padding: 16px;
                    background: rgba(255,255,255,0.75);
                    min-height: 230px;
                ">
                  <div style="font-weight:800; font-size:16px;">{plan['label']}</div>
                  <div style="font-size:24px; font-weight:900; margin-top:6px;">{plan['price_text']}</div>
                  <div style="margin-top:10px; opacity:0.90;">
                    <ul style="padding-left: 18px; margin: 0;">
                      {''.join([f"<li>{x}</li>" for x in plan["features"]])}
                    </ul>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if show_cta and user:
                if st.button(f"Välj {plan['label']}", key=f"choose_{key}", use_container_width=True):
                    try:
                        url = create_checkout_session(user, key)
                        st.link_button("Fortsätt till betalning", url, use_container_width=True)
                        st.caption("Om knappen inte öppnar: kopiera länken nedan.")
                        st.code(url)
                    except Exception as e:
                        st.error(str(e))


def auth_box():
    st.markdown("## Logga in / Skapa konto")
    tab1, tab2 = st.tabs(["Logga in", "Skapa konto"])

    with tab1:
        email = st.text_input("E-post", key="login_email")
        password = st.text_input("Lösenord", type="password", key="login_password")
        if st.button("Logga in", use_container_width=True):
            if not valid_email(email):
                st.error("Ange en giltig e-postadress.")
                return
            u = get_user_by_email(email)
            if not u or not verify_password(password, u["password_hash"]):
                st.error("Fel e-post eller lösenord.")
                return
            login_user(u["id"])
            st.rerun()

    with tab2:
        email = st.text_input("E-post", key="signup_email")
        password = st.text_input("Lösenord (minst 8 tecken)", type="password", key="signup_password")
        password2 = st.text_input("Upprepa lösenord", type="password", key="signup_password2")
        if st.button("Skapa konto", use_container_width=True):
            if not valid_email(email):
                st.error("Ange en giltig e-postadress.")
                return
            if len(password) < 8:
                st.error("Lösenordet måste vara minst 8 tecken.")
                return
            if password != password2:
                st.error("Lösenorden matchar inte.")
                return
            user_id = create_user(email, password)
            if not user_id:
                st.error("Det finns redan ett konto med den e-postadressen.")
                return
            login_user(user_id)
            st.success("✅ Konto skapat!")
            st.rerun()


def landing_page():
    st.markdown(f"# {APP_TITLE}")
    st.markdown(f"<div style='opacity:.75'>{APP_TAGLINE}</div>", unsafe_allow_html=True)
    st.write("")
    st.markdown("### Målgrupp")
    st.write(" ".join([f"`{c}`" for c in TARGET_CHIPS]))

    st.write("")
    st.markdown("### Varför Offertly?")
    st.markdown(
        """
- ⏱ Skapa offert på under 1 minut  
- 📄 Snygg PDF direkt till kund  
- 💰 Tydlig prisuppdelning  
- 🧠 AI-text som låter professionell  
"""
    )
    st.write("")
    pricing_cards(show_cta=False)
    st.write("")
    st.divider()
    auth_box()


def paywall(user: dict):
    st.markdown("## Aktivera konto")
    st.caption("För att använda Offertly behöver du en aktiv prenumeration.")
    if not stripe_setup():
        st.error("Stripe är inte konfigurerat. Lägg STRIPE_SECRET_KEY i Secrets.")
        st.stop()
    pricing_cards(show_cta=True, user=user)
    st.write("")
    st.caption("Tips: Om du nyss betalade och status inte uppdaterats, logga ut/in eller uppdatera sidan.")
    if st.button("🔄 Uppdatera status", use_container_width=True):
        sync_subscription_from_stripe(user)
        st.rerun()


def quota_bar(user: dict) -> tuple[int, int, int]:
    plan_key = (user.get("plan") or "starter").lower()
    if plan_key not in PLANS:
        plan_key = "starter"
    limit = int(PLANS[plan_key]["limit_per_month"])
    used = count_offers_current_month(user["id"])
    remaining = max(0, limit - used)

    st.caption(f"Plan: **{PLANS[plan_key]['label']}** • Använt denna månad: **{used}/{limit}** • Kvar: **{remaining}**")
    st.progress(min(1.0, used / max(1, limit)))
    return used, limit, remaining


def main_app(user: dict):
    st.markdown(f"# {APP_TITLE}")
    st.markdown(f"<div style='opacity:.75'>{APP_TAGLINE}</div>", unsafe_allow_html=True)
    st.write("")

    used, limit, remaining = quota_bar(user)

    st.write("")
    left, right = st.columns([1.05, 1.25], gap="large")

    # Sidebar settings
    with st.sidebar:
        st.divider()
        st.markdown("### Kundens logo (valfritt)")
        st.caption("Ladda upp logo som ska synas i PDF (PNG/JPG).")
        customer_logo_file = st.file_uploader(" ", type=["png", "jpg", "jpeg"], label_visibility="collapsed")
        st.session_state["customer_logo_bytes"] = customer_logo_file.read() if customer_logo_file else None

        st.divider()
        st.markdown("### Snabbtips")
        st.caption("Börja med Standard → lägg till fler mallar senare.")

    # Form
    with left:
        st.subheader("Offertgenerator")

        template = st.selectbox("Mall", list(TEMPLATES.keys()), index=0)

        c1, c2 = st.columns(2)
        with c1:
            company = st.text_input("Företagsnamn (utförare)", value="")
            contact = st.text_input("Kontaktinfo (tel/mejl)", value="")
        with c2:
            date_str = st.date_input("Datum", value=datetime.now()).strftime("%Y-%m-%d")
            location = st.text_input("Plats/ort", value="")

        customer = st.text_input("Beställare / kundens namn", value="")
        job_type = st.text_input("Tjänst / typ av jobb", value="", placeholder="T.ex. Altanbygge, Badrumsrenovering, VVS-installation…")
        size = st.text_input("Omfattning / storlek", value="", placeholder="T.ex. 25 kvm, 1 badrum, 6 radiatorer…")
        material = st.text_input("Material", value="", placeholder="T.ex. tryckimpregnerat, kakel, PEX…")

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
            placeholder="T.ex. ROT (sen), tidsönskemål, specifika material, garanti…",
        )

        st.write("")
        if st.button("Generera offert", use_container_width=True):
            # kvot
            if used >= limit:
                st.error("Du har nått din månadsgräns. Uppgradera plan för fler offerter.")
                st.stop()

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
                st.stop()

            offer = {
                "template": template,
                "offer_id": generate_offer_id(),
                "company": company.strip(),
                "contact": contact.strip(),
                "customer": customer.strip(),
                "location": location.strip(),
                "job_type": job_type.strip(),
                "size": size.strip(),
                "material": material.strip(),
                "comment": comment.strip(),
                "date": date_str,
                "price_work": int(price_work),
                "price_material": int(price_material),
                "price_other": int(price_other),
                "price_total": int(total_price),
            }

            with st.spinner("AI skriver offerten…"):
                md = generate_offer_text(offer)

            st.session_state["last_offer"] = {"offer": offer, "md": md}
            insert_offer(user["id"], offer, md)
            st.success("✅ Offert skapad!")
            st.rerun()

    # Output
    with right:
        st.subheader("Färdig offert")

        last = st.session_state.get("last_offer")
        if not last:
            st.info("Skapa en offert så dyker den upp här.")
        else:
            offer = last["offer"]
            md = last["md"]

            st.markdown(md)

            st.write("")
            st.markdown("### Ladda ner")
            fname_base = f"offert_{safe_filename(offer['job_type'])}_{safe_filename(offer['customer'])}_{offer['date']}"

            customer_logo_bytes = st.session_state.get("customer_logo_bytes")
            pdf_bytes = generate_pdf_premium(md, offer, customer_logo_bytes)

            st.download_button(
                "📄 Ladda ner premium-PDF",
                data=pdf_bytes,
                file_name=f"{fname_base}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )

            st.download_button(
                "⬇️ Ladda ner som .md",
                data=md,
                file_name=f"{fname_base}.md",
                mime="text/markdown; charset=utf-8",
                use_container_width=True,
            )

        st.write("")
        st.divider()
        st.markdown("### Offert-historik")

        recent = get_recent_offers(user["id"], limit=12)
        if recent:
            st.dataframe(recent, use_container_width=True, hide_index=True)

            st.write("")
            st.markdown("### Öppna en tidigare offert")
            picked = st.selectbox("Välj Offert-ID", [""] + [r["Offert-ID"] for r in recent])
            if picked:
                row = get_offer_md_by_offer_id(user["id"], picked)
                if row and row.get("md"):
                    st.session_state["last_offer"] = {
                        "offer": {
                            "template": row.get("template") or "Standard",
                            "offer_id": picked,
                            "company": row.get("company") or "",
                            "contact": row.get("contact") or "",
                            "customer": row.get("customer") or "",
                            "location": row.get("location") or "",
                            "job_type": row.get("job_type") or "",
                            "size": row.get("size") or "",
                            "material": row.get("material") or "",
                            "comment": "",
                            "date": datetime.now().strftime("%Y-%m-%d"),
                            "price_work": int(row.get("price_work") or 0),
                            "price_material": int(row.get("price_material") or 0),
                            "price_other": int(row.get("price_other") or 0),
                            "price_total": int(row.get("price_total") or 0),
                        },
                        "md": row.get("md") or "",
                    }
                    st.success("✅ Laddade vald offert till preview.")
                    st.rerun()
        else:
            st.caption("Inga offerter ännu.")


# =========================================================
# Page setup
# =========================================================
st.set_page_config(page_title=APP_NAME, page_icon="📄", layout="wide")
st.markdown(
    """
<style>
.block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
.stButton button, .stDownloadButton button {
  border-radius: 12px !important;
  padding: .65rem 1rem !important;
}
</style>
""",
    unsafe_allow_html=True,
)

init_db()

# Sidebar status
with st.sidebar:
    st.markdown(f"## {APP_NAME}")
    if os.path.exists("logo.png"):
        st.image("logo.png", use_container_width=True)

    st.divider()
    u = current_user()
    if u:
        st.caption(f"Inloggad som: **{u['email']}**")
        st.caption(f"Plan: **{u.get('plan') or '-'}**")
        st.caption(f"Status: **{u.get('stripe_subscription_status') or '-'}**")
        if st.button("Logga ut", use_container_width=True):
            logout_user()
            st.rerun()
    else:
        st.caption("Inte inloggad")

    st.divider()
    st.caption("Stripe: " + ("✅ OK" if get_secret("STRIPE_SECRET_KEY") else "⚠️ saknas STRIPE_SECRET_KEY"))
    st.caption("APP_BASE_URL: " + ("✅ OK" if app_base_url() else "⚠️ saknas APP_BASE_URL"))
    st.caption("OpenAI: " + ("✅ OK" if get_secret("OPENAI_API_KEY") else "⚠️ saknas OPENAI_API_KEY"))


# =========================================================
# Flow
# =========================================================
user = current_user()

if not user:
    landing_page()
    st.stop()

handle_stripe_success_callback(user)
sync_subscription_from_stripe(user)
user = current_user() or user

if not has_active_subscription(user):
    paywall(user)
    st.stop()

main_app(user)







 






    































