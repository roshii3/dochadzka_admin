import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Alignment
from openpyxl.utils.dataframe import dataframe_to_rows

# ================== CONFIG ==================
st.set_page_config(
    page_title="Admin - Dochádzka",
    layout="wide",
    initial_sidebar_state="expanded"
)

hide_css = """
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
</style>
"""
st.markdown(hide_css, unsafe_allow_html=True)

# ================== SECRETS ==================
DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")

databaze: Client = create_client(DATABAZA_URL, DATABAZA_KEY)
tz = pytz.timezone("Europe/Bratislava")

# ================== KONŠTANTY ==================
POSITIONS = [
    "Veliteľ", "CCTV", "Brány", "Sklad2", "Sklad3",
    "Turniket2", "Turniket3", "Plombovac2", "Plombovac3"
]
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 15.25
VELITEL_DOUBLE = 16.25
SWAP_WINDOW_MINUTES = 30  # medzera medzi intervalmi pre merge

# ================== HELPERS ==================
def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    res = (
        databaze.table("attendance")
        .select("*")
        .gte("timestamp", start_dt.isoformat())
        .lt("timestamp", end_dt.isoformat())
        .execute()
    )
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["timestamp"] = df["timestamp"].apply(
        lambda x: tz.localize(x) if pd.notna(x) and x.tzinfo is None else x
    )
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.time
    return df

def get_user_pairs(pos_day_df: pd.DataFrame):
    pairs = {}
    if pos_day_df.empty:
        return pairs
    for user in pos_day_df["user_code"].unique():
        u = pos_day_df[pos_day_df["user_code"] == user]
        pr = u[u["action"].str.lower() == "príchod"]["timestamp"]
        od = u[u["action"].str.lower() == "odchod"]["timestamp"]
        pr_min = pr.min() if not pr.empty else pd.NaT
        od_max = od.max() if not od.empty else pd.NaT
        pairs[user] = {"pr": pr_min, "od": od_max, "pr_count": len(pr), "od_count": len(od)}
    return pairs

def merge_intervals(pairs):
    intervals = []
    for pair in pairs.values():
        if pd.notna(pair["pr"]) and pd.notna(pair["od"]):
            intervals.append((pair["pr"], pair["od"]))
    if not intervals:
        return []
    intervals.sort(key=lambda x: x[0])
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        gap_min = (start - last_end).total_seconds() / 60
        if gap_min <= SWAP_WINDOW_MINUTES:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    """
    Zhrnie jednu pozíciu za deň.
    Nová logika AMAZON1/2 pre smenu 22:00–02:00
    """
    morning = {"status": "absent", "hours": 0.0, "detail": None}
    afternoon = {"status": "absent", "hours": 0.0, "detail": None}
    amazon1 = {"status": "absent", "hours": 0.0, "detail": None}
    amazon2 = {"status": "absent", "hours": 0.0, "detail": None}
    details = []

    if pos_day_df.empty:
        return morning, afternoon, amazon1, amazon2, details

    pairs = get_user_pairs(pos_day_df)
    merged = merge_intervals(pairs)

    morning_hours = 0.0
    afternoon_hours = 0.0
    night_shifts = []

    for start, end in merged:
        # Ranné okno
        mor_start = datetime.combine(start.date(), time(6,0)).replace(tzinfo=start.tzinfo)
        mor_end   = datetime.combine(start.date(), time(22,0)).replace(tzinfo=start.tzinfo)  # do 22:00

        # intersect ranná/poobedná
        inter_start = max(start, mor_start)
        inter_end   = min(end, mor_end)
        if inter_end > inter_start:
            morning_hours += (inter_end - inter_start).total_seconds() / 3600

        # Nočná AMAZON (22:00–02:00)
        night_start = datetime.combine(start.date(), time(22,0)).replace(tzinfo=start.tzinfo)
        night_end   = datetime.combine(start.date() + timedelta(days=1), time(2,0)).replace(tzinfo=start.tzinfo)
        inter_start = max(start, night_start)
        inter_end   = min(end, night_end)
        if inter_end > inter_start:
            night_shifts.append((inter_start, inter_end))

    morning_hours = round(morning_hours,2)
    if morning_hours > 0:
        morning = {"status": "Prítomný", "hours": morning_hours, "detail": " + ".join([f"{u}: {p['pr']}–{p['od']}" for u,p in pairs.items()])}

    # rozdeľ nočné smeny na AMAZON1 a AMAZON2 podľa poradia odchodov
    night_shifts.sort(key=lambda x: x[1])
    if len(night_shifts) >= 1:
        h = round((night_shifts[0][1]-night_shifts[0][0]).total_seconds()/3600,2)
        amazon1 = {"status":"Nočná", "hours":h, "detail": " + ".join([f"{u}: {p['pr']}–{p['od']}" for u,p in pairs.items()])}
    if len(night_shifts) >= 2:
        h = round((night_shifts[1][1]-night_shifts[1][0]).total_seconds()/3600,2)
        amazon2 = {"status":"Nočná", "hours":h, "detail": " + ".join([f"{u}: {p['pr']}–{p['od']}" for u,p in pairs.items()])}

    return morning, afternoon, amazon1, amazon2, details

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, amazon1, amazon2, details = summarize_position_day(pos_df, pos)
        total_hours = morning.get("hours",0) + afternoon.get("hours",0) + amazon1.get("hours",0) + amazon2.get("hours",0)
        results[pos] = {
            "morning": morning,
            "afternoon": afternoon,
            "amazon1": amazon1,
            "amazon2": amazon2,
            "details": details,
            "total_hours": round(total_hours,2)
        }
    return results

