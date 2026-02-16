import os
import json
import time
import urllib.request
import urllib.error

import streamlit as st

# ----------------------------
# Config / Secrets
# ----------------------------
APP_TITLE = "Offertly – offertmotor för bygg & VVS"

def sget(key: str, default=""):
    # Streamlit Cloud secrets -> st.secrets
    try:
        return st.secrets.get(key, default)
    except Exception:
        return os.environ.get(key, default)

STRIPE_PRICE_ID_STARTER = sget("STRIPE_PRICE_ID_STARTER")
STRIPE_PRICE_ID_PRO = sget("STRIPE_PRICE_ID_PRO")
STRIPE_PRICE_ID_TEAM = sget("STRIPE_PRICE_ID_TEAM")

BACKEND_BASE_URL = (sget("BACKEND_BASE_URL") or "").rstrip("/")
APP_WEBHOOK_TOKEN = sget("APP_WEBHOOK_TOKEN")

OPENAI_API_KEY = sget("OPENAI_API_KEY", "")  # valfritt

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
    req.add_header("Authorization", f"Bearer {APP_WEBHOOK_TOKEN}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))

def backend_post(path: str, payload: dict):
    if not BACKEND_BASE_URL:
        raise RuntimeError("BACKEND_BASE_URL saknas i secrets")

    url = BACKEND_BASE_URL + path
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {APP_WEBHOOK_TOKEN}")

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
# UI
# ----------------------------
st.set_page_config(page_title="Offertly", layout="wide")
st.title(APP_TITLE)
st.caption("Välj paket, betala och skapa offerter (PDF).")

# Sidebar (login + debug)
with st.sidebar:
    st.header("Inloggning")
    email = st.text_input("Email", placeholder="din@email.se").strip().lower()

    st.divider()
    st.subheader("Status (debug)")

    stripe_ok = all([STRIPE_PRICE_ID_STARTER, STRIPE_PRICE_ID_PRO, STRIPE_PRICE_ID_TEAM])
    st.write("Stripe:", "✅" if stripe_ok else "❌")

    st.write("BACKEND_BASE_URL:", "✅" if BACKEND_BASE_URL else "❌")
    st.write("APP_WEBHOOK_TOKEN:", "✅" if APP_WEBHOOK_TOKEN else "❌")
    st.write("Price IDs:", "✅" if stripe_ok else "❌")

    st.divider()
    if st.button("Logga ut"):
        st.session_state.clear()
        st.rerun()

# Logo (om fil finns)
try:
    if os.path.exists("logo.png"):
        st.sidebar.image("logo.png", width=160)
except Exception:
    pass

# Require email to proceed
if not email:
    st.info("Skriv din email för att fortsätta.")
    st.stop()

# Check subscription status from backend
if not APP_WEBHOOK_TOKEN:
    st.error("APP_WEBHOOK_TOKEN saknas i Streamlit secrets.")
    st.stop()

ok, sub_resp = ok_or_err(backend_get, "/api/subscription", {"email": email})
if not ok:
    st.error(f"Backend error: {sub_resp}")
    st.stop()

active = bool(sub_resp.get("active"))

# ----------------------------
# Plans / Checkout
# ----------------------------
def go_checkout(price_id: str, plan_name: str):
    # success/cancel tillbaka till samma sida
    current_url = st.get_option("browser.serverAddress")  # kan vara None på cloud
    # robust: använd st.experimental_get_query_params + known base
    # enklast: hardcode din app-url i secrets om du vill, men vi kör "relative safe":
    app_url = sget("APP_BASE_URL", "").rstrip("/")
    if not app_url:
        # fallback: använd nuvarande sida (brukar funka på Streamlit Cloud via browser)
        app_url = st.request.url if hasattr(st, "request") else ""

    success_url = f"{app_url}?success=1"
    cancel_url = f"{app_url}?cancel=1"

    payload = {
        "email": email,
        "price_id": price_id,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    ok2, resp2 = ok_or_err(backend_post, "/api/create-checkout-session", payload)
    if not ok2:
        st.error(f"Kunde inte skapa checkout: {resp2}")
        return

    url = resp2.get("url")
    if not url:
        st.error("Ingen checkout-URL returnerades.")
        return

    st.success(f"Skickar dig till Stripe Checkout för {plan_name}…")
    st.link_button("Öppna Stripe Checkout", url)

# If not active -> show pricing
if not active:
    st.warning("Din plan är inte aktiv ännu. Välj ett paket för att fortsätta.")
    cols = st.columns(3)

    with cols[0]:
        st.subheader("Starter")
        st.write("För små firmor")
        st.write("✅ Offertgenerator")
        st.write("✅ PDF")
        if st.button("Välj Starter", use_container_width=True):
            go_checkout(STRIPE_PRICE_ID_STARTER, "Starter")

    with cols[1]:
        st.subheader("Pro")
        st.write("För växande firmor")
        st.write("✅ Allt i Starter")
        st.write("✅ Mer kapacitet")
        if st.button("Välj Pro", use_container_width=True):
            go_checkout(STRIPE_PRICE_ID_PRO, "Pro")

    with cols[2]:
        st.subheader("Team")
        st.write("För team")
        st.write("✅ Allt i Pro")
        st.write("✅ Flera användare")
        if st.button("Välj Team", use_container_width=True):
            go_checkout(STRIPE_PRICE_ID_TEAM, "Team")

    st.info("När betalningen är klar kan det ta några sekunder innan webbhooken uppdaterar din plan. Uppdatera sidan.")
    st.stop()

# ----------------------------
# Offertgenerator (låst bakom aktiv plan)
# ----------------------------
st.success("Plan aktiv ✅ Du kan skapa offerter.")

st.header("Offertgenerator")

colA, colB = st.columns([2, 1], gap="large")
with colA:
    company = st.text_input("Företagsnamn", value="")
    customer = st.text_input("Kundens namn", value="")
    desc = st.text_area("Beskrivning", height=140, placeholder="t.ex. totalrenovering badrum, 6 kvm...")

    if st.button("Generera offert (AI)", type="primary", use_container_width=True):
        if not (company and customer and desc):
            st.error("Fyll i företagsnamn, kundnamn och beskrivning.")
        else:
            # Här kan du koppla på din befintliga AI/PDF-logik.
            # Jag gör en stabil fallback-text så det aldrig kraschar.
            offer_text = f"""OFFERT

Företag: {company}
Kund: {customer}

Beskrivning:
{desc}

Pris: (AI/beräkning kan kopplas in här)
Villkor: 30 dagar betalning
"""
            st.text_area("Genererad offert (utkast)", offer_text, height=260)

with colB:
    st.subheader("Tips")
    st.write("• Om du vill ha din tidigare PDF-funktion, säg till så bygger vi in den här igen.")
    st.write("• Om din AI del ska använda OpenAI: lägg `OPENAI_API_KEY` i Streamlit secrets.")







