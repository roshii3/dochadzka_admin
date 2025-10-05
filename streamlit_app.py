# streamlit_app.py
import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO
from openpyxl.styles import PatternFill, Font
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl import Workbook

# ========== CONFIG ==========
st.set_page_config(page_title="Admin - Dochádzka", layout="wide", initial_sidebar_state="expanded")

# hide streamlit header/menu/footer
hide_css = """
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
</style>
"""
st.markdown(hide_css, unsafe_allow_html=True)

# Secrets (musíš nastaviť v Streamlit Cloud alebo env)
DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")

# Supabase client
databaze: Client = create_client(DATABAZA_URL, DATABAZA_KEY)

tz = pytz.timezone("Europe/Bratislava")

POSITIONS = ["Veliteľ","CCTV","Brány","Sklad2","Sklad3","Turniket2","Turniket3","Plombovac2","Plombovac3"]
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 15.25
VELITEL_DOUBLE = 16.25

# ========== HELPERS ==========

def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """Načíta záznamy medzi start_dt (inclusive) a end_dt (exclusive)."""
    res = databaze.table("attendance").select("*").gte("timestamp", start_dt.isoformat()).lt("timestamp", end_dt.isoformat()).execute()
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    # parse timestamps
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    # localize/convert to tz
    try:
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(tz)
        else:
            df["timestamp"] = df["timestamp"].dt.tz_convert(tz)
    except Exception:
        # fallback per-row
        def loc(x):
            if pd.isna(x):
                return x
            if x.tzinfo is None:
                return tz.localize(x)
            return x.astimezone(tz)
        df["timestamp"] = df["timestamp"].apply(loc)
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.time
    return df

def get_user_pairs(pos_day_df: pd.DataFrame):
    """Pre pozíciu v danom dni vráti pre každého user minimalny príchod a maximalny odchod."""
    pairs = {}
    if pos_day_df.empty:
        return pairs
    for user in pos_day_df["user_code"].unique():
        u = pos_day_df[pos_day_df["user_code"] == user]
        pr = u[u["action"].str.lower() == "príchod"]["timestamp"]
        od = u[u["action"].str.lower() == "odchod"]["timestamp"]
        pr_min = pr.min() if not pr.empty else pd.NaT
        od_max = od.max() if not od.empty else pd.NaT
        pairs[user] = {"pr": pr_min, "od": od_max}
    return pairs

