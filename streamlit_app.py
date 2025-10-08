# streamlit_admin_complete.py

import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO
from openpyxl.styles import PatternFill
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl import Workbook
import re

# ========== CONFIG ==========
st.set_page_config(page_title="Admin - Dochádzka", layout="wide", initial_sidebar_state="expanded")

# hide streamlit header/menu/footer
st.markdown("""
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
</style>
""", unsafe_allow_html=True)

DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")

databaze: Client = create_client(DATABAZA_URL, DATABAZA_KEY)
tz = pytz.timezone("Europe/Bratislava")

POSITIONS = ["Veliteľ","CCTV","Brány","Sklad2","Sklad3","Turniket2","Turniket3","Plombovac2","Plombovac3"]
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 15.25
VELITEL_DOUBLE = 16.25

# ----------------- HELPERS -----------------
def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    res = databaze.table("attendance").select("*")\
        .gte("timestamp", start_dt.isoformat())\
        .lt("timestamp", end_dt.isoformat()).execute()
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["timestamp"] = df["timestamp"].apply(lambda x: tz.localize(x) if pd.notna(x) and x.tzinfo is None else x)
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
        pairs[user] = {"pr": pr.min() if not pr.empty else pd.NaT,
                       "od": od.max() if not od.empty else pd.NaT}
    return pairs

def classify_pair(pr, od, position):
    msgs = []
    if (pd.isna(pr) or pr is None) and (pd.isna(od) or od is None):
        return ("none","none",0.0,0.0, msgs)
    if pd.isna(pr) or pr is None:
        msgs.append("missing_prichod")
        return ("missing_pr","none",0.0,0.0, msgs)
    if pd.isna(od) or od is None:
        msgs.append("missing_odchod")
        return ("none","missing_od",0.0,0.0, msgs)
    pr_t = pr.time(); od_t = od.time()
    # Veliteľ special
    if position.lower().startswith("vel"):
        if pr_t <= time(7,0) and (od_t >= time(21,0) or od_t < time(2,0)):
            return ("R+P OK","R+P OK", VELITEL_DOUBLE, VELITEL_DOUBLE, msgs)
    if pr_t <= time(7,0) and (od_t >= time(21,0) or od_t < time(2,0)):
        return ("R+P OK","R+P OK", DOUBLE_SHIFT_HOURS, DOUBLE_SHIFT_HOURS, msgs)
    if pr_t <= time(7,0) and od_t <= time(15,0):
        return ("Ranna OK","none", SHIFT_HOURS, 0.0, msgs)
    if pr_t >= time(13,0) and od_t >= time(21,0):
        return ("none","Poobedna OK", 0.0, SHIFT_HOURS, msgs)
    msgs.append("invalid_times")
    return ("invalid","invalid",0.0,0.0, msgs)

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    morning = {"status":"absent","hours":0.0,"detail":None}
    afternoon = {"status":"absent","hours":0.0,"detail":None}
    details = []
    if pos_day_df.empty:
        return morning, afternoon, details
    pairs = get_user_pairs(pos_day_df)
    for user, pair in pairs.items():
        role_m, role_p, h_m, h_p, msgs = classify_pair(pair["pr"], pair["od"], position)
        if role_m == "R+P OK" and role_p == "R+P OK":
            morning = {"status":"R+P OK", "hours": h_m, "detail": f"Príchod: {pair['pr']}, Odchod: {pair['od']}"}
            afternoon = {"status":"R+P OK", "hours": h_p, "detail": f"Príchod: {pair['pr']}, Odchod: {pair['od']}"}
            return morning, afternoon, details
    for user, pair in pairs.items():
        role_m, role_p, h_m, h_p, msgs = classify_pair(pair["pr"], pair["od"], position)
        if role_m == "Ranna OK":
            morning = {"status":"Ranna OK", "hours": h_m, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if role_p == "Poobedna OK":
            afternoon = {"status":"Poobedna OK", "hours": h_p, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        for m in msgs:
            details.append(f"{user}: {m} — pr:{pair['pr']} od:{pair['od']}")
    return morning, afternoon, details

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, details = summarize_position_day(pos_df, pos)
        if morning["status"] == "R+P OK" and afternoon["status"] == "R+P OK":
            total = VELITEL_DOUBLE if pos.lower().startswith("vel") else DOUBLE_SHIFT_HOURS
        elif morning["status"] in ("Ranna OK","R+P OK") and afternoon["status"] in ("Poobedna OK","R+P OK"):
            total = VELITEL_DOUBLE if pos.lower().startswith("vel") else DOUBLE_SHIFT_HOURS
        else:
            total = morning.get("hours",0.0) + afternoon.get("hours",0.0)
        results[pos] = {"morning": morning, "afternoon": afternoon, "details": details, "total_hours": total}
    return results

def summarize_week_matrix(df_week: pd.DataFrame, monday: date):
    days = [monday + timedelta(days=i) for i in range(7)]
    cols = [d.strftime("%a %d.%m") for d in days]
    matrix = pd.DataFrame(index=POSITIONS, columns=cols)
    for d in days:
        df_d = df_week[df_week["date"] == d]
        summ = summarize_day(df_d, d)
        for pos in POSITIONS:
            matrix.at[pos, d.strftime("%a %d.%m")] = summ[pos]["total_hours"] if summ[pos]["total_hours"]>0 else "—"
    matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x,(int,float)) else 0 for x in row), axis=1)
    return matrix

