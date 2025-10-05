# streamlit_admin_dochadzka.py
import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO

# ---------- CONFIG ----------
st.set_page_config(page_title="Admin - Dochádzka", layout="wide", initial_sidebar_state="expanded")
# skryť hlavičku streamlitu
hide_st_style = """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    </style>
"""
st.markdown(hide_st_style, unsafe_allow_html=True)

# ---------- DATABASE (používaj "databaze") ----------
DATABAZA_URL = st.secrets.get("DATABAZA_URL")
DATABAZA_KEY = st.secrets.get("DATABAZA_KEY")
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")
if not DATABAZA_URL or not DATABAZA_KEY:
    st.error("Chýbajú databázové prístupy v secrets (DATABAZA_URL / DATABAZA_KEY).")
    st.stop()
databaze: Client = create_client(DATABAZA_URL, DATABAZA_KEY)

tz = pytz.timezone("Europe/Bratislava")

# ---------- CONSTANTS ----------
POSITIONS = ["Veliteľ","CCTV","Brány","Sklad2","Sklad3","Turniket2","Turniket3","Plombovac2","Plombovac3"]
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 15.25
VELITEL_DOUBLE = 16.25

# ---------- HELPERS ----------

def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """Načíta attendance medzi start_dt (inclusive) a end_dt (exclusive). Vracia prázdny DF, ak žiadne dáta."""
    try:
        res = databaze.table("attendance").select("*")\
            .gte("timestamp", start_dt.isoformat())\
            .lt("timestamp", end_dt.isoformat()).execute()
    except Exception as e:
        st.error(f"Chyba pri načítaní z DB: {e}")
        return pd.DataFrame()
    df = pd.DataFrame(res.data or [])
    if df.empty:
        return df
    # parse timestamp robustne
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    # lokalizuj alebo konvertuj na Europe/Bratislava
    def _localize(ts):
        if pd.isna(ts):
            return pd.NaT
        if ts.tzinfo is None:
            return tz.localize(ts)
        return ts.astimezone(tz)
    df["timestamp"] = df["timestamp"].apply(_localize)
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.time
    return df

def get_user_pairs(pos_day_df: pd.DataFrame):
    """Pre konkrétnu pozíciu a deň vráti pre každého user_code pair {pr, od} (min príchod, max odchod)."""
    pairs = {}
    if pos_day_df is None or pos_day_df.empty:
        return pairs
    for user in pos_day_df["user_code"].unique():
        u = pos_day_df[pos_day_df["user_code"] == user]
        pr = u[u["action"] == "Príchod"]["timestamp"]
        od = u[u["action"] == "Odchod"]["timestamp"]
        pr_min = pr.min() if not pr.empty else pd.NaT
        od_max = od.max() if not od.empty else pd.NaT
        pairs[user] = {"pr": pr_min, "od": od_max}
    return pairs

def calculate_shift_hours(pr, od, position):
    """
    Vráti tuple (status_str, hours_float, detail_str).
    - Implementuje pravidlá: Ranná, Poobedná, R+P.
    - Veliteľ má iný double shift (16.25).
    - Ak chýba príchod alebo odchod - vráti stav s detailom.
    """
    if (pd.isna(pr) or pr is None) and (pd.isna(od) or od is None):
        return ("❌ bez príchodu a odchodu", 0.0, "Príchod ani odchod nie sú zaznamenané.")
    if pd.isna(pr) or pr is None:
        detail = f"Príchod: NaT, Odchod: {od}" if not pd.isna(od) else "Príchod: NaT, Odchod: NaT"
        return ("⚠ chýba príchod", 0.0, detail)
    if pd.isna(od) or od is None:
        detail = f"Príchod: {pr}, Odchod: NaT"
        return ("⚠ chýba odchod", 0.0, detail)

    pr_t = pr.time()
    od_t = od.time()

    # normalize crossing midnight cases: treat od < pr as next-day od
    pr_dt = pr
    od_dt = od
    if od_dt <= pr_dt:
        # assume od next day
        od_dt = od_dt + timedelta(days=1)
        od_t = od_dt.time()

    # Veliteľ rule: may come early (03:00) and leave late (<=24:00) considered R+P Veliteľ
    if position.lower().startswith("vel"):
        # If covers majority of both shifts -> consider double
        if (pr_t <= time(5,0)) and (od_dt.time() >= time(22,0) or od_dt - pr_dt >= timedelta(hours=20)):
            return ("✅ R+P Veliteľ OK", VELITEL_DOUBLE, f"Príchod: {pr}, Odchod: {od} (veliteľ špeciál)")

    # R+P generic: príchod do 07:00 a odchod po 21:00 (alebo od next day)
    if (pr_t <= time(7,0)) and (od_dt.time() >= time(21,0) or (od_dt - pr_dt) >= timedelta(hours=14)):
        return ("✅ R+P OK", DOUBLE_SHIFT_HOURS, f"Príchod: {pr}, Odchod: {od}")

    # Ranná: pr do 07:00 a od do 15:00 (allow small tolerance)
    if (pr_t <= time(7,0)) and (od_dt.time() <= time(15,30) or (od_dt - pr_dt) <= timedelta(hours=9)):
        return ("✅ Ranná OK", SHIFT_HOURS, f"Príchod: {pr}, Odchod: {od}")

    # Poobedná: pr >=13:00 a od >=21:00
    if (pr_t >= time(13,0)) and (od_dt.time() >= time(21,0) or (od_dt - pr_dt) >= timedelta(hours=7)):
        return ("✅ Poobedná OK", SHIFT_HOURS, f"Príchod: {pr}, Odchod: {od}")

    # Otherwise suspicious / invalid pattern (e.g., pr 05:30 od 14:00 but od earlier than allowed)
    return ("⚠ chybná smena", 0.0, f"Príchod: {pr}, Odchod: {od} — nesplnené pravidlá zmien")

