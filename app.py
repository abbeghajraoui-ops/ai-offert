# app.py — Offertly (Fas 3A)
# Innehåller: landningssida + inloggning + Stripe checkout + PAYWALL + offertgenerator + PDF + WEBHOOK sync (auto-aktiv efter betalning)

import os
import re
import sqlite3
import time
import uuid
import hmac
import hashlib
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional, Dict, Any

import streamlit as st

import stripe
import bcrypt

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

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
        "features": ["50 offerter/mån", "PDF + .md", "Kundlogo i PDF", "Standardmall"],
        "stripe_price_id_secret": "STRIPE_PRICE_ID_STARTER",
    },
    "pro": {
        "label": "Pro (populär)",
        "price_text": "499 kr/mån",
        "limit_per_month": 300,
        "features": ["300 offerter/mån", "Premium-PDF", "Flera mallar (altan, badrum, VVS)", "Spara kunddata"],
        "stripe_price_id_secret": "STRIPE_PRICE_ID_PRO",
    },
    "team": {
        "label": "Team",
        "price_text": "1 199 kr/mån",
        "limit_per_month": 1000,
        "features": ["1 000 offerter/mån", "Flera användare", "Offert-historik", "Företagsanpassad mall"],
        "stripe_price_id_secret": "STRIPE_PRICE_ID_TEAM",
    },
}


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
            job_type TEXT,
            customer_name TEXT,
            location TEXT,
            total_price INTEGER,
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


