import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Alignment
from openpyxl.utils.dataframe import dataframe_to_rows

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

# ================== KONŠTANTY ==================
POSITIONS = [
    "Veliteľ", "CCTV", "Brány", "Sklad2", "Sklad3",
    "Turniket2", "Turniket3", "Plombovac2", "Plombovac3"
]
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 15.25
VELITEL_DOUBLE = 16.25
SWAP_WINDOW_MINUTES = 30

# ================== HELPERS ==================
def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    res = (
        databaze.table("attendance")
        .select("*")
        .gte("timestamp", start_dt.isoformat())
        .lt("timestamp", end_dt.isoformat())
        .execute()
    )
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
        return ("none", "none", 0.0, 0.0, msgs)
    if pd.isna(pr) or pr is None:
        msgs.append("missing_prichod")
        return ("missing_pr", "none", 0.0, 0.0, msgs)
    if pd.isna(od) or od is None:
        msgs.append("missing_odchod")
        return ("none", "missing_od", 0.0, 0.0, msgs)

    pr_t = pr.time()
    od_t = od.time()

    # Veliteľ má špeciálne hodiny
    if position.lower().startswith("vel"):
        if pr_t <= time(7, 0) and (od_t >= time(21, 0) or od_t < time(2, 0)):
            return ("R+P OK", "R+P OK", VELITEL_DOUBLE, VELITEL_DOUBLE, msgs)

    # Dvojitá smena (non-veliteľ)
    if pr_t <= time(7, 0) and (od_t >= time(21, 0) or od_t < time(2, 0)):
        return ("R+P OK", "R+P OK", DOUBLE_SHIFT_HOURS, DOUBLE_SHIFT_HOURS, msgs)

    # Ranná
    if pr_t <= time(7, 0) and od_t <= time(15, 0):
        return ("Ranna OK", "none", SHIFT_HOURS, 0.0, msgs)

    # Poobedná
    if pr_t >= time(13, 0) and od_t >= time(21, 0):
        return ("none", "Poobedna OK", 0.0, SHIFT_HOURS, msgs)

    msgs.append("invalid_times")
    return ("invalid", "invalid", 0.0, 0.0, msgs)

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    morning = {"status": "absent", "hours": 0.0, "detail": None}
    afternoon = {"status": "absent", "hours": 0.0, "detail": None}
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
        morning = {"status": "R+P OK", "hours": h_m, "detail": f"Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        afternoon = {"status": "R+P OK", "hours": h_p, "detail": f"Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        return morning, afternoon, details

    for user, pair in pairs.items():
        role_m, role_p, h_m, h_p, msgs = classify_pair(pair["pr"], pair["od"], position)
        if role_m == "Ranna OK" and morning["status"] not in ("Ranna OK", "R+P OK"):
            morning = {"status": "Ranna OK", "hours": h_m, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if role_p == "Poobedna OK" and afternoon["status"] not in ("Poobedna OK", "R+P OK"):
            afternoon = {"status": "Poobedna OK", "hours": h_p, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if msgs:
            for m in msgs:
                details.append(f"{user}: {m} — pr:{pair['pr']} od:{pair['od']}")

    return morning, afternoon, details

def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, details = summarize_position_day(pos_df, pos)
        total = morning.get("hours", 0.0) + afternoon.get("hours", 0.0)
        results[pos] = {
            "morning": morning,
            "afternoon": afternoon,
            "details": details,
            "total_hours": round(total, 2)
        }
    return results

# ================== STREAMLIT UI ==================
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad)")

# --- Výber týždňa a dňa ---
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začína pondelkom):", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday, time(0, 0)))
end_dt = tz.localize(datetime.combine(monday + timedelta(days=7), time(0, 0)))
df_week = load_attendance(start_dt, end_dt)

default_day = today if monday <= today <= monday + timedelta(days=6) else monday
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=default_day, min_value=monday, max_value=monday + timedelta(days=6))
df_day = df_week[df_week["date"] == selected_day] if not df_week.empty else pd.DataFrame()

# ================== Denný prehľad ==================
st.header(f"✅ Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
if not df_day.empty:
    summary = summarize_day(df_day, selected_day)
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
            "morning_hours": m.get('hours', 0),
            "morning_detail": m.get('detail') or "-",
            "afternoon_status": p['status'],
            "afternoon_hours": p.get('hours', 0),
            "afternoon_detail": p.get('detail') or "-",
            "total_hours": info['total_hours']
        })
    df_day_summary = pd.DataFrame(day_details_rows)
else:
    st.warning("Žiadne dáta pre vybraný deň.")
    df_day_summary = pd.DataFrame()

# ================== Týždenný prehľad ==================
st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} – {(monday + timedelta(days=6)).strftime('%d.%m.%Y')})")
days = [monday + timedelta(days=i) for i in range(7)]
cols_matrix = [d.strftime("%a %d.%m") for d in days]
matrix = pd.DataFrame(index=POSITIONS, columns=cols_matrix)

for d in days:
    df_d = df_week[df_week["date"] == d] if not df_week.empty else pd.DataFrame()
    summ = summarize_day(df_d, d)
    for pos in POSITIONS:
        matrix.at[pos, d.strftime("%a %d.%m")] = summ[pos]["total_hours"] if summ[pos]["total_hours"] > 0 else 0

# Pridanie EXTRA1 a EXTRA2 pod Plombovac3 (0 hodín)
matrix_extra = pd.DataFrame(index=["EXTRA1", "EXTRA2"], columns=matrix.columns)
matrix_extra = matrix_extra.fillna(0)
matrix = pd.concat([matrix, matrix_extra])
matrix["Spolu"] = matrix.sum(axis=1)

st.dataframe(matrix.fillna(0), use_container_width=True)

# ================== EXPORT DO EXCELU ==================
def export_excel(df_day_summary, df_week_matrix):
    wb = Workbook()
    
    # Denný prehľad
    ws1 = wb.active
    ws1.title = "Denný prehľad"
    for r in dataframe_to_rows(df_day_summary, index=False, header=True):
        ws1.append(r)
    # jednoduché formátovanie
    for cell in ws1[1]:
        cell.fill = PatternFill("solid", fgColor="BDD7EE")
        cell.alignment = Alignment(horizontal="center")
    
    # Týždenný prehľad
    ws2 = wb.create_sheet("Týždenný prehľad")
    df_week_matrix_reset = df_week_matrix.reset_index().rename(columns={"index": "Pozícia"})
    for r in dataframe_to_rows(df_week_matrix_reset, index=False, header=True):
        ws2.append(r)
    for cell in ws2[1]:
        cell.fill = PatternFill("solid", fgColor="BDD7EE")
        cell.alignment = Alignment(horizontal="center")
    
    # Export do bytu
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio

excel_data = export_excel(df_day_summary, matrix)
st.download_button(
    label="⬇️ Export do Excelu",
    data=excel_data,
    file_name=f"dochadzka_{monday.strftime('%d%m%Y')}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
