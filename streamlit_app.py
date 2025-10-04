# streamlit_admin_dochadzka.py
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
SHIFT_TIMES = {
    "ranna": (time(6, 0), time(14, 0)),
    "poobedna": (time(14, 0), time(22, 0))
}
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 16.25  # Veliteľ max
NORMAL_DOUBLE_HOURS = 15.25  # ostatní

# ---------- HELPERS ----------
def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    res = databaze.table("attendance").select("*")\
        .gte("timestamp", start_dt.isoformat())\
        .lt("timestamp", end_dt.isoformat()).execute()
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    # robustne timestamp
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["timestamp"] = df["timestamp"].apply(lambda x: tz.localize(x) if (pd.notna(x) and x.tzinfo is None) else (x.tz_convert(tz) if pd.notna(x) else x))
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.time
    return df

def get_user_pairs(pos_day_df: pd.DataFrame):
    pairs = {}
    if pos_day_df.empty:
        return pairs
    for user in pos_day_df["user_code"].unique():
        u = pos_day_df[pos_day_df["user_code"] == user]
        pr = u[u["action"] == "Príchod"]["timestamp"]
        od = u[u["action"] == "Odchod"]["timestamp"]
        pr_min = pr.min() if not pr.empty else pd.NaT
        od_max = od.max() if not od.empty else pd.NaT
        pairs[user] = {"pr": pr_min, "od": od_max}
    return pairs

def classify_pair(pr, od, position):
    if pd.isna(pr) and pd.isna(od):
        return {"status": "absent", "hours": 0, "pr": None, "od": None}
    if pd.isna(pr):
        return {"status": "⚠ chýba príchod", "hours": 0, "pr": None, "od": od}
    if pd.isna(od):
        return {"status": "⚠ chýba odchod", "hours": 0, "pr": pr, "od": None}

    pr_t = pr.time()
    od_t = od.time()

    # Veliteľ
    if position == "Veliteľ":
        return {"status": "R+P Veliteľ OK", "hours": DOUBLE_SHIFT_HOURS, "pr": pr, "od": od}

    # ostatní
    hours = 0
    status = []
    if pr_t <= time(7, 30) and od_t >= time(14, 0):
        hours += SHIFT_HOURS
        status.append("Ranna OK")
    if pr_t <= time(14, 0) and od_t >= time(21, 0):
        hours += SHIFT_HOURS
        status.append("Poobedna OK")
    if not status:
        return {"status": "CHYBNA SMENA", "hours": 0, "pr": pr, "od": od}
    # maximalny hodin za den pre ostatných
    if hours > NORMAL_DOUBLE_HOURS:
        hours = NORMAL_DOUBLE_HOURS
    return {"status": " + ".join(status), "hours": hours, "pr": pr, "od": od}

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    morning = {"status": "absent", "hours":0, "pr": None, "od": None}
    afternoon = {"status": "absent", "hours":0, "pr": None, "od": None}
    comments = []

    pairs = get_user_pairs(pos_day_df)
    if not pairs:
        return morning, afternoon, comments

    for user, pair in pairs.items():
        res = classify_pair(pair["pr"], pair["od"], position)
        stt = res["status"]
        h = res["hours"]

        if position == "Veliteľ" and stt.startswith("R+P Veliteľ OK"):
            morning = {"status": stt, "hours": h, "pr": res["pr"], "od": res["od"]}
            afternoon = morning.copy()
            break
        else:
            # ostatní
            if "Ranna OK" in stt and morning["hours"]==0:
                morning = {"status": "Ranna OK", "hours": SHIFT_HOURS, "pr": res["pr"], "od": res["od"]}
            if "Poobedna OK" in stt and afternoon["hours"]==0:
                afternoon = {"status": "Poobedna OK", "hours": SHIFT_HOURS, "pr": res["pr"], "od": res["od"]}
            if stt.startswith("⚠") or stt=="CHYBNA SMENA":
                comments.append(f"{user}: {stt} (pr: {pair['pr']}, od: {pair['od']})")

    return morning, afternoon, comments

