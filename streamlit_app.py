# streamlit_app.py
import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
import pytz
from supabase import create_client, Client
from io import BytesIO
from openpyxl.styles import PatternFill
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl import Workbook
from httpx import ReadTimeout

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

# Secrets (Streamlit Cloud)
DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")

# Supabase client
supabase: Client = create_client(DATABAZA_URL, DATABAZA_KEY)

tz = pytz.timezone("Europe/Bratislava")

POSITIONS = ["Veliteľ","CCTV","Brány","Sklad2","Sklad3","Turniket2","Turniket3","Plombovac2","Plombovac3"]
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 15.25
VELITEL_DOUBLE = 16.25

# ========== HELPERS ==========

def safe_fetch_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """Načíta záznamy medzi start_dt (inclusive) a end_dt (exclusive) s ošetrením timeoutu."""
    try:
        res = supabase.table("attendance").select("*").gte("timestamp", start_dt.isoformat()).lt("timestamp", end_dt.isoformat()).execute()
        df = pd.DataFrame(res.data)
    except ReadTimeout:
        st.error("⚠️ Timeout pri načítaní dát z databázy. Skús 'Obnoviť dáta' znova.")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"❌ Chyba pri načítaní dát z attendance: {e}")
        return pd.DataFrame()

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
    """Klasifikuje pr/od pre jednu osobu na pozícii podľa pravidiel."""
    msgs = []
    if (pd.isna(pr) or pr is None) and (pd.isna(od) or od is None):
        return ("none","none",0.0,0.0, msgs)
    if pd.isna(pr) or pr is None:
        msgs.append("missing_prichod")
        od_t = od.time() if od is not pd.NaT else None
        if od_t and od_t >= time(21,0):
            return ("none","missing_pr",0.0,0.0, msgs)
        else:
            return ("missing_pr","none",0.0,0.0, msgs)
    if pd.isna(od) or od is None:
        msgs.append("missing_odchod")
        pr_t = pr.time() if pr is not pd.NaT else None
        if pr_t and pr_t < time(13,0):
            return ("missing_od","none",0.0,0.0, msgs)
        else:
            return ("none","missing_od",0.0,0.0, msgs)

    pr_t = pr.time(); od_t = od.time()

    # Veliteľ special
    if position.lower().startswith("vel"):
        if pr_t <= time(7,0) and (od_t >= time(21,0) or od_t < time(2,0)):
            return ("R+P OK","R+P OK", VELITEL_DOUBLE, VELITEL_DOUBLE, msgs)

    if pr_t <= time(7,0) and (od_t >= time(21,0) or od_t < time(2,0)):
        return ("R+P OK","R+P OK", DOUBLE_SHIFT_HOURS, DOUBLE_SHIFT_HOURS, msgs)

    if pr_t <= time(7,0) and od_t <= time(15,0):
        return ("Ranna OK","none", SHIFT_HOURS, 0.0, msgs)

    if pr_t >= time(13,0) and od_t >= time(21,0):
        return ("none","Poobedna OK", 0.0, SHIFT_HOURS, msgs)

    msgs.append("invalid_times")
    return ("invalid","invalid",0.0,0.0, msgs)

