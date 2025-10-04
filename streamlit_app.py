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
def calculate_shift_hours(pr, od, position):
    """Vracia (status, hodiny) podľa príchodu/odchodu a pozície"""
    if pd.isna(pr) and pd.isna(od):
        return ("❌ absent", 0.0)
    if pd.isna(pr):
        return (f"⚠️ chýba príchod (od: {od})", 0.0)
    if pd.isna(od):
        return (f"⚠️ chýba odchod (pr: {pr})", 0.0)

    pr_t = pr.time()
    od_t = od.time()

    # Veliteľ: max 16,25 h, R+P
    if position.lower().startswith("vel"):
        if pr_t <= time(5, 0) and od_t >= time(22, 0):
            return ("✅ R+P Veliteľ OK", 16.25)
        else:
            return ("⚠️ chybná smena Veliteľ", 0.0)

    # Ostatní
    hours = 0
    status_list = []
    if pr_t <= time(7, 0) and od_t >= time(14, 0):
        hours += 7.5
        status_list.append("✅ Ranná OK")
    if pr_t <= time(14, 0) and od_t >= time(21, 0):
        hours += 7.5
        status_list.append("✅ Poobedná OK")
    if hours > 15.25:
        hours = 15.25  # max za deň
    if not status_list:
        return ("⚠️ chybná smena", 0.0)
    return (" + ".join(status_list), hours)


def summarize_day(df_day: pd.DataFrame, target_date: date):
    """Vytvorí denný prehľad pre všetky pozície s detailom"""
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos]
        pairs = get_user_pairs(pos_df)
        morning = {"status": "❌ absent", "hours": 0}
        afternoon = {"status": "❌ absent", "hours": 0}
        comments = []

        for user, pair in pairs.items():
            status, hours = calculate_shift_hours(pair["pr"], pair["od"], pos)
            # Veliteľ: rovno R+P
            if pos.lower().startswith("vel") and "R+P" in status:
                morning = {"status": status, "hours": hours}
                afternoon = {"status": status, "hours": hours}
            else:
                # ostatní
                if "Ranná" in status:
                    morning = {"status": status, "hours": 7.5}
                if "Poobedná" in status:
                    afternoon = {"status": status, "hours": 7.5}
                if "chybná" in status or "chýba" in status:
                    comments.append(f"{user}: {status} (pr: {pair['pr']}, od: {pair['od']})")

        results[pos] = {"morning": morning, "afternoon": afternoon, "comments": comments}
    return results

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
