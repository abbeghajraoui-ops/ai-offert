
import os
import re
import sqlite3
import time
import secrets
from typing import Optional

import streamlit as st
import stripe
import bcrypt


# =============================
# App Branding / Planer
# =============================
APP_NAME = "Offertly"
APP_TITLE = "Offertly – offertmotor för bygg & VVS"
APP_TAGLINE = (
    "För byggfirmor och VVS-firmor som skickar offerter till privatkunder. "
    "Skapa en proffsig offert på under 60 sekunder."
)

PLANS = {
    "starter": {
        "label": "Starter",
        "price_text": "199 kr/mån",
        "features": ["50 offerter/mån", "PDF + .md", "Kundlogo i PDF", "Standardmall"],
        "secret_candidates": ["STRIPE_PRICE_STARTER", "STRIPE_PRICE_ID_STARTER"],
    },
    "pro": {
        "label": "Pro (populär)",
        "price_text": "499 kr/mån",
        "features": ["300 offerter/mån", "Premium-PDF", "Flera mallar", "Spara kunddata"],
        "secret_candidates": ["STRIPE_PRICE_PRO", "STRIPE_PRICE_ID_PRO"],
    },
    "team": {
        "label": "Team",
        "price_text": "1 199 kr/mån",
        "features": ["1 000 offerter/mån", "Flera användare", "Offert-historik", "Företagsanpassad mall"],
        "secret_candidates": ["STRIPE_PRICE_TEAM", "STRIPE_PRICE_ID_TEAM"],
    },
}


# =============================
# Secrets helpers
# =============================
def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        if name in st.secrets:
            v = str(st.secrets[name]).strip()
            return v or default
    except Exception:
        pass
    return (os.getenv(name) or default)


def get_price_id(plan_key: str) -> Optional[str]:
    for key in PLANS[plan_key]["secret_candidates"]:
        v = get_secret(key)
        if v:
            return v
    return None


def app_base_url() -> str:
    url = get_secret("APP_BASE_URL", "")
    return url.rstrip("/")


def stripe_ready() -> bool:
    sk = get_secret("STRIPE_SECRET_KEY")
    if not sk:
        return False
    stripe.api_key = sk
    return True


# =============================
# DB (SQLite)
# =============================
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
    conn.commit()
    conn.close()


def row_to_dict(cur, row) -> dict:
    cols = [d[0] for d in cur.description]  # type: ignore
    return dict(zip(cols, row))


def get_user_by_email(email: str) -> Optional[dict]:
    conn = db()
    cur = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),))
    row = cur.fetchone()
    u = row_to_dict(cur, row) if row else None
    conn.close()
    return u


def get_user_by_id(user_id: int) -> Optional[dict]:
    conn = db()
    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    u = row_to_dict(cur, row) if row else None
    conn.close()
    return u


def create_user_with_temp_password(email: str) -> Optional[int]:
    """
    Skapar användare med ett slumpat temporärt lösenord (okänt för användaren).
    Direkt efter betalning låter vi användaren sätta sitt riktiga lösenord.
    """
    email = email.lower().strip()
    temp_pw = secrets.token_urlsafe(32)
    password_hash = bcrypt.hashpw(temp_pw.encode("utf-8"), bcrypt.gensalt())

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


def update_password(email: str, new_password: str) -> bool:
    email = email.lower().strip()
    pw_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt())
    conn = db()
    cur = conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (pw_hash, email))
    conn.commit()
    rows = cur.rowcount
    conn.close()
    return rows > 0


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


# =============================
# Auth helpers
# =============================
def valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip().lower()))


def verify_password(password: str, password_hash: bytes) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash)
    except Exception:
        return False


def login_user(user_id: int):
    st.session_state["user_id"] = user_id


def logout_user():
    for k in ["user_id", "pending_email"]:
        if k in st.session_state:
            del st.session_state[k]


def current_user() -> Optional[dict]:
    uid = st.session_state.get("user_id")
    if not uid:
        return None
    return get_user_by_id(int(uid))


def has_active_subscription(u: dict) -> bool:
    status = (u.get("stripe_subscription_status") or "").lower()
    return status in ("active", "trialing")


# =============================
# Stripe flows (Plan-först)
# =============================
def create_checkout_session(plan_key: str) -> str:
    if not stripe_ready():
        raise RuntimeError("Stripe saknar STRIPE_SECRET_KEY i Secrets.")
    base = app_base_url()
    if not base:
        raise RuntimeError("APP_BASE_URL saknas i Secrets. Ex: https://dinapp.streamlit.app")

    price_id = get_price_id(plan_key)
    if not price_id:
        raise RuntimeError(f"Saknar Stripe Price ID i Secrets för plan: {plan_key}")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{base}?success=1&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base}?canceled=1",
        allow_promotion_codes=True,
        metadata={"plan_key": plan_key, "app": APP_NAME},
    )
    return session.url  # type: ignore