def classify_pair(pr, od, position):
    """Klasifikuje pr/od pre jednu osobu na pozícii podľa pravidiel.
       Vracia tuple (role_for_morning, role_for_afternoon, hours_morning, hours_afternoon, detail_msgs)
       role_*: 'Ranna OK', 'Poobedna OK', 'R+P OK', 'missing_pr', 'missing_od', 'invalid', 'none'"""
    msgs = []
    if (pd.isna(pr) or pr is None) and (pd.isna(od) or od is None):
        return ("none","none",0.0,0.0, msgs)
    if pd.isna(pr) or pr is None:
        # only odchod
        msgs.append("missing_prichod")
        # try to decide to which shift the odchod belongs (if late -> afternoon)
        od_t = od.time() if od is not pd.NaT else None
        if od_t and od_t >= time(21,0):
            return ("none","missing_pr",0.0,0.0, msgs)
        else:
            return ("missing_pr","none",0.0,0.0, msgs)
    if pd.isna(od) or od is None:
        # only prichod
        msgs.append("missing_odchod")
        pr_t = pr.time() if pr is not pd.NaT else None
        if pr_t and pr_t < time(13,0):
            return ("missing_od","none",0.0,0.0, msgs)
        else:
            return ("none","missing_od",0.0,0.0, msgs)

    # now both present
    pr_t = pr.time(); od_t = od.time()

    # Veliteľ special: can come early (e.g., 03:00) and leave late => counts as double 16.25
    if position.lower().startswith("vel"):
        # if covers whole day (early pr and late od) -> R+P Veliteľ OK
        if pr_t <= time(7,0) and (od_t >= time(21,0) or od_t < time(2,0)):
            return ("R+P OK","R+P OK", VELITEL_DOUBLE, VELITEL_DOUBLE, msgs)

    # General R+P (single person whole day)
    if pr_t <= time(7,0) and (od_t >= time(21,0) or od_t < time(2,0)):
        return ("R+P OK","R+P OK", DOUBLE_SHIFT_HOURS, DOUBLE_SHIFT_HOURS, msgs)

    # Morning case
    if pr_t <= time(7,0) and od_t <= time(15,0):
        return ("Ranna OK","none", SHIFT_HOURS, 0.0, msgs)

    # Afternoon case
    if pr_t >= time(13,0) and od_t >= time(21,0):
        return ("none","Poobedna OK", 0.0, SHIFT_HOURS, msgs)

    # Overlap or odd times -> consider invalid
    msgs.append("invalid_times")
    return ("invalid","invalid",0.0,0.0, msgs)

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    """Pre pozíciu a deň určí morning + afternoon výsledky + detaily (na zobrazenie chyby)."""
    morning = {"status":"absent","hours":0.0,"detail":None}
    afternoon = {"status":"absent","hours":0.0,"detail":None}
    details = []  # log lines (string)

    if pos_day_df.empty:
        return morning, afternoon, details

    pairs = get_user_pairs(pos_day_df)

    # aggregate: prefer R+P person, else choose any Ranna OK and Poobedna OK
    rp_user = None
    for user, pair in pairs.items():
        role_m, role_p, h_m, h_p, msgs = classify_pair(pair["pr"], pair["od"], position)
        if role_m == "R+P OK" and role_p == "R+P OK":
            rp_user = (user, pair, h_m, h_p)
            break

    if rp_user:
        user, pair, h_m, h_p = rp_user
        morning = {"status":"R+P OK", "hours": h_m, "detail": f"Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        afternoon = {"status":"R+P OK", "hours": h_p, "detail": f"Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        return morning, afternoon, details

    # otherwise look for separate morning/afternoon qualifiers
    for user, pair in pairs.items():
        role_m, role_p, h_m, h_p, msgs = classify_pair(pair["pr"], pair["od"], position)
        if role_m == "Ranna OK":
            # set morning only if not already set with Ranna OK
            if morning["status"] not in ("Ranna OK","R+P OK"):
                morning = {"status":"Ranna OK", "hours": h_m, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if role_p == "Poobedna OK":
            if afternoon["status"] not in ("Poobedna OK","R+P OK"):
                afternoon = {"status":"Poobedna OK", "hours": h_p, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        # collect messages (missing etc)
        if msgs:
            for m in msgs:
                details.append(f"{user}: {m} — pr:{pair['pr']} od:{pair['od']}")

    # if both morning and afternoon OK but by different users -> sum special
    if morning["status"] == "Ranna OK" and afternoon["status"] == "Poobedna OK":
        if position.lower().startswith("vel"):
            # Veliteľ double both present => 16.25 (per day)
            total = VELITEL_DOUBLE
            morning["hours"] = total/2  # for display keep each half? we want to show per shift too; keep as 7.5 but total will be 16.25
            afternoon["hours"] = total/2
            # We'll treat display: show 16.25 in summary row later
        else:
            # non-velitel: if two different people cover morning and afternoon -> total 15.25
            morning["hours"] = SHIFT_HOURS
            afternoon["hours"] = SHIFT_HOURS
    # If missing pr or od found earlier it sits in details

    return morning, afternoon, details

def summarize_day(df_day: pd.DataFrame, target_date: date):
    """Vráti slovník výsledkov pre každú pozíciu."""
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, details = summarize_position_day(pos_df, pos)
        # compute display_total (for matrix / excel)
        if morning["status"] == "R+P OK" and afternoon["status"] == "R+P OK":
            # same person covered whole day -> use double shift hours (or velitel double)
            if pos.lower().startswith("vel"):
                total = VELITEL_DOUBLE
            else:
                total = DOUBLE_SHIFT_HOURS
        elif morning["status"] in ("Ranna OK","R+P OK") and afternoon["status"] in ("Poobedna OK","R+P OK"):
            # both shifts present (possibly different people)
            if pos.lower().startswith("vel"):
                total = VELITEL_DOUBLE
            else:
                total = DOUBLE_SHIFT_HOURS
        else:
            total = morning.get("hours",0.0) + afternoon.get("hours",0.0)

        results[pos] = {
            "morning": morning,
            "afternoon": afternoon,
            "details": details,
            "total_hours": total
        }
    return results

def summarize_week_matrix(df_week: pd.DataFrame, monday: date):
    days = [monday + timedelta(days=i) for i in range(7)]
    cols = [d.strftime("%a %d.%m") for d in days]
    matrix = pd.DataFrame(index=POSITIONS, columns=cols)
    for d in days:
        df_d = df_week[df_week["date"] == d]
        summ = summarize_day(df_d, d)
        for pos in POSITIONS:
            matrix.at[pos, d.strftime("%a %d.%m")] = summ[pos]["total_hours"] if summ[pos]["total_hours"]>0 else "—"
    # add weekly sum
    matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x,(int,float)) else 0 for x in row), axis=1)
    return matrix