def summarize_position_day(pos_day_df: pd.DataFrame):
    """Vyhodnotí morning/afternoon pre danú pozíciu a deň a vráti detail."""
    morning = {"status": "absent", "hours": 0.0, "detail": ""}
    afternoon = {"status": "absent", "hours": 0.0, "detail": ""}
    comments = []

    if pos_day_df is None or pos_day_df.empty:
        return morning, afternoon, comments

    pairs = get_user_pairs(pos_day_df)
    # prefer R+P single user - stop after found
    for user, pair in pairs.items():
        status, hours, detail = calculate_shift_hours(pair["pr"], pair["od"], pos_day_df["position"].iloc[0])
        if "R+P" in status:
            # both shifts covered by one user
            morning = {"status": status, "hours": hours, "detail": detail}
            afternoon = {"status": status, "hours": hours, "detail": detail}
            return morning, afternoon, comments

    # if no single R+P, assign best matches:
    # iterate users, try to fill morning then afternoon
    for user, pair in pairs.items():
        status, hours, detail = calculate_shift_hours(pair["pr"], pair["od"], pos_day_df["position"].iloc[0])
        if "Ranná" in status and morning["hours"] == 0.0:
            morning = {"status": status, "hours": hours, "detail": detail}
        elif "Poobedná" in status and afternoon["hours"] == 0.0:
            afternoon = {"status": status, "hours": hours, "detail": detail}
        else:
            # if missing/alarms, append to comments for admin detail
            if status.startswith("⚠") or status.startswith("❌"):
                comments.append(f"{user}: {status} — {detail}")

    # If both are still absent but there are partials, assign partials sensibly:
    for user, pair in pairs.items():
        status, hours, detail = calculate_shift_hours(pair["pr"], pair["od"], pos_day_df["position"].iloc[0])
        if morning["hours"] == 0.0 and status.startswith("⚠") and ("príchod" in status or "chýba" in status.lower()):
            morning = {"status": status, "hours": hours, "detail": detail}
        if afternoon["hours"] == 0.0 and status.startswith("⚠") and ("odchod" in status or "chýba" in status.lower()):
            afternoon = {"status": status, "hours": hours, "detail": detail}

    return morning, afternoon, comments

def summarize_day(df_day: pd.DataFrame, target_date: date):
    """Vráti dict s morning/afternoon info pre každú pozíciu pre daný deň."""
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, comments = summarize_position_day(pos_df)
        results[pos] = {"morning": morning, "afternoon": afternoon, "comments": comments}
    return results

def summarize_hours_week(df_week: pd.DataFrame, monday: date):
    days = [monday + timedelta(days=i) for i in range(7)]
    matrix = pd.DataFrame(index=POSITIONS, columns=[d.strftime("%a %d.%m") for d in days])
    for d in days:
        df_d = df_week[df_week["date"] == d]
        summ = summarize_day(df_d, d)
        for pos in POSITIONS:
            m = summ[pos]["morning"]["hours"]
            a = summ[pos]["afternoon"]["hours"]
            # ak bol R+P (m==a and >0) a pozicia Veliteľ -> už hours sú nastavené na VELITEL_DOUBLE pre oba
            total = 0.0
            # ak R+P (both same non-zero) treat as double (already set)
            if m == a and m > 0:
                total = m  # pre R+P už m obsahuje DOUBLE_SHIFT_HOURS alebo VELITEL_DOUBLE
            else:
                # inak sčítaj (ak absent -> 0)
                total = (m or 0.0) + (a or 0.0)
                # ak morning=7.5 and afternoon=7.5 -> sum should be 15.25 (not 15.0)
                if (abs((m or 0.0) - SHIFT_HOURS) < 0.001) and (abs((a or 0.0) - SHIFT_HOURS) < 0.001):
                    total = DOUBLE_SHIFT_HOURS
            matrix.at[pos, d.strftime("%a %d.%m")] = round(total, 2)
    matrix["SUM"] = matrix.sum(axis=1)
    return matrix

