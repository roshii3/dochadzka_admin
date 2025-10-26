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

# Skryť Streamlit menu
st.markdown("""
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
</style>
""", unsafe_allow_html=True)

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

    weekday = pos_day_df["timestamp"].dt.weekday.iloc[0]  # 0=pondelok, 5=sobota, 6=nedeľa
    if weekday in (5, 6):  # Sobota alebo Nedeľa
        pr_list = [p["pr"] for p in pairs.values() if pd.notna(p["pr"])]
        od_list = [p["od"] for p in pairs.values() if pd.notna(p["od"])]
        if pr_list and od_list:
            earliest_pr = min(pr_list)
            latest_od = max(od_list)
            start_time = max(earliest_pr, datetime.combine(earliest_pr.date(), time(6,0)).replace(tzinfo=earliest_pr.tzinfo))
            end_time = latest_od
            total_hours = round((end_time - start_time).total_seconds() / 3600, 2)
            detail_str = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u, p in pairs.items()])
            morning = {"status": "Obsadené", "hours": total_hours, "detail": detail_str}
            return morning, afternoon, details

    # Pondelok–Piatok logika
    for user, pair in pairs.items():
        role_m, role_p, h_m, h_p, msgs = classify_pair(pair["pr"], pair["od"], position)
        if role_m == "Ranna OK" and morning["status"] not in ("Ranna OK", "R+P OK"):
            morning = {"status": "Ranna OK", "hours": h_m, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if role_p == "Poobedna OK" and afternoon["status"] not in ("Poobedna OK", "R+P OK"):
            afternoon = {"status": "Poobedna OK", "hours": h_p, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if msgs:
            for m in msgs:
                details.append(f"{user}: {m} — pr:{pair['pr']} od:{pair['od']}")

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

    morning_hours = round(morning_hours, 2)
    afternoon_hours = round(afternoon_hours, 2)

    if morning_hours > 0:
        morning["status"] = "Čiastočná"
        morning["hours"] = morning_hours
        morning["detail"] = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u, p in pairs.items()])
    if afternoon_hours > 0:
        afternoon["status"] = "Čiastočná"
        afternoon["hours"] = afternoon_hours
        afternoon["detail"] = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u, p in pairs.items()])

    if morning_hours == 0 and afternoon_hours == 0:
        morning["status"] = "absent"
        morning["hours"] = total_hours
        morning["detail"] = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u, p in pairs.items()])

    return morning, afternoon, details

# ================== STREAMLIT UI ==================
st.title("Dochádzka - Admin Report")

# Výber dátumu
week_start = st.date_input("Vyber týždeň (pondelok)", value=date.today() - timedelta(days=date.today().weekday()))
week_end = week_start + timedelta(days=7)

st.write(f"Report od {week_start} do {week_end - timedelta(days=1)}")

# Načítanie dát
attendance_df = load_attendance(
    datetime.combine(week_start, time.min),
    datetime.combine(week_end, time.min)
)

if attendance_df.empty:
    st.warning("Žiadne záznamy dochádzky v tomto týždni.")
else:
    report_rows = []
    for pos in POSITIONS:
        for single_date in pd.date_range(week_start, week_end - timedelta(days=1)):
            day_df = attendance_df[attendance_df["date"] == single_date.date()]
            pos_df = day_df[day_df["position"] == pos]
            morning, afternoon, details = summarize_position_day(pos_df, pos)
            report_rows.append({
                "Dátum": single_date.date(),
                "Pozícia": pos,
                "Ranná stav": morning["status"],
                "Ranná hodiny": morning["hours"],
                "Poobedná stav": afternoon["status"],
                "Poobedná hodiny": afternoon["hours"],
                "Detail": morning["detail"] or afternoon["detail"]
            })

    report_df = pd.DataFrame(report_rows)
    st.dataframe(report_df)

    # Export do Excel
    def to_excel(df):
        output = BytesIO()
        writer = pd.ExcelWriter(output, engine="openpyxl")
        df.to_excel(writer, index=False, sheet_name="Dochadzka")
        writer.save()
        processed_data = output.getvalue()
        return processed_data

    excel_data = to_excel(report_df)
    st.download_button(
        label="Export do Excel",
        data=excel_data,
        file_name=f"Dochadzka_{week_start}_{week_end - timedelta(days=1)}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