def summarize_position_day(pos_day_df: pd.DataFrame, position):
    """Pre pozíciu a deň určí morning + afternoon výsledky + detaily."""
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
        if role_m == "Ranna OK":
            if morning["status"] not in ("Ranna OK","R+P OK"):
                morning = {"status":"Ranna OK", "hours": h_m, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if role_p == "Poobedna OK":
            if afternoon["status"] not in ("Poobedna OK","R+P OK"):
                afternoon = {"status":"Poobedna OK", "hours": h_p, "detail": f"{user}: Príchod: {pair['pr']}, Odchod: {pair['od']}"}
        if msgs:
            for m in msgs:
                details.append(f"{user}: {m} — pr:{pair['pr']} od:{pair['od']}")

    if morning["status"] == "Ranna OK" and afternoon["status"] == "Poobedna OK":
        if position.lower().startswith("vel"):
            total = VELITEL_DOUBLE
            morning["hours"] = total/2
            afternoon["hours"] = total/2
        else:
            morning["hours"] = SHIFT_HOURS
            afternoon["hours"] = SHIFT_HOURS

    return morning, afternoon, details

def summarize_day(df_day: pd.DataFrame, target_date: date):
    """Vráti slovník výsledkov pre každú pozíciu."""
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

def summarize_week_matrix(df_week: pd.DataFrame, monday: date):
    days = [monday + timedelta(days=i) for i in range(7)]
    cols = [d.strftime("%a %d.%m") for d in days]
    matrix = pd.DataFrame(index=POSITIONS, columns=cols)
    for d in days:
        df_d = df_week[df_week["date"] == d]
        summ = summarize_day(df_d, d)
        for pos in POSITIONS:
            matrix.at[pos, d.strftime("%a %d.%m")] = summ[pos]["total_hours"] if summ[pos]["total_hours"]>0 else "—"
    matrix["Spolu"] = matrix.apply(lambda row: sum(x if isinstance(x,(int,float)) else 0 for x in row), axis=1)
    return matrix

def excel_with_colors(df_matrix: pd.DataFrame, df_day_details: pd.DataFrame, df_raw: pd.DataFrame, monday: date) -> BytesIO:
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Týždenný prehľad"

    green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    red = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
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
            elif val == "—":
                pass

    ws2 = wb.create_sheet("Denné - detail")
    for r in dataframe_to_rows(df_day_details, index=False, header=True):
        ws2.append(r)
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

    ws3 = wb.create_sheet("Surové dáta")
    for r in dataframe_to_rows(df_raw, index=False, header=True):
        ws3.append(r)

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out

# ========== INTERACTIVE DB INSERT with safe handling ==========
def insert_missing_record(user_code, position, target_date, record_type, chosen_time):
    """
    Zapíše chýbajúci príchod/odchod do attendance.
    - target_date: datetime.date
    - record_type: 'prichod' alebo 'odchod'
    - chosen_time: môže byť:
        * "HH:MM" (string),
        * "YYYY-MM-DD HH:MM:SS" alebo ISO string,
        * datetime.time,
        * datetime.datetime
    Vloží timestamp v ISO formáte s časovou zónou a valid=False.
    """
    # získaj klienta (podporuje premenné 'supabase' alebo 'databaze')
    client = globals().get("supabase") or globals().get("databaze")
    if client is None:
        st.error("Supabase client nie je definovaný (očakáva sa 'supabase' alebo 'databaze').")
        return

    # parsovanie chosen_time -> tz-aware datetime 'ts'
    try:
        # 1) string typu "HH:MM"
        if isinstance(chosen_time, str):
            s = chosen_time.strip()
            try:
                t = datetime.strptime(s, "%H:%M").time()
                ts = tz.localize(datetime.combine(target_date, t))
            except ValueError:
                # 2) string typu full datetime / ISO
                dt = pd.to_datetime(s)
                if pd.isna(dt):
                    raise ValueError("Nepodarilo sa parsovať string ako datetime.")
                # dt môže byť tz-naive alebo tz-aware
                if dt.tzinfo is None:
                    ts = tz.localize(dt.to_pydatetime())
                else:
                    ts = dt.to_pydatetime().astimezone(tz)

        # 3) datetime.datetime
        elif isinstance(chosen_time, datetime):
            dt = chosen_time
            if dt.tzinfo is None:
                ts = tz.localize(dt)
            else:
                ts = dt.astimezone(tz)

        # 4) datetime.time
        elif isinstance(chosen_time, time):
            ts = tz.localize(datetime.combine(target_date, chosen_time))

        else:
            st.error("Neplatný formát pre 'chosen_time' — použi 'HH:MM', datetime alebo time objekt.")
            return

    except Exception as e:
        st.error(f"Chyba pri parsovaní času: {e}")
        return

    iso_ts = ts.isoformat()
    action = "Príchod" if str(record_type).lower().startswith("pr") else "Odchod"

    payload = {
        "user_code": str(user_code),
        "position": position,
        "action": action,
        "timestamp": iso_ts,
        "valid": False
    }

    try:
        client.table("attendance").insert(payload).execute()
        st.success(f"✅ Uložené: {user_code} — {position} — {action} @ {iso_ts}")
        # označíme, že treba reload dát (tvoj refresh tlačidlo)
        st.session_state["_reload_needed"] = True
    except ReadTimeout:
        st.warning("⚠️ Timeout pri zápise do DB. Skús znovu.")
    except Exception as e:
        st.error(f"❌ Chyba pri zápise do attendance: {e}")

    
# ========== CONFLICTS BY SHIFT ==========
def collect_conflicts_by_shift(df_day: pd.DataFrame):
    """Deteguje, či má user viac než 1 pozíciu na danej zmene (ranná/poobedná)."""
    rows = []
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        pairs = get_user_pairs(pos_df)
        for user, pair in pairs.items():
            role_m, role_p, _, _, _ = classify_pair(pair["pr"], pair["od"], pos)
            rows.append({
                "user_code": user,
                "position": pos,
                "morning_role": role_m,
                "afternoon_role": role_p
            })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df_morning = df[df["morning_role"].isin(["Ranna OK","R+P OK"])].groupby("user_code")["position"].nunique().reset_index().rename(columns={"position":"morning_count"})
    df_afternoon = df[df["afternoon_role"].isin(["Poobedna OK","R+P OK"])].groupby("user_code")["position"].nunique().reset_index().rename(columns={"position":"afternoon_count"})
    merged = pd.merge(df_morning, df_afternoon, on="user_code", how="outer").fillna(0)
    merged["morning_count"] = merged["morning_count"].astype(int)
    merged["afternoon_count"] = merged["afternoon_count"].astype(int)
    conflicts = merged[(merged["morning_count"] > 1) | (merged["afternoon_count"] > 1)]
    return conflicts

# ========== UI / App logic ==========

st.title("🕓 Admin — Dochádzka (Denný + Týždenný prehľad) — stabilná verzia")

# Admin login
if "admin_logged" not in st.session_state:
    st.session_state.admin_logged = False
if not st.session_state.admin_logged:
    st.sidebar.header("Admin prihlásenie")
    pw = st.sidebar.text_input("Heslo", type="password")
    if st.sidebar.button("Prihlásiť"):
        if ADMIN_PASS and pw == ADMIN_PASS:
            st.session_state.admin_logged = True
            st.success("Prihlásenie úspešné — stlač 'Obnoviť dáta' v bočnom paneli.")
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

# Reload controls (manual; vyhýbame sa st.experimental_rerun)
if "_reload_needed" not in st.session_state:
    st.session_state["_reload_needed"] = True  # prvé načítanie
if st.sidebar.button("Obnoviť dáta (načítaj z DB)"):
    st.session_state["_reload_needed"] = True

# Load week data (cached in session_state to minimalizovať volania)
if st.session_state.get("_reload_needed", False) or "_df_week_cache" not in st.session_state:
    df_week = safe_fetch_attendance(start_dt, end_dt)
    st.session_state["_df_week_cache"] = df_week.to_dict('records') if not df_week.empty else []
    st.session_state["_reload_needed"] = False
else:
    cached = st.session_state.get("_df_week_cache", [])
    df_week = pd.DataFrame(cached)

# daily selection
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=today, min_value=monday, max_value=monday+timedelta(days=6))
if df_week.empty:
    df_day = pd.DataFrame()
