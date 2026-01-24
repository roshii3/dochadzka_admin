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

def classify_pair(pr, od, position):
    msgs = []
    if pd.isna(pr) or pd.isna(od):
        if pd.isna(pr):
            msgs.append("missing_prichod")
        if pd.isna(od):
            msgs.append("missing_odchod")
        return ("none", "none", 0.0, 0.0, msgs)
    
    pr_t = pr.time()
    od_t = od.time()

    # Veliteľ
    if position.lower().startswith("vel"):
        total_hours = (od - pr).total_seconds()/3600 if od>pr else 0
        return ("R+P OK", "R+P OK", round(total_hours,2), round(total_hours,2), msgs)
    else:
        total_hours = (od - pr).total_seconds()/3600 if od>pr else 0
        return ("OK", "OK", round(total_hours,2), round(total_hours,2), msgs)

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
        gap_min = (start - last_end).total_seconds()/60
        if gap_min <= SWAP_WINDOW_MINUTES:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start,end))
    return merged

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    morning = {"status":"absent","hours":0.0,"detail":None}
    afternoon = {"status":"absent","hours":0.0,"detail":None}
    details = []
    extra_rows = []

    if pos_day_df.empty:
        return morning, afternoon, details, extra_rows

    pairs = get_user_pairs(pos_day_df)

    # pre každý user
    for user, pair in pairs.items():
        role_m, role_p, h_m, h_p, msgs = classify_pair(pair["pr"], pair["od"], position)
        morning["hours"] += h_m
        afternoon["hours"] += h_p
        morning["detail"] = f"{user}: {pair['pr']}–{pair['od']}"
        afternoon["detail"] = f"{user}: {pair['pr']}–{pair['od']}"

        # ak končí po 22:00 alebo do 02:00, pridáme EXTRA riadok
        od_hour = pair["od"].hour if pd.notna(pair["od"]) else 0
        if od_hour >= 22 or od_hour < 2:
            if position.lower().startswith("vel"):
                extra_rows.append({
                    "position":"EXTRA1",
                    "morning_status":"EXTRA",
                    "afternoon_status":"EXTRA",
                    "total_hours": round(h_m + h_p - VELITEL_DOUBLE,2) if h_m+h_p>VELITEL_DOUBLE else 0.0
                })
            elif position.lower() in ["brány","sklad2","sklad3"]:
                extra_rows.append({
                    "position":"EXTRA2",
                    "morning_status":"EXTRA",
                    "afternoon_status":"EXTRA",
                    "total_hours": round(h_m + h_p - DOUBLE_SHIFT_HOURS,2) if h_m+h_p>DOUBLE_SHIFT_HOURS else 0.0
                })
    morning["hours"] = round(morning["hours"],2)
    afternoon["hours"] = round(afternoon["hours"],2)
    total_hours = morning["hours"] + afternoon["hours"]
    return morning, afternoon, details, extra_rows

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    extra_rows_all = []
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"]==pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, details, extra_rows = summarize_position_day(pos_df,pos)
        results[pos] = {
            "morning":morning,
            "afternoon":afternoon,
            "details":details,
            "total_hours": morning["hours"] + afternoon["hours"]
        }
        extra_rows_all.extend(extra_rows)
    return results, extra_rows_all

def save_attendance(user_code, position, action, now=None):
    user_code = user_code.strip()
    if not now:
        now = datetime.now(tz)
    ts_str = now.strftime("%Y-%m-%d %H:%M:%S.%f") + "+00"
    databaze.table("attendance").insert({
        "user_code": user_code,
        "position": position,
        "action": action,
        "timestamp": ts_str,
        "valid": True
    }).execute()
    return True

# ================== STREAMLIT UI ==================
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad)")

if "admin_logged" not in st.session_state:
    st.session_state.admin_logged=False

if not st.session_state.admin_logged:
    st.sidebar.header("Admin prihlásenie")
    pw = st.sidebar.text_input("Heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw==ADMIN_PASS:
            st.session_state.admin_logged=True
            st.experimental_rerun()
        else:
            st.sidebar.error("Nesprávne heslo")
if not st.session_state.admin_logged:
    st.stop()

today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday,time(0,0)))
end_dt = tz.localize(datetime.combine(monday+timedelta(days=7),time(0,0)))
df_week = load_attendance(start_dt,end_dt)

default_day = today if monday <= today <= monday+timedelta(days=6) else monday
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=default_day,
                                     min_value=monday,max_value=monday+timedelta(days=6))
df_day = df_week[df_week["date"]==selected_day] if not df_week.empty else pd.DataFrame()

if df_week.empty:
    st.warning("Žiadne dáta pre vybraný týždeň")
else:
    summary, extra_rows = summarize_day(df_day, selected_day)

# ================== Týždenný prehľad ==================
st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} – {(monday+timedelta(days=6)).strftime('%d.%m.%Y')})")
days = [monday + timedelta(days=i) for i in range(7)]
matrix_index = POSITIONS + ["EXTRA1","EXTRA2"]
matrix = pd.DataFrame(index=matrix_index, columns=[d.strftime("%a %d.%m") for d in days])

for d in days:
    df_d = df_week[df_week["date"]==d] if not df_week.empty else pd.DataFrame()
    summ, extra_rows = summarize_day(df_d,d)
    for pos in POSITIONS:
        matrix.at[pos,d.strftime("%a %d.%m")] = summ[pos]["total_hours"] if summ[pos]["total_hours"]>0 else "—"
    # pridať EXTRA riadky
    for extra in extra_rows:
        lbl = extra["position"]
        matrix.at[lbl,d.strftime("%a %d.%m")] = extra["total_hours"]

matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x,(int,float)) else 0 for x in row),axis=1)
st.dataframe(matrix.fillna("—"), use_container_width=True)
