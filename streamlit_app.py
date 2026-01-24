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

# ================== NOVÁ FUNKCIA: summarize_position_day s EXTRA riadkami ==================
def summarize_position_day(pos_day_df: pd.DataFrame, position):
    """
    Pre každú pozíciu v deň:
    - zistí rannú a poobednú smenu
    - rozdelí hodiny presahu do EXTRA1/EXTRA2
    - vráti morning, afternoon, details, extra_rows
    """
    morning = {"status": "absent", "hours": 0.0, "detail": None}
    afternoon = {"status": "absent", "hours": 0.0, "detail": None}
    details = []
    extra_rows = []  # nová časť: EXTRA riadky

    if pos_day_df.empty:
        return morning, afternoon, details, extra_rows

    pairs = get_user_pairs(pos_day_df)
    merged = merge_intervals(pairs)
    if not merged:
        return morning, afternoon, details, extra_rows

    total_start = min(s[0] for s in merged)
    total_end = max(s[1] for s in merged)
    total_hours = (total_end - total_start).total_seconds() / 3600

    # určujeme štandardnú smenu podľa pozície
    if position.lower().startswith("vel"):
        standard_hours = VELITEL_DOUBLE
        extra_label = "EXTRA1"
    else:
        standard_hours = DOUBLE_SHIFT_HOURS if total_hours >= DOUBLE_SHIFT_HOURS else SHIFT_HOURS
        extra_label = "EXTRA2"

    # ak presahuje štandardnú smenu, rozdelíme
    if total_hours > standard_hours + 0.01:
        main_hours = standard_hours
        extra_hours = round(total_hours - standard_hours, 2)
    else:
        main_hours = total_hours
        extra_hours = 0.0

    # určujeme rannú a poobednú podľa časového okna
    morning_window = (datetime.combine(total_start.date(), time(6,0)).replace(tzinfo=total_start.tzinfo),
                      datetime.combine(total_start.date(), time(15,0)).replace(tzinfo=total_start.tzinfo))
    afternoon_window = (datetime.combine(total_start.date(), time(13,0)).replace(tzinfo=total_start.tzinfo),
                        datetime.combine(total_start.date(), time(22,0)).replace(tzinfo=total_start.tzinfo))

    morning_hours = 0.0
    afternoon_hours = 0.0
    for start, end in merged:
        inter_start = max(start, morning_window[0])
        inter_end = min(end, morning_window[1])
        if inter_end > inter_start:
            morning_hours += (inter_end - inter_start).total_seconds()/3600
        inter_start = max(start, afternoon_window[0])
        inter_end = min(end, afternoon_window[1])
        if inter_end > inter_start:
            afternoon_hours += (inter_end - inter_start).total_seconds()/3600

    morning["hours"] = round(morning_hours,2)
    afternoon["hours"] = round(afternoon_hours,2)
    morning["status"] = "Ranna OK" if morning_hours>0 else "absent"
    afternoon["status"] = "Poobedna OK" if afternoon_hours>0 else "absent"
    morning["detail"] = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u,p in pairs.items()])
    afternoon["detail"] = morning["detail"]

    # EXTRA riadok iba ak je presah
    if extra_hours > 0:
        extra_rows.append({
            "position": extra_label,
            "morning_status": "-",
            "morning_hours": 0.0,
            "morning_detail": "-",
            "afternoon_status": "-",
            "afternoon_hours": extra_hours,
            "afternoon_detail": f"Presah {position}",
            "total_hours": extra_hours
        })

    return morning, afternoon, details, extra_rows

# ================== summarize_day ==================
def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    extra_rows_total = []

    for pos in POSITIONS:
        pos_df = df_day[df_day["position"]==pos] if not df_day.empty else pd.DataFrame()
        m,p,details,extra_rows = summarize_position_day(pos_df,pos)
        total = m.get("hours",0)+p.get("hours",0)
        results[pos] = {
            "morning": m,
            "afternoon": p,
            "details": details,
            "total_hours": round(total,2)
        }
        if extra_rows:
            extra_rows_total.extend(extra_rows)
    return results, extra_rows_total

# ================== Funkcie save, excel, UI ostávajú pôvodné ==================
# Zvyšok skriptu (load_attendance, save_attendance, excel export, streamlit UI) ostáva tak, ako si ho mal,
# iba pri tvorbe day_details_rows a týždenného prehľadu treba pridať tieto extra riadky:
# 
# summary, extra_rows = summarize_day(df_day, selected_day)
# day_details_rows.extend(extra_rows)
# pri týždennom prehľade pre každý deň zohľadniť extra_rows a pridať do matrix, alebo samostatne pod pôvodnou pozíciou

