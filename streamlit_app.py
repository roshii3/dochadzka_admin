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
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")  # nastav v secrets
databaze: Client = create_client(DATABAZA_URL, DATABAZA_KEY)

tz = pytz.timezone("Europe/Bratislava")

POSITIONS = ["Veliteľ","CCTV","Brány","Sklad2","Sklad3","Turniket2","Turniket3","Plombovac2","Plombovac3"]
SHIFT_TIMES = {
    "ranna": (time(6, 0), time(14, 0)),
    "poobedna": (time(14, 0), time(22, 0))
}
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 16.25  # veliteľ
OTHER_DOUBLE_HOURS = 15.25  # ostatné pozície

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
    if (pd.isna(pr) or pr is None) and (pd.isna(od) or od is None):
        return {"status": "absent", "hours": 0}
    if pd.isna(pr) or pr is None:
        return {"status": "⚠ zabudnutý príchod", "hours": 0, "od": od}
    if pd.isna(od) or od is None:
        return {"status": "⚠ zabudnutý odchod", "hours": 0, "pr": pr}

    pr_t = pr.time()
    od_t = od.time()

    # R+P pre všetky pozície
    if pr_t <= time(7,0) and od_t >= time(21,0):
        hours = DOUBLE_SHIFT_HOURS if position=="Veliteľ" else OTHER_DOUBLE_HOURS
        return {"status": "R+P OK", "hours": hours, "pr": pr, "od": od}
    # ranná
    if pr_t <= time(7,0) and od_t <= time(15,0):
        return {"status": "Ranna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}
    # poobedná
    if pr_t >= time(13,0) and od_t >= time(21,0):
        return {"status": "Poobedna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}

    return {"status": "CHYBNA SMENA", "hours": 0, "pr": pr, "od": od}

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    morning = {"status": "absent", "pr": None, "od": None, "hours": 0}
    afternoon = {"status": "absent", "pr": None, "od": None, "hours": 0}
    comments = []

    pairs = get_user_pairs(pos_day_df)
    if not pairs:
        return morning, afternoon, comments

    for user, pair in pairs.items():
        res = classify_pair(pair["pr"], pair["od"], position)
        stt = res["status"]

        if stt == "R+P OK":
            morning = {"status": stt, "pr": res["pr"], "od": res["od"], "hours": res["hours"]}
            afternoon = morning.copy()
            break
        elif stt == "Ranna OK":
            if morning["status"] not in ("R+P OK", "Ranna OK"):
                morning = {"status": stt, "pr": res["pr"], "od": res["od"], "hours": res["hours"]}
        elif stt == "Poobedna OK":
            if afternoon["status"] not in ("R+P OK", "Poobedna OK"):
                afternoon = {"status": stt, "pr": res["pr"], "od": res["od"], "hours": res["hours"]}
        elif stt.startswith("⚠"):
            if "príchod" in stt:
                if morning["status"] not in ("R+P OK", "Ranna OK"):
                    morning = {"status": stt, "pr": None, "od": res.get("od"), "hours": 0}
            else:
                if afternoon["status"] not in ("R+P OK", "Poobedna OK"):
                    afternoon = {"status": stt, "pr": res.get("pr"), "od": None, "hours": 0}
        else:
            comments.append(f"{user}: neplatná zmena (pr: {pair['pr']}, od: {pair['od']})")

    return morning, afternoon, comments

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, comments = summarize_position_day(pos_df, pos)
        results[pos] = {"morning": morning, "afternoon": afternoon, "comments": comments}
    return results

def export_df_to_excel_with_hours(df_week: pd.DataFrame):
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df_week.to_excel(writer, index=False, sheet_name="Dochadzka")
        # priprav sumar hodin
        monday = df_week["date"].min()
        days = [monday + timedelta(days=i) for i in range(7)]
        hours_matrix = pd.DataFrame(index=POSITIONS, columns=[d.strftime("%A") for d in days])
        for d in days:
            df_d = df_week[df_week["date"] == d]
            summ = summarize_day(df_d, d)
            for pos in POSITIONS:
                h_m = summ[pos]["morning"]["hours"]
                h_a = summ[pos]["afternoon"]["hours"]
                hours_matrix.at[pos, d.strftime("%A")] = h_m + h_a
        hours_matrix["SUM"] = hours_matrix.sum(axis=1)
        hours_matrix.loc["TOTAL"] = hours_matrix.sum()
        hours_matrix.to_excel(writer, sheet_name="Sumar hodin")
    out.seek(0)
    return out

# ---------- UI ----------
st.set_page_config(page_title="Admin - Dochádzka", layout="wide")
st.title("Admin — Denný / Týždenný prehľad a opravy")

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
            st.sidebar.error("Nesprávne heslo alebo ADMIN_PASS nie je nastavené.")
if not st.session_state.admin_logged:
    st.stop()

# výber týždňa
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začne pondelkom)", value=datetime.now(tz).date())
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = datetime.combine(monday, time(0,0))
end_dt = start_dt + timedelta(days=7)
df_week = load_attendance(tz.localize(start_dt), tz.localize(end_dt))

# výber dňa
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=datetime.now(tz).date(), min_value=monday, max_value=monday+timedelta(days=6))
st.header(f"Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
df_day = df_week[df_week["date"] == selected_day]
summary = summarize_day(df_day, selected_day)

# zobraz denny prehlad
cols = st.columns(3)
for i, pos in enumerate(POSITIONS):
    col = cols[i % 3]
    info = summary[pos]
    morn = info["morning"]
    aft = info["afternoon"]

    def fmt(item):
        if item["status"] in ("absent", "none"):
            return ("❌ bez príchodu", "0 h")
        if item["status"].startswith("⚠"):
            pr_s = item.get("pr").strftime("%H:%M") if item.get("pr") else "-"
            od_s = item.get("od").strftime("%H:%M") if item.get("od") else "-"
            return (item["status"], f"{item['hours']} h ({pr_s} - {od_s})")
        return (item["status"], f"{item['hours']} h ({item['pr'].strftime('%H:%M')} - {item['od'].strftime('%H:%M')})")

    m_status, m_times = fmt(morn)
    a_status, a_times = fmt(aft)

    col.markdown(f"### **{pos}**")
    col.markdown(f"**Ranná:** {m_status}  \n{m_times}")
    col.markdown(f"**Poobedná:** {a_status}  \n{a_times}")
    if info["comments"]:
        col.error(" • ".join(info["comments"]))

# Export
st.header("Export dát")
if st.button("Exportuj tento týždeň (Excel + hodiny)"):
    if df_week.empty:
        st.warning("Žiadne dáta za tento týždeň.")
    else:
        for col_name in df_week.select_dtypes(include=["datetimetz"]):
            df_week[col_name] = df_week[col_name].dt.tz_localize(None)
        xls = export_df_to_excel_with_hours(df_week)
        st.download_button(
            "Stiahnuť XLSX", 
            data=xls, 
            file_name=f"dochadzka_{monday}.xlsx", 
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
