# streamlit_admin_dochadzka_full.py
import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO

# ---------- CONFIG ----------
DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")
databaze: Client = create_client(DATABAZA_URL, DATABAZA_KEY)

tz = pytz.timezone("Europe/Bratislava")
POSITIONS = ["Veliteľ","CCTV","Brány","Sklad2","Sklad3","Turniket2","Turniket3","Plombovac2","Plombovac3"]
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 16.25  # R+P plná zmena

# ---------- HELPERS ----------
def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    res = databaze.table("attendance").select("*")\
        .gte("timestamp", start_dt.isoformat())\
        .lt("timestamp", end_dt.isoformat()).execute()
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["timestamp"] = df["timestamp"].apply(lambda x: tz.localize(x) if pd.notna(x) and x.tzinfo is None else (x.tz_convert(tz) if pd.notna(x) else x))
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.time
    return df

def get_user_pairs(pos_day_df: pd.DataFrame):
    pairs = {}
    if pos_day_df.empty:
        return pairs
    for user in pos_day_df["user_code"].unique():
        u = pos_day_df[pos_day_df["user_code"] == user]
        pr = u[u["action"]=="Príchod"]["timestamp"]
        od = u[u["action"]=="Odchod"]["timestamp"]
        pr_min = pr.min() if not pr.empty else pd.NaT
        od_max = od.max() if not od.empty else pd.NaT
        pairs[user] = {"pr": pr_min, "od": od_max}
    return pairs

def classify_hours(pr, od):
    """Vracia počet hodín podľa príchodu a odchodu"""
    if pd.isna(pr) or pd.isna(od):
        return 0
    pr_t, od_t = pr.time(), od.time()
    if pr_t <= time(7,0) and od_t >= time(21,0):
        return DOUBLE_SHIFT_HOURS
    if pr_t <= time(7,0) and od_t <= time(15,0):
        return SHIFT_HOURS
    if pr_t >= time(13,0) and od_t >= time(21,0):
        return SHIFT_HOURS
    # kombinovaná ranná+poobedná
    return 15.25


def summarize_position_day(pos_day_df: pd.DataFrame):
    morning, afternoon = None, None
    pairs = get_user_pairs(pos_day_df)
    for user, pair in pairs.items():
        pr, od = pair["pr"], pair["od"]

        # ak je pr alebo od chýbajúci, pokračuj
        if pd.isna(pr) or pd.isna(od):
            continue

        h = classify_hours(pr, od)
        pr_time, od_time = pr.time(), od.time()

        if h == DOUBLE_SHIFT_HOURS:
            morning = {"prichod": pr, "odchod": od, "hours": h}
            afternoon = morning.copy()
            break
        if pr_time <= time(7,0) and od_time <= time(15,0):
            morning = {"prichod": pr, "odchod": od, "hours": SHIFT_HOURS}
        if pr_time >= time(13,0) and od_time >= time(21,0):
            afternoon = {"prichod": pr, "odchod": od, "hours": SHIFT_HOURS}
        if morning and afternoon:
            morning["hours"] = afternoon["hours"] = 15.25

    if not morning:
        morning = {"prichod": None, "odchod": None, "hours": 0}
    if not afternoon:
        afternoon = {"prichod": None, "odchod": None, "hours": 0}
    return morning, afternoon

def summarize_day(df_day: pd.DataFrame):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"]==pos] if not df_day.empty else pd.DataFrame()
        morn, aft = summarize_position_day(pos_df)
        results[pos] = {"morning": morn, "afternoon": aft}
    return results

def build_hours_matrix(df_week: pd.DataFrame, monday: date):
    days = [monday + timedelta(days=i) for i in range(7)]
    matrix = pd.DataFrame(index=POSITIONS, columns=[d.strftime("%a %d.%m") for d in days])
    for d in days:
        df_d = df_week[df_week["date"]==d]
        summ = summarize_day(df_d)
        for pos in POSITIONS:
            m = summ[pos]["morning"]["hours"]
            a = summ[pos]["afternoon"]["hours"]
            if m == DOUBLE_SHIFT_HOURS:
                total = DOUBLE_SHIFT_HOURS
            else:
                total = m + a
                if total == 15.0:
                    total = 15.25
            matrix.at[pos, d.strftime("%a %d.%m")] = total
    matrix.loc["SUM"] = matrix.sum(numeric_only=True)
    return matrix

