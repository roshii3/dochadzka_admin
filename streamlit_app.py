# streamlit_admin_dochadzka.py
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
DOUBLE_SHIFT_HOURS = 16.25  # Veliteľ max

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
    """Klasifikuje jeden pár prichod/odchod podľa pravidiel."""
    if (pd.isna(pr) or pr is None) and (pd.isna(od) or od is None):
        return {"status": "absent", "hours": 0}
    if pd.isna(pr) or pr is None:
        return {"status": "⚠ chýba príchod", "hours": 0, "pr": pr, "od": od}
    if pd.isna(od) or od is None:
        return {"status": "⚠ chýba odchod", "hours": 0, "pr": pr, "od": od}
    
    pr_t = pr.time()
    od_t = od.time()

    if position == "Veliteľ":
        if pr_t <= time(7,0) and od_t >= time(21,0):
            return {"status": "R+P OK", "hours": DOUBLE_SHIFT_HOURS, "pr": pr, "od": od}
        elif pr_t <= time(7,0) and od_t <= time(15,0):
            return {"status": "Ranna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}
        elif pr_t >= time(13,0) and od_t >= time(21,0):
            return {"status": "Poobedna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}
    else:
        if pr_t <= time(7,0) and od_t >= time(21,0):
            return {"status": "R+P OK", "hours": 15.25, "pr": pr, "od": od}
        elif pr_t <= time(7,0) and od_t <= time(15,0):
            return {"status": "Ranna OK", "hours": 7.5, "pr": pr, "od": od}
        elif pr_t >= time(13,0) and od_t >= time(21,0):
            return {"status": "Poobedna OK", "hours": 7.5, "pr": pr, "od": od}
    return {"status": "CHYBNA SMENA", "hours": 0, "pr": pr, "od": od}

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    """Pre pozíciu a deň zistí rannú a poobednú zmene + hodiny správne."""
    morning = {"status": "absent", "hours": 0, "pr": None, "od": None}
    afternoon = {"status": "absent", "hours": 0, "pr": None, "od": None}
    comments = []
    pairs = get_user_pairs(pos_day_df)
    if not pairs:
        return morning, afternoon, comments

    # Len prvý platný príchod/odchod per zmenu
    for user, pair in pairs.items():
        res = classify_pair(pair.get("pr"), pair.get("od"), position)
        stt = res["status"]
        if stt == "R+P OK":
            morning = {"status": stt, "hours": res["hours"], "pr": res["pr"], "od": res["od"]}
            afternoon = morning.copy()
            break
        elif stt == "Ranna OK":
            if morning["hours"] == 0:
                morning = {"status": stt, "hours": res["hours"], "pr": res["pr"], "od": res["od"]}
        elif stt == "Poobedna OK":
            if afternoon["hours"] == 0:
                afternoon = {"status": stt, "hours": res["hours"], "pr": res["pr"], "od": res["od"]}
        elif stt.startswith("⚠") or stt=="CHYBNA SMENA":
            comments.append(f"{user}: {stt} (pr: {pair.get('pr')}, od: {pair.get('od')})")
    return morning, afternoon, comments

def summarize_day(df_day: pd.DataFrame):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, comments = summarize_position_day(pos_df, pos)
        results[pos] = {"morning": morning, "afternoon": afternoon, "comments": comments}
    return results

def compute_week_hours(df_week):
    """Vytvorí DataFrame s hodinami za týždeň per pozícia."""
    days = sorted(df_week["date"].unique()) if not df_week.empty else []
    cols = [d.strftime("%A") for d in days]
    matrix = pd.DataFrame(index=POSITIONS, columns=cols)
    for d in days:
        df_d = df_week[df_week["date"] == d]
        summ = summarize_day(df_d)
        for pos in POSITIONS:
            m = summ[pos]["morning"]["hours"]
            a = summ[pos]["afternoon"]["hours"]
            matrix.at[pos, d.strftime("%A")] = m + a
    matrix = matrix.fillna(0)
    matrix["SUM"] = matrix.sum(axis=1)
    matrix.loc["SUM"] = matrix.sum()
    return matrix

def export_to_excel(df_week):
    out = BytesIO()
    days = sorted(df_week["date"].unique()) if not df_week.empty else []
    weekly_hours = compute_week_hours(df_week)
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df_week.to_excel(writer, index=False, sheet_name="Attendance")
        weekly_hours.to_excel(writer, index=True, sheet_name="Week_Hours")
        # farebne označenie v druhom sheete
        wb = writer.book
        ws = wb["Week_Hours"]
        fill = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=2, max_col=ws.max_column-1):
            for cell in row:
                if cell.value == 0:
                    cell.fill = fill
    out.seek(0)
    return out

# ---------- UI ----------
st.set_page_config(page_title="Admin - Dochádzka", layout="wide")
st.title("🕒 Dochádzkový prehľad SBS")

# Admin login
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

# Vyber týždňa
week_ref = st.sidebar.date_input("Vyber deň v týždni (pondelok až nedeľa)", value=datetime.now(tz).date())
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = datetime.combine(monday, time(0,0))
end_dt = start_dt + timedelta(days=7)

df_week = load_attendance(tz.localize(start_dt), tz.localize(end_dt))
if df_week.empty:
    st.error("⚠ Dáta nie sú k dispozícii pre vybraný týždeň")
    st.stop()

# Denný prehľad
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=datetime.now(tz).date(), min_value=monday, max_value=monday+timedelta(days=6))
df_day = df_week[df_week["date"] == selected_day]
if df_day.empty:
    st.warning("Rozsah nie je k dispozícii pre vybraný deň")
    st.stop()

st.header(f"Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
summary = summarize_day(df_day)
cols = st.columns(3)
for i, pos in enumerate(POSITIONS):
    col = cols[i % 3]
    info = summary[pos]
    morn = info["morning"]
    aft = info["afternoon"]

    def fmt(item):
        if item["status"] in ("absent", "none"):
            return ("❌ absent", "0 h")
        if item["status"].startswith("⚠"):
            pr_s = item.get("pr").strftime("%H:%M") if item.get("pr") else "-"
            od_s = item.get("od").strftime("%H:%M") if item.get("od") else "-"
            return (item["status"], f"{item['hours']} h ({pr_s} - {od_s})")
        if item["status"] in ("R+P OK","Ranna OK","Poobedna OK"):
            pr_s = item.get("pr").strftime("%H:%M") if item.get("pr") else "-"
            od_s = item.get("od").strftime("%H:%M") if item.get("od") else "-"
            return (item["status"], f"{item['hours']} h ({pr_s} - {od_s})")
        return (str(item["status"]), f"{item.get('hours',0)} h")

    m_status, m_times = fmt(morn)
    a_status, a_times = fmt(aft)

    col.markdown(f"### **{pos}**")
    col.markdown(f"**Ranná:** {m_status}  \n{m_times}")
    col.markdown(f"**Poobedná:** {a_status}  \n{a_times}")
    if info["comments"]:
        col.error(" • ".join(info["comments"]))

# Týždenná matica
st.header("Týždenný prehľad (matrix) — hodiny za týždeň")
weekly_hours = compute_week_hours(df_week)
st.dataframe(weekly_hours.fillna(0))

# Export
st.header("Export dát")
if st.button("Exportuj tento týždeň (Excel)"):
    xls = export_to_excel(df_week)
    st.download_button(
        "Stiahnuť XLSX", 
        data=xls, 
        file_name=f"dochadzka_{monday}.xlsx", 
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
