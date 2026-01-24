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

# ================== KONSTANTY ==================
POSITIONS = [
    "Veliteľ", "CCTV", "Brány", "Sklad2", "Sklad3",
    "Turniket2", "Turniket3", "Plombovac2", "Plombovac3"
]
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 15.25
VELITEL_DOUBLE = 16.25
SWAP_WINDOW_MINUTES = 30

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

# ================== FUNKCIE NA EXTRA HODINY ==================
def split_shift_by_day(pr, od, max_hours):
    if pr is None or od is None or pd.isna(pr) or pd.isna(od):
        return 0.0, 0.0
    total_hours = (od - pr).total_seconds() / 3600
    main_hours = min(total_hours, max_hours)
    extra_hours = max(total_hours - max_hours, 0.0)
    return round(main_hours, 2), round(extra_hours, 2)

def classify_pair_with_extra(pr, od, position):
    if pd.isna(pr) or pd.isna(od):
        return 0.0, 0.0
    if position.lower().startswith("vel"):
        return split_shift_by_day(pr, od, VELITEL_DOUBLE)
    elif position.lower() in ("brány", "sklad2", "sklad3"):
        return split_shift_by_day(pr, od, DOUBLE_SHIFT_HOURS)
    else:
        return split_shift_by_day(pr, od, SHIFT_HOURS)

def summarize_position_day_with_extra(pos_day_df: pd.DataFrame, position):
    morning = {"hours": 0.0, "extra_hours": 0.0, "detail": None}
    afternoon = {"hours": 0.0, "extra_hours": 0.0, "detail": None}
    details = []

    if pos_day_df.empty:
        return morning, afternoon, details

    pairs = get_user_pairs(pos_day_df)
    for user, pair in pairs.items():
        main_h, extra_h = classify_pair_with_extra(pair["pr"], pair["od"], position)
        morning["hours"] += main_h / 2
        afternoon["hours"] += main_h / 2
        morning["extra_hours"] += extra_h / 2
        afternoon["extra_hours"] += extra_h / 2
        details.append(f"{user}: Hlavná {main_h}h, EXTRA {extra_h}h, PR {pair['pr']}, OD {pair['od']}")

    morning["hours"] = round(morning["hours"], 2)
    afternoon["hours"] = round(afternoon["hours"], 2)
    morning["extra_hours"] = round(morning["extra_hours"], 2)
    afternoon["extra_hours"] = round(afternoon["extra_hours"], 2)

    return morning, afternoon, details

def summarize_day_with_extra(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, details = summarize_position_day_with_extra(pos_df, pos)
        total = morning["hours"] + afternoon["hours"]
        total_extra = morning["extra_hours"] + afternoon["extra_hours"]
        results[pos] = {
            "morning": morning,
            "afternoon": afternoon,
            "details": details,
            "total_hours": round(total, 2),
            "total_extra": round(total_extra, 2)
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
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začína pondelkom):", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday, time(0, 0)))
end_dt = tz.localize(datetime.combine(monday + timedelta(days=7), time(0, 0)))
df_week = load_attendance(start_dt, end_dt)

default_day = today if monday <= today <= monday + timedelta(days=6) else monday
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=default_day,
                                     min_value=monday, max_value=monday + timedelta(days=6))
df_day = df_week[df_week["date"] == selected_day] if not df_week.empty else pd.DataFrame()
summary = summarize_day_with_extra(df_day, selected_day) if not df_week.empty else {}

# ================== Denný prehľad ==================
st.header(f"✅ Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
cols = st.columns(3)
day_details_rows = []

for i, pos in enumerate(POSITIONS):
    col = cols[i % 3]
    info = summary.get(pos, {"morning":{"hours":0,"extra_hours":0}, "afternoon":{"hours":0,"extra_hours":0}, "details":[]})
    m = info["morning"]
    p = info["afternoon"]

    col.markdown(f"### **{pos}**")
    col.markdown(f"**Hlavná ráno:** {m['hours']} h, **EXTRA:** {m['extra_hours']} h")
    col.markdown(f"**Hlavná poob:** {p['hours']} h, **EXTRA:** {p['extra_hours']} h")

    for d in info["details"]:
        col.text(d)

    day_details_rows.append({
        "position": pos,
        "morning_hours": m['hours'],
        "morning_extra": m['extra_hours'],
        "afternoon_hours": p['hours'],
        "afternoon_extra": p['extra_hours'],
        "total_hours": info['total_hours'],
        "total_extra": info['total_extra']
    })

# ================== Týždenný prehľad ==================
st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} – {(monday + timedelta(days=6)).strftime('%d.%m.%Y')})")
days = [monday + timedelta(days=i) for i in range(7)]
cols_matrix = [d.strftime("%a %d.%m") for d in days]
matrix = pd.DataFrame(index=POSITIONS, columns=cols_matrix)

for d in days:
    df_d = df_week[df_week["date"] == d] if not df_week.empty else pd.DataFrame()
    summ = summarize_day_with_extra(df_d, d)
    for pos in POSITIONS:
        matrix.at[pos, d.strftime("%a %d.%m")] = summ[pos]["total_hours"] if pos in summ else 0

matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x, (int,float)) else 0 for x in row), axis=1)
st.dataframe(matrix.fillna("—"), use_container_width=True)
