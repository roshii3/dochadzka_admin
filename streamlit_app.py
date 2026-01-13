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

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    morning = {"status": "absent", "hours": 0.0, "detail": None}
    afternoon = {"status": "absent", "hours": 0.0, "detail": None}
    amazon = []
    details = []

    if pos_day_df.empty:
        return morning, afternoon, amazon, details

    pairs = get_user_pairs(pos_day_df)

    for user, pair in pairs.items():
        pr = pair["pr"]
        od = pair["od"]
        if pd.isna(pr) or pd.isna(od):
            details.append(f"{user}: missing pr/od — pr:{pr} od:{od}")
            continue

        # Denná časť do 22:00
        day_end = datetime.combine(pr.date(), time(22,0)).replace(tzinfo=pr.tzinfo)
        day_hours = (min(od, day_end) - pr).total_seconds() / 3600
        day_hours = round(max(day_hours, 0.0), 2)

        # AMAZON po 22:00
        if od > day_end:
            amazon_hours = (od - day_end).total_seconds() / 3600
            amazon_hours = round(amazon_hours, 2)
            amazon.append({"user": user, "hours": amazon_hours, "start": day_end, "end": od})

        pr_t = pr.time()
        od_t = od.time()
        if pr_t <= time(7,0) and od_t <= time(15,0):
            morning["status"] = "Ranna OK"
            morning["hours"] = day_hours
            morning["detail"] = f"{user}: {pr}–{min(od, day_end)}"
        elif pr_t >= time(13,0) and od_t >= time(21,0):
            afternoon["status"] = "Poobedna OK"
            afternoon["hours"] = day_hours
            afternoon["detail"] = f"{user}: {pr}–{min(od, day_end)}"
        else:
            morning["status"] = "Čiastočná"
            afternoon["status"] = "Čiastočná"
            morning["hours"] = round(day_hours/2, 2)
            afternoon["hours"] = round(day_hours/2, 2)
            morning["detail"] = f"{user}: {pr}–{min(od, day_end)}"
            afternoon["detail"] = f"{user}: {pr}–{min(od, day_end)}"

    return morning, afternoon, amazon, details

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, amazon_list, details = summarize_position_day(pos_df, pos)
        total = morning.get("hours",0) + afternoon.get("hours",0) + sum(a['hours'] for a in amazon_list)
        results[pos] = {
            "morning": morning,
            "afternoon": afternoon,
            "amazon": amazon_list,
            "details": details,
            "total_hours": round(total,2)
        }
    return results

def save_attendance(user_code, position, action, now=None):
    user_code = user_code.strip()
    if not now:
        now = datetime.now(tz)
    if now.second == 0 and now.microsecond == 0:
        current = datetime.now(tz)
        now = now.replace(second=current.second, microsecond=current.microsecond)

    ts_str = now.strftime("%Y-%m-%d %H:%M:%S.%f") + "+00"
    databaze.table("attendance").insert({
        "user_code": user_code,
        "position": position,
        "action": action,
        "timestamp": ts_str,
        "valid": True
    }).execute()
    return True

# ================== EXCEL EXPORT ==================
from datetime import timedelta as _tdelta
from datetime import time as _time

def get_chip_assignments(df_raw: pd.DataFrame, monday):
    assignments = {}
    if df_raw.empty:
        return assignments

    df_raw["timestamp"] = pd.to_datetime(df_raw["timestamp"], errors="coerce")
    df_raw["date"] = df_raw["timestamp"].dt.date

    for pos in df_raw["position"].unique():
        pos_df = df_raw[df_raw["position"] == pos]
        for i in range(7):
            d = monday + _tdelta(days=i)
            day_df = pos_df[pos_df["date"] == d]
            if day_df.empty:
                continue
            pairs = get_user_pairs(day_df)
            for user, pair in pairs.items():
                if pd.isna(pair["pr"]) or pd.isna(pair["od"]):
                    continue
                pr_t = pair["pr"].time()
                od_t = pair["od"].time()

                day_end = datetime.combine(pair["pr"].date(), time(22,0)).replace(tzinfo=pair["pr"].tzinfo)

                # Ranná
                if pr_t <= time(7, 0) and od_t <= time(15, 0):
                    shift = "06:00-14_00"
                    assignments[(pos, shift, i)] = assignments.get((pos, shift, i), []) + [user]
                # Poobedná
                elif pr_t >= time(13, 0) and od_t <= time(22, 0):
                    shift = "14:00-22_00"
                    assignments[(pos, shift, i)] = assignments.get((pos, shift, i), []) + [user]

                # AMAZON po 22:00
                if od_t > time(22,0):
                    amazon_shift = "AMAZON1" if (pos, "AMAZON1", i) not in assignments else "AMAZON2"
                    assignments[(pos, amazon_shift, i)] = assignments.get((pos, amazon_shift, i), []) + [user]

    return assignments

