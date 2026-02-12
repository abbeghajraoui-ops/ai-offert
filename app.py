import os
import re
import sqlite3
import time
from typing import Optional

import streamlit as st

# Stripe
import stripe

# Password hashing
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
        "stripe_price_id_secret": "STRIPE_PRICE_ID_STARTER",
    },
    "pro": {
        "label": "Pro (populär)",
        "price_text": "499 kr/mån",
        "features": ["300 offerter/mån", "Premium-PDF", "Flera mallar (altan, badrum, VVS)", "Spara kunddata"],
        "stripe_price_id_secret": "STRIPE_PRICE_ID_PRO",
    },
    "team": {
        "label": "Team",
        "price_text": "1 199 kr/mån",
        "features": ["1 000 offerter/mån", "Flera användare", "Offert-historik", "Företagsanpassad mall"],
        "stripe_price_id_secret": "STRIPE_PRICE_ID_TEAM",
    },
}

PLAN_LIMITS = {
    "starter": 50,
    "pro": 300,
    "team": 1000,
}


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

    # Fas 1: spara "offerter skapade" för att kunna räkna per månad
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        """
    )

    conn.commit()
    conn.close()


def get_user_by_email(email: str) -> Optional[dict]:
    conn = db()
    cur = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),))
    row = cur.fetchone()
    cols = [d[0] for d in cur.description]  # type: ignore
    conn.close()
    if not row:
        return None
    return dict(zip(cols, row))


def get_user_by_id(user_id: int) -> Optional[dict]:
    conn = db()
    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    cols = [d[0] for d in cur.description]  # type: ignore
    conn.close()
    if not row:
        return None
    return dict(zip(cols, row))


def create_user(email: str, password: str) -> Optional[int]:
    email = email.lower().strip()
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    conn = db()
    try:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at, plan) VALUES (?,?,?,?)",
            (email, password_hash, int(time.time()), "starter"),
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


# =============================
# Offer-counting (Fas 1)
# =============================
def start_of_current_month_epoch() -> int:
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))


def count_offers_this_month(user_id: int) -> int:
    conn = db()
    cur = conn.execute(
        """
        SELECT COUNT(*)
        FROM offers
        WHERE user_id = ?
          AND created_at >= ?
        """,
        (user_id, start_of_current_month_epoch()),
    )
    n = cur.fetchone()[0]
    conn.close()
    return int(n)


def record_offer(user_id: int):
    conn = db()
    conn.execute(
        "INSERT INTO offers (user_id, created_at) VALUES (?, ?)",
        (user_id, int(time.time())),
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
    st.session_state.pop("user_id", None)


def current_user() -> Optional[dict]:
    uid = st.session_state.get("user_id")
    if not uid:
        return None
    return get_user_by_id(int(uid))


def has_active_subscription(u: dict) -> bool:
    # Stripe status: active, trialing, past_due, canceled, unpaid, incomplete...
    status = (u.get("stripe_subscription_status") or "").lower()
    return status in ("active", "trialing")


# =============================
# Secrets / Stripe setup
# =============================
def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        if name in st.secrets:
            v = str(st.secrets[name]).strip()
            return v or default
    except Exception:
        pass
    return os.getenv(name) or default


def stripe_setup() -> bool:
    secret = get_secret("STRIPE_SECRET_KEY")
    if not secret:
        return False
    stripe.api_key = secret
    return True


def app_base_url() -> str:
    # Lägg i Streamlit Secrets: APP_BASE_URL="https://din-app.streamlit.app"
    url = get_secret("APP_BASE_URL") or ""
    return url.rstrip("/")


# =============================
# Stripe flows (Checkout + Sync + Portal)
# =============================
def get_price_id(plan_key: str) -> Optional[str]:
    secret_name = PLANS[plan_key]["stripe_price_id_secret"]
    return get_secret(secret_name)


def create_checkout_session(user: dict, plan_key: str) -> str:
    if not stripe_setup():
        raise RuntimeError("Stripe är inte konfigurerat (STRIPE_SECRET_KEY saknas).")

    base = app_base_url()
    if not base:
        raise RuntimeError('APP_BASE_URL saknas i Secrets. Ex: APP_BASE_URL="https://din-app.streamlit.app"')

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


def open_billing_portal(user: dict):
    if not stripe_setup():
        st.error("Stripe är inte konfigurerat (STRIPE_SECRET_KEY saknas).")
        return
    if not user.get("stripe_customer_id"):
        st.info("Ingen Stripe-kund kopplad ännu. Betala en plan först.")
        return

    try:
        portal = stripe.billing_portal.Session.create(
            customer=user["stripe_customer_id"],
            return_url=app_base_url() or None,
        )
        st.link_button("Öppna kundportal", portal.url, use_container_width=True)
        st.caption("Här kan kunden byta plan, avsluta, och uppdatera kort.")
    except Exception as e:
        st.error(f"Kunde inte öppna kundportal: {e}")


# =============================
# UI Components
# =============================
def pricing_cards():
    st.markdown("### Prisplaner")
    cols = st.columns(3)
    keys = list(PLANS.keys())
    for i, key in enumerate(keys):
        plan = PLANS[key]
        with cols[i]:
            st.markdown(
                f"""
                <div style="
                    border: 1px solid rgba(0,0,0,0.10);
                    border-radius: 16px;
                    padding: 16px;
                    background: rgba(255,255,255,0.75);
                    min-height: 220px;
                ">
                  <div style="font-weight:700; font-size:16px;">{plan['label']}</div>
                  <div style="font-size:26px; font-weight:800; margin-top:6px;">{plan['price_text']}</div>
                  <div style="margin-top:10px; opacity:0.85;">
                    <ul style="padding-left: 18px; margin: 0;">
                      {''.join([f"<li>{x}</li>" for x in plan["features"]])}
                    </ul>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def auth_box():
    st.markdown("## Logga in")
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
    pricing_cards()

    st.write("")
    c1, c2, c3 = st.columns(3)
    for plan_key, col in [("starter", c1), ("pro", c2), ("team", c3)]:
        with col:
            if st.button(f"Välj {PLANS[plan_key]['label']}", use_container_width=True):
                try:
                    url = create_checkout_session(user, plan_key)
                    st.link_button("Fortsätt till betalning", url, use_container_width=True)
                    st.caption("Om knappen inte öppnar: kopiera länken nedan och öppna i ny flik.")
                    st.code(url)
                except Exception as e:
                    st.error(str(e))

    st.write("")
    if st.button("🔄 Jag har redan betalat – uppdatera status", use_container_width=True):
        sync_subscription_from_stripe(user)
        st.rerun()

    st.write("")
    with st.expander("Hantera prenumeration"):
        open_billing_portal(user)


