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
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")  # nastav v secrets
databaze: Client = create_client(DATABAZA_URL, DATABAZA_KEY)

tz = pytz.timezone("Europe/Bratislava")
# Skrytie hamburger menu a footeru
hide_menu = """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    </style>
"""
st.markdown(hide_menu, unsafe_allow_html=True)
POSITIONS = ["Veliteľ","CCTV","Brány","Sklad2","Sklad3","Turniket2","Turniket3","Plombovac2","Plombovac3"]
SHIFT_TIMES = {
    "ranna": (time(6, 0), time(14, 0)),
    "poobedna": (time(14, 0), time(22, 0))
}
SHIFT_HOURS = 7.5
DOUBLE_SHIFT_HOURS = 15.25

# ---------- HELPERS ----------
def load_attendance(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """Načíta záznamy medzi start_dt (inclusive) a end_dt (exclusive)."""
    res = databaze.table("attendance").select("*")\
        .gte("timestamp", start_dt.isoformat())\
        .lt("timestamp", end_dt.isoformat()).execute()
    df = pd.DataFrame(res.data)
    if df.empty:
        return df
    # parse timestamps robustne
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    # ak sú tz-naive, lokalizuj ako Europe/Bratislava (predpoklad lokálneho ukladania)
    try:
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(tz)
        else:
            df["timestamp"] = df["timestamp"].dt.tz_convert(tz)
    except Exception:
        # ak sa pokazí, lokalizujeme jednotlivé prvky (bez crashu)
        df["timestamp"] = df["timestamp"].apply(lambda x: tz.localize(x) if (pd.notna(x) and x.tzinfo is None) else (x.tz_convert(tz) if pd.notna(x) else x))
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.time
    return df

def get_user_pairs(pos_day_df: pd.DataFrame):
    """Pre pozíciu v danom dni vráti pre každého user_code minimalny príchod a maximalny odchod."""
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

def classify_pair(pr, od):
    """Klasifikuje jeden pár prichod/odchod podľa pravidiel.
       Vracia dict s kľúčmi: status, hours (ak relevantné), pr, od."""
    # missing cases
    if (pd.isna(pr) or pr is None) and (pd.isna(od) or od is None):
        return {"status": "none"}
    if pd.isna(pr) or pr is None:
        return {"status": "missing_prichod", "od": od}
    if pd.isna(od) or od is None:
        return {"status": "missing_odchod", "pr": pr}

    pr_t = pr.time()
    od_t = od.time()
    # R+P (both)
    if pr_t <= time(7, 0) and od_t >= time(21, 0):
        return {"status": "R+P OK", "hours": DOUBLE_SHIFT_HOURS, "pr": pr, "od": od}
    # morning
    if pr_t <= time(7, 0) and od_t <= time(15, 0):
        return {"status": "Ranna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}
    # afternoon
    if pr_t >= time(13, 0) and od_t >= time(21, 0):
        return {"status": "Poobedna OK", "hours": SHIFT_HOURS, "pr": pr, "od": od}
    # otherwise invalid
    return {"status": "CHYBNA SMENA", "pr": pr, "od": od}

def summarize_position_day(pos_day_df: pd.DataFrame):
    """Pre danú pozíciu a deň vracia morning info, afternoon info, a komentáre."""
    morning = {"status": "absent", "pr": None, "od": None}
    afternoon = {"status": "absent", "pr": None, "od": None}
    comments = []
    pairs = get_user_pairs(pos_day_df)
    if not pairs:
        return morning, afternoon, comments

    for user, pair in pairs.items():
        res = classify_pair(pair["pr"], pair["od"])
        stt = res["status"]
        # R+P pokryje obe zmeny
        if stt == "R+P OK":
            morning = {"status": "R+P OK", "pr": res["pr"], "od": res["od"]}
            afternoon = morning.copy()
            # R+P už nemôže byť prepísaný, môžeme ukončiť pre túto pozíciu
            break
        elif stt == "Ranna OK":
            # len ak ešte nemáme R+P
            if morning["status"] not in ("R+P OK", "Ranna OK"):
                morning = {"status": "Ranna OK", "pr": res["pr"], "od": res["od"]}
        elif stt == "Poobedna OK":
            if afternoon["status"] not in ("R+P OK", "Poobedna OK"):
                afternoon = {"status": "Poobedna OK", "pr": res["pr"], "od": res["od"]}
        elif stt == "missing_odchod":
            # pr určíme, do ktorej zmeny priradíme (podľa času príchodu)
            pr = res["pr"]
            if pr and pr.time() < time(13, 0):
                if morning["status"] not in ("R+P OK", "Ranna OK"):
                    morning = {"status": "⚠ zabudnutý odchod", "pr": pr, "od": None}
            else:
                if afternoon["status"] not in ("R+P OK", "Poobedna OK"):
                    afternoon = {"status": "⚠ zabudnutý odchod", "pr": pr, "od": None}
        elif stt == "missing_prichod":
            od = res["od"]
            if od and od.time() >= time(21, 0):
                if afternoon["status"] not in ("R+P OK", "Poobedna OK"):
                    afternoon = {"status": "⚠ zabudnutý príchod", "pr": None, "od": od}
            else:
                if morning["status"] not in ("R+P OK", "Ranna OK"):
                    morning = {"status": "⚠ zabudnutý príchod", "pr": None, "od": od}
        elif stt == "CHYBNA SMENA":
            comments.append(f"{user}: neplatná zmena (pr: {pair['pr']}, od: {pair['od']})")

    return morning, afternoon, comments

def summarize_day(df_day: pd.DataFrame, target_date: date):
    """Vráti dict s info pre každú pozíciu pre vybraný deň."""
    results = {}
    for pos in POSITIONS:
        pos_df = df_day[df_day["position"] == pos] if not df_day.empty else pd.DataFrame()
        morning, afternoon, comments = summarize_position_day(pos_df)
        results[pos] = {"morning": morning, "afternoon": afternoon, "comments": comments}
    return results

def detect_conflicts_week(df_week: pd.DataFrame, week_start: date):
    """Deteguje používateľov, ktorí majú v tom istom dni rovnakú zmenu na viacerých pozíciách."""
    conflicts = []
    for single_day in (week_start + timedelta(days=i) for i in range(7)):
        df_day = df_week[df_week["date"] == single_day]
        for shift_name, (sstart, send) in SHIFT_TIMES.items():
            # shift window: consider prichody within shift (approx) or use pairs classification
            # jednoducho: zober všetky users, ktorí majú príchod v tejto smene alebo priradení ako R+P
            # tu zjednodušene: zober príchody medzi sstart-1h .. send+1h
            lower = (datetime.combine(single_day, sstart) - timedelta(hours=1)).replace(tzinfo=tz)
            upper = (datetime.combine(single_day, send) + timedelta(hours=1)).replace(tzinfo=tz)
            df_shift = df_day[(df_day["timestamp"] >= lower) & (df_day["timestamp"] <= upper)]
            # vytvor mapu user -> positions
            for user in df_shift["user_code"].unique():
                pos_list = df_shift[df_shift["user_code"] == user]["position"].unique().tolist()
                if len(pos_list) > 1:
                    conflicts.append({"date": single_day, "shift": shift_name, "user": user, "positions": pos_list})
    return conflicts

def add_missing_shift_to_db(day: date, position: str, shift: str):
    """Doplní chýbajúcu zmenu (neprepíše existujúce). Vloží 2 záznamy s user_code='admin'."""
    start_t, end_t = SHIFT_TIMES[shift]
    pr_ts = tz.localize(datetime.combine(day, start_t))
    od_ts = tz.localize(datetime.combine(day, end_t))
    # kontrola: či už príchod/odchod existujú pre tú pozíciu a deň
    existing = databaze.table("attendance").select("*")\
        .eq("position", position).gte("timestamp", datetime.combine(day, time(0,0)).isoformat())\
        .lt("timestamp", (datetime.combine(day, time(0,0)) + timedelta(days=1)).isoformat()).execute()
    df_ex = pd.DataFrame(existing.data)
    if not df_ex.empty:
        df_ex["timestamp"] = pd.to_datetime(df_ex["timestamp"], errors="coerce")
        # ak existuje príchod v tom rozsahu neskladame
        pr_exists = ((df_ex["action"] == "Príchod") & (df_ex["timestamp"] >= pr_ts) & (df_ex["timestamp"] <= od_ts)).any()
        od_exists = ((df_ex["action"] == "Odchod") & (df_ex["timestamp"] >= pr_ts) & (df_ex["timestamp"] <= od_ts)).any()
        # ak už existuje pr/od, tak nebudeme dopĺňať duplicitne
        inserts = []
        if not pr_exists:
            inserts.append({"user_code": "admin", "position": position, "action": "Príchod", "timestamp": pr_ts.isoformat(), "valid": True})
        if not od_exists:
            inserts.append({"user_code": "admin", "position": position, "action": "Odchod", "timestamp": od_ts.isoformat(), "valid": True})
        if inserts:
            databaze.table("attendance").insert(inserts).execute()
            return True, "Doplnené záznamy: " + ", ".join([f"{i['action']}" for i in inserts])
        else:
            return False, "Záznamy už existujú, nič sa nedopĺňa."
    else:
        # žiadne existujúce -> vlož oboje
        inserts = [
            {"user_code": "admin", "position": position, "action": "Príchod", "timestamp": pr_ts.isoformat(), "valid": True},
            {"user_code": "admin", "position": position, "action": "Odchod", "timestamp": od_ts.isoformat(), "valid": True}
        ]
        databaze.table("attendance").insert(inserts).execute()
        return True, "Doplnené: príchod a odchod (admin)."

def export_df_to_excel(df: pd.DataFrame):
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Dochadzka")
    out.seek(0)
    return out

# ---------- UI ----------
st.set_page_config(page_title="Admin - Dochádzka", layout="wide")
st.title("Admin — Denný / Týždenný prehľad a opravy")

# ADMIN login (jednoduché)
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

# len ak admin prihlásený pokračujeme
if not st.session_state.admin_logged:
    st.stop()

# vyber týždňa / dňa
today = datetime.now(tz).date()
week_ref = st.sidebar.date_input("Vyber deň v týždni (týždeň začne pondelkom)", value=today)
# vypočítaj pondelok
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = datetime.combine(monday, time(0,0))
end_dt = start_dt + timedelta(days=7)

df_week = load_attendance(tz.localize(start_dt) , tz.localize(end_dt))

# vyber deň na denny prehľad
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=today, min_value=monday, max_value=monday+timedelta(days=6))

st.header(f"Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
df_day = df_week[df_week["date"] == selected_day]

summary = summarize_day(df_day, selected_day)

# zobraz 3x3 grid (karty) - rozdelenie do 3 stlpcov
cols = st.columns(3)
for i, pos in enumerate(POSITIONS):
    col = cols[i % 3]
    info = summary[pos]
    morn = info["morning"]
    aft = info["afternoon"]

    def fmt(item):
        if item["status"] in ("absent", "none"):
            return ("❌ bez príchodu", "")
        if item["status"].startswith("⚠"):
            pr = item.get("pr")
            od = item.get("od")
            pr_s = pr.strftime("%H:%M") if pr is not None and pd.notna(pr) else "-"
            od_s = od.strftime("%H:%M") if od is not None and pd.notna(od) else "-"
            return (item["status"], f"{pr_s} - {od_s}")
        if item["status"] == "R+P OK":
            pr_s = item["pr"].strftime("%H:%M") if item.get("pr") is not None else "-"
            od_s = item["od"].strftime("%H:%M") if item.get("od") is not None else "-"
            return ("R+P OK", f"{pr_s} - {od_s}")
        if item["status"] in ("Ranna OK", "Poobedna OK"):
            pr_s = item["pr"].strftime("%H:%M") if item.get("pr") is not None else "-"
            od_s = item["od"].strftime("%H:%M") if item.get("od") is not None else "-"
            return (item["status"], f"{pr_s} - {od_s}")
        return (str(item["status"]), "")

    m_status, m_times = fmt(morn)
    a_status, a_times = fmt(aft)

    col.markdown(f"### **{pos}**")
    col.markdown(f"**Ranná:** {m_status}  \n{m_times}")
    col.markdown(f"**Poobedná:** {a_status}  \n{a_times}")
    if info["comments"]:
        col.error(" • ".join(info["comments"]))

# --------- Týždenný prehľad (matica) ----------
st.header("Týždenný prehľad (matrix) — stav zobrazený pre každý deň / zmenu")
# priprav DataFrame s index=pozicie, columns=MonR,MonP,TueR,...
cols_names = []
days = [monday + timedelta(days=i) for i in range(7)]
for d in days:
    short = d.strftime("%a %d.%m")
    cols_names.append(f"{short}_R")
    cols_names.append(f"{short}_P")

matrix = pd.DataFrame(index=POSITIONS, columns=cols_names)
for d in days:
    df_d = df_week[df_week["date"] == d]
    summ = summarize_day(df_d, d)
    for pos in POSITIONS:
        m = summ[pos]["morning"]["status"]
        a = summ[pos]["afternoon"]["status"]
        matrix.at[pos, f"{d.strftime('%a %d.%m')}_R"] = m
        matrix.at[pos, f"{d.strftime('%a %d.%m')}_P"] = a

st.dataframe(matrix.fillna("—"))

# --------- Konflikty ----------
st.header("Detekcia konfliktov (rovnaký user v jednej zmene na viacerých pozíciách)")
conflicts = detect_conflicts_week(df_week, monday)
if conflicts:
    for c in conflicts:
        st.write(f"{c['date']} • {c['shift']} • user {c['user']} • pozície: {', '.join(c['positions'])}")
else:
    st.success("Žiadne konflikty")

# --------- Oprava chýb (admin) ----------
st.header("Oprava chýb — doplniť chýbajúcu zmenu (vkladá admin záznamy)")
# zisti chybové pozície pre vybraný deň
bad_positions = []
for pos in POSITIONS:
    pinfo = summary[pos]
    if pinfo["morning"]["status"] in ("absent", "⚠ zabudnutý odchod", "⚠ zabudnutý príchod", "CHYBNA SMENA"):
        bad_positions.append((pos, "ranna", pinfo["morning"]["status"]))
    if pinfo["afternoon"]["status"] in ("absent", "⚠ zabudnutý odchod", "⚠ zabudnutý príchod", "CHYBNA SMENA"):
        bad_positions.append((pos, "poobedna", pinfo["afternoon"]["status"]))

if bad_positions:
    st.write("Chybné / neúplné zmeny dnes:")
    for pos, shift, stt in bad_positions:
        st.write(f"- {pos} • {shift} • {stt}")
    st.write("---")
    sel_pos = st.selectbox("Vyber pozíciu na doplnenie", [p for p, s, t in bad_positions])
    sel_shift = st.selectbox("Vyber zmenu", ["ranna", "poobedna"])
    if st.button("Doplniť zmenu (vloží 'admin' príchod/odchod)"):
        ok, msg = add_missing_shift_to_db(selected_day, sel_pos, sel_shift)
        if ok:
            st.success(msg)
        else:
            st.warning(msg)
else:
    st.info("Žiadne chybné zmeny dnes (pre túto pozíciu).")

# --------- Export ----------
st.header("Export dát")
if st.button("Exportuj tento týždeň (Excel)"):
    if df_week.empty:
        st.warning("Žiadne dáta za tento týždeň.")
    else:
        xls = export_df_to_excel(df_week)
        st.download_button("Stiahnuť XLSX", data=xls, file_name=f"dochadzka_{monday}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# koniec
