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
SHIFT_TIMES = {"ranna": (time(6,0), time(14,0)), "poobedna": (time(14,0), time(22,0))}
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 16.25
BOTH_SHIFT_HOURS = 15.25

# ---------- HELPERS ----------
def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    res = databaze.table("attendance").select("*")\
        .gte("timestamp", start_dt.isoformat())\
        .lt("timestamp", end_dt.isoformat()).execute()
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["timestamp"] = df["timestamp"].apply(
        lambda x: tz.localize(x) if pd.notna(x) and x.tzinfo is None else (x.tz_convert(tz) if pd.notna(x) else x)
    )
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.time
    return df

def get_user_pairs(pos_day_df: pd.DataFrame):
    pairs = {}
    if pos_day_df.empty:
        return pairs
    for user in pos_day_df["user_code"].unique():
        u = pos_day_df[pos_day_df["user_code"]==user]
        pr = u[u["action"]=="Príchod"]["timestamp"]
        od = u[u["action"]=="Odchod"]["timestamp"]
        pr_min = pr.min() if not pr.empty else pd.NaT
        od_max = od.max() if not od.empty else pd.NaT
        pairs[user] = {"pr": pr_min, "od": od_max}
    return pairs

def classify_pair(pr, od, position):
    if pd.isna(pr) and pd.isna(od):
        return {"status": "absent", "hours": 0}
    if pd.isna(pr):
        return {"status": "⚠ chýba príchod", "hours": 0, "pr": None, "od": od}
    if pd.isna(od):
        return {"status": "⚠ chýba odchod", "hours": 0, "pr": pr, "od": None}
    pr_t = pr.time()
    od_t = od.time()
    if position=="Veliteľ" and pr_t <= time(7,0) and od_t >= time(21,0):
        return {"status": "R+P OK", "hours": DOUBLE_SHIFT_HOURS, "pr": pr, "od": od}
    if pr_t <= time(7,0) and od_t <= time(15,0):
        return {"status": "Ranna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}
    if pr_t >= time(13,0) and od_t >= time(21,0):
        return {"status": "Poobedna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}
    return {"status": "⚠ neplatná zmena", "hours": 0, "pr": pr, "od": od}

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
        hours = res.get("hours",0)
        if stt=="R+P OK":
            morning = {"status": stt, "pr": res["pr"], "od": res["od"], "hours": hours}
            afternoon = morning.copy()
            break
        if stt=="Ranna OK":
            morning = {"status": stt, "pr": res["pr"], "od": res["od"], "hours": SHIFT_HOURS}
        if stt=="Poobedna OK":
            afternoon = {"status": stt, "pr": res["pr"], "od": res["od"], "hours": SHIFT_HOURS}
        if stt.startswith("⚠"):
            comments.append(f"{user}: {stt} (pr: {res.get('pr')}, od: {res.get('od')})")
            if stt=="⚠ chýba príchod":
                morning = {"status": stt, "pr": res.get("pr"), "od": res.get("od"), "hours":0}
            if stt=="⚠ chýba odchod":
                afternoon = {"status": stt, "pr": res.get("pr"), "od": res.get("od"), "hours":0}
    # ak obe smeny sú OK, okrem veliteľa s R+P
    if morning["status"]=="Ranna OK" and afternoon["status"]=="Poobedna OK":
        morning["hours"] = afternoon["hours"] = BOTH_SHIFT_HOURS
    return morning, afternoon, comments

def summarize_day(df_day: pd.DataFrame):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"]==pos] if not df_day.empty else pd.DataFrame()
        morn, aft, comments = summarize_position_day(pos_df, pos)
        results[pos] = {"morning": morn, "afternoon": aft, "comments": comments}
    return results

def summarize_week_hours(df_week: pd.DataFrame, week_start: date):
    days = [week_start + timedelta(days=i) for i in range(7)]
    summary_hours = pd.DataFrame(index=POSITIONS, columns=[d.strftime("%A") for d in days])
    for d in days:
        df_d = df_week[df_week["date"]==d]
        day_sum = summarize_day(df_d)
        for pos in POSITIONS:
            morn_h = day_sum[pos]["morning"].get("hours",0)
            aft_h = day_sum[pos]["afternoon"].get("hours",0)
            if pos=="Veliteľ" and day_sum[pos]["morning"]["status"]=="R+P OK":
                summary_hours.at[pos, d.strftime("%A")] = DOUBLE_SHIFT_HOURS
            else:
                total = morn_h + aft_h
                summary_hours.at[pos, d.strftime("%A")] = total
    summary_hours["SUM"] = summary_hours.sum(axis=1)
    summary_hours.loc["SUM"] = summary_hours.sum()
    return summary_hours

def export_df_to_excel(df_week: pd.DataFrame, summary_hours: pd.DataFrame):
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df_week.to_excel(writer, index=False, sheet_name="Surove_dáta")
        summary_hours.to_excel(writer, sheet_name="Sumar_hodin")
    out.seek(0)
    return out

# ---------- UI ----------
st.set_page_config(page_title="Admin - Dochádzka", layout="wide")
st.title("Admin — Denný / Týždenný prehľad a opravy")

if "admin_logged" not in st.session_state:
    st.session_state.admin_logged = False
if not st.session_state.admin_logged:
    st.sidebar.header("Admin prihlásenie")
    pw = st.sidebar.text_input("Admin heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw==ADMIN_PASS:
            st.session_state.admin_logged = True
            st.experimental_rerun()
        else:
            st.sidebar.error("Nesprávne heslo alebo ADMIN_PASS nie je nastavené.")
if not st.session_state.admin_logged:
    st.stop()

today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = datetime.combine(monday, time(0,0))
end_dt = start_dt + timedelta(days=7)

df_week = load_attendance(tz.localize(start_dt), tz.localize(end_dt))
if df_week.empty:
    st.error("Rozsah nie je k dispozícii.")
else:
    selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=today, min_value=monday, max_value=monday+timedelta(days=6))
    df_day = df_week[df_week["date"]==selected_day]
    day_summary = summarize_day(df_day)
    st.header(f"Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
    cols = st.columns(3)
    for i,pos in enumerate(POSITIONS):
        col = cols[i%3]
        info = day_summary[pos]
        morn = info["morning"]
        aft = info["afternoon"]
        col.markdown(f"### **{pos}**")
        def fmt(item):
            if item["status"]=="absent":
                return ("❌ absent", "0 h")
            elif item["status"].startswith("⚠"):
                pr_s = item.get("pr").strftime("%H:%M") if item.get("pr") is not None else "-"
                od_s = item.get("od").strftime("%H:%M") if item.get("od") is not None else "-"
                return (f"⚠ {item['status']}", f"{pr_s} - {od_s} | {item['hours']} h")
            else:
                pr_s = item.get("pr").strftime("%H:%M") if item.get("pr") is not None else "-"
                od_s = item.get("od").strftime("%H:%M") if item.get("od") is not None else "-"
                return (item["status"], f"{pr_s} - {od_s} | {item['hours']} h")
        m_status, m_times = fmt(morn)
        a_status, a_times = fmt(aft)
        col.markdown(f"**Ranná:** {m_status}  \n{m_times}")
        col.markdown(f"**Poobedná:** {a_status}  \n{a_times}")
        if info["comments"]:
            col.error(" • ".join(info["comments"]))
    
    # Týždenný prehľad
    st.header("Týždenný prehľad (matrix hodín)")
    week_hours = summarize_week_hours(df_week, monday)
    st.dataframe(week_hours.fillna(0))
    
    # Export
    st.header("Export dát")
    if st.button("Exportuj tento týždeň (Excel)"):
        xls = export_df_to_excel(df_week, week_hours)
        st.download_button("Stiahnuť XLSX",
                           data=xls,
                           file_name=f"dochadzka_{monday}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