def insert_offer(
    user_id: int,
    offer_id: str,
    created_at: int,
    job_type: str,
    customer_name: str,
    location: str,
    total_price: int,
    md: str,
):
    conn = db()
    conn.execute(
        """
        INSERT INTO offers (user_id, offer_id, created_at, job_type, customer_name, location, total_price, md)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (user_id, offer_id, created_at, job_type, customer_name, location, total_price, md),
    )
    conn.commit()
    conn.close()


def count_offers_current_month(user_id: int) -> int:
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    start_ts = int(month_start.timestamp())
    conn = db()
    cur = conn.execute(
        "SELECT COUNT(*) FROM offers WHERE user_id = ? AND created_at >= ?",
        (user_id, start_ts),
    )
    n = int(cur.fetchone()[0])
    conn.close()
    return n


def get_recent_offers(user_id: int, limit: int = 10) -> list:
    conn = db()
    cur = conn.execute(
        """
        SELECT offer_id, created_at, job_type, customer_name, location, total_price
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
        offer_id, created_at, job_type, customer_name, location, total_price = r
        dt = datetime.fromtimestamp(int(created_at), tz=timezone.utc).strftime("%Y-%m-%d")
        out.append(
            {
                "Offert-ID": offer_id,
                "Datum": dt,
                "Tjänst": job_type or "",
                "Kund": customer_name or "",
                "Ort": location or "",
                "Total (SEK)": total_price or 0,
            }
        )
    return out


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


# =========================================================
# Stripe checkout + success redirect
# =========================================================
def create_checkout_session(user: dict, plan_key: str) -> str:
    base = app_base_url()
    if not base:
        raise RuntimeError('APP_BASE_URL saknas i Secrets (ex: APP_BASE_URL="https://din-app.streamlit.app")')

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
# WEBHOOK (Fas 3A)
# =========================================================
def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_webhook_gate() -> bool:
    # Enkel extra spärr för att ingen ska spamma endpointen
    token = st.query_params.get("token", "")
    expected = get_secret("APP_WEBHOOK_TOKEN", "")
    if not expected:
        return False
    return constant_time_equals(str(token), str(expected))


def webhook_handler():
    """
    Streamlit har ingen "raw body" på samma sätt som Flask.
    För snabbaste/säkraste: vi använder en "gated" endpoint som triggas via GET,
    och hämtar senaste eventen från Stripe via API med session_id/subscription id från query.
    DETTA är Fas 3A: tillräckligt stabilt för att auto-aktivera efter checkout.
    """
    if not stripe_setup():
        st.error("Stripe ej konfigurerat.")
        st.stop()

    if not verify_webhook_gate():
        st.error("Webhook token saknas/fel.")
        st.stop()

    # Stöd: uppdatera subscription via subscription_id eller session_id
    subscription_id = st.query_params.get("sub", "")
    session_id = st.query_params.get("session_id", "")

    try:
        if session_id:
            sess = stripe.checkout.Session.retrieve(session_id)
            subscription_id = sess.get("subscription") or subscription_id

        if not subscription_id:
            st.error("Saknar subscription_id/session_id.")
            st.stop()

        sub = stripe.Subscription.retrieve(subscription_id)
        status = (sub.get("status") or "").lower()

        # Hitta customer email -> matcha user lokalt
        customer = stripe.Customer.retrieve(sub.get("customer"))
        email = (customer.get("email") or "").lower().strip()

        plan_key = None
        # Försök hitta plan via price id i subscription items
        price_id = None
        items = ((sub.get("items") or {}).get("data") or [])
        if items:
            price_id = (items[0].get("price") or {}).get("id")

        if price_id:
            for pk, p in PLANS.items():
                if get_price_id(pk) == price_id:
                    plan_key = pk
                    break

        u = get_user_by_email(email) if email else None
        if not u:
            st.error("Kunde inte hitta användare för email från Stripe customer.")
            st.stop()

        update_user_subscription(
            u["id"],
            customer_id=str(sub.get("customer")) if sub.get("customer") else None,
            subscription_id=str(subscription_id),
            status=status,
            plan=plan_key,
        )

        st.success("✅ Webhook-synk klar.")
        st.write({"email": email, "status": status, "plan": plan_key})
        st.stop()

    except Exception as e:
        st.error(f"Webhook fel: {e}")
        st.stop()


# =========================================================
# Offert-generator (AI + PDF)
# =========================================================
def safe_filename(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_\-]", "", s)
    return (s[:60] or "offert")


def generate_offer_id() -> str:
    return "OFF-" + uuid.uuid4().hex[:8].upper()


def build_prompt(d: dict) -> str:
    return f"""
Du är en professionell offertskrivare för bygg- och VVS-tjänster till privatkunder. Skriv på svenska.

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

    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y, "OFFERT")
    c.setFont("Helvetica", 10)
    c.drawRightString(width - margin, y, APP_NAME)
    y -= 10 * mm

    if customer_logo_bytes:
        try:
            img = ImageReader(BytesIO(customer_logo_bytes))
            logo_w = 38 * mm
            logo_h = 22 * mm
            c.drawImage(img, width - margin - logo_w, height - margin - logo_h - 6 * mm, logo_w, logo_h, mask="auto")
        except Exception:
            pass

    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Offert-ID: {data.get('offer_id','')}")
    c.drawRightString(width - margin, y, f"Datum: {data.get('date','')}")
    y -= 8 * mm

    c.setFont("Helvetica-Bold", 11)
    c.drawString(x, y, data.get("company", ""))
    y -= 5.5 * mm
    c.setFont("Helvetica", 10)
    y = draw_wrapped_text(c, f"Kontakt: {data.get('contact','')}", x, y, 95, 5.2 * mm)
    y -= 2 * mm

    c.setFont("Helvetica-Bold", 11)
    c.drawString(x, y, f"Kund: {data.get('customer','')}")
    y -= 5.5 * mm
    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Plats/ort: {data.get('location','')}")
    y -= 8 * mm

    c.setFont("Helvetica-Bold", 11)
    c.drawString(x, y, f"Tjänst: {data.get('job_type','')}")
    y -= 5.5 * mm
    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Omfattning: {data.get('size','')}")
    y -= 5.5 * mm
    c.drawString(x, y, f"Material: {data.get('material','')}")
    y -= 8 * mm

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


def generate_offer_text(d: dict) -> str:
    api_key = get_secret("OPENAI_API_KEY")
    if (not api_key) or (OpenAI is None):
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

    client = OpenAI(api_key=api_key)
    prompt = build_prompt(d)
    resp = client.chat.completions.create(
        model=get_secret("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": "Du skriver professionella svenska offerter för bygg- och VVS-tjänster till privatkunder."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=900,
    )
    return (resp.choices[0].message.content or "").strip()


# =========================================================
# UI
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


def paywall(user: dict):
    st.markdown("## Aktivera konto")
    st.caption("För att använda Offertly behöver du en aktiv prenumeration.")
    if not stripe_setup():
        st.error("Stripe är inte konfigurerat. Lägg STRIPE_SECRET_KEY i Secrets.")
        st.stop()
    pricing_cards(show_cta=True, user=user)
    st.write("")
    st.caption("Efter Fas 3A (webhook) blir status normalt aktiv automatiskt efter betalning.")


def landing_page():
    st.markdown(f"# {APP_TITLE}")
    st.markdown(f"<div style='opacity:.75'>{APP_TAGLINE}</div>", unsafe_allow_html=True)
    st.write("")
    st.markdown("**Sälj med tydlighet.** Offertly gör pris, omfattning, material och villkor kristallklara – utan att du sitter och formaterar.")
    st.write("")
    c1, c2 = st.columns([1, 1])
    with c1:
        st.info("✅ Skapa offert snabbt\n\n✅ Snygg PDF\n\n✅ Tydlig prisuppdelning\n\n✅ AI-text som låter professionell")
    with c2:
        st.markdown("### Så funkar det")
        st.markdown(
            """
1. Fyll i jobb + pris  
2. Offerttext skapas automatiskt  
3. Ladda ner PDF  
4. Skicka till privatkund  
"""
        )
    st.write("")
    pricing_cards(show_cta=False)
    st.write("")
    st.divider()
    auth_box()


def quota_bar(user: dict):
    plan_key = (user.get("plan") or "starter").lower()
    if plan_key not in PLANS:
        plan_key = "starter"
    limit = int(PLANS[plan_key]["limit_per_month"])
    used = count_offers_current_month(user["id"])
    remaining = max(0, limit - used)
    st.caption(f"Plan: **{PLANS[plan_key]['label']}** • Använt denna månad: **{used}/{limit}** • Kvar: **{remaining}**")
    st.progress(min(1.0, used / max(1, limit)))
    return used, limit


def generator_ui(user: dict):
    st.markdown("## Offertgenerator")
    used, limit = quota_bar(user)

    left, right = st.columns([1.05, 1.25], gap="large")

    with st.sidebar:
        st.divider()
        st.markdown("### Kundens logo (valfritt)")
        st.caption("Ladda upp logo för PDF (PNG/JPG).")
        customer_logo_file = st.file_uploader(" ", type=["png", "jpg", "jpeg"], label_visibility="collapsed")
        st.session_state["customer_logo_bytes"] = customer_logo_file.read() if customer_logo_file else None

    with left:
        st.subheader("Projektdata")

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
        comment = st.text_area("Kommentar / önskemål (valfritt)", height=110)

        st.write("")
        if st.button("Generera offert", use_container_width=True):
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
            if used >= limit:
                st.error("Du har nått din månadsgräns. Uppgradera plan.")
                st.stop()

            offer_id = generate_offer_id()
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
                "offer_id": offer_id,
                "price_work": int(price_work),
                "price_material": int(price_material),
                "price_other": int(price_other),
                "price_total": int(total_price),
            }

            with st.spinner("Skapar offert…"):
                md = generate_offer_text(d)

            st.session_state["last_offer"] = {"data": d, "md": md}
            insert_offer(
                user_id=user["id"],
                offer_id=offer_id,
                created_at=int(time.time()),
                job_type=d["job_type"],
                customer_name=d["customer"],
                location=d["location"],
                total_price=int(total_price),
                md=md,
            )
            st.success("✅ Offert skapad!")

    with right:
        st.subheader("Färdig offert")
        last = st.session_state.get("last_offer")
        if not last:
            st.info("Skapa en offert så dyker den upp här.")
        else:
            d = last["data"]
            md = last["md"]
            st.markdown(md)

            st.write("")
            st.markdown("### Ladda ner")
            fname_base = f"offert_{safe_filename(d['job_type'])}_{safe_filename(d['customer'])}_{d['date']}"
            customer_logo_bytes = st.session_state.get("customer_logo_bytes")
            pdf_bytes = generate_pdf_premium(md, d, customer_logo_bytes)

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
        st.markdown("### Senaste offerter")
        recent = get_recent_offers(user["id"], limit=8)
        if recent:
            st.dataframe(recent, use_container_width=True, hide_index=True)
        else:
            st.caption("Inga offerter ännu.")


def app_shell(user: dict):
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
    st.divider()
    generator_ui(user)


# =========================================================
# Layout
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

# WEBHOOK route (triggeras via ?webhook=1...)
if st.query_params.get("webhook") == "1":
    webhook_handler()

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

st.markdown(f"# {APP_TITLE}")
st.markdown(f"<div style='opacity:.75'>{APP_TAGLINE}</div>", unsafe_allow_html=True)
st.write("")
app_shell(user)






 






    





