# ================== STREAMLIT UI ==================
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad)")

# --- Login ---
if "admin_logged" not in st.session_state:
    st.session_state.admin_logged = False
if not st.session_state.admin_logged:
    st.sidebar.header("Admin prihlásenie")
    pw = st.sidebar.text_input("Heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw == ADMIN_PASS:
            st.session_state.admin_logged = True
            st.experimental_rerun()
        else:
            st.sidebar.error("Nesprávne heslo alebo ADMIN_PASS nie je nastavené.")
if not st.session_state.admin_logged:
    st.stop()

# --- Výber týždňa a dňa ---
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni (pondelok)", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday, time(0,0)))
end_dt   = tz.localize(datetime.combine(monday+timedelta(days=7), time(0,0)))
df_week = load_attendance(start_dt, end_dt)

# Denný výber
default_day = today if monday <= today <= monday + timedelta(days=6) else monday
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=default_day, min_value=monday, max_value=monday+timedelta(days=6))
df_day = df_week[df_week["date"]==selected_day] if not df_week.empty else pd.DataFrame()
summary = summarize_day(df_day, selected_day) if not df_week.empty else {}

# ================== Týždenný prehľad ==================
st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} – {(monday+timedelta(days=6)).strftime('%d.%m.%Y')})")
days = [monday+timedelta(days=i) for i in range(7)]
cols_matrix = [d.strftime("%a %d.%m") for d in days]
matrix = pd.DataFrame(index=POSITIONS+["AMAZON1","AMAZON2"], columns=cols_matrix)

for d in days:
    df_d = df_week[df_week["date"]==d] if not df_week.empty else pd.DataFrame()
    summ = summarize_day(df_d, d) if not df_week.empty else {}
    for pos in POSITIONS:
        matrix.at[pos, d.strftime("%a %d.%m")] = summ[pos]["total_hours"] if pos in summ else 0
        matrix.at["AMAZON1", d.strftime("%a %d.%m")] = summ[pos]["amazon1"]["hours"] if pos in summ else 0
        matrix.at["AMAZON2", d.strftime("%a %d.%m")] = summ[pos]["amazon2"]["hours"] if pos in summ else 0

matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x,(int,float)) else 0 for x in row), axis=1)
st.dataframe(matrix.fillna("—"), use_container_width=True)
