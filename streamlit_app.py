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

    if position.lower().startswith("vel"):
        if pr_t <= time(7, 0) and (od_t >= time(21, 0) or od_t < time(2, 0)):
            return ("R+P OK", "R+P OK", VELITEL_DOUBLE, VELITEL_DOUBLE, msgs)
    if pr_t <= time(7, 0) and (od_t >= time(21, 0) or od_t < time(2, 0)):
        return ("R+P OK", "R+P OK", DOUBLE_SHIFT_HOURS, DOUBLE_SHIFT_HOURS, msgs)
    if pr_t <= time(7, 0) and od_t <= time(15, 0):
        return ("Ranna OK", "none", SHIFT_HOURS, 0.0, msgs)
    if pr_t >= time(13, 0) and od_t >= time(21, 0):
        return ("none", "Poobedna OK", 0.0, SHIFT_HOURS, msgs)

    msgs.append("invalid_times")
    return ("invalid", "invalid", 0.0, 0.0, msgs)

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

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    morning = {"status": "absent", "hours": 0.0, "detail": None}
    afternoon = {"status": "absent", "hours": 0.0, "detail": None}
    details = []

    if pos_day_df.empty:
        return morning, afternoon, details

    pairs = get_user_pairs(pos_day_df)

    weekday = pos_day_df["timestamp"].dt.weekday.iloc[0]  # 0=pondelok, 5=sobota, 6=nedeľa
    if weekday in (5, 6):  # Sobota alebo Nedeľa
        pr_list = [p["pr"] for p in pairs.values() if pd.notna(p["pr"])]
        od_list = [p["od"] for p in pairs.values() if pd.notna(p["od"])]
        if pr_list and od_list:
            earliest_pr = min(pr_list)
            latest_od = max(od_list)
            start_time = max(earliest_pr, datetime.combine(earliest_pr.date(), time(6,0)).replace(tzinfo=earliest_pr.tzinfo))
            end_time = latest_od
            total_hours = round((end_time - start_time).total_seconds() / 3600, 2)
            detail_str = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u, p in pairs.items()])
            morning = {"status": "Obsadené", "hours": total_hours, "detail": detail_str}
            return morning, afternoon, details

    # Pondelok–Piatok logika
    for user, pair in pairs.items():
        role_m, role_p, h_m, h_p, msgs = classify_pair(pair["pr"], pair["od"], position)
        if role_m == "Ranna OK" and morning["status"] not in ("Ranna OK", "R+P OK"):
            morning = {"status": "Ranna OK", "hours": h_m, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if role_p == "Poobedna OK" and afternoon["status"] not in ("Poobedna OK", "R+P OK"):
            afternoon = {"status": "Poobedna OK", "hours": h_p, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if msgs:
            for m in msgs:
                details.append(f"{user}: {m} — pr:{pair['pr']} od:{pair['od']}")

    merged = merge_intervals(pairs)
    total_hours = round(sum((end - start).total_seconds() / 3600 for start, end in merged), 2) if merged else 0.0

    morning_hours = 0.0
    afternoon_hours = 0.0
    for start, end in merged:
        morning_window_start = datetime.combine(start.date(), time(6,0)).replace(tzinfo=start.tzinfo)
        morning_window_end = datetime.combine(start.date(), time(15,0)).replace(tzinfo=start.tzinfo)
        afternoon_window_start = datetime.combine(start.date(), time(13,0)).replace(tzinfo=start.tzinfo)
        afternoon_window_end = datetime.combine(start.date(), time(22,0)).replace(tzinfo=start.tzinfo)

        inter_start = max(start, morning_window_start)
        inter_end = min(end, morning_window_end)
        if inter_end > inter_start:
            morning_hours += (inter_end - inter_start).total_seconds() / 3600

        inter_start = max(start, afternoon_window_start)
        inter_end = min(end, afternoon_window_end)
        if inter_end > inter_start:
            afternoon_hours += (inter_end - inter_start).total_seconds() / 3600

    morning_hours = round(morning_hours, 2)
    afternoon_hours = round(afternoon_hours, 2)

    if morning_hours > 0:
        morning["status"] = "Čiastočná"
        morning["hours"] = morning_hours
        morning["detail"] = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u, p in pairs.items()])
    if afternoon_hours > 0:
        afternoon["status"] = "Čiastočná"
        afternoon["hours"] = afternoon_hours
        afternoon["detail"] = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u, p in pairs.items()])

    if morning_hours == 0 and afternoon_hours == 0:
        morning["status"] = "absent"
        morning["hours"] = total_hours
        morning["detail"] = " + ".join([f"{u}: {p['pr']}–{p['od']}" for u, p in pairs.items()])

    return morning, afternoon, details

