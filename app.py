import os
import requests
import streamlit as st

st.set_page_config(page_title="Offertly", layout="wide")

# ====== Secrets / ENV ======
BACKEND_BASE_URL = (st.secrets.get("BACKEND_BASE_URL") or os.environ.get("BACKEND_BASE_URL") or "").rstrip("/")
APP_WEBHOOK_TOKEN = st.secrets.get("APP_WEBHOOK_TOKEN") or os.environ.get("APP_WEBHOOK_TOKEN") or ""

# Price IDs (visas som “OK” i din sidebar när de finns)
PRICE_STARTER = st.secrets.get("STRIPE_PRICE_ID_STARTER") or os.environ.get("STRIPE_PRICE_ID_STARTER") or ""
PRICE_PRO = st.secrets.get("STRIPE_PRICE_ID_PRO") or os.environ.get("STRIPE_PRICE_ID_PRO") or ""
PRICE_TEAM = st.secrets.get("STRIPE_PRICE_ID_TEAM") or os.environ.get("STRIPE_PRICE_ID_TEAM") or ""

def auth_headers():
    return {"Authorization": f"Bearer {APP_WEBHOOK_TOKEN}"}

def backend_get(path: str, params=None):
    url = f"{BACKEND_BASE_URL}{path}"
    r = requests.get(url, headers=auth_headers(), params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def backend_post(path: str, payload: dict):
    url = f"{BACKEND_BASE_URL}{path}"
    r = requests.post(url, headers=auth_headers(), json=payload, timeout=25)
    r.raise_for_status()
    return r.json()

# ====== UI ======
left, main = st.columns([0.26, 0.74], gap="large")

with left:
    st.markdown("### Inloggning")
    email = st.text_input("Email", value=st.session_state.get("email", ""))

    st.divider()

    st.markdown("#### Status (debug)")
    stripe_ok = bool(PRICE_STARTER and PRICE_PRO and PRICE_TEAM)
    st.write("Stripe:", "✅" if stripe_ok else "❌")
    st.write("BACKEND_BASE_URL:", "✅" if BACKEND_BASE_URL else "❌")
    st.write("APP_WEBHOOK_TOKEN:", "✅" if APP_WEBHOOK_TOKEN else "❌")
    st.write("Price IDs:", "✅" if stripe_ok else "❌")

    st.divider()

    if st.button("Logga ut"):
        st.session_state.clear()
        st.rerun()

with main:
    st.title("Offertly – offertmotor för bygg & VVS")
    st.caption("Välj paket, betala och skapa offerter (PDF).")

    # Require email
    if not email:
        st.info("Skriv din email för att fortsätta.")
        st.stop()

    st.session_state["email"] = email.strip().lower()

    # Basic sanity checks
    if not BACKEND_BASE_URL:
        st.error("BACKEND_BASE_URL saknas i Streamlit secrets.")
        st.stop()
    if not APP_WEBHOOK_TOKEN:
        st.error("APP_WEBHOOK_TOKEN saknas i Streamlit secrets.")
        st.stop()

    # Fetch status + prices
    try:
        status = backend_get("/api/status", params={"email": st.session_state["email"]})
        prices = backend_get("/api/prices")
    except requests.HTTPError as e:
        st.error(f"Backend error: {e}")
        st.stop()
    except Exception as e:
        st.error(f"Kunde inte nå backend: {e}")
        st.stop()

    plan = status.get("plan", "free")
    acct_status = status.get("status", "inactive")

    # Packages
    st.subheader("Paket")

    col1, col2, col3 = st.columns(3)

    def package_card(col, key, title, price_id, perks):
        with col:
            st.markdown(f"#### {title}")
            for p in perks:
                st.write("•", p)

            if not price_id:
                st.warning("Price ID saknas (kolla secrets).")
                return

            if plan == key and acct_status == "active":
                st.success("Aktivt abonnemang")
                return

            if st.button(f"Välj {title}", key=f"buy_{key}", use_container_width=True):
                try:
                    out = backend_post("/api/create-checkout-session", {"email": st.session_state["email"], "price_id": price_id})
                    st.success("Öppnar Stripe Checkout…")
                    st.link_button("Fortsätt till betalning", out["url"], use_container_width=True)
                except requests.HTTPError as e:
                    st.error(f"Checkout error: {e}")
                except Exception as e:
                    st.error(f"Checkout error: {e}")

    package_card(
        col1,
        "starter",
        "Starter",
        prices.get("starter", {}).get("price_id") or PRICE_STARTER,
        ["AI-offert", "PDF-export", "Grundläggande mallar"],
    )
    package_card(
        col2,
        "pro",
        "Pro",
        prices.get("pro", {}).get("price_id") or PRICE_PRO,
        ["Allt i Starter", "Fler mallar", "Offert-historik"],
    )
    package_card(
        col3,
        "team",
        "Team",
        prices.get("team", {}).get("price_id") or PRICE_TEAM,
        ["Allt i Pro", "Teamkonton", "Roller & behörigheter"],
    )

    st.divider()

    st.subheader("Offertgenerator")

    if acct_status != "active":
        st.info("Du behöver ett aktivt abonnemang för att skapa offerter.")
        st.stop()

    # Logo (optional) – visar bara om filen finns i Streamlit deploy
    if os.path.exists("logo.png"):
        st.image("logo.png", width=120)

    company = st.text_input("Företagsnamn")
    customer = st.text_input("Kundens namn")
    desc = st.text_area("Beskrivning", height=140, placeholder="Ex: totalrenovering badrum, 6 kvm…")

    if st.button("Generera offert (AI)", use_container_width=True):
        # Här kopplar vi in din AI + PDF i nästa steg.
        # Just nu ger vi en tydlig placeholder så du inte får “ingenting händer”.
        if not (company and customer and desc):
            st.warning("Fyll i företagsnamn, kundens namn och beskrivning.")
        else:
            st.success("OK – nästa steg är att koppla AI + PDF-generering här.")
            st.write({"company": company, "customer": customer, "desc": desc, "plan": plan})











 






    






































