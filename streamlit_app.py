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
    if (pd.isna(pr) or pr is None) and (pd.isna(od) or od is None):
        return ("none", "none", 0.0, 0.0, msgs)
    if pd.isna(pr) or pr is None:
        msgs.append("missing_prichod")
        return ("missing_pr", "none", 0.0, 0.0, msgs)
    if pd.isna(od) or od is None:
        msgs.append("missing_odchod")
        return ("none", "missing_od", 0.0, 0.0, msgs)

    pr_t = pr.time()
    od_t = od.time()

    if position.lower().startswith("vel"):
        if pr_t <= time(7, 0) and (od_t >= time(21, 0) or od_t < time(2, 0)):
            return ("R+P OK", "R+P OK", VELITEL_DOUBLE, VELITEL_DOUBLE, msgs)

    if pr_t <= time(7, 0) and (od_t >= time(21, 0) or od_t < time(2, 0)):
        return ("R+P OK", "R+P OK", DOUBLE_SHIFT_HOURS, DOUBLE_SHIFT_HOURS, msgs)

    if pr_t <= time(7, 0) and od_t <= time(15, 0):
        return ("Ranna OK", "none", SHIFT_HOURS, 0.0, msgs)

    if pr_t >= time(13, 0) and od_t >= time(21, 0):
        return ("none", "Poobedna OK", 0.0, SHIFT_HOURS, msgs)

    msgs.append("invalid_times")
    return ("invalid", "invalid", 0.0, 0.0, msgs)

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
    morning = {"status": "absent", "hours": 0.0, "detail": None}
    afternoon = {"status": "absent", "hours": 0.0, "detail": None}
    details = []

    if pos_day_df.empty:
        return morning, afternoon, details

    pairs = get_user_pairs(pos_day_df)
    rp_user = None
    for user, pair in pairs.items():
        role_m, role_p, h_m, h_p, msgs = classify_pair(pair["pr"], pair["od"], position)
        if role_m == "R+P OK" and role_p == "R+P OK":
            rp_user = (user, pair, h_m, h_p)
            break

    if rp_user:
        user, pair, h_m, h_p = rp_user
        morning = {"status": "R+P OK", "hours": h_m, "detail": f"Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        afternoon = {"status": "R+P OK", "hours": h_p, "detail": f"Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        return morning, afternoon, details

    # fallback - zlúčenie intervalov
    merged = merge_intervals(pairs)
    total_hours = round(sum((end - start).total_seconds() / 3600 for start, end in merged), 2) if merged else 0.0

    morning_hours = 0.0
    afternoon_hours = 0.0
    for start, end in merged:
        morning_window_start = datetime.combine(start.date(), time(6,0)).replace(tzinfo=start.tzinfo)
        morning_window_end = datetime.combine(start.date(), time(15,0)).replace(tzinfo=start.tzinfo)
        afternoon_window_start = datetime.combine(start.date(), time(13,0)).replace(tzinfo=start.tzinfo)
        afternoon_window_end = datetime.combine(start.date(), time(22,0)).replace(tzinfo=start.tzinfo)

        inter_start = max(start, morning_window_start)
        inter_end = min(end, morning_window_end)
        if inter_end > inter_start:
            morning_hours += (inter_end - inter_start).total_seconds() / 3600

        inter_start = max(start, afternoon_window_start)
        inter_end = min(end, afternoon_window_end)
        if inter_end > inter_start:
            afternoon_hours += (inter_end - inter_start).total_seconds() / 3600

    morning_hours = round(morning_hours,2)
    afternoon_hours = round(afternoon_hours,2)

    if morning_hours > 0:
        morning["status"] = "Čiastočná"
        morning["hours"] = morning_hours
        morning["detail"] = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u, p in pairs.items()])
    if afternoon_hours > 0:
        afternoon["status"] = "Čiastočná"
        afternoon["hours"] = afternoon_hours
        afternoon["detail"] = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u, p in pairs.items()])

    return morning, afternoon, details

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, details = summarize_position_day(pos_df, pos)

        if morning["status"] == "R+P OK" and afternoon["status"] == "R+P OK":
            total = VELITEL_DOUBLE if pos.lower().startswith("vel") else DOUBLE_SHIFT_HOURS
        elif morning["status"] in ("Ranna OK", "R+P OK") and afternoon["status"] in ("Poobedna OK", "R+P OK"):
            total = VELITEL_DOUBLE if pos.lower().startswith("vel") else DOUBLE_SHIFT_HOURS
        else:
            total = morning.get("hours",0.0) + afternoon.get("hours",0.0)

        results[pos] = {
            "morning": morning,
            "afternoon": afternoon,
            "details": details,
            "total_hours": round(total,2)
        }
    return results

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