# ================== UI & LOGIKA ==================
# Tu vložíš pôvodný Streamlit UI kód, načítanie dát, exporty, výber týždňa, pozícií atď.
# Funkcia summarize_position_day sa volá pri generovaní reportu.




# ================== STREAMLIT UI ==================
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
week_ref = st.sidebar.date_input(
    "Vyber deň v týždni (týždeň začína pondelkom):",
    value=today
)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday, time(0, 0)))
end_dt = tz.localize(datetime.combine(monday + timedelta(days=7), time(0, 0)))
df_week = load_attendance(start_dt, end_dt)

# 🔧 Prednastavenie denného výberu
default_day = today if monday <= today <= monday + timedelta(days=6) else monday
selected_day = st.sidebar.date_input(
    "Denný prehľad - vyber deň",
    value=default_day,
    min_value=monday,
    max_value=monday + timedelta(days=6)
)
df_day = df_week[df_week["date"] == selected_day]

if df_week.empty:
    st.warning("Rozsah nie je dostupný v DB (žiadne dáta pre vybraný týždeň).")
else:
    summary = summarize_day(df_day, selected_day)

# ================== Denný prehľad zobrazenie ==================
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
        "morning_hours": m.get('hours', 0),
        "morning_detail": m.get('detail') or "-",
        "afternoon_status": p['status'],
        "afternoon_hours": p.get('hours', 0),
        "afternoon_detail": p.get('detail') or "-",
        "total_hours": info['total_hours']
    })

    # ak ide o minulý deň, zobrazíme formuláre na doplnenie chýbajúcich záznamov
    if selected_day < today and info["details"]:
        for idx, d in enumerate(info["details"]):
            if "missing_prichod" in d:
                st.markdown(f"#### Doplniť chýbajúci PRÍCHOD pre pozíciu {pos}")
                user_code = st.text_input(f"User code ({pos})", value="USER123456", key=f"{pos}_prichod_user_{idx}")
                hour = st.select_slider("Hodina", options=list(range(6, 23, 1)), key=f"{pos}_prichod_hour_{idx}")
                minute = st.select_slider("Minúta", options=[0, 15, 30, 45], key=f"{pos}_prichod_minute_{idx}")
                if st.button(f"Uložiť príchod ({pos})", key=f"{pos}_prichod_save_{idx}"):
                    ts = tz.localize(datetime.combine(selected_day, time(hour, minute)))
                    save_attendance(user_code, pos, "Príchod", ts)
                    st.success("Záznam uložený ✅")
                    st.experimental_rerun()
            if "missing_odchod" in d:
                st.markdown(f"#### Doplniť chýbajúci ODCHOD pre pozíciu {pos}")
                user_code = st.text_input(f"User code ({pos})", value="USER123456", key=f"{pos}_odchod_user_{idx}")
                hour = st.select_slider("Hodina", options=list(range(6, 23, 1)), key=f"{pos}_odchod_hour_{idx}")
                minute = st.select_slider("Minúta", options=[0, 15, 30, 45], key=f"{pos}_odchod_minute_{idx}")
                if st.button(f"Uložiť odchod ({pos})", key=f"{pos}_odchod_save_{idx}"):
                    ts = tz.localize(datetime.combine(selected_day, time(hour, minute)))
                    save_attendance(user_code, pos, "Odchod", ts)
                    st.success("Záznam uložený ✅")
                    st.experimental_rerun()

# ================== Týždenný prehľad ==================
st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} – {(monday + timedelta(days=6)).strftime('%d.%m.%Y')})")
days = [monday + timedelta(days=i) for i in range(7)]
cols_matrix = [d.strftime("%a %d.%m") for d in days]
matrix = pd.DataFrame(index=POSITIONS, columns=cols_matrix)