def detect_conflicts(df_week: pd.DataFrame, monday: date):
    conflicts = []
    for i in range(7):
        day = monday + timedelta(days=i)
        df_day = df_week[df_week["date"]==day]
        if df_day.empty:
            continue
        for shift_name, (shift_start, shift_end) in {"Ranná": (time(6,0), time(14,0)), "Poobedná": (time(14,0), time(22,0))}.items():
            lower = datetime.combine(day, shift_start) - timedelta(hours=1)
            upper = datetime.combine(day, shift_end) + timedelta(hours=1)
            lower = tz.localize(lower)
            upper = tz.localize(upper)
            df_shift = df_day[(df_day["timestamp"] >= lower) & (df_day["timestamp"] <= upper)]
            for user in df_shift["user_code"].unique():
                pos_list = df_shift[df_shift["user_code"]==user]["position"].unique().tolist()
                if len(pos_list) > 1:
                    conflicts.append({"date": day, "shift": shift_name, "user": user, "positions": pos_list})
    return conflicts

def export_to_excel(df_week: pd.DataFrame, hours_matrix: pd.DataFrame):
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df_week.to_excel(writer, index=False, sheet_name="Dochadzka")
        hours_matrix.to_excel(writer, sheet_name="Hodiny")
    out.seek(0)
    return out

# ---------- UI ----------
st.set_page_config(page_title="Admin - Dochádzka", layout="wide")
st.title("Admin — Denný / Týždenný prehľad a hodiny")

# ADMIN login
if "admin_logged" not in st.session_state:
    st.session_state.admin_logged = False
if not st.session_state.admin_logged:
    st.sidebar.header("Admin prihlásenie")
    pw = st.sidebar.text_input("Admin heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw == ADMIN_PASS:
            st.session_state.admin_logged = True
            st.experimental_rerun()
        else:
            st.sidebar.error("Nesprávne heslo.")
if not st.session_state.admin_logged:
    st.stop()

# výber týždňa
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začne pondelkom)", value=datetime.now(tz).date())
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = datetime.combine(monday, time(0,0))
end_dt = start_dt + timedelta(days=7)
df_week = load_attendance(tz.localize(start_dt), tz.localize(end_dt))

# Denný prehľad
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=datetime.now(tz).date())
df_day = df_week[df_week["date"]==selected_day] if not df_week.empty else pd.DataFrame()
if df_day.empty:
    st.warning("⚠ Dáta nie sú dostupné pre vybraný deň.")
else:
    day_summary = summarize_day(df_day)
    st.header(f"Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
    cols_grid = st.columns(3)
    for i, pos in enumerate(POSITIONS):
        col = cols_grid[i % 3]
        s = day_summary[pos]
        for shift in ["morning", "afternoon"]:
            s_shift = s[shift]
            if s_shift["hours"] >= 15:
                color = "#d4edda"
            elif s_shift["hours"] > 0:
                color = "#fff3cd"
            else:
                color = "#f8d7da"
            pr_text = s_shift["prichod"].strftime("%H:%M") if s_shift["prichod"] else "—"
            od_text = s_shift["odchod"].strftime("%H:%M") if s_shift["odchod"] else "—"
            col.markdown(f"""
                <div style="padding:8px; background-color:{color}; border-radius:5px; margin-bottom:5px;">
                    <h4>{pos} — {shift.capitalize()}</h4>
                    <p><b>Príchod:</b> {pr_text} | <b>Odchod:</b> {od_text}</p>
                    <p><b>Hodiny:</b> {s_shift['hours']}</p>
                </div>
            """, unsafe_allow_html=True)

# Týždenná tabuľka hodín
st.header("Týždenná tabuľka hodín")
if df_week.empty:
    st.warning("⚠ Dáta nie sú dostupné pre tento týždeň.")
else:
    hours_matrix = build_hours_matrix(df_week, monday)
    st.dataframe(hours_matrix.fillna(0))

# Konflikty
st.header("Detekcia konfliktov")
conflicts = detect_conflicts(df_week, monday)
if conflicts:
    for c in conflicts:
        st.write(f"{c['date']} • {c['shift']} • user {c['user']} • pozície: {', '.join(c['positions'])}")
else:
    st.success("Žiadne konflikty.")

# Export
st.header("Export týždňa")
if st.button("Exportuj Excel"):
    if df_week.empty:
        st.warning("Žiadne dáta na export.")
    else:
        xls = export_to_excel(df_week, hours_matrix)
        st.download_button(
            "Stiahnuť XLSX",
            data=xls,
            file_name=f"dochadzka_{monday}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
