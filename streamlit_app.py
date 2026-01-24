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

def merge_intervals(pairs):
    intervals = []
    for pair in pairs.values():
        if pd.notna(pair["pr"]) and pd.notna(pair["od"]):
            intervals.append((pair["pr"], pair["od"]))
    if not intervals:
        return []

    intervals.sort(key=lambda x: x[0])
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        gap_min = (start - last_end).total_seconds() / 60
        if gap_min <= SWAP_WINDOW_MINUTES:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged

# ================== FUNKCIA: summarize_position_day s EXTRA ==================
def summarize_position_day(pos_day_df: pd.DataFrame, position):
    morning = {"status": "absent", "hours": 0.0, "detail": None}
    afternoon = {"status": "absent", "hours": 0.0, "detail": None}
    details = []
    extra_rows = []

    if pos_day_df.empty:
        return morning, afternoon, details, extra_rows

    pairs = get_user_pairs(pos_day_df)
    merged = merge_intervals(pairs)
    if not merged:
        return morning, afternoon, details, extra_rows

    total_start = min(s[0] for s in merged)
    total_end = max(s[1] for s in merged)
    total_hours = (total_end - total_start).total_seconds() / 3600

    # standard smena
    if position.lower().startswith("vel"):
        standard_hours = VELITEL_DOUBLE
        extra_label = "EXTRA1"
    else:
        standard_hours = DOUBLE_SHIFT_HOURS if total_hours >= DOUBLE_SHIFT_HOURS else SHIFT_HOURS
        extra_label = "EXTRA2"

    # rozdelenie presahu
    if total_hours > standard_hours + 0.01:
        main_hours = standard_hours
        extra_hours = round(total_hours - standard_hours,2)
    else:
        main_hours = total_hours
        extra_hours = 0.0

    # okná
    morning_window = (datetime.combine(total_start.date(), time(6,0)).replace(tzinfo=total_start.tzinfo),
                      datetime.combine(total_start.date(), time(15,0)).replace(tzinfo=total_start.tzinfo))
    afternoon_window = (datetime.combine(total_start.date(), time(13,0)).replace(tzinfo=total_start.tzinfo),
                        datetime.combine(total_start.date(), time(22,0)).replace(tzinfo=total_start.tzinfo))

    morning_hours = 0.0
    afternoon_hours = 0.0
    for start,end in merged:
        inter_start = max(start, morning_window[0])
        inter_end = min(end, morning_window[1])
        if inter_end > inter_start:
            morning_hours += (inter_end-inter_start).total_seconds()/3600
        inter_start = max(start, afternoon_window[0])
        inter_end = min(end, afternoon_window[1])
        if inter_end > inter_start:
            afternoon_hours += (inter_end-inter_start).total_seconds()/3600

    morning["hours"] = round(morning_hours,2)
    afternoon["hours"] = round(afternoon_hours,2)
    morning["status"] = "Ranna OK" if morning_hours>0 else "absent"
    afternoon["status"] = "Poobedna OK" if afternoon_hours>0 else "absent"
    morning["detail"] = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u,p in pairs.items()])
    afternoon["detail"] = morning["detail"]

    if extra_hours > 0:
        extra_rows.append({
            "position": extra_label,
            "morning_status": "-",
            "morning_hours": 0.0,
            "morning_detail": "-",
            "afternoon_status": "-",
            "afternoon_hours": extra_hours,
            "afternoon_detail": f"Presah {position}",
            "total_hours": extra_hours
        })

    return morning, afternoon, details, extra_rows

# ================== summarize_day ==================
def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    extra_rows_total = []
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"]==pos] if not df_day.empty else pd.DataFrame()
        m,p,details,extra_rows = summarize_position_day(pos_df,pos)
        total = m.get("hours",0)+p.get("hours",0)
        results[pos] = {
            "morning": m,
            "afternoon": p,
            "details": details,
            "total_hours": round(total,2)
        }
        if extra_rows:
            extra_rows_total.extend(extra_rows)
    return results, extra_rows_total