def excel_with_colors(df_matrix, df_day_details, df_raw, monday):
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Týždenný prehľad"
    green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    yellow = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")

    for r in dataframe_to_rows(df_matrix.reset_index().rename(columns={"index": "Pozícia"}), index=False, header=True):
        ws1.append(r)
    for row in ws1.iter_rows(min_row=2, min_col=2, max_col=1 + len(df_matrix.columns), max_row=1 + len(df_matrix)):
        for cell in row:
            val = cell.value
            if isinstance(val, (int, float)):
                cell.fill = green
            elif isinstance(val, str) and val.strip().startswith("⚠"):
                cell.fill = yellow

    ws2 = wb.create_sheet("Denné - detail")
    for r in dataframe_to_rows(df_day_details, index=False, header=True):
        ws2.append(r)

    ws3 = wb.create_sheet("Surové dáta")
    for r in dataframe_to_rows(df_raw, index=False, header=True):
        ws3.append(r)

    ws4 = wb.create_sheet("Rozpis čipov")
    days = ["pondelok", "utorok", "streda", "štvrtok", "piatok", "sobota", "nedeľa"]
    header = ["position", "shift"] + days
    ws4.append(header)

    chip_map = get_chip_assignments(df_raw, monday)
    POS = sorted(df_raw["position"].unique()) if not df_raw.empty else POSITIONS
    shifts = ["06:00-14_00","14:00-22_00","AMAZON1","AMAZON2"]

    for pos in POS:
        for shift in shifts:
            row_vals = []
            for i in range(7):
                users = chip_map.get((pos, shift, i), [])
                row_vals.append(", ".join(users) if users else "")
            ws4.append([pos, shift] + row_vals)

    for col in ws4.columns:
        for cell in col:
            cell.alignment = Alignment(horizontal="center", vertical="center")

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out

# ================== STREAMLIT UI ==================
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad)")

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

today = datetime.now(tz).date()
week_ref = st.sidebar.date_input(
    "Vyber deň v týždni (týždeň začína pondelkom):",
    value=today
)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday, time(0, 0)))
end_dt = tz.localize(datetime.combine(monday + timedelta(days=7), time(0, 0)))
df_week = load_attendance(start_dt, end_dt)

default_day = today if monday <= today <= monday + timedelta(days=6) else monday
selected_day = st.sidebar.date_input(
    "Denný prehľad - vyber deň",
    value=default_day,
    min_value=monday,
    max_value=monday + timedelta(days=6)
)
df_day = df_week[df_week["date"] == selected_day] if not df_week.empty else pd.DataFrame()

if df_week.empty:
    st.warning("Rozsah nie je dostupný v DB (žiadne dáta pre vybraný týždeň).")
else:
    summary = summarize_day(df_day, selected_day)

st.header(f"✅ Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
cols = st.columns(3)
day_details_rows = []

for i, pos in enumerate(POSITIONS):
    col = cols[i % 3]
    info = summary[pos]
    m = info["morning"]
    p = info["afternoon"]
    amazons = info.get("amazon", [])

    col.markdown(f"### **{pos}**")
    col.markdown(f"**Ranná:** {m['status']} — {m['hours']} h")
    col.markdown(f"**Poobedná:** {p['status']} — {p['hours']} h")

    for idx, a in enumerate(amazons, 1):
        col.markdown(f"**AMAZON{idx}:** {a['hours']} h ({a['start'].strftime('%H:%M')}–{a['end'].strftime('%H:%M')})")

    if info["details"]:
        for d in info["details"]:
            col.error(d)

    day_details_rows.append({
        "position": pos,
        "morning_status": m['status'],
        "morning_hours": m.get('hours', 0),
        "morning_detail": m.get('detail') or "-",
        "afternoon_status": p['status'],
        "afternoon_hours": p.get('hours', 0),
        "afternoon_detail": p.get('detail') or "-",
        "amazon_hours": ", ".join([f"{a['user']}:{a['hours']}" for a in amazons]) if amazons else "-",
        "total_hours": info['total_hours']
    })

if st.button("Exportuj Excel (Farebné)"):
    df_matrix = pd.DataFrame(summary).T
    df_day_details = pd.DataFrame(day_details_rows)
    df_raw = df_week.copy()
    if "timestamp" in df_raw.columns:
        df_raw["timestamp"] = df_raw["timestamp"].apply(lambda x: x.isoformat() if pd.notna(x) else "")
    xls = excel_with_colors(df_matrix, df_day_details, df_raw, monday)
    st.download_button(
        "Stiahnuť XLSX",
        data=xls,
        file_name=f"dochadzka_{monday}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
