import streamlit as st
from datetime import datetime, date, time
import pytz
from supabase import create_client

# ----------------------------
# KONFIGURÁCIA
# ----------------------------
st.set_page_config(page_title="Attendance Editor", layout="wide")

# Supabase konfigurácia (zmeň podľa seba)
SUPABASE_URL = "https://tvoja_supabase_url.supabase.co"
SUPABASE_KEY = "tvoj_supabase_key"

databaze = create_client(SUPABASE_URL, SUPABASE_KEY)
tz = pytz.timezone("Europe/Bratislava")

# ----------------------------
# FUNKCIE
# ----------------------------

def save_attendance(user_code, position, action, timestamp):
    """Uloží príchod/odchod do DB v UTC formáte"""
    timestamp_utc = timestamp.astimezone(pytz.utc)
    databaze.table("attendance").insert({
        "user_code": user_code,
        "position": position,
        "action": action,
        "timestamp": timestamp_utc.isoformat()
    }).execute()
    return True


def summarize_position_day(user_records):
    """Zistí, či chýba príchod alebo odchod"""
    result = {"arrival": {"status": "OK", "timestamp": None},
              "departure": {"status": "OK", "timestamp": None}}

    arrivals = [r for r in user_records if r["action"].lower() == "príchod"]
    departures = [r for r in user_records if r["action"].lower() == "odchod"]

    if not arrivals:
        result["arrival"]["status"] = "MISSING_ARRIVAL"
    else:
        result["arrival"]["timestamp"] = arrivals[0]["timestamp"]

    if not departures:
        result["departure"]["status"] = "MISSING_DEPARTURE"
    else:
        result["departure"]["timestamp"] = departures[-1]["timestamp"]

    return result


def load_records_for_day(selected_day):
    """Načíta záznamy pre daný deň"""
    start = datetime.combine(selected_day, time(0, 0)).astimezone(pytz.utc).isoformat()
    end = datetime.combine(selected_day, time(23, 59)).astimezone(pytz.utc).isoformat()
    data = databaze.table("attendance").select("*").gte("timestamp", start).lte("timestamp", end).execute()
    return data.data if data.data else []


# ----------------------------
# UI
# ----------------------------

st.title("🕓 Attendance Editor")
selected_day = st.date_input("Vyber deň", value=date.today())

records = load_records_for_day(selected_day)

if not records:
    st.info("Žiadne záznamy pre tento deň.")
else:
    st.write(f"Načítaných záznamov: **{len(records)}**")

# Vytvor prehľad podľa pozície
positions = sorted(list(set(r["position"] for r in records)))

for pos in positions:
    pos_records = [r for r in records if r["position"] == pos]
    summary = summarize_position_day(pos_records)
    m = summary["arrival"]
    p = summary["departure"]

    st.markdown(f"### 📍 Pozícia: {pos}")

    if m["timestamp"]:
        st.write(f"**Príchod:** {m['timestamp']}")
    else:
        st.warning("❌ Chýba príchod")

    if p["timestamp"]:
        st.write(f"**Odchod:** {p['timestamp']}")
    else:
        st.warning("❌ Chýba odchod")

    # Možnosť doplniť príchod / odchod
    for act, stat in [("Príchod", m["status"]), ("Odchod", p["status"])]:
        if "missing" in stat.lower():
            with st.expander(f"🛠 Opraviť {act} pre pozíciu {pos}"):
                user_code = st.text_input(
                    f"User code pre {act} ({pos})",
                    value="USER123456",
                    key=f"{pos}_{act}_user"
                )
                hour = st.select_slider("Hodina", options=list(range(0, 24)), key=f"{pos}_{act}_hour")
                minute = st.select_slider("Minúta", options=[0, 15, 30, 45], key=f"{pos}_{act}_minute")

                if st.button(f"💾 Uložiť {act} ({pos})", key=f"{pos}_{act}_save"):
                    ts = datetime.combine(selected_day, time(hour, minute))
                    ts = tz.localize(ts)  # lokalizuj na Bratislavu
                    save_attendance(user_code, pos, act, ts)
                    st.success(f"{act} uložený do databázy (UTC) ✅")
                    st.experimental_rerun()