def landing_page():
    st.markdown(f"# {APP_TITLE}")
    st.markdown(f"<div style='opacity:.75'>{APP_TAGLINE}</div>", unsafe_allow_html=True)
    st.write("")

    st.markdown("### Målgrupp")
    chips = ["Byggfirmor", "Snickare", "VVS-firmor", "Plattsättare", "Elektriker", "Målare"]
    st.write(" ".join([f"`{c}`" for c in chips]))

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
    pricing_cards()

    st.write("")
    st.markdown("### Så funkar det")
    st.markdown(
        """
1. Fyll i jobb + pris  
2. Offerttext skapas automatiskt  
3. Ladda ner PDF  
4. Skicka till privatkund  
"""
    )

    st.divider()
    st.markdown("## Logga in / Skapa konto")
    auth_box()


def main_app_ui(user: dict):
    st.markdown(f"# {APP_TITLE}")
    st.markdown(f"<div style='opacity:.75'>{APP_TAGLINE}</div>", unsafe_allow_html=True)
    st.write("")

    # Billing portal
    with st.expander("Hantera prenumeration"):
        open_billing_portal(user)

    st.divider()

    st.markdown("## Offertgenerator (Fas 1)")
    st.info("Detta är en demo-koppling. I Fas 2 kopplar vi in din riktiga offertgenerator (formulär + PDF).")

    # Plan limit status
    plan = (user.get("plan") or "starter").lower()
    limit = PLAN_LIMITS.get(plan, 50)
    used = count_offers_this_month(user["id"])
    st.caption(f"Plan: **{plan}** • Du har använt **{used} / {limit}** offerter denna månad.")

    # Dummy inputs (ersätt med din riktiga generator)
    company = st.text_input("Företagsnamn", value="")
    customer = st.text_input("Kundens namn", value="")
    desc = st.text_area("Beskrivning", value="")

    if used >= limit:
        st.error("Du har nått gränsen för din plan. Uppgradera för fler offerter.")
        st.button("Generera offert (AI)", use_container_width=True, disabled=True)
        return

    if st.button("Generera offert (AI)", use_container_width=True):
        if not company.strip() or not customer.strip() or not desc.strip():
            st.error("Fyll i Företagsnamn, Kundens namn och Beskrivning.")
        else:
            # Fas 1: vi räknar att en offert skapats
            record_offer(user["id"])
            st.success("✅ Offert skapad (demo). Din riktiga generator kopplas in i Fas 2.")
            st.rerun()


# =============================
# Page layout + Sidebar
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
        st.caption(f"Plan: **{u.get('plan') or '-'}**")
        st.caption(f"Status: **{u.get('stripe_subscription_status') or '-'}**")
        if st.button("Logga ut", use_container_width=True):
            logout_user()
            st.rerun()
    else:
        st.caption("Inte inloggad")

    st.divider()
    ok_stripe = bool(get_secret("STRIPE_SECRET_KEY"))
    ok_base = bool(app_base_url())
    st.caption("Stripe: " + ("✅ OK" if ok_stripe else "⚠️ saknas STRIPE_SECRET_KEY"))
    st.caption("APP_BASE_URL: " + ("✅ OK" if ok_base else "⚠️ saknas APP_BASE_URL"))


# =============================
# Flow
# =============================
user = current_user()

# Om inte inloggad -> visa landningssida + auth
if not user:
    landing_page()
    st.stop()

# Stripe callback efter betalning
handle_stripe_success_callback(user)

# Sync status när man är inloggad (utan webhooks)
sync_subscription_from_stripe(user)
user = current_user()  # hämta igen efter sync

# Paywall om inte aktiv
if not has_active_subscription(user):
    paywall(user)
    st.stop()

# Aktiv prenumeration -> app
main_app_ui(user)




 






    



