def export_excel(matrix_df, df_week):
    wb = Workbook()
    
    # --- Týždenný prehľad ---
    ws1 = wb.active
    ws1.title = "Tyzdenny prehlad"
    for r in dataframe_to_rows(matrix_df.fillna("—"), index=True, header=True):
        ws1.append(r)
    for cell in ws1[1]:
        cell.fill = PatternFill("solid", fgColor="FFC000")
        cell.alignment = Alignment(horizontal="center")
    for row in ws1.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(horizontal="center")
    
    # --- Denné detaily ---
    ws2 = wb.create_sheet("Denne detaily")
    if not df_week.empty:
        df_week_sorted = df_week.sort_values(["date", "position", "user_code"])
        for r in dataframe_to_rows(df_week_sorted, index=False, header=True):
            ws2.append(r)
    for cell in ws2[1]:
        cell.fill = PatternFill("solid", fgColor="92D050")
        cell.alignment = Alignment(horizontal="center")
    
    # --- Surové dáta ---
    ws3 = wb.create_sheet("Surove data")
    if not df_week.empty:
        for r in dataframe_to_rows(df_week, index=False, header=True):
            ws3.append(r)
    
    # --- Rozpis čipov (dummy) ---
    ws4 = wb.create_sheet("Rozpis cipu")
    ws4.append(["UserCode", "Position", "ChipID"])
    
    stream = BytesIO()
    wb.save(stream)
    return stream

# ================== STREAMLIT UI ==================
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad + Export)")

# --- Admin login ---
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

# --- Výber týždňa ---
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začína pondelkom):", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday, time(0,0)))
end_dt = tz.localize(datetime.combine(monday + timedelta(days=7), time(0,0)))
df_week = load_attendance(start_dt, end_dt)

# --- Denný výber ---
default_day = today if monday <= today <= monday + timedelta(days=6) else monday
selected_day = st.sidebar.date_input(
    "Denný prehľad - vyber deň",
    value=default_day,
    min_value=monday,
    max_value=monday + timedelta(days=6)
)
df_day = df_week[df_week["date"] == selected_day] if not df_week.empty else pd.DataFrame()
if not df_week.empty:
    summary_day = summarize_day(df_day, selected_day)

# ================== Týždenný prehľad so EXTRA ==================
days = [monday + timedelta(days=i) for i in range(7)]
cols_matrix = [d.strftime("%a %d.%m") for d in days]
matrix = pd.DataFrame(index=POSITIONS, columns=cols_matrix)

for d in days:
    df_d = df_week[df_week["date"] == d] if not df_week.empty else pd.DataFrame()
    summ = summarize_day(df_d, d)
    for pos in POSITIONS:
        matrix.at[pos, d.strftime("%a %d.%m")] = summ[pos]["total_hours"] if summ[pos]["total_hours"]>0 else "—"

matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x,(int,float)) else 0 for x in row), axis=1)

# --- PRIDAŤ EXTRA1/EXTRA2 pod Plombovac3 ---
extra_rows = pd.DataFrame(index=["EXTRA1", "EXTRA2"], columns=matrix.columns)
for col in matrix.columns:
    if col != "Spolu":
        val = matrix.at["Plombovac3", col]
        extra_rows.at["EXTRA1", col] = val
        extra_rows.at["EXTRA2", col] = val
extra_rows["Spolu"] = 0
matrix = pd.concat([matrix.iloc[:POSITIONS.index("Plombovac3")+1], extra_rows, matrix.iloc[POSITIONS.index("Plombovac3")+1:]])

st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} – {(monday + timedelta(days=6)).strftime('%d.%m.%Y')})")
st.dataframe(matrix.fillna("—"), use_container_width=True)

# --- Export Excel ---
if st.button("Exportovať Excel"):
    excel_stream = export_excel(matrix, df_week)
    st.download_button(
        label="Stiahnuť XLSX",
        data=excel_stream,
        file_name=f"Dochadzka_{monday.strftime('%d%m%Y')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