# ----------------- SAVE ATTENDANCE -----------------
def is_valid_code(code: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{10}", code))

def save_attendance(user_code, position, action, selected_time=None):
    user_code = user_code.strip()
    if not is_valid_code(user_code):
        st.warning("⚠️ Neplatné číslo čipu!")
        return False
    now = datetime.now(tz)
    if selected_time:
        now = tz.localize(datetime.combine(now.date(), selected_time))
    is_valid = True
    databaze.table("attendance").insert({
        "user_code": user_code,
        "position": position,
        "action": action,
        "timestamp": now.isoformat(),
        "valid": is_valid
    }).execute()
    return True

# ----------------- UI -----------------
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad)")

if "admin_logged" not in st.session_state:
    st.session_state.admin_logged = False

if not st.session_state.admin_logged:
    st.sidebar.header("Admin prihlásenie")
    pw = st.sidebar.text_input("Heslo", type="password")
    if st.sidebar.button("Prihlásiť") and pw == ADMIN_PASS:
        st.session_state.admin_logged = True
        st.experimental_rerun()
    if not st.session_state.admin_logged:
        st.stop()

# Week selection
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni:", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday, time(0,0)))
end_dt = tz.localize(datetime.combine(monday+timedelta(days=7), time(0,0)))
df_week = load_attendance(start_dt, end_dt)

selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=today, min_value=monday, max_value=monday+timedelta(days=6))
df_day = df_week[df_week["date"] == selected_day]

if df_week.empty:
    st.warning("Rozsah nie je dostupný v DB (žiadne dáta pre vybraný týždeň).")
else:
    summary = summarize_day(df_day, selected_day)
    st.header(f"✅ Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
    cols = st.columns(3)
    day_details_rows = []

    time_choices = [time(h,0) for h in range(6,23,2)]

    for i, pos in enumerate(POSITIONS):
        col = cols[i % 3]
        info = summary[pos]
        m = info["morning"]
        p = info["afternoon"]
        col.markdown(f"### **{pos}**")
        col.markdown(f"**Ranná:** {m['status']} — {m.get('hours',0)} h")
        col.markdown(f"**Poobedná:** {p['status']} — {p.get('hours',0)} h")
        if info["details"]:
            for d in info["details"]:
                col.error(d)

        # ----------------- OPRAVA CHÝBAJÚCEHO -----------------
        for d in info["details"]:
            if "missing" in d:
                missing_action = "Príchod" if "pr" in d else "Odchod"
                user_code_default = d.split(":")[0]
                col.markdown(f"#### Opraviť chýbajúci záznam ({missing_action})")
                selected_time = col.selectbox(f"Vyber čas pre {missing_action}", time_choices, key=f"time_{pos}")
                user_code_input = col.text_input("User code:", value=user_code_default, key=f"user_{pos}")
                if col.button(f"Uložiť opravu ({pos})", key=f"save_{pos}"):
                    success = save_attendance(user_code_input, pos, missing_action, selected_time)
                    if success:
                        st.success("✅ Záznam uložený")
                        st.experimental_rerun()

    # ----------------- TÝŽDENNÁ TABUĽKA -----------------
    st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} – {(monday+timedelta(days=6)).strftime('%d.%m.%Y')})")
    matrix = summarize_week_matrix(df_week, monday)
    st.dataframe(matrix.fillna("—"), use_container_width=True)

    # ----------------- EXPORT EXCEL -----------------
    if st.button("Exportuj Excel"):
        wb = Workbook()
        ws1 = wb.active
        ws1.title = "Týždenný prehľad"
        for r in dataframe_to_rows(matrix.reset_index().rename(columns={"index":"position"}), index=False, header=True):
            ws1.append(r)

        # sheet 2: denný detail
        ws2 = wb.create_sheet("Denné - detail")
        df_day_details = pd.DataFrame(day_details_rows)
        for r in dataframe_to_rows(df_day_details, index=False, header=True):
            ws2.append(r)

        # sheet 3: surové dáta
        ws3 = wb.create_sheet("Surové dáta")
        df_raw = df_week.copy()
        df_raw["timestamp"] = df_raw["timestamp"].apply(lambda x: x.isoformat() if pd.notna(x) else "")
        for r in dataframe_to_rows(df_raw, index=False, header=True):
            ws3.append(r)

        out = BytesIO()
        wb.save(out)
        out.seek(0)
        st.download_button(
            "Stiahnuť XLSX",
            data=out,
            file_name=f"dochadzka_{monday}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
