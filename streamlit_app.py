import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO
from openpyxl.styles import PatternFill
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter

# ================== CONFIG ==================
st.set_page_config(
    page_title="Admin - Dochádzka",
    layout="wide",
    initial_sidebar_state="expanded"
)

hide_css = """
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}
</style>
"""
st.markdown(hide_css, unsafe_allow_html=True)

# ================== SECRETS ==================
DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")
databaze: Client = create_client(DATABAZA_URL, DATABAZA_KEY)
tz = pytz.timezone("Europe/Bratislava")

POSITIONS = [
    "Veliteľ","CCTV","Brány","Sklad2","Sklad3",
    "Turniket2","Turniket3","Plombovac2","Plombovac3"
]
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 15.25
VELITEL_DOUBLE = 16.25

# ================== HELPERS ==================
def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    res = databaze.table("attendance").select("*")\
        .gte("timestamp", start_dt.isoformat())\
        .lt("timestamp", end_dt.isoformat()).execute()
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["timestamp"] = df["timestamp"].apply(
        lambda x: tz.localize(x) if pd.notna(x) and x.tzinfo is None else x
    )
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.time
    return df

def get_user_pairs(pos_day_df: pd.DataFrame):
    pairs = {}
    if pos_day_df.empty: 
        return pairs
    for user in pos_day_df["user_code"].unique():
        u = pos_day_df[pos_day_df["user_code"] == user]
        pr = u[u["action"].str.lower() == "príchod"]["timestamp"]
        od = u[u["action"].str.lower() == "odchod"]["timestamp"]
        pr_min = pr.min() if not pr.empty else pd.NaT
        od_max = od.max() if not od.empty else pd.NaT
        pairs[user] = {"pr": pr_min, "od": od_max, "pr_count": len(pr), "od_count": len(od)}
    return pairs