for d in days:
    df_d = df_week[df_week["date"] == d]
    summ = summarize_day(df_d, d)
    for pos in POSITIONS:
        matrix.at[pos, d.strftime("%a %d.%m")] = summ[pos]["total_hours"] if summ[pos]["total_hours"] > 0 else "—"

matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x, (int, float)) else 0 for x in row), axis=1)
st.dataframe(matrix.fillna("—"), use_container_width=True)

# ================== Export Excel ==================
if st.button("Exportuj Excel (Farebné)"):
    df_matrix = matrix.reset_index().rename(columns={"index": "position"})
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

# --- dvojtýždňová kontrola duplicít (voliteľné zobrazenie) ---
start_2w = today - timedelta(days=7)
start_dt_2w = tz.localize(datetime.combine(start_2w, time(0, 0)))
end_dt_2w = tz.localize(datetime.combine(today + timedelta(days=1), time(0, 0)))
df_2w = load_attendance(start_dt_2w, end_dt_2w)

df_2w_summary = []
for pos in POSITIONS:
    pos_df = df_2w[df_2w["position"] == pos] if not df_2w.empty else pd.DataFrame()
    pairs = get_user_pairs(pos_df)
    for user, pair in pairs.items():
        pr_count = pair["pr_count"]
        od_count = pair["od_count"]
        if pr_count != 1 or od_count != 1:
            df_2w_summary.append({
                "position": pos,
                "user_code": user,
                "pr_count": pr_count,
                "od_count": od_count,
                "first_pr": pair["pr"],
                "last_od": pair["od"]
            })
# --- posledných 5 dní (okrem dnes) ---
start_5d = today - timedelta(days=5)
days_5d = [start_5d + timedelta(days=i) for i in range(5)]

st.subheader("📝 Doplnkové smeny za posledných 5 dní")

for day in days_5d:
    st.markdown(f"### 📅 {day.strftime('%A %d.%m.%Y')}")
    df_day = df_week[df_week["date"] == day] if not df_week.empty else pd.DataFrame()
    summary = summarize_day(df_day, day)

    for pos in POSITIONS:
        morning = summary[pos]["morning"]
        afternoon = summary[pos]["afternoon"]

        # ======== Doplniť rannú smenu ========
        if morning["status"] not in ("Ranna OK", "R+P OK"):
            st.markdown(f"#### 🌅 Doplniť rannú smenu — {pos}")
            user_code_m = st.text_input(
                f"Zadaj čip pre rannú ({pos}, {day})",
                key=f"user_m_{pos}_{day}"
            )
            if st.button(f"💾 Uložiť rannú — {pos} ({day})", key=f"{pos}_morning_btn_{day}"):
                if not user_code_m.strip():
                    st.warning("⚠️ Zadaj čip používateľa!")
                else:
                    ts_pr = tz.localize(datetime.combine(day, time(6, 0, 0, 123456)))
                    ts_od = tz.localize(datetime.combine(day, time(14, 0, 0, 654321)))
                    save_attendance(user_code_m, pos, "Príchod", ts_pr)
                    save_attendance(user_code_m, pos, "Odchod", ts_od)
                    st.success(f"Ranná smena pre {pos} uložená ✅")
                    st.experimental_rerun()

        # ======== Doplniť poobednú smenu ========
        if afternoon["status"] not in ("Poobedna OK", "R+P OK"):
            st.markdown(f"#### 🌇 Doplniť poobednú smenu — {pos}")
            user_code_p = st.text_input(
                f"Zadaj čip pre poobednú ({pos}, {day})",
                key=f"user_p_{pos}_{day}"
            )
            if st.button(f"💾 Uložiť poobednú — {pos} ({day})", key=f"{pos}_afternoon_btn_{day}"):
                if not user_code_p.strip():
                    st.warning("⚠️ Zadaj čip používateľa!")
                else:
                    ts_pr = tz.localize(datetime.combine(day, time(14, 0, 0, 234567)))
                    ts_od = tz.localize(datetime.combine(day, time(22, 0, 0, 987654)))
                    save_attendance(user_code_p, pos, "Príchod", ts_pr)
                    save_attendance(user_code_p, pos, "Odchod", ts_od)
                    st.success(f"Poobedná smena pre {pos} uložená ✅")
                    st.experimental_rerun()


