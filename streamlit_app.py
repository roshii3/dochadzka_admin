# streamlit_dochadzka.py
import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO
from openpyxl.styles import PatternFill

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
        return {"status": "absent", "hours": 0}
    if pd.isna(pr):
        return {"status": "⚠ chýba príchod", "hours": 0, "pr": None, "od": od}
    if pd.isna(od):
        return {"status": "⚠ chýba odchod", "hours": 0, "pr": pr, "od": None}

    pr_t = pr.time()
    od_t = od.time()
    if position == "Veliteľ":
        return {"status": "R+P OK", "hours": DOUBLE_SHIFT_HOURS, "pr": pr, "od": od}
    # ostatní
    if pr_t <= time(7, 0) and od_t >= time(21, 0):
        return {"status": "R+P OK", "hours": BOTH_SHIFT_HOURS, "pr": pr, "od": od}
    if pr_t <= time(7, 0) and od_t <= time(15, 0):
        return {"status": "Ranna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}
    if pr_t >= time(13, 0) and od_t >= time(21, 0):
        return {"status": "Poobedna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}
    return {"status": "CHYBNA SMENA", "hours": 0, "pr": pr, "od": od}

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    morning = {"status": "absent", "pr": None, "od": None, "hours": 0}
    afternoon = {"status": "absent", "pr": None, "od": None, "hours": 0}
    details = []
    pairs = get_user_pairs(pos_day_df)
    if not pairs:
        return morning, afternoon, details

    for user, pair in pairs.items():
        res = classify_pair(pair["pr"], pair["od"], position)
        stt = res["status"]
        hrs = res.get("hours",0)

        if stt == "R+P OK":
            if position == "Veliteľ":
                morning = {"status": stt, "pr": res["pr"], "od": res["od"], "hours": DOUBLE_SHIFT_HOURS}
                afternoon = morning.copy()
                break
            else:
                morning = {"status": stt, "pr": res["pr"], "od": res["od"], "hours": BOTH_SHIFT_HOURS}
                afternoon = morning.copy()
                break
        elif stt == "Ranna OK":
            morning = {"status": stt, "pr": res["pr"], "od": res["od"], "hours": SHIFT_HOURS}
        elif stt == "Poobedna OK":
            afternoon = {"status": stt, "pr": res["pr"], "od": res["od"], "hours": SHIFT_HOURS}
        elif stt.startswith("⚠") or stt=="CHYBNA SMENA":
            details.append(f"{user}: {stt} (pr: {pair.get('pr')}, od: {pair.get('od')})")

    return morning, afternoon, details

def summarize_day(df_day: pd.DataFrame):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, details = summarize_position_day(pos_df, pos)
        results[pos] = {"morning": morning, "afternoon": afternoon, "details": details}
    return results

def compute_week_hours(df_week: pd.DataFrame):
    days = sorted(df_week["date"].unique())
    matrix = pd.DataFrame(index=POSITIONS, columns=[d.strftime("%A") for d in days])
    for pos in POSITIONS:
        for d in days:
            df_d = df_week[(df_week["date"]==d)&(df_week["position"]==pos)]
            morning, afternoon, _ = summarize_position_day(df_d, pos)
            hrs = morning.get("hours",0)+afternoon.get("hours",0)
            matrix.at[pos,d.strftime("%A")] = hrs
    matrix["SUM"] = matrix.sum(axis=1)
    return matrix

def export_excel(df_raw, df_hours):
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df_raw.to_excel(writer, index=False, sheet_name="raw_data")
        df_hours.to_excel(writer, sheet_name="week_hours")
        # farebne bunky
        ws = writer.sheets["week_hours"]
        for row in range(2, 2+len(df_hours)):
            for col in range(2,2+len(df_hours.columns)-1):
                val = ws.cell(row=row, column=col).value
                if val==0:
                    fill = PatternFill(start_color="FF0000", end_color="FF0000", fill_type="solid")
                elif val>0 and val<BOTH_SHIFT_HOURS:
                    fill = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
                else:
                    fill = PatternFill(start_color="00FF00", end_color="00FF00", fill_type="solid")
                ws.cell(row=row, column=col).fill = fill
    out.seek(0)
    return out

# ---------- UI ----------
st.set_page_config(page_title="Dochádzka SBS", layout="wide")
st.title("🕒 Dochádzkový prehľad SBS")

if "admin_logged" not in st.session_state:
    st.session_state.admin_logged = False

if not st.session_state.admin_logged:
    st.sidebar.header("Admin login")
    pw = st.sidebar.text_input("Admin heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw==ADMIN_PASS:
            st.session_state.admin_logged=True
            st.experimental_rerun()
        else:
            st.sidebar.error("Nesprávne heslo")
    st.stop()

# výber týždňa
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začne pondelkom)", value=datetime.now(tz).date())
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = datetime.combine(monday, time(0,0))
end_dt = start_dt + timedelta(days=7)
df_week = load_attendance(tz.localize(start_dt), tz.localize(end_dt))

# Denný prehľad
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=datetime.now(tz).date())
if selected_day not in df_week["date"].values:
    st.error("⚠ Dáta nie sú dostupné pre tento deň")
else:
    df_day = df_week[df_week["date"]==selected_day]
    summary = summarize_day(df_day)
    for pos in POSITIONS:
        st.subheader(pos)
        morn = summary[pos]["morning"]
        aft = summary[pos]["afternoon"]
        details = summary[pos]["details"]

        def fmt(item):
            pr_s = item.get("pr")
            pr_s = pr_s.strftime("%H:%M") if pr_s else "-"
            od_s = item.get("od")
            od_s = od_s.strftime("%H:%M") if od_s else "-"
            if item["status"].startswith("⚠") or item["status"]=="CHYBNA SMENA":
                return f"⚠ {item['status']} | {pr_s}-{od_s}"
            if item["status"]=="absent":
                return f"❌ absent"
            return f"✅ {item['status']} ({item['hours']} h)"

        st.markdown(f"**Ranná:** {fmt(morn)}  \n**Poobedná:** {fmt(aft)}")
        if details:
            for d in details:
                st.error(d)

# Týždenná matica hodín
st.header("Týždenný prehľad hodín")
if df_week.empty:
    st.info("Žiadne dáta")
else:
    df_hours = compute_week_hours(df_week)
    st.dataframe(df_hours.fillna(0))

# Export Excel
if st.button("Export Excel"):
    xls = export_excel(df_week, df_hours)
    st.download_button("Stiahnuť XLSX", data=xls, file_name=f"dochadzka_{monday}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