def classify_pair(pr, od, position):
    msgs = []
    if (pd.isna(pr) or pr is None) and (pd.isna(od) or od is None):
        return ("none","none",0.0,0.0, msgs)
    if pd.isna(pr) or pr is None:
        msgs.append("missing_prichod")
        return ("missing_pr", "none",0.0,0.0, msgs)
    if pd.isna(od) or od is None:
        msgs.append("missing_odchod")
        return ("none","missing_od",0.0,0.0, msgs)
    
    pr_t = pr.time()
    od_t = od.time()
    
    if position.lower().startswith("vel"):
        if pr_t <= time(7,0) and (od_t >= time(21,0) or od_t < time(2,0)):
            return ("R+P OK","R+P OK", VELITEL_DOUBLE, VELITEL_DOUBLE, msgs)
    
    if pr_t <= time(7,0) and od_t <= time(15,0):
        return ("Ranna OK","none", SHIFT_HOURS,0.0,msgs)
    if pr_t >= time(13,0) and od_t >= time(21,0):
        return ("none","Poobedna OK",0.0, SHIFT_HOURS,msgs)
    
    msgs.append("invalid_times")
    return ("invalid","invalid",0.0,0.0,msgs)

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    morning = {"status":"absent","hours":0.0,"detail":None}
    afternoon = {"status":"absent","hours":0.0,"detail":None}
    details = []
    
    if pos_day_df.empty:
        return morning, afternoon, details
    
    pairs = get_user_pairs(pos_day_df)
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
    
    for user, pair in pairs.items():
        role_m, role_p, h_m, h_p, msgs = classify_pair(pair["pr"], pair["od"], position)
        if role_m == "Ranna OK" and morning["status"] not in ("Ranna OK","R+P OK"):
            morning = {"status":"Ranna OK", "hours": h_m, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if role_p == "Poobedna OK" and afternoon["status"] not in ("Poobedna OK","R+P OK"):
            afternoon = {"status":"Poobedna OK", "hours": h_p, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if msgs:
            for m in msgs:
                details.append(f"{user}: {m} — pr:{pair['pr']} od:{pair['od']}")
    
    return morning, afternoon, details

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, details = summarize_position_day(pos_df, pos)
        if morning["status"] == "R+P OK" and afternoon["status"] == "R+P OK":
            total = VELITEL_DOUBLE if pos.lower().startswith("vel") else DOUBLE_SHIFT_HOURS
        elif morning["status"] in ("Ranna OK","R+P OK") and afternoon["status"] in ("Poobedna OK","R+P OK"):
            total = VELITEL_DOUBLE if pos.lower().startswith("vel") else DOUBLE_SHIFT_HOURS
        else:
            total = morning.get("hours",0.0) + afternoon.get("hours",0.0)
        results[pos] = {
            "morning": morning,
            "afternoon": afternoon,
            "details": details,
            "total_hours": total
        }
    return results

def save_attendance(user_code, position, action, now=None):
    user_code = user_code.strip()
    if not now:
        now = datetime.now(tz)
        if now.second == 0 and now.microsecond == 0:
            current = datetime.now(tz)
            now = now.replace(second=current.second, microsecond=current.microsecond)
    ts_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "+00"
    databaze.table("attendance").insert({
        "user_code": user_code,
        "position": position,
        "action": action,
        "timestamp": ts_str,
        "valid": True
    }).execute()
    return True

# ================== EXCEL EXPORT ==================
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Alignment
from openpyxl.utils.dataframe import dataframe_to_rows
from datetime import timedelta, time
from io import BytesIO
import pandas as pd


def get_chip_assignments(df_raw: pd.DataFrame, monday):
    """
    Vygeneruje mapovanie (pozícia, smena, deň) -> [user_codes].
    """
    assignments = {}
    if df_raw.empty:
        return assignments

    df_raw["timestamp"] = pd.to_datetime(df_raw["timestamp"], errors="coerce")
    df_raw["date"] = df_raw["timestamp"].dt.date

    for pos in df_raw["position"].unique():
        pos_df = df_raw[df_raw["position"] == pos]
        for i in range(7):
            d = monday + timedelta(days=i)
            day_df = pos_df[pos_df["date"] == d]
            if day_df.empty:
                continue
            pairs = get_user_pairs(day_df)
            for user, pair in pairs.items():
                if pd.isna(pair["pr"]) or pd.isna(pair["od"]):
                    continue
                pr_t = pair["pr"].time()
                od_t = pair["od"].time()

                # Ranná
                if pr_t <= time(7, 0) and od_t <= time(15, 0):
                    shift = "06:00-14_00"
                # Poobedná
                elif pr_t >= time(13, 0) and od_t >= time(21, 0):
                    shift = "14:00-22:00"
                # Dvojitá
                elif pr_t <= time(7, 0) and (od_t >= time(21, 0) or od_t < time(2, 0)):
                    assignments[(pos, "06:00-14_00", i)] = assignments.get((pos, "06:00-14_00", i), []) + [user]
                    assignments[(pos, "14:00-22:00", i)] = assignments.get((pos, "14:00-22:00", i), []) + [user]
                    continue
                else:
                    continue

                assignments[(pos, shift, i)] = assignments.get((pos, shift, i), []) + [user]
    return assignments



def excel_with_colors(df_matrix, df_day_details, df_raw, monday):
    """
    Vytvorí farebný Excel so 4 sheetmi:
    - Týždenný prehľad
    - Denné - detail
    - Surové dáta
    - Rozpis čipov
    """
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Týždenný prehľad"
    green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    yellow = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")

    # === SHEET 1: Týždenný prehľad ===
    for r in dataframe_to_rows(df_matrix.reset_index().rename(columns={"index": "Pozícia"}), index=False, header=True):
        ws1.append(r)

    for row in ws1.iter_rows(min_row=2, min_col=2, max_col=1 + len(df_matrix.columns), max_row=1 + len(df_matrix)):
        for cell in row:
            val = cell.value
            if isinstance(val, (int, float)):
                cell.fill = green
            elif isinstance(val, str) and val.strip().startswith("⚠"):
                cell.fill = yellow

    # === SHEET 2: Denné - detail ===
    ws2 = wb.create_sheet("Denné - detail")
    for r in dataframe_to_rows(df_day_details, index=False, header=True):
        ws2.append(r)

    # === SHEET 3: Surové dáta ===
    ws3 = wb.create_sheet("Surové dáta")
    for r in dataframe_to_rows(df_raw, index=False, header=True):
        ws3.append(r)

    # === SHEET 4: Rozpis čipov ===
    ws4 = wb.create_sheet("Rozpis čipov")
    days = ["pondelok", "utorok", "streda", "štvrtok", "piatok", "sobota", "nedeľa"]
    header = ["position", "shift"] + days
    ws4.append(header)

    chip_map = get_chip_assignments(df_raw, monday)
    POSITIONS = sorted(df_raw["position"].unique())

    for pos in POSITIONS:
        for shift in ["06:00-14_00", "14:00-22:00"]:
            row_vals = []
            for i in range(7):
                users = chip_map.get((pos, shift, i), [])
                row_vals.append(", ".join(users) if users else "")
            ws4.append([pos, shift] + row_vals)

    for col in ws4.columns:
        for cell in col:
            cell.alignment = Alignment(horizontal="center", vertical="center")

    # --- Uloženie ---
    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out


# ================== STREAMLIT APP ==================
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad)")

# --- Login ---
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

# --- Výber týždňa a dňa ---
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začína pondelkom):", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday, time(0,0)))
end_dt = tz.localize(datetime.combine(monday + timedelta(days=7), time(0,0)))
df_week = load_attendance(start_dt, end_dt)

