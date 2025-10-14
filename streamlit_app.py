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

# ================== HELPERS ==================
def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """Načíta záznamy z tabuľky attendance medzi start_dt (inclusive) a end_dt (exclusive)."""
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
    # lokalizuj iba naive datetimes
    df["timestamp"] = df["timestamp"].apply(
        lambda x: tz.localize(x) if pd.notna(x) and x.tzinfo is None else x
    )
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.time
    return df

def get_user_pairs(pos_day_df: pd.DataFrame):
    """Pre daný pos_day_df (záznamy pre jednu pozíciu a deň) vráti dict user-> {pr, od, pr_count, od_count}."""
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
    """Klasifikuje pár pr/od podľa časov a pozície, vracia (mor_status, aft_status, hours_m, hours_p, msgs)."""
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

    # Dvojitá smena (non-veliteľ) - pokryté nižšie
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
    """Zhrnie jednu pozíciu za deň: ranná, poobedná, detaily."""
    morning = {"status": "absent", "hours": 0.0, "detail": None}
    afternoon = {"status": "absent", "hours": 0.0, "detail": None}
    details = []

    if pos_day_df.empty:
        return morning, afternoon, details

    pairs = get_user_pairs(pos_day_df)

    # preferujeme užívateľa s kompletnou R+P OK (ak existuje)
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

    # inak skontrolujeme jednotlivcov
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
    """Zhrnie všetky pozície pre daný deň."""
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, details = summarize_position_day(pos_df, pos)
        if morning["status"] == "R+P OK" and afternoon["status"] == "R+P OK":
            total = VELITEL_DOUBLE if pos.lower().startswith("vel") else DOUBLE_SHIFT_HOURS
        elif morning["status"] in ("Ranna OK", "R+P OK") and afternoon["status"] in ("Poobedna OK", "R+P OK"):
            total = VELITEL_DOUBLE if pos.lower().startswith("vel") else DOUBLE_SHIFT_HOURS
        else:
            total = morning.get("hours", 0.0) + afternoon.get("hours", 0.0)
        results[pos] = {
            "morning": morning,
            "afternoon": afternoon,
            "details": details,
            "total_hours": total
        }
    return results
def save_attendance(user_code: str, pos: str, action: str, ts: datetime = None):
    """
    Uloží záznam do tabuľky 'attendance' v Supabase,
    presne podľa toho, ako to používa QR appka.
    """
    user_code = user_code.strip()
    if not user_code:
        st.error("⚠️ Chýba user_code!")
        return False

    ts = ts or datetime.now(tz)

    # validácia času (podľa QR app logiky)
    def valid_time(action_name, now_dt):
        if action_name == "Príchod":
            return (time(5,0) <= now_dt.time() <= time(7,0)) or (time(13,0) <= now_dt.time() <= time(15,0))
        else:
            return (time(13,30) <= now_dt.time() <= time(15,0)) or (time(21,0) <= now_dt.time() <= time(23,0))

    is_valid = valid_time(action, ts)

    try:
        databaza.table("attendance").insert({
            "user_code": user_code,
            "position": pos,
            "action": action,
            "timestamp": ts.isoformat(),
            "valid": is_valid
        }).execute()
        st.success(f"✅ Záznam uložený: {user_code}, {pos}, {action} ({'platný' if is_valid else 'mimo času'})")
        return True
    except Exception as e:
        st.error(f"❌ Chyba pri ukladaní: {e}")
        return False