# ================== SAVE ATTENDANCE ==================
def save_attendance(user_code, position, action, now=None):
    user_code = user_code.strip()
    if not now:
        now = datetime.now(tz)
    if now.second == 0 and now.microsecond == 0:
        current = datetime.now(tz)
        now = now.replace(second=current.second, microsecond=current.microsecond)
    ts_str = now.strftime("%Y-%m-%d %H:%M:%S.%f") + "+00"
    databaze.table("attendance").insert({
        "user_code": user_code,
        "position": position,
        "action": action,
        "timestamp": ts_str,
        "valid": True
    }).execute()
    return True

# ================== EXCEL EXPORT ==================
def excel_with_colors(df_matrix, df_day_details, df_raw, monday):
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Týždenný prehľad"
    green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    yellow = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")

    for r in dataframe_to_rows(df_matrix.reset_index().rename(columns={"index":"Pozícia"}), index=False, header=True):
        ws1.append(r)
    for row in ws1.iter_rows(min_row=2, min_col=2, max_col=1+len(df_matrix.columns), max_row=1+len(df_matrix)):
        for cell in row:
            val = cell.value
            if isinstance(val,(int,float)):
                cell.fill = green
            elif isinstance(val,str) and val.strip().startswith("⚠"):
                cell.fill = yellow

    # Denné - detail
    ws2 = wb.create_sheet("Denné - detail")
    for r in dataframe_to_rows(df_day_details, index=False, header=True):
        ws2.append(r)

    # Surové dáta
    ws3 = wb.create_sheet("Surové dáta")
    for r in dataframe_to_rows(df_raw, index=False, header=True):
        ws3.append(r)

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out

# ================== STREAMLIT UI ==================
st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad)")

if "admin_logged" not in st.session_state:
    st.session_state.admin_logged=False
if not st.session_state.admin_logged:
    st.sidebar.header("Admin prihlásenie")
    pw=st.sidebar.text_input("Heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw==ADMIN_PASS:
            st.session_state.admin_logged=True
            st.experimental_rerun()
        else:
            st.sidebar.error("Nesprávne heslo alebo ADMIN_PASS nie je nastavené.")
if not st.session_state.admin_logged:
    st.stop()

today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začína pondelkom):", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday,time(0,0)))
end_dt = tz.localize(datetime.combine(monday+timedelta(days=7),time(0,0)))
df_week = load_attendance(start_dt,end_dt)

default_day = today if monday<=today<=monday+timedelta(days=6) else monday
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=default_day, min_value=monday, max_value=monday+timedelta(days=6))
df_day = df_week[df_week["date"]==selected_day] if not df_week.empty else pd.DataFrame()

if df_week.empty:
    st.warning("Rozsah nie je dostupný v DB (žiadne dáta pre vybraný týždeň).")
else:
    summary, extra_rows = summarize_day(df_day, selected_day)

# --- Denný prehľad ---
st.header(f"✅ Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
cols = st.columns(3)
day_details_rows = []

for i,pos in enumerate(POSITIONS):
    col=cols[i%3]
    info=summary[pos]
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

# Pridanie EXTRA riadkov
day_details_rows.extend(extra_rows)

# --- Týždenný prehľad ---
st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} – {(monday+timedelta(days=6)).strftime('%d.%m.%Y')})")
days = [monday + timedelta(days=i) for i in range(7)]
cols_matrix = [d.strftime("%a %d.%m") for d in days]
matrix = pd.DataFrame(index=POSITIONS, columns=cols_matrix)

for d in days:
    df_d = df_week[df_week["date"]==d] if not df_week.empty else pd.DataFrame()
    summ,_ = summarize_day(df_d,d)
    for pos in POSITIONS:
        matrix.at[pos,d.strftime("%a %d.%m")] = summ[pos]["total_hours"] if summ[pos]["total_hours"]>0 else "—"

matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x,(int,float)) else 0 for x in row),axis=1)
st.dataframe(matrix.fillna("—"), use_container_width=True)

# --- Export Excel ---
if st.button("Exportuj Excel (Farebné)"):
    df_matrix = matrix.reset_index().rename(columns={"index":"position"})
    df_day_details = pd.DataFrame(day_details_rows)
    df_raw = df_week.copy()
    if "timestamp" in df_raw.columns:
        df_raw["timestamp"] = df_raw["timestamp"].apply(lambda x:x.isoformat() if pd.notna(x) else "")
    xls = excel_with_colors(df_matrix, df_day_details, df_raw, monday)
    st.download_button("Stiahnuť XLSX", data=xls, file_name=f"dochadzka_{monday}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