# 🔧 Denný prehľad
selected_day = st.sidebar.date_input(
    "Denný prehľad - vyber deň",
    value=today if monday <= today <= monday + timedelta(days=6) else monday,
    min_value=monday,
    max_value=monday + timedelta(days=6)
)
df_day = df_week[df_week["date"] == selected_day]
summary = summarize_day(df_day, selected_day)

# ================== Zobrazenie denného prehľadu ==================
st.header(f"✅ Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
cols = st.columns(3)
day_details_rows = []
for i, pos in enumerate(POSITIONS):
    col = cols[i % 3]
    info = summary[pos]
    m = info["morning"]
    p = info["afternoon"]
    col.markdown(f"### **{pos}**")
    col.markdown(f"**Ranná:** {m['status']} — {m['hours']} h")
    col.markdown(f"**Poobedná:** {p['status']} — {p['hours']} h")
    if info["details"]:
        for d in info["details"]:
            col.error(d)
    day_details_rows.append({
        "position": pos,
        "morning_status": m['status'],
        "morning_hours": m.get('hours',0),
        "morning_detail": m.get('detail') or "-",
        "afternoon_status": p['status'],
        "afternoon_hours": p.get('hours',0),
        "afternoon_detail": p.get('detail') or "-",
        "total_hours": info['total_hours']
    })

# ================== Týždenný prehľad ==================
st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} – {(monday+timedelta(days=6)).strftime('%d.%m.%Y')})")
days = [monday + timedelta(days=i) for i in range(7)]
cols_matrix = [d.strftime("%a %d.%m") for d in days]
matrix = pd.DataFrame(index=POSITIONS, columns=cols_matrix)
for d in days:
    df_d = df_week[df_week["date"] == d]
    summ = summarize_day(df_d, d)
    for pos in POSITIONS:
        matrix.at[pos, d.strftime("%a %d.%m")] = summ[pos]["total_hours"] if summ[pos]["total_hours"]>0 else "—"
matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x,(int,float)) else 0 for x in row), axis=1)
st.dataframe(matrix.fillna("—"), use_container_width=True)

# ================== Export Excel ==================
if st.button("Exportuj Excel (Farebné)"):
    df_matrix = matrix.reset_index().rename(columns={"index":"position"})
    df_day_details = pd.DataFrame(day_details_rows)
    df_raw = df_week.copy()
    if "timestamp" in df_raw.columns:
        df_raw["timestamp"] = df_raw["timestamp"].apply(lambda x: x.isoformat() if pd.notna(x) else "")
    xls = excel_with_colors(df_matrix, df_day_details, df_raw, monday)
    st.download_button(
        "Stiahnuť XLSX",
        data=xls,
        file_name=f"dochadzka_{monday}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