def export_to_excel(df_week: pd.DataFrame, monday: date) -> BytesIO:
    """Exportuje 2 sheety: týždenný prehľad (matrix) a Denný prehľad (detail)."""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        week_matrix = summarize_hours_week(df_week, monday)
        week_matrix.to_excel(writer, sheet_name="Týždenný prehľad")
        # daily detailed sheet
        days = [monday + timedelta(days=i) for i in range(7)]
        rows = []
        for d in days:
            df_d = df_week[df_week["date"] == d]
            summ = summarize_day(df_d, d)
            for pos in POSITIONS:
                m = summ[pos]["morning"]
                a = summ[pos]["afternoon"]
                rows.append({
                    "Dátum": d,
                    "Pozícia": pos,
                    "Ranná status": m["status"],
                    "Ranná hodiny": m["hours"],
                    "Ranná detail": m.get("detail",""),
                    "Poobedná status": a["status"],
                    "Poobedná hodiny": a["hours"],
                    "Poobedná detail": a.get("detail","")
                })
        daily_df = pd.DataFrame(rows)
        daily_df.to_excel(writer, sheet_name="Denný prehľad", index=False)
    output.seek(0)
    return output

# ---------- UI / FLOW ----------
st.title("🕒 Dochádzkový prehľad - Admin")

# jednoduché admin login
if "admin_logged" not in st.session_state:
    st.session_state.admin_logged = False
if not st.session_state.admin_logged:
    st.sidebar.header("Admin login")
    pw = st.sidebar.text_input("Heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw == ADMIN_PASS:
            st.session_state.admin_logged = True
            st.experimental_rerun()
        else:
            st.sidebar.error("Nesprávne heslo alebo ADMIN_PASS nie je nastavené.")
if not st.session_state.admin_logged:
    st.stop()

# Výber referenčného dňa/týždňa
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začne pondelkom)", value=today)
if st.sidebar.button("Načítať týždeň"):
    st.session_state.selected_week = week_ref

selected_week = st.session_state.get("selected_week", today)
monday = selected_week - timedelta(days=selected_week.weekday())
start_dt = tz.localize(datetime.combine(monday, time(0,0)))
end_dt = start_dt + timedelta(days=7)

# Načítanie dát
df_week = load_attendance(start_dt, end_dt)
if df_week.empty:
    st.warning("Rozsah nie je k dispozícii alebo nie sú žiadne dáta pre tento týždeň.")
# Denný výber
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=today, min_value=monday, max_value=monday+timedelta(days=6))

# Denný prehľad (3x3 mriežka)
st.header(f"✅ Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
df_day = df_week[df_week["date"] == selected_day] if not df_week.empty else pd.DataFrame()
summary = summarize_day(df_day, selected_day)

cols = st.columns(3)
for i, pos in enumerate(POSITIONS):
    col = cols[i % 3]
    info = summary[pos]
    morning = info["morning"]
    afternoon = info["afternoon"]
    col.markdown(f"### **{pos}**")
    # morning
    m_status = morning["status"]
    m_hours = morning["hours"]
    m_detail = morning.get("detail","")
    col.markdown(f"**Ranná:** {m_status} — {m_hours} h")
    if m_detail:
        col.caption(f"Detail: {m_detail}")
    # afternoon
    a_status = afternoon["status"]
    a_hours = afternoon["hours"]
    a_detail = afternoon.get("detail","")
    col.markdown(f"**Poobedná:** {a_status} — {a_hours} h")
    if a_detail:
        col.caption(f"Detail: {a_detail}")
    # comments (conflicts / notes)
    if info.get("comments"):
        col.error(" • ".join(info["comments"]))

# Týždenný prehľad (matica hodín)
st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} — {(monday+timedelta(days=6)).strftime('%d.%m.%Y')})")
if df_week.empty:
    st.info("Žiadne dáta pre tento týždeň.")
else:
    matrix = summarize_hours_week(df_week, monday)
    st.dataframe(matrix.fillna(0.0))

# Export do Excelu (2 sheety)
st.header("Export")
if st.button("Exportuj tento týždeň do Excelu"):
    if df_week.empty:
        st.warning("Žiadne dáta na export pre tento týždeň.")
    else:
        excel_bytes = export_to_excel(df_week, monday)
        st.download_button(
            "📥 Stiahnuť XLSX (Týždeň + Denný detail)",
            data=excel_bytes,
            file_name=f"dochadzka_{monday}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

# koniec
