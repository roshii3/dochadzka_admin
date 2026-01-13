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
SWAP_WINDOW_MINUTES = 30  # 30 minút

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
    morning_hours = 0.0
    amazon_intervals = []

    if pos_day_df.empty:
        return {"hours": 0.0, "detail": "-"}, [{"status": "AMAZON1", "hours": 0.0}, {"status": "AMAZON2", "hours": 0.0}]

    pairs = get_user_pairs(pos_day_df)
    merged = merge_intervals(pairs)

    for start, end in merged:
        day_start = datetime.combine(start.date(), time(6,0)).replace(tzinfo=start.tzinfo)
        day_end = datetime.combine(start.date(), time(22,0)).replace(tzinfo=start.tzinfo)
        amazon_start = day_end
        amazon_end = amazon_start + timedelta(hours=4)

        inter_start = max(start, day_start)
        inter_end = min(end, day_end)
        if inter_end > inter_start:
            morning_hours += (inter_end - inter_start).total_seconds()/3600

        inter_start = max(start, amazon_start)
        inter_end = min(end, amazon_end)
        if inter_end > inter_start:
            amazon_intervals.append((inter_start, inter_end))

    morning = {"hours": round(morning_hours, 2),
               "detail": " + ".join([f"{u}: {p['pr']}–{p['od']}" for u, p in pairs.items()]) if pairs else "-"}

    amazon1_total = 0.0
    amazon2_total = 0.0
    for i, (s,e) in enumerate(amazon_intervals):
        h = round((e-s).total_seconds()/3600, 2)
        if i%2==0:
            amazon1_total += h
        else:
            amazon2_total += h

    amazon_hours = [
        {"status": "AMAZON1", "hours": round(amazon1_total,2)},
        {"status": "AMAZON2", "hours": round(amazon2_total,2)}
    ]
    return morning, amazon_hours

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"]==pos] if not df_day.empty else pd.DataFrame()
        morning, amazon = summarize_position_day(pos_df, pos)
        total = morning["hours"] + sum(a["hours"] for a in amazon)
        results[pos] = {"morning": morning, "amazon": amazon, "total_hours": round(total,2)}
    return results

def generate_weekly_matrix(df_week, monday):
    days = [monday + timedelta(days=i) for i in range(7)]
    cols_matrix = [d.strftime("%a %d.%m") for d in days]
    matrix = pd.DataFrame(index=POSITIONS, columns=cols_matrix)
    amazon1_total = [0]*7
    amazon2_total = [0]*7

    for i, d in enumerate(days):
        df_d = df_week[df_week["date"]==d] if not df_week.empty else pd.DataFrame()
        summ = summarize_day(df_d, d)
        for pos in POSITIONS:
            matrix.at[pos, cols_matrix[i]] = summ[pos]["total_hours"] if summ[pos]["total_hours"]>0 else "—"
            amazon1_total[i] += sum(a["hours"] for a in summ[pos]["amazon"][:1])
            amazon2_total[i] += sum(a["hours"] for a in summ[pos]["amazon"][1:])

    matrix.loc["AMAZON1"] = [round(h,2) if h>0 else "—" for h in amazon1_total]
    matrix.loc["AMAZON2"] = [round(h,2) if h>0 else "—" for h in amazon2_total]

    matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x,(int,float)) else 0 for x in row), axis=1)
    return matrix

# ================== Streamlit UI ==================
st.title("Týždenný prehľad dochádzky")

today = date.today()
monday = today - timedelta(days=today.weekday())
sunday = monday + timedelta(days=6)

df_week = load_attendance(datetime.combine(monday,time.min), datetime.combine(sunday,time.max))
weekly_matrix = generate_weekly_matrix(df_week, monday)

st.dataframe(weekly_matrix)

# Export do Excelu
def excel_export(matrix):
    wb = Workbook()
    ws = wb.active
    ws.title = "Týždenný prehľad"
    for r in dataframe_to_rows(matrix, index=True, header=True):
        ws.append(r)
    for col in ws.columns:
        for cell in col:
            cell.alignment = Alignment(horizontal="center", vertical="center")
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio

bio = excel_export(weekly_matrix)
st.download_button("Export do Excelu", data=bio, file_name="tyzdenny_prehlad.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