def handle_stripe_return():
    """
    När Stripe skickar tillbaka användaren:
    - verifiera session
    - hämta email + subscription
    - skapa user (om inte finns)
    - spara subscription-status
    - be användaren sätta lösenord (första gången)
    """
    params = st.query_params
    if params.get("canceled"):
        st.warning("Betalningen avbröts.")
        st.query_params.clear()
        return

    if not params.get("success") or not params.get("session_id"):
        return

    if not stripe_ready():
        st.error("Stripe är inte konfigurerat (STRIPE_SECRET_KEY saknas).")
        return

    session_id = params.get("session_id")
    try:
        sess = stripe.checkout.Session.retrieve(session_id)
        customer_id = sess.get("customer")
        subscription_id = sess.get("subscription")

        # Email
        email = None
        cd = sess.get("customer_details") or {}
        if isinstance(cd, dict):
            email = cd.get("email")

        if not email:
            # fallback
            email = sess.get("customer_email")

        if not email or not valid_email(email):
            st.error("Kunde inte läsa e-post från Stripe Checkout. Kontrollera att Checkout samlar in e-post.")
            return

        # Plan (från metadata)
        plan_key = None
        md = sess.get("metadata") or {}
        if isinstance(md, dict):
            plan_key = md.get("plan_key")

        # Status
        status = None
        if subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            status = (sub.get("status") or "").lower()

        # Skapa eller hämta user
        u = get_user_by_email(email)
        if not u:
            user_id = create_user_with_temp_password(email)
            u = get_user_by_email(email) if user_id else None

        if not u:
            st.error("Kunde inte skapa konto. Prova igen.")
            return

        update_user_subscription(
            u["id"],
            customer_id=str(customer_id) if customer_id else None,
            subscription_id=str(subscription_id) if subscription_id else None,
            status=status,
            plan=plan_key,
        )

        # Be användaren sätta lösenord (snabbt och säkert)
        st.session_state["pending_email"] = email.lower().strip()

        st.query_params.clear()
        st.success("✅ Betalning klar! Sätt ett lösenord så skapas din inloggning.")
        st.rerun()

    except Exception as e:
        st.error(f"Kunde inte verifiera betalningen: {e}")


