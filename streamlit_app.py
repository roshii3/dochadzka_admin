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
SWAP_WINDOW_MINUTES = 30
VELITEL_DOUBLE = 16.25
DOUBLE_SHIFT_HOURS = 15.25

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
    if pd.isna(pr) or pd.isna(od):
        return 0.0
    total_hours = (od - pr).total_seconds()/3600 if od>pr else 0
    return round(total_hours,2)

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    total_hours = 0.0
    extra_rows = []
    if pos_day_df.empty:
        return total_hours, extra_rows

    pairs = get_user_pairs(pos_day_df)

    for user, pair in pairs.items():
        hours = classify_pair(pair["pr"], pair["od"], position)
        total_hours += hours

        # pridanie EXTRA riadku pri presahu po 22:00–02:00
        if pd.notna(pair["od"]):
            od_hour = pair["od"].hour
            if od_hour >= 22 or od_hour < 2:
                if position.lower().startswith("vel"):
                    extra_hours = max(0.0, hours - VELITEL_DOUBLE)
                    extra_rows.append({"position":"EXTRA1","hours":round(extra_hours,2)})
                elif position.lower() in ["brány","sklad2","sklad3"]:
                    extra_hours = max(0.0, hours - DOUBLE_SHIFT_HOURS)
                    extra_rows.append({"position":"EXTRA2","hours":round(extra_hours,2)})
    return round(total_hours,2), extra_rows

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    extra_rows_all = []
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"]==pos] if not df_day.empty else pd.DataFrame()
        total_hours, extra_rows = summarize_position_day(pos_df,pos)
        results[pos] = total_hours
        extra_rows_all.extend(extra_rows)
    return results, extra_rows_all

def create_excel(matrix: pd.DataFrame) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Týždenný prehľad"

    for r in dataframe_to_rows(matrix, index=True, header=True):
        ws.append(r)

    # jednoduché formátovanie
    for col in ws.columns:
        for cell in col:
            cell.alignment = Alignment(horizontal='center', vertical='center')
            if isinstance(cell.value,(int,float)) and cell.value>0:
                cell.fill = PatternFill(start_color="DDFFDD", fill_type="solid")
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer

# ================== STREAMLIT UI ==================
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad s Excel exportom)")

today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday,time(0,0)))
end_dt = tz.localize(datetime.combine(monday+timedelta(days=7),time(0,0)))
df_week = load_attendance(start_dt,end_dt)

days = [monday + timedelta(days=i) for i in range(7)]
matrix_index = POSITIONS + ["EXTRA1","EXTRA2"]
matrix = pd.DataFrame(index=matrix_index, columns=[d.strftime("%a %d.%m") for d in days])

for d in days:
    df_d = df_week[df_week["date"]==d] if not df_week.empty else pd.DataFrame()
    summ, extra_rows = summarize_day(df_d,d)
    for pos in POSITIONS:
        matrix.at[pos,d.strftime("%a %d.%m")] = summ[pos] if summ[pos]>0 else "—"
    for extra in extra_rows:
        lbl = extra["position"]
        matrix.at[lbl,d.strftime("%a %d.%m")] = extra["hours"]

matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x,(int,float)) else 0 for x in row),axis=1)
st.dataframe(matrix.fillna("—"), use_container_width=True)

# ================== EXCEL EXPORT ==================
st.download_button(
    label="⬇️ Stiahnuť Excel",
    data=create_excel(matrix),
    file_name=f"Dochadzka_{monday.strftime('%d%m%Y')}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
