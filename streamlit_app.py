# streamlit_admin_dochadzka.py
import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO

# ---------- CONFIG ----------
st.set_page_config(page_title="Admin - Dochádzka", layout="wide", initial_sidebar_state="expanded")

# Skryj hlavičku Streamlitu
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
    if pd.isna(pr) or pd.isna(od):
        return ("❌ bez príchodu/odchodu", 0.0)

    pr_t = pr.time()
    od_t = od.time()

    # Veliteľ špeciálne pravidlo
    if position.lower().startswith("vel"):
        if pr_t <= time(5, 0) and (od_t >= time(22, 0) or od_t < time(2, 0)):
            return ("✅ R+P Veliteľ OK", VELITEL_DOUBLE)

    # R+P
    if pr_t <= time(7, 0) and (od_t >= time(21, 0) or od_t < time(2, 0)):
        return ("✅ R+P OK", DOUBLE_SHIFT_HOURS)

    # Ranná
    if pr_t <= time(7, 0) and od_t <= time(15, 0):
        return ("✅ Ranná OK", SHIFT_HOURS)

    # Poobedná
    if pr_t >= time(13, 0) and od_t >= time(21, 0):
        return ("✅ Poobedná OK", SHIFT_HOURS)

    return ("⚠️ chybná smena", 0.0)

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos]
        pairs = get_user_pairs(pos_df)
        morning = {"status": "❌ bez príchodu", "hours": 0}
        afternoon = {"status": "❌ bez príchodu", "hours": 0}
        for user, pair in pairs.items():
            status, hours = calculate_shift_hours(pair["pr"], pair["od"], pos)
            if "Ranná" in status:
                morning = {"status": status, "hours": hours}
            elif "Poobedná" in status:
                afternoon = {"status": status, "hours": hours}
            elif "R+P" in status:
                morning = {"status": status, "hours": hours}
                afternoon = {"status": status, "hours": hours}
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
            matrix.at[pos, d.strftime("%a %d.%m")] = total if total > 0 else "—"
    matrix["Spolu"] = matrix.apply(lambda x: sum(v for v in x if isinstance(v, (int, float, float))), axis=1)
    return matrix

def export_to_excel(df: pd.DataFrame) -> BytesIO:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Týždenný prehľad")
    output.seek(0)
    return output

# ---------- UI ----------
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad)")

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

# ---------- VÝBER TÝŽDŇA ----------
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni", value=today)
if st.sidebar.button("Načítať týždeň"):
    st.session_state.selected_week = week_ref

selected_week = st.session_state.get("selected_week", today)
monday = selected_week - timedelta(days=selected_week.weekday())
start_dt = tz.localize(datetime.combine(monday, time(0, 0)))
end_dt = start_dt + timedelta(days=7)

df_week = load_attendance(start_dt, end_dt)

# ---------- DENNÝ PREHĽAD ----------
selected_day = st.sidebar.date_input("Denný prehľad pre deň", value=today)
df_day = df_week[df_week["date"] == selected_day]
summary = summarize_day(df_day, selected_day)

st.header(f"✅ Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
cols = st.columns(3)
for i, pos in enumerate(POSITIONS):
    col = cols[i % 3]
    info = summary[pos]
    col.markdown(f"### **{pos}**")
    col.markdown(f"**Ranná:** {info['morning']['status']} ({info['morning']['hours']} h)")
    col.markdown(f"**Poobedná:** {info['afternoon']['status']} ({info['afternoon']['hours']} h)")

# ---------- TÝŽDENNÝ PREHĽAD ----------
st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m')} – {(monday + timedelta(days=6)).strftime('%d.%m')})")
matrix = summarize_hours_week(df_week, monday)
st.dataframe(matrix, use_container_width=True)

# ---------- EXPORT DO EXCELU ----------
excel_file = export_to_excel(matrix)
st.download_button(
    label="📥 Stiahnuť Excel (Týždenný prehľad)",
    data=excel_file,
    file_name=f"dochadzka_tyden_{monday.strftime('%Y-%m-%d')}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
