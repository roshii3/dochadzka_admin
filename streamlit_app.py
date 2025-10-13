import streamlit as st
import pandas as pd
from datetime import datetime, date, timedelta
import pytz
from supabase import create_client

# ---------- CONFIG ----------
st.set_page_config(page_title="Veliteľ - Dochádzka", layout="wide")
hide_st_style = """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    </style>
"""
st.markdown(hide_st_style, unsafe_allow_html=True)

# ---------- DB ----------
DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
VELITEL_PASS = st.secrets["velitel_password"]
databaze = create_client(DATABAZA_URL, DATABAZA_KEY)
tz = pytz.timezone("Europe/Bratislava")

POSITIONS = ["Veliteľ","CCTV","Brány","Sklad2","Sklad3","Turniket2","Turniket3","Plombovac2","Plombovac3"]

# ---------- LOGIN ----------
if "velitel_logged" not in st.session_state:
    st.session_state.velitel_logged = False

if not st.session_state.velitel_logged:
    password = st.text_input("Zadaj heslo pre prístup", type="password")
    if st.button("Prihlásiť"):
        if password == VELITEL_PASS:
            st.session_state.velitel_logged = True
        else:
            st.error("Nesprávne heslo.")
    st.stop()

# ---------- LOAD DATA ----------
def load_attendance(start_dt, end_dt):
    res = databaze.table("attendance").select("*")\
        .gte("timestamp", start_dt.isoformat())\
        .lt("timestamp", end_dt.isoformat()).execute()
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["timestamp"] = df["timestamp"].apply(lambda x: tz.localize(x) if pd.notna(x) and x.tzinfo is None else x)
    df["date"] = df["timestamp"].dt.date
    return df

# ---------- PAIRING ----------
def get_user_pairs(pos_day_df: pd.DataFrame):
    """Vytvorí všetky páry príchod/odchod, vrátane nesparovaných zápisov"""
    pairs = []
    if pos_day_df.empty:
        return pairs

    prichody_ts = pos_day_df[pos_day_df["action"].str.lower() == "príchod"].sort_values("timestamp")["timestamp"].tolist()
    odchody_ts = pos_day_df[pos_day_df["action"].str.lower() == "odchod"].sort_values("timestamp")["timestamp"].tolist()
    
    used_odchody = [False]*len(odchody_ts)

    # Sparovanie príchodov s nasledujúcim odchodom
    for pr in prichody_ts:
        od = None
        for i, od_ts in enumerate(odchody_ts):
            if not used_odchody[i] and od_ts > pr:
                od = od_ts
                used_odchody[i] = True
                break
        pairs.append({"pr": pr, "od": od})

    # Zostávajúce odchody bez príchodu
    for i, od_ts in enumerate(odchody_ts):
        if not used_odchody[i]:
            pairs.append({"pr": None, "od": od_ts})

    return pairs

# ---------- DISPLAY ----------
st.title("🕒 Prehľad dochádzky - Veliteľ")

today = datetime.now(tz).date()
yesterday = today - timedelta(days=1)
start_dt = tz.localize(datetime.combine(yesterday, datetime.min.time()))
end_dt = tz.localize(datetime.combine(today + timedelta(days=1), datetime.min.time()))

df = load_attendance(start_dt, end_dt)

if df.empty:
    st.warning("⚠️ Nie sú dostupné žiadne dáta pre dnešok ani včerajšok.")
else:
    for day in [yesterday, today]:
        st.subheader(day.strftime("%A %d.%m.%Y"))
        df_day = df[df["date"] == day]
        if df_day.empty:
            st.write("— žiadne záznamy —")
            continue
        for pos in POSITIONS:
            pos_df = df_day[df_day["position"] == pos]
            st.markdown(f"**{pos}**")
            if pos_df.empty:
                st.write("— žiadne záznamy —")
                continue
            pairs = get_user_pairs(pos_df)
            for p in pairs:
                pr = p["pr"].strftime("%H:%M") if p["pr"] else "—"
                od = p["od"].strftime("%H:%M") if p["od"] else "—"
                st.write(f"➡️ Príchod: {pr} | Odchod: {od}")