def summarize_day(df_day: pd.DataFrame):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morn, aft, comments = summarize_position_day(pos_df, pos)
        results[pos] = {"morning": morn, "afternoon": aft, "comments": comments}
    return results

def summarize_week_hours(df_week: pd.DataFrame):
    days = sorted(df_week["date"].unique())
    hours_matrix = pd.DataFrame(index=POSITIONS, columns=[d.strftime("%a") for d in days])
    total_per_day = pd.Series(index=[d.strftime("%a") for d in days], data=0.0)
    total_per_pos = pd.Series(index=POSITIONS, data=0.0)
    for d in days:
        df_d = df_week[df_week["date"]==d]
        summ = summarize_day(df_d)
        for pos in POSITIONS:
            morn = summ[pos]["morning"]["hours"]
            aft = summ[pos]["afternoon"]["hours"]
            if pos=="Veliteľ":
                hours_matrix.at[pos,d.strftime("%a")] = max(morn,aft)
            else:
                hours_matrix.at[pos,d.strftime("%a")] = morn+aft
            total_per_pos[pos] += hours_matrix.at[pos,d.strftime("%a")]
            total_per_day[d.strftime("%a")] += hours_matrix.at[pos,d.strftime("%a")]
    hours_matrix.loc["SUM"] = total_per_day
    hours_matrix["SUM"] = total_per_pos.append(pd.Series({"SUM": total_per_pos.sum()}))
    return hours_matrix.fillna(0)

def export_df_to_excel(df_week: pd.DataFrame):
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df_week.to_excel(writer, index=False, sheet_name="Dochadzka")
        # Sumar hodin
        hours_matrix = summarize_week_hours(df_week)
        hours_matrix.to_excel(writer, sheet_name="Sumar_hodin")
    out.seek(0)
    return out

# ---------- UI ----------
st.set_page_config(page_title="Admin - Dochádzka", layout="wide")
st.title("Admin — Denný / Týždenný prehľad a opravy")

# ADMIN login
if "admin_logged" not in st.session_state:
    st.session_state.admin_logged=False
if not st.session_state.admin_logged:
    st.sidebar.header("Admin prihlásenie")
    pw=st.sidebar.text_input("Admin heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw==ADMIN_PASS:
            st.session_state.admin_logged=True
            st.experimental_rerun()
        else:
            st.sidebar.error("Nesprávne heslo")
if not st.session_state.admin_logged:
    st.stop()

# výber týždňa
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = datetime.combine(monday, time(0,0))
end_dt = start_dt + timedelta(days=7)
df_week = load_attendance(tz.localize(start_dt), tz.localize(end_dt))

# denný prehľad
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=today, min_value=monday, max_value=monday+timedelta(days=6))
df_day = df_week[df_week["date"]==selected_day]
if df_day.empty:
    st.warning("Rozsah nie je k dispozícii")
else:
    summary = summarize_day(df_day)
    st.header(f"Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
    cols = st.columns(3)
    for i,pos in enumerate(POSITIONS):
        col = cols[i%3]
        info = summary[pos]
        morn = info["morning"]
        aft = info["afternoon"]

        def fmt(item):
            if item["status"]=="absent":
                return ("❌ absent", "0 h")
            return (item["status"], f"{item['hours']} h")

        m_status, m_times = fmt(morn)
        a_status, a_times = fmt(aft)
        col.markdown(f"### **{pos}**")
        col.markdown(f"**Ranná:** {m_status} — {m_times}")
        col.markdown(f"**Poobedná:** {a_status} — {a_times}")
        if info["comments"]:
            col.error(" • ".join(info["comments"]))

# export
st.header("Export")
if st.button("Exportuj tento týždeň (Excel)"):
    if df_week.empty:
        st.warning("Žiadne dáta za tento týždeň.")
    else:
        xls = export_df_to_excel(df_week)
        st.download_button(
            "Stiahnuť XLSX",
            data=xls,
            file_name=f"dochadzka_{monday}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
