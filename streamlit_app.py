Perfektné. Tu je kompletne prepracovaný skript s jednotnou logikou výpočtu hodín, detailom chýb a exportom do Excelu s dvoma sheety: denný prehľad a týždenný prehľad.

# streamlit_admin_dochadzka.py
import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO
from openpyxl.styles import PatternFill

# ---------- CONFIG ----------
st.set_page_config(page_title="Admin - Dochádzka", layout="wide", initial_sidebar_state="expanded")
hide_st_style = """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    </style>
"""
st.markdown(hide_st_style, unsafe_allow_html=True)

# ---------- DATABASE ----------
DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")
databaze: Client = create_client(DATABAZA_URL, DATABAZA_KEY)
tz = pytz.timezone("Europe/Bratislava")

POSITIONS = ["Veliteľ","CCTV","Brány","Sklad2","Sklad3","Turniket2","Turniket3","Plombovac2","Plombovac3"]
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 15.25
VELITEL_DOUBLE = 16.25

# ---------- HELPERS ----------
def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    res = databaze.table("attendance").select("*")\
        .gte("timestamp", start_dt.isoformat())\
        .lt("timestamp", end_dt.isoformat()).execute()
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    try:
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(tz)
        else:
            df["timestamp"] = df["timestamp"].dt.tz_convert(tz)
    except Exception:
        df["timestamp"] = df["timestamp"].apply(lambda x: tz.localize(x) if (pd.notna(x) and x.tzinfo is None) else (x.tz_convert(tz) if pd.notna(x) else x))
    df["date"] = df["timestamp"].dt.date
    return df

def get_user_pairs(pos_day_df: pd.DataFrame):
    pairs = {}
    for user in pos_day_df["user_code"].unique():
        u = pos_day_df[pos_day_df["user_code"] == user]
        pr = u[u["action"] == "Príchod"]["timestamp"]
        od = u[u["action"] == "Odchod"]["timestamp"]
        pr_min = pr.min() if not pr.empty else pd.NaT
        od_max = od.max() if not od.empty else pd.NaT
        pairs[user] = {"pr": pr_min, "od": od_max}
    return pairs

def calculate_shift_hours(pr, od, position):
    """Vracia (status, hodiny, detail)"""
    if pd.isna(pr) and pd.isna(od):
        return ("❌ bez príchodu/odchodu", 0.0, "Príchod a odchod chýbajú")
    if pd.isna(pr):
        return ("⚠ chýba príchod", 0.0, f"Príchod: NaT, Odchod: {od}")
    if pd.isna(od):
        return ("⚠ chýba odchod", 0.0, f"Príchod: {pr}, Odchod: NaT")

    pr_t = pr.time()
    od_t = od.time()

    # Veliteľ špeciálne pravidlo
    if position.lower().startswith("vel"):
        if pr_t <= time(5,0) and (od_t >= time(22,0) or od_t < time(2,0)):
            return ("✅ R+P Veliteľ OK", VELITEL_DOUBLE, f"Príchod: {pr}, Odchod: {od}")

    # R+P pre ostatných
    if pr_t <= time(7,0) and (od_t >= time(21,0) or od_t < time(2,0)):
        return ("✅ R+P OK", DOUBLE_SHIFT_HOURS, f"Príchod: {pr}, Odchod: {od}")

    # Ranná
    if pr_t <= time(7,0) and od_t <= time(15,0):
        return ("✅ Ranná OK", SHIFT_HOURS, f"Príchod: {pr}, Odchod: {od}")

    # Poobedná
    if pr_t >= time(13,0) and od_t >= time(21,0):
        return ("✅ Poobedná OK", SHIFT_HOURS, f"Príchod: {pr}, Odchod: {od}")

    return ("⚠ chybná smena", 0.0, f"Príchod: {pr}, Odchod: {od}")

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos]
        pairs = get_user_pairs(pos_df)
        morning = {"status": "❌ bez príchodu", "hours": 0.0, "detail": ""}
        afternoon = {"status": "❌ bez príchodu", "hours": 0.0, "detail": ""}
        for user, pair in pairs.items():
            status, hours, detail = calculate_shift_hours(pair["pr"], pair["od"], pos)
            if "Ranná" in status:
                morning = {"status": status, "hours": hours, "detail": detail}
            elif "Poobedná" in status:
                afternoon = {"status": status, "hours": hours, "detail": detail}
            elif "R+P" in status:
                morning = {"status": status, "hours": hours, "detail": detail}
                afternoon = {"status": status, "hours": hours, "detail": detail}
            elif "⚠" in status or "❌" in status:
                # ak niekto má chybu, zobraz v dennom prehľade
                if morning["hours"] == 0:
                    morning = {"status": status, "hours": hours, "detail": detail}
                elif afternoon["hours"] == 0:
                    afternoon = {"status": status, "hours": hours, "detail": detail}
        results[pos] = {"morning": morning, "afternoon": afternoon}
    return results

def summarize_hours_week(df_week: pd.DataFrame, monday: date):
    days = [monday + timedelta(days=i) for i in range(7)]
    matrix = pd.DataFrame(index=POSITIONS, columns=[d.strftime("%a %d.%m") for d in days])
    for d in days:
        df_d = df_week[df_week["date"] == d]
        summary = summarize_day(df_d, d)
        for pos in POSITIONS:
            m = summary[pos]["morning"]["hours"]
            a = summary[pos]["afternoon"]["hours"]
            total = m + a
            matrix.at[pos, d.strftime("%a %d.%m")] = total if total > 0 else 0.0
    matrix["SUM"] = matrix.sum(axis=1)
    return matrix

def export_to_excel(df_week: pd.DataFrame, monday: date) -> BytesIO:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        # Týždenný prehľad
        week_matrix = summarize_hours_week(df_week, monday)
        week_matrix.to_excel(writer, sheet_name="Týždenný prehľad")

        # Denný detail
        daily_list = []
        days = [monday + timedelta(days=i) for i in range(7)]
        for d in days:
            df_d = df_week[df_week["date"] == d]
            summ = summarize_day(df_d, d)
            for pos in POSITIONS:
                m = summ[pos]["morning"]
                a = summ[pos]["afternoon"]
                daily_list.append({
                    "Dátum": d,
                    "Pozícia": pos,
                    "Ranná status": m["status"],
                    "Ranná hodiny": m["hours"],
                    "Ranná detail": m["detail"],
                    "Poobedná status": a["status"],
                    "Poobedná hodiny": a["hours"],
                    "Poobedná detail": a["detail"]
                })
        daily_df = pd.DataFrame(daily_list)
        daily_df.to_excel(writer, sheet_name="Denný prehľad", index=False)
    output.seek(0)
    return output

# ---------- UI ----------
st.title("🕒 Dochádzkový prehľad SBS")

if "admin_logged" not in st.session_state:
    st.session_state.admin_logged = False
if not st.session_state.admin_logged:
    pw = st.sidebar.text_input("Admin heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw == ADMIN_PASS:
            st.session_state.admin_logged = True
            st.experimental_rerun()
        else:
            st.sidebar.error("Nesprávne heslo.")
if not st.session_state.admin_logged:
    st.stop()

# Výber týždňa
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni", value=today)
if st.sidebar.button("Načítať týždeň"):
    st.session_state.selected_week = week_ref

