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
import re
import time as tmode

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

# Secrets
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

# ========== NOVÉ: Funkcia save_attendance (rovnaká ako v zamestnaneckej appke) ==========
def is_valid_code(code: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{10}", code))

def valid_arrival(now):
    return (time(5,0) <= now.time() <= time(7,0)) or (time(13,0) <= now.time() <= time(15,0))

def valid_departure(now):
    return (time(13,30) <= now.time() <= time(15,0)) or (time(21,0) <= now.time() <= time(23,0))

def save_attendance(user_code, position, action, selected_time=None):
    user_code = user_code.strip()
    if not is_valid_code(user_code):
        st.warning("⚠️ Neplatné číslo čipu!")
        return False

    now = datetime.now(tz)
    if selected_time:
        now = datetime.combine(now.date(), selected_time)
        now = tz.localize(now)

    is_valid = valid_arrival(now) if action == "Príchod" else valid_departure(now)

    databaze.table("attendance").insert({
        "user_code": user_code,
        "position": position,
        "action": action,
        "timestamp": now.isoformat(),
        "valid": is_valid
    }).execute()

    return True

# ========== EXISTUJÚCE FUNKCIE ========== 
# (tu ponechaj všetky tvoje funkcie load_attendance, summarize_day, classify_pair atď. bezo zmeny)
# ...

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
week_ref = st.sidebar.date_input("Vyber deň v týždni:", value=today)
monday = week_ref - timedelta(days=week_ref.weekday())
start_dt = tz.localize(datetime.combine(monday, time(0,0)))
end_dt = tz.localize(datetime.combine(monday + timedelta(days=7), time(0,0)))

df_week = load_attendance(start_dt, end_dt)

# chose day for daily view
selected_day = st.sidebar.date_input("Denný prehľad - vyber deň", value=today, min_value=monday, max_value=monday+timedelta(days=6))
df_day = df_week[df_week["date"] == selected_day]

if df_week.empty:
    st.warning("Rozsah nie je dostupný v DB (žiadne dáta pre vybraný týždeň).")
else:
    summary = summarize_day(df_day, selected_day)
    st.header(f"✅ Denný prehľad — {selected_day.strftime('%A %d.%m.%Y')}")
    cols = st.columns(3)
    day_details_rows = []

    # ====== NOVÉ: Časy pre doplnenie (6:00 - 22:00 po 2h) ======
    time_choices = [time(h,0) for h in range(6,23,2)]

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
        col.markdown(f"**Ranná:** {m_status} — {m.get('hours',0)} h")
        if m_detail:
            col.caption(f"Detail: {m_detail}")
        col.markdown(f"**Poobedná:** {a_status} — {p.get('hours',0)} h")
        if a_detail:
            col.caption(f"Detail: {a_detail}")

        # 🔧 NOVÉ: možnosť opravy
        for d in info["details"]:
            if "missing_pr" in d or "missing_od" in d:
                col.warning(d)
                user_code = d.split(":")[0].strip()  # vytiahnutie user_code z textu detailu
                if col.button(f"Opraviť ({user_code})", key=f"fix_{pos}_{user_code}_{i}"):
                    st.session_state["fix_target"] = {"user": user_code, "position": pos, "detail": d}
                    st.experimental_rerun()

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

    # === NOVÉ: okno na doplnenie chýbajúceho záznamu ===
    if "fix_target" in st.session_state:
        fix = st.session_state["fix_target"]
        st.subheader("🛠️ Oprava záznamu")
        st.write(f"Zamestnanec: **{fix['user']}**, Pozícia: **{fix['position']}**")
        st.write(f"Detail: {fix['detail']}")
        action = "Príchod" if "missing_pr" in fix["detail"] else "Odchod"
        selected_time = st.selectbox("Vyber čas:", time_choices)
        if st.button("💾 Uložiť opravu"):
            ok = save_attendance(fix["user"], fix["position"], action, selected_time)
            if ok:
                st.success("✅ Záznam uložený.")
                del st.session_state["fix_target"]
                tmode.sleep(1.5)
                st.experimental_rerun()

    # weekly matrix
    st.header(f"📅 Týždenný prehľad ({monday.strftime('%d.%m.%Y')} – {(monday+timedelta(days=6)).strftime('%d.%m.%Y')})")
    matrix = summarize_week_matrix(df_week, monday)
    st.dataframe(matrix.fillna("—"), use_container_width=True)
