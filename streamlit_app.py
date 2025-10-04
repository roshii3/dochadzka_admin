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
DOUBLE_SHIFT_HOURS = 16.25  # teraz pre Veliteľa a celodenné pokrytie

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
        return {"status": "none", "hours": 0}
    if pd.isna(pr) or pr is None:
        return {"status": "missing_prichod", "pr": None, "od": od, "hours": 0}
    if pd.isna(od) or od is None:
        return {"status": "missing_odchod", "pr": pr, "od": None, "hours": 0}

    pr_t = pr.time()
    od_t = od.time()

    # Veliteľ môže mať R+P od skorých ranných hodín do polnoci
    if position == "Veliteľ" and pr_t <= time(7,0) and od_t >= time(21,0):
        return {"status": "R+P OK", "hours": DOUBLE_SHIFT_HOURS, "pr": pr, "od": od}

    # ostatné pozície
    if pr_t <= time(7, 0) and od_t <= time(15, 0):
        return {"status": "Ranna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}
    if pr_t >= time(13, 0) and od_t >= time(21, 0):
        return {"status": "Poobedna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}

    return {"status": "CHYBNA SMENA", "pr": pr, "od": od, "hours": 0}

def summarize_position_day(pos_day_df: pd.DataFrame, position: str):
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
            morning = {"status": "R+P OK", "pr": res["pr"], "od": res["od"], "hours": res["hours"]}
            afternoon = morning.copy()
            break  # už netreba ďalších prepísať

        elif stt == "Ranna OK":
            if morning["status"] not in ("R+P OK", "Ranna OK"):
                morning = {"status": "Ranna OK", "pr": res["pr"], "od": res["od"], "hours": res["hours"]}
        elif stt == "Poobedna OK":
            if afternoon["status"] not in ("R+P OK", "Poobedna OK"):
                afternoon = {"status": "Poobedna OK", "pr": res["pr"], "od": res["od"], "hours": res["hours"]}
        elif stt == "CHYBNA SMENA":
            comments.append(f"{user}: neplatná zmena (pr: {pair['pr']}, od: {pair['od']})")

    return morning, afternoon, comments

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, comments = summarize_position_day(pos_df, pos)
        results[pos] = {"morning": morning, "afternoon": afternoon, "comments": comments}
    return results

# ---------- EXPORT EXCEL ----------
def export_df_to_excel_with_hours(df_week):
    out = BytesIO()
    # denny matrix + hodiny
    days = sorted(df_week["date"].unique())
    cols = []
    for d in days:
        cols.append(d.strftime("%a %d.%m"))

    matrix = pd.DataFrame(index=POSITIONS, columns=cols)
    hours_matrix = pd.DataFrame(index=POSITIONS, columns=cols)
    for d in days:
        df_d = df_week[df_week["date"] == d]
        summ = summarize_day(df_d, d)
        for pos in POSITIONS:
            m = summ[pos]["morning"]
            a = summ[pos]["afternoon"]
            # ak je R+P, hodiny iba raz
            if m["status"] == "R+P OK":
                matrix.at[pos, d.strftime("%a %d.%m")] = "✅ R+P OK"
                hours_matrix.at[pos, d.strftime("%a %d.%m")] = m["hours"]
            else:
                matrix.at[pos, d.strftime("%a %d.%m")] = f"R: {m['status']} | P: {a['status']}"
                hours_matrix.at[pos, d.strftime("%a %d.%m")] = (m.get("hours",0) or 0) + (a.get("hours",0) or 0)
    # sumy
    hours_matrix["SUM"] = hours_matrix.sum(axis=1)
    hours_matrix.loc["SUM"] = hours_matrix.sum(axis=0)

    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        matrix.fillna("—").to_excel(writer, sheet_name="Denný prehľad", index=True)
        hours_matrix.fillna(0).to_excel(writer, sheet_name="SUMAR_HODIN", index=True)
    out.seek(0)
    return out

# ---------- STREAMLIT UI ----------
st.set_page_config(page_title="Admin - Dochádzka", layout="wide")
st.title("Admin — Denný / Týždenný prehľad a opravy")

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

# Výber týždňa
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začne pondelkom)", value=datetime.now(tz).date())
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = datetime.combine(monday, time(0,0))
end_dt = start_dt + timedelta(days=7)

df_week = load_attendance(tz.localize(start_dt), tz.localize(end_dt))

# Výber denného prehľadu
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=datetime.now(tz).date(), min_value=monday, max_value=monday+timedelta(days=6))
df_day = df_week[df_week["date"] == selected_day]
summary = summarize_day(df_day, selected_day)

st.header(f"Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
cols_ui = st.columns(3)
for i, pos in enumerate(POSITIONS):
    col = cols_ui[i%3]
    info = summary[pos]
    morn = info["morning"]
    aft = info["afternoon"]
    col.markdown(f"### **{pos}**")
    # zobraz R+P alebo samostatne
    if morn["status"] == "R+P OK":
        col.success(f"✅ R+P {pos} OK ({morn['hours']} h)")
    else:
        col.markdown(f"**Ranná:** {morn['status']}  \n**Poobedná:** {aft['status']}  \nR: {morn.get('hours',0)} h | P: {aft.get('hours',0)} h")
    if info["comments"]:
        col.error(" • ".join(info["comments"]))

# Export tlačidlo
st.header("Export dát")
if st.button("Exportuj tento týždeň (Excel)"):
    if df_week.empty:
        st.warning("Žiadne dáta za tento týždeň.")
    else:
        xls = export_df_to_excel_with_hours(df_week)
        st.download_button(
            "Stiahnuť XLSX", 
            data=xls, 
            file_name=f"dochadzka_{monday}.xlsx", 
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