# ================== EXCEL EXPORT (s rozpisom čipov) ==================
def excel_with_colors(df_matrix: pd.DataFrame, df_day_details: pd.DataFrame, df_raw: pd.DataFrame, monday: date) -> BytesIO:
    """
    Vytvorí Excel s listami:
    - Týždenný prehľad (farebne)
    - Denné - detail
    - Surové dáta
    - Rozpis čipov (user_code per position/shift/day)
    """
    wb = Workbook()

    # --- Sheet 1: Týždenný prehľad ---
    ws1 = wb.active
    ws1.title = "Týždenný prehľad"
    green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    yellow = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")

    for r in dataframe_to_rows(df_matrix.reset_index().rename(columns={"index": "Pozícia"}), index=False, header=True):
        ws1.append(r)

    # apply fills for numeric cells and warnings
    for row in ws1.iter_rows(min_row=2, min_col=2, max_col=1 + len(df_matrix.columns), max_row=1 + len(df_matrix)):
        for cell in row:
            val = cell.value
            if isinstance(val, (int, float)):
                cell.fill = green
            elif isinstance(val, str) and val.strip().startswith("⚠"):
                cell.fill = yellow

    # --- Sheet 2: Denné - detail ---
    ws2 = wb.create_sheet("Denné - detail")
    for r in dataframe_to_rows(df_day_details, index=False, header=True):
        ws2.append(r)

    # --- Sheet 3: Surové dáta ---
    ws3 = wb.create_sheet("Surové dáta")
    for r in dataframe_to_rows(df_raw, index=False, header=True):
        ws3.append(r)

    # --- Sheet 4: Rozpis čipov ---
    ws4 = wb.create_sheet("Rozpis čipov")
    days = ["pondelok", "utorok", "streda", "štvrtok", "piatok", "sobota", "nedeľa"]
    header = ["position", "shift"] + days
    ws4.append(header)

    # Použijeme df_day_details (ktorý obsahuje morning_detail / afternoon_detail) a extrahujeme user_code
    # Každý riadok day_details reprezentuje jednu pozíciu (ráno/poobede) pre daný týždeň
    for det in df_day_details.to_dict(orient="records"):
        position = det.get("position", "")
        # vybereme smenu podľa morning_status/afternoon_status - ak je morning_status nastavené, použijeme rannú smenu
        shift = det.get("morning_status") if det.get("morning_status") else det.get("afternoon_status")
        # ak shift neobsahuje štandardný string, ponecháme ho tak, neskôr sa dá doladiť
        row_values = []
        for i in range(7):
            # pre každý deň zoberieme detail pre danú smenu
            if shift == "06:00-14_00" or (shift and shift.lower().startswith("ranna")):
                detail_text = det.get("morning_detail", "")
            else:
                detail_text = det.get("afternoon_detail", "")
            user_code = ""
            if detail_text and detail_text != "-":
                # detail má formát buď "USER: Príchod..." alebo "user: timestamp"
                parts = detail_text.split(":", 1)
                if parts:
                    candidate = parts[0].strip()
                    # jednoduchá validácia (ak niečo vyzerá ako USER)
                    user_code = candidate
            row_values.append(user_code)
        ws4.append([position, shift] + row_values)

    # center align Rozpis čipov
    for col in ws4.columns:
        for cell in col:
            cell.alignment = Alignment(horizontal="center", vertical="center")

    # uložíme do pamäte
    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out

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
        # --- Dopĺňanie chýbajúcich záznamov ---
        if selected_day < today and info["details"]:
            for idx, d in enumerate(info["details"]):
                if "missing_prichod" in d:
                    st.markdown(f"#### Doplniť chýbajúci PRÍCHOD pre pozíciu {pos}")
                    user_code_input = st.text_input(f"User code ({pos})", value="USER123456", key=f"{pos}_prichod_user_{idx}")
                    hour = st.select_slider("Hodina", options=list(range(6, 23, 1)), key=f"{pos}_prichod_hour_{idx}")
                    minute = st.select_slider("Minúta", options=[0, 15, 30, 45], key=f"{pos}_prichod_minute_{idx}")
                    if st.button(f"Uložiť príchod ({pos})", key=f"{pos}_prichod_save_{idx}"):
                        ts = tz.localize(datetime.combine(selected_day, time(hour, minute)))
                        save_attendance(user_code_input, pos, "Príchod", ts)
                        st.experimental_rerun()
    
                if "missing_odchod" in d:
                    st.markdown(f"#### Doplniť chýbajúci ODCHOD pre pozíciu {pos}")
                    user_code_input = st.text_input(f"User code ({pos})", value="USER123456", key=f"{pos}_odchod_user_{idx}")
                    hour = st.select_slider("Hodina", options=list(range(6, 23, 1)), key=f"{pos}_odchod_hour_{idx}")
                    minute = st.select_slider("Minúta", options=[0, 15, 30, 45], key=f"{pos}_odchod_minute_{idx}")
                    if st.button(f"Uložiť odchod ({pos})", key=f"{pos}_odchod_save_{idx}"):
                        ts = tz.localize(datetime.combine(selected_day, time(hour, minute)))
                        save_attendance(user_code_input, pos, "Odchod", ts)
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
        # ak chceme mať timestamps ako iso stringy v exceli
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

if df_2w_summary:
    st.subheader("⚠️ Upozornenia — viacnásobné záznamy za 7 dní")
    st.dataframe(pd.DataFrame(df_2w_summary))
