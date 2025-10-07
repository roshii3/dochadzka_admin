# streamlit_app.py
import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO
from openpyxl.styles import PatternFill
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl import Workbook
from httpx import ReadTimeout

# ========== CONFIG ==========
st.set_page_config(page_title="Admin - Dochádzka", layout="wide", initial_sidebar_state="expanded")

# hide streamlit header/menu/footer
hide_css = """
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
</style>
"""
st.markdown(hide_css, unsafe_allow_html=True)

# Secrets (Streamlit Cloud)
DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")

# Supabase client
supabase: Client = create_client(DATABAZA_URL, DATABAZA_KEY)

tz = pytz.timezone("Europe/Bratislava")

POSITIONS = ["Veliteľ","CCTV","Brány","Sklad2","Sklad3","Turniket2","Turniket3","Plombovac2","Plombovac3"]
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 15.25
VELITEL_DOUBLE = 16.25

# ================= HELPERS =================

def safe_fetch_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """Načíta záznamy medzi start_dt a end_dt s ošetrením timestamp tz-aware."""
    try:
        res = supabase.table("attendance").select("*").gte("timestamp", start_dt.isoformat()).lt("timestamp", end_dt.isoformat()).execute()
        df = pd.DataFrame(res.data)
    except ReadTimeout:
        st.error("⚠️ Timeout pri načítaní dát z databázy. Skús 'Obnoviť dáta' znova.")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"❌ Chyba pri načítaní dát z attendance: {e}")
        return pd.DataFrame()

    if df.empty:
        return df

    # parse timestamps
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")

    # konvertovať na tz-aware Europe/Bratislava
    df["timestamp"] = df["timestamp"].apply(lambda x: tz.localize(x) if x.tzinfo is None else x.astimezone(tz))

    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.time
    return df

# ================== INSERT RECORD ==================
def insert_missing_record(user_code, position, target_date, record_type, chosen_time):
    """Zapíše chýbajúci príchod/odchod do attendance s tz-aware timestamp."""
    client = supabase
    # parsovanie času
    if isinstance(chosen_time, str):
        try:
            t = datetime.strptime(chosen_time.strip(), "%H:%M").time()
            ts = tz.localize(datetime.combine(target_date, t))
        except ValueError:
            dt = pd.to_datetime(chosen_time)
            if dt.tzinfo is None:
                ts = tz.localize(dt)
            else:
                ts = dt.astimezone(tz)
    elif isinstance(chosen_time, datetime):
        ts = tz.localize(chosen_time) if chosen_time.tzinfo is None else chosen_time.astimezone(tz)
    elif isinstance(chosen_time, time):
        ts = tz.localize(datetime.combine(target_date, chosen_time))
    else:
        st.error("Neplatný formát času")
        return

    # tu pridáme +2 hodiny, aby sa zápis zobrazoval správne v DB
    ts += timedelta(hours=2)

    iso_ts = ts.isoformat()
    action = "Príchod" if str(record_type).lower().startswith("pr") else "Odchod"

    payload = {
        "user_code": str(user_code),
        "position": position,
        "action": action,
        "timestamp": iso_ts,
        "valid": False
    }

    try:
        client.table("attendance").insert(payload).execute()
        st.success(f"✅ Uložené: {user_code} — {position} — {action} @ {iso_ts}")
        st.session_state["_reload_needed"] = True
    except Exception as e:
        st.error(f"❌ Chyba pri zápise: {e}")

# ================== DAY FILTER ==================
def filter_day(df_week: pd.DataFrame, selected_day: date) -> pd.DataFrame:
    """Správny filter tz-aware timestamptz pre selected_day."""
    day_start = tz.localize(datetime.combine(selected_day, time.min))
    day_end = tz.localize(datetime.combine(selected_day, time.max))
    return df_week[(df_week["timestamp"] >= day_start) & (df_week["timestamp"] <= day_end)]

# ================== UI ==================
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad)")

# Admin login
if "admin_logged" not in st.session_state:
    st.session_state.admin_logged = False
if not st.session_state.admin_logged:
    st.sidebar.header("Admin prihlásenie")
    pw = st.sidebar.text_input("Heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw == ADMIN_PASS:
            st.session_state.admin_logged = True
            st.success("Prihlásenie úspešné — stlač 'Obnoviť dáta'.")
        else:
            st.sidebar.error("Nesprávne heslo.")
    if not st.session_state.admin_logged:
        st.stop()

# Výber týždňa
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni:", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday, time.min))
end_dt = tz.localize(datetime.combine(monday + timedelta(days=7), time.min))

# Reload
if "_reload_needed" not in st.session_state:
    st.session_state["_reload_needed"] = True
if st.sidebar.button("Obnoviť dáta"):
    st.session_state["_reload_needed"] = True

# Načítanie dát
if st.session_state.get("_reload_needed", False) or "_df_week_cache" not in st.session_state:
    df_week = safe_fetch_attendance(start_dt, end_dt)
    st.session_state["_df_week_cache"] = df_week.to_dict('records') if not df_week.empty else []
    st.session_state["_reload_needed"] = False
else:
    cached = st.session_state.get("_df_week_cache", [])
    df_week = pd.DataFrame(cached)

# Denný výber
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=today, min_value=monday, max_value=monday+timedelta(days=6))
if not df_week.empty:
    df_day = filter_day(df_week, selected_day)
else:
    df_day = pd.DataFrame()

st.write(f"📅 Denný prehľad pre {selected_day} — {len(df_day)} záznamov")
st.dataframe(df_day[["user_code","position","action","timestamp","valid"]], use_container_width=True)