# =============================
# UI
# =============================
def pricing_cards_plan_first():
    st.markdown(f"# {APP_TITLE}")
    st.markdown(f"<div style='opacity:.75'>{APP_TAGLINE}</div>", unsafe_allow_html=True)
    st.write("")

    left, right = st.columns([1.2, 1])

    with left:
        st.markdown("### Sälj med tydlighet")
        st.markdown(
            """
✅ Skapa offert snabbt  
✅ Snygg PDF  
✅ Tydlig prisuppdelning  
✅ AI-text som låter professionell  
"""
        )
        st.markdown("### Så funkar det")
        st.markdown(
            """
1. Fyll i jobb + pris  
2. Offerttext skapas automatiskt  
3. Ladda ner PDF  
4. Skicka till privatkund  
"""
        )

    with right:
        st.markdown("### Prisplaner")
        cols = st.columns(3)
        for i, (key, p) in enumerate(PLANS.items()):
            with cols[i]:
                st.markdown(
                    f"""
                    <div style="
                        border: 1px solid rgba(0,0,0,0.10);
                        border-radius: 16px;
                        padding: 16px;
                        background: rgba(255,255,255,0.85);
                        min-height: 250px;
                    ">
                      <div style="font-weight:700; font-size:16px;">{p['label']}</div>
                      <div style="font-size:24px; font-weight:800; margin-top:6px;">{p['price_text']}</div>
                      <div style="margin-top:10px; opacity:0.9;">
                        <ul style="padding-left: 18px; margin: 0;">
                          {''.join([f"<li>{x}</li>" for x in p["features"]])}
                        </ul>
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                st.write("")
                if st.button(f"Starta {p['label']}", use_container_width=True, key=f"buy_{key}"):
                    try:
                        url = create_checkout_session(key)
                        st.link_button("Fortsätt till betalning", url, use_container_width=True)
                        st.caption("Om knappen inte öppnar: kopiera länken och öppna i ny flik.")
                        st.code(url)
                    except Exception as e:
                        st.error(str(e))


def password_set_after_payment():
    """
    Efter betalning: användaren sätter lösenord.
    När klart: loggas in direkt.
    """
    email = st.session_state.get("pending_email")
    if not email:
        return False

    st.markdown("## Skapa ditt lösenord")
    st.caption(f"Konto: **{email}**")

    pw1 = st.text_input("Lösenord (minst 8 tecken)", type="password")
    pw2 = st.text_input("Upprepa lösenord", type="password")

    if st.button("Spara lösenord & logga in", use_container_width=True):
        if len(pw1) < 8:
            st.error("Lösenordet måste vara minst 8 tecken.")
            return True
        if pw1 != pw2:
            st.error("Lösenorden matchar inte.")
            return True

        ok = update_password(email, pw1)
        if not ok:
            st.error("Kunde inte spara lösenord. Prova igen.")
            return True

        u = get_user_by_email(email)
        if not u:
            st.error("Något gick fel när kontot skulle laddas.")
            return True

        # logga in
        login_user(u["id"])
        del st.session_state["pending_email"]
        st.success("✅ Klart! Du är nu inloggad.")
        st.rerun()

    return True


def login_box():
    st.markdown("## Logga in")
    email = st.text_input("E-post")
    password = st.text_input("Lösenord", type="password")
    if st.button("Logga in", use_container_width=True):
        if not valid_email(email):
            st.error("Ange en giltig e-postadress.")
            return
        u = get_user_by_email(email)
        if not u:
            st.error("Fel e-post eller lösenord.")
            return
        if not verify_password(password, u["password_hash"]):
            st.error("Fel e-post eller lösenord.")
            return
        login_user(u["id"])
        st.rerun()


def sync_subscription_from_stripe(user: dict):
    if not stripe_ready():
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


def main_app_ui(user: dict):
    st.markdown(f"# {APP_TITLE}")
    st.markdown(f"<div style='opacity:.75'>{APP_TAGLINE}</div>", unsafe_allow_html=True)
    st.write("")
    st.markdown("## Offertgenerator")
    st.info("Här kopplar vi in din riktiga offert-generator (formulär + PDF).")

    # Demo-inputs
    st.text_input("Företagsnamn", value="")
    st.text_input("Kundens namn", value="")
    st.text_area("Beskrivning", value="")
    st.button("Generera offert (AI)", use_container_width=True)


# =============================
# Page setup + Sidebar
# =============================
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

with st.sidebar:
    st.markdown(f"## {APP_NAME}")
    if os.path.exists("logo.png"):
        st.image("logo.png", use_container_width=True)

    st.divider()
    u = current_user()
    if u:
        st.caption(f"Inloggad som: **{u['email']}**")
        plan = u.get("plan") or "-"
        status = u.get("stripe_subscription_status") or "-"
        st.caption(f"Plan: **{plan}**")
        st.caption(f"Status: **{status}**")
        if st.button("Logga ut", use_container_width=True):
            logout_user()
            st.rerun()
    else:
        st.caption("Inte inloggad")

    st.divider()
    ok_stripe = bool(get_secret("STRIPE_SECRET_KEY"))
    ok_base = bool(app_base_url())
    ok_prices = all(get_price_id(k) for k in PLANS.keys())
    st.caption("Stripe: " + ("✅ OK" if ok_stripe else "⚠️ saknas STRIPE_SECRET_KEY"))
    st.caption("APP_BASE_URL: " + ("✅ OK" if ok_base else "⚠️ saknas APP_BASE_URL"))
    st.caption("Price IDs: " + ("✅ OK" if ok_prices else "⚠️ saknas någon Price ID"))


# =============================
# Flow
# =============================
# 1) Om vi kommer tillbaka från Stripe: skapa user + spara subscription + be om lösenord
handle_stripe_return()

# 2) Om betalningen var klar och vi väntar på att användaren sätter lösenord
if password_set_after_payment():
    st.stop()

# 3) Inloggning / plan-först-sida
user = current_user()

if not user:
    # PLAN-FÖRST landningssida + login längst ner
    pricing_cards_plan_first()
    st.divider()
    login_box()
    st.stop()

# 4) Synka subscription-status
sync_subscription_from_stripe(user)
user = current_user() or user

# 5) Kräver aktiv subscription för att använda appen
if not has_active_subscription(user):
    st.warning("Du är inloggad men saknar aktiv prenumeration.")
    st.info("Välj en plan nedan för att aktivera.")
    pricing_cards_plan_first()
    st.stop()

# 6) Aktiv prenumeration -> appen
main_app_ui(user)








 






    


