else:
    # ensure timestamp col is datetime
    if "timestamp" in df_week.columns and not pd.api.types.is_datetime64_any_dtype(df_week["timestamp"]):
        df_week["timestamp"] = pd.to_datetime(df_week["timestamp"], errors="coerce")
    df_day = df_week[df_week["date"] == selected_day]

if df_week.empty:
    st.warning("Rozsah nie je dostupný v DB (žiadne dáta pre vybraný týždeň).")
else:
    # 1) Konflikty podľa zmien
    conflicts = collect_conflicts_by_shift(df_day)
    if not conflicts.empty:
        st.error("⚠️ Nájdené konflikty: používateľ má viacero pozícií NA RANNEJ alebo NA POOBEDNEJ zmene (to je chyba).")
        for _, r in conflicts.iterrows():
            note = []
            if r["morning_count"] > 1:
                morning_positions = []
                for pos in POSITIONS:
                    pos_df = df_day[df_day["position"] == pos]
                    pairs = get_user_pairs(pos_df)
                    p = pairs.get(r["user_code"])
                    if p:
                        role_m, _, _, _, _ = classify_pair(p["pr"], p["od"], pos)
                        if role_m in ("Ranna OK","R+P OK"):
                            morning_positions.append(pos)
                note.append(f"Ranná: {', '.join(morning_positions)}")
            if r["afternoon_count"] > 1:
                afternoon_positions = []
                for pos in POSITIONS:
                    pos_df = df_day[df_day["position"] == pos]
                    pairs = get_user_pairs(pos_df)
                    p = pairs.get(r["user_code"])
                    if p:
                        _, role_p, _, _, _ = classify_pair(p["pr"], p["od"], pos)
                        if role_p in ("Poobedna OK","R+P OK"):
                            afternoon_positions.append(pos)
                note.append(f"Poobedná: {', '.join(afternoon_positions)}")
            st.write(f"👤 **{r['user_code']}** — " + " | ".join(note))

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
        m_status = m["status"]
        a_status = p["status"]
        m_detail = m.get("detail") or "-"
        a_detail = p.get("detail") or "-"
        col.markdown(f"### **{pos}**")

        # morning
        if m_status in ("Ranna OK","R+P OK"):
            col.success(f"Ranná: {m_status} — {m.get('hours',0)} h")
        elif m_status == "absent":
            col.info("Ranná: neprítomný")
        elif "missing" in str(m_status).lower() or "missing" in str(m_detail).lower():
            col.warning(f"Ranná: {m_status} — {m.get('hours',0)} h")
        else:
            col.info(f"Ranná: {m_status} — {m.get('hours',0)} h")
        if m_detail and m_detail != "-":
            col.caption(f"Detail: {m_detail}")

        # afternoon
        if a_status in ("Poobedna OK","R+P OK"):
            col.success(f"Poobedná: {a_status} — {p.get('hours',0)} h")
        elif a_status == "absent":
            col.info("Poobedná: neprítomný")
        elif "missing" in str(a_status).lower() or "missing" in str(a_detail).lower():
            col.warning(f"Poobedná: {a_status} — {p.get('hours',0)} h")
        else:
            col.info(f"Poobedná: {a_status} — {p.get('hours',0)} h")
        if a_detail and a_detail != "-":
            col.caption(f"Detail: {a_detail}")

        # detail messages & interactive fixes
        if info["details"]:
            for k, d in enumerate(info["details"]):
                with col.expander(f"⚠️ Chybný záznam — {d.split('—')[0].strip()}", expanded=False):
                    parts = d.split("—")
                    left = parts[0].strip()
                    right = parts[1].strip() if len(parts) > 1 else ""
                    user_code = left.split(":")[0].strip()
                    st.markdown(f"**Používateľ:** `{user_code}`")
                    if "missing_prichod" in d or "missing_pr" in d:
                        st.warning("Chýba **príchod**")
                        st.caption(f"Info: {right}")
                        chosen_time = st.selectbox(
                            "Vyber čas pre doplnenie príchodu:",
                            ["06:00", "14:00", "22:00"],
                            key=f"pr_{user_code}_{pos}_{k}"
                        )
                        if st.button(f"Doplniť príchod pre {user_code} na {pos}", key=f"btn_pr_{user_code}_{pos}_{k}"):
                            insert_missing_record(user_code, pos, selected_day, "prichod", chosen_time)
                            st.info("Po vložení stlač 'Obnoviť dáta (načítaj z DB)' v bočnom paneli.")
                    if "missing_odchod" in d or "missing_od" in d:
                        st.warning("Chýba **odchod**")
                        st.caption(f"Info: {right}")
                        chosen_time = st.selectbox(
                            "Vyber čas pre doplnenie odchodu:",
                            ["06:00", "14:00", "22:00"],
                            key=f"od_{user_code}_{pos}_{k}"
                        )
                        if st.button(f"Doplniť odchod pre {user_code} na {pos}", key=f"btn_od_{user_code}_{pos}_{k}"):
                            insert_missing_record(user_code, pos, selected_day, "odchod", chosen_time)
                            st.info("Po vložení stlač 'Obnoviť dáta (načítaj z DB)' v bočnom paneli.")
                    if "invalid" in d or "invalid_times" in d:
                        st.error("Neštandardné časy (invalid_times) — skontroluj záznamy ručne.")
                        st.caption(f"Info: {right}")

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

    # Export to Excel (3 sheets)
    if st.button("Exportuj Excel (Farebné)"):
        df_matrix = matrix.reset_index().rename(columns={"index":"position"})
        df_day_details = pd.DataFrame(day_details_rows)
        df_raw = df_week.copy()
        if "timestamp" in df_raw.columns:
            df_raw["timestamp"] = df_raw["timestamp"].apply(lambda x: x.isoformat() if pd.notna(x) else "")
        xls = excel_with_colors(df_matrix, df_day_details, df_raw, monday)
        st.download_button("Stiahnuť XLSX", data=xls, file_name=f"dochadzka_{monday}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.markdown("---")
st.caption("Po doplnení chýbajúcich záznamov stlač v bočnom paneli 'Obnoviť dáta (načítaj z DB)'. Nepoužívame st.experimental_rerun() kvôli známej chybe — preto ručné obnovenie.")