# Excel export with colors
def excel_with_colors(df_matrix: pd.DataFrame, df_day_details: pd.DataFrame, df_raw: pd.DataFrame, monday: date) -> BytesIO:
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Týždenný prehľad"

    # Style fills
    green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    red = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    yellow = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")

    # write matrix
    for r in dataframe_to_rows(df_matrix.reset_index().rename(columns={"index":"Pozícia"}), index=False, header=True):
        ws1.append(r)
    # color cells based on value (if numeric >0 -> green, if '—' -> no fill)
    for row in ws1.iter_rows(min_row=2, min_col=2, max_col=1+len(df_matrix.columns), max_row=1+len(df_matrix)):
        for cell in row:
            val = cell.value
            if isinstance(val,(int,float)):
                cell.fill = green
            elif isinstance(val,str) and val.strip().startswith("⚠"):
                cell.fill = yellow
            elif val == "—":
                pass

    # sheet 2: daily details (df_day_details)
    ws2 = wb.create_sheet("Denné - detail")
    for r in dataframe_to_rows(df_day_details, index=False, header=True):
        ws2.append(r)
    # color rows in details by status column if exists
    status_col_idx = None
    headers = list(df_day_details.columns)
    if "status" in headers:
        status_col_idx = headers.index("status") + 1
    if status_col_idx:
        for row in ws2.iter_rows(min_row=2, max_row=1+len(df_day_details), min_col=1, max_col=len(df_day_details.columns)):
            s = row[status_col_idx-1].value
            if s and "OK" in str(s):
                for c in row:
                    c.fill = green
            elif s and ("missing" in str(s) or "bez" in str(s) or "chybn" in str(s).lower()):
                for c in row:
                    c.fill = red

    # sheet 3: raw data
    ws3 = wb.create_sheet("Surové dáta")
    for r in dataframe_to_rows(df_raw, index=False, header=True):
        ws3.append(r)

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out

# ========== UI / App logic ==========
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad)")

# Simple admin login
if "admin_logged" not in st.session_state:
    st.session_state.admin_logged = False
if not st.session_state.admin_logged:
    st.sidebar.header("Admin prihlásenie")
    pw = st.sidebar.text_input("Heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw == ADMIN_PASS:
            st.session_state.admin_logged = True
            st.experimental_rerun()
        else:
            st.sidebar.error("Nesprávne heslo alebo ADMIN_PASS nie je nastavené.")
if not st.session_state.admin_logged:
    st.stop()

# Week selection controls
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začína pondelkom):", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = datetime.combine(monday, time(0,0))
end_dt = start_dt + timedelta(days=7)
start_dt = tz.localize(start_dt)
end_dt = tz.localize(end_dt)

df_week = load_attendance(start_dt, end_dt)

# chose day for daily view
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=today, min_value=monday, max_value=monday+timedelta(days=6))
df_day = df_week[df_week["date"] == selected_day]

# If df_day empty -> friendly message
if df_week.empty:
    st.warning("Rozsah nie je dostupný v DB (žiadne dáta pre vybraný týždeň).")
else:
    # summarize selected day
    summary = summarize_day(df_day, selected_day)

    st.header(f"✅ Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
    cols = st.columns(3)
    day_details_rows = []
    for i, pos in enumerate(POSITIONS):
        col = cols[i % 3]
        info = summary[pos]
        m = info["morning"]
        p = info["afternoon"]
        # create readable status & detail strings
        m_status = m["status"]
        a_status = p["status"]
        m_detail = m.get("detail") or "-"
        a_detail = p.get("detail") or "-"
        col.markdown(f"### **{pos}**")
        col.markdown(f"**Ranná:** {m_status} — {m.get('hours',0)} h")
        if m_detail:
            col.caption(f"Detail: {m_detail}")
        col.markdown(f"**Poobedná:** {a_status} — {p.get('hours',0)} h")
        if a_detail:
            col.caption(f"Detail: {a_detail}")
        if info["details"]:
            for d in info["details"]:
                col.error(d)
        # collect for excel sheet
        day_details_rows.append({
            "position": pos,
            "morning_status": m_status,
            "morning_hours": m.get("hours",0),
            "morning_detail": m_detail,
            "afternoon_status": a_status,
            "afternoon_hours": p.get("hours",0),
            "afternoon_detail": a_detail,
            "total_hours": info["total_hours"]
        })

    # weekly matrix
    st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} – {(monday+timedelta(days=6)).strftime('%d.%m.%Y')})")
    matrix = summarize_week_matrix(df_week, monday)
    st.dataframe(matrix.fillna("—"), use_container_width=True)

    # Export to Excel (3 sheets: weekly matrix, daily details, raw)
    if st.button("Exportuj Excel (Farebné)"):
        df_matrix = matrix.reset_index().rename(columns={"index":"position"})
        df_day_details = pd.DataFrame(day_details_rows)
        df_raw = df_week.copy()
        # remove tz info for raw for export if present
        if "timestamp" in df_raw.columns:
            df_raw["timestamp"] = df_raw["timestamp"].apply(lambda x: x.isoformat() if pd.notna(x) else "")
        xls = excel_with_colors(df_matrix, df_day_details, df_raw, monday)
        st.download_button("Stiahnuť XLSX", data=xls, file_name=f"dochadzka_{monday}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
