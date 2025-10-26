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
WEEKEND_SHIFT_HOURS = 6.0
SWAP_WINDOW_MINUTES = 30  # minúty na merge intervaly

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

def classify_pair(pr, od, position, is_weekend=False):
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

    shift_hours = WEEKEND_SHIFT_HOURS if is_weekend else SHIFT_HOURS
    double_shift_hours = DOUBLE_SHIFT_HOURS if not position.lower().startswith("vel") else VELITEL_DOUBLE

    # Dvojitá smena (vrátane víkendu)
    if pr_t <= time(7, 0) and (od_t >= time(21, 0) or od_t < time(2, 0)):
        return ("R+P OK", "R+P OK", double_shift_hours, double_shift_hours, msgs)

    # Ranná
    morning_limit = time(15, 0) if not is_weekend else time(13, 0)
    if pr_t <= time(7, 0) and od_t <= morning_limit:
        return ("Ranna OK", "none", shift_hours, 0.0, msgs)

    # Poobedná
    afternoon_start = time(13, 0)
    afternoon_end = time(22, 0) if not is_weekend else time(19, 0)
    if pr_t >= afternoon_start and od_t >= afternoon_end:
        return ("none", "Poobedna OK", 0.0, shift_hours, msgs)

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
    """Zhrnie jednu pozíciu za deň."""
    morning = {"status": "absent", "hours": 0.0, "detail": None}
    afternoon = {"status": "absent", "hours": 0.0, "detail": None}
    details = []

    if pos_day_df.empty:
        return morning, afternoon, details

    pairs = get_user_pairs(pos_day_df)

    # === overíme, či je víkend ===
    weekday = pos_day_df["timestamp"].dt.weekday.iloc[0]  # 0=pondelok, 5=sobota, 6=nedeľa
    if weekday in (5, 6):  # Sobota alebo Nedeľa
        # Najskorší príchod (ale od 06:00 začíname počítať)
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
            afternoon = {"status": "Obsadené", "hours": 0.0, "detail": None}  # cez víkend nepotrebujeme poobednú
        return morning, afternoon, details

    # === Pôvodná logika pondelok–piatok ===
    # ... sem vlož celú pôvodnú logiku, ako máš teraz



def summarize_day(df_day: pd.DataFrame, target_date: date):
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, details = summarize_position_day(pos_df, pos)
        total = morning.get("hours",0.0) + afternoon.get("hours",0.0)
        results[pos] = {
            "morning": morning,
            "afternoon": afternoon,
            "details": details,
            "total_hours": round(total,2)
        }
    return results

def save_attendance(user_code, position, action, now=None):
    user_code = user_code.strip()
    if not now:
        now = datetime.now(tz)
    ts_str = now.strftime("%Y-%m-%d %H:%M:%S.%f") + "+00"
    databaze.table("attendance").insert({
        "user_code": user_code,
        "position": position,
        "action": action,
        "timestamp": ts_str,
        "valid": True
    }).execute()
    return True

# ================== STREAMLIT UI ==================
st.title("Admin - Dochádzka")

tab1, tab2 = st.tabs(["Záznam príchod/odchod", "Prehľad a export"])

with tab1:
    st.subheader("Zaznamenať príchod alebo odchod")
    col1, col2, col3 = st.columns(3)
    with col1:
        user_code = st.text_input("Kód zamestnanca")
    with col2:
        position = st.selectbox("Pozícia", POSITIONS)
    with col3:
        action = st.radio("Akcia", ["Príchod", "Odchod"])
    if st.button("Uložiť záznam"):
        if user_code.strip() == "":
            st.warning("Zadaj kód zamestnanca")
        else:
            save_attendance(user_code, position, action)
            st.success(f"Záznam {action} uložený pre {user_code} na pozícii {position}")

with tab2:
    st.subheader("Prehľad dochádzky")
    d1 = st.date_input("Dátum od", date.today())
    d2 = st.date_input("Dátum do", date.today())
    if d1 > d2:
        st.warning("Dátum od musí byť pred dátumom do")
    else:
        start_dt = datetime.combine(d1, time(0,0)).replace(tzinfo=tz)
        end_dt = datetime.combine(d2+timedelta(days=1), time(0,0)).replace(tzinfo=tz)
        df = load_attendance(start_dt, end_dt)
        if df.empty:
            st.info("Žiadne záznamy v tomto období")
        else:
            summary = {}
            for single_date in pd.date_range(d1, d2):
                day_df = df[df["date"] == single_date.date()]
                summary[single_date.date()] = summarize_day(day_df, single_date.date())

            # Export do Excel
            if st.button("Export Excel"):
                wb = Workbook()
                ws = wb.active
                ws.title = "Dochádzka"
                header = ["Dátum", "Pozícia", "Ranná (hodiny)", "Poobedná (hodiny)", "Detaily"]
                ws.append(header)
                for dt, day_sum in summary.items():
                    for pos, val in day_sum.items():
                        ws.append([
                            dt,
                            pos,
                            val["morning"]["hours"],
                            val["afternoon"]["hours"],
                            "; ".join(val["details"])
                        ])
                buffer = BytesIO()
                wb.save(buffer)
                st.download_button("Stiahnuť Excel", buffer.getvalue(), file_name="dochadzka.xlsx")
