import streamlit as st
import pandas as pd
from datetime import datetime, timedelta, time
from supabase import create_client
from io import BytesIO

# ---------- CONFIG ----------
DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")
databaze: Client = create_client(DATABAZA_URL, DATABAZA_KEY)

st.set_page_config(page_title="🕒 Dochádzkový prehľad SBS", layout="wide")

# ---------- FUNKCIA NAČÍTANIA DÁT ----------
@st.cache_data(ttl=300)
def load_data():
    try:
        data = databaze.table("attendance").select("*").execute()
        df = pd.DataFrame(data.data)
        if df.empty:
            return pd.DataFrame()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df["date"] = df["timestamp"].dt.date
        return df
    except Exception as e:
        st.error(f"❌ Chyba pri načítaní údajov: {e}")
        return pd.DataFrame()

df = load_data()
if df.empty:
    st.warning("⚠ Dáta nie sú dostupné.")
    st.stop()

# ---------- UPRAVENÁ FUNKCIA get_user_pairs ----------
def get_user_pairs(df_day):
    pairs = []
    for user_code, user_df in df_day.groupby("user_code"):
        user_df = user_df.sort_values("timestamp")
        actions = list(user_df["action"])
        timestamps = list(user_df["timestamp"])

        # Skupiny príchod–odchod (po dvojiciach)
        user_shifts = []
        for i in range(0, len(actions) - 1):
            if actions[i].lower() == "prichod" and actions[i+1].lower() == "odchod":
                pr = timestamps[i]
                od = timestamps[i+1]
                user_shifts.append((pr, od))

        # Ak sa našla aspoň jedna dvojica
        for pr, od in user_shifts:
            duration = (od - pr).total_seconds() / 3600
            pairs.append({
                "user_code": user_code,
                "prichod": pr,
                "odchod": od,
                "hours": round(duration, 2)
            })
    return pd.DataFrame(pairs)

# ---------- UPRAVENÁ FUNKCIA summarize_day ----------
def summarize_day(df_day):
    df_pairs = get_user_pairs(df_day)
    result = {}

    if df_pairs.empty:
        return result

    for pos, pos_df in df_day.groupby("position"):
        morning, afternoon = None, None
        detail_text = ""

        valid_shifts = get_user_pairs(pos_df)
        total_hours = round(valid_shifts["hours"].sum(), 2) if not valid_shifts.empty else 0

        # Skontroluj, či sú dve smeny
        if len(valid_shifts) == 2:
            morning, afternoon = valid_shifts.iloc[0], valid_shifts.iloc[1]
            status = "✅ R+P OK"
            total_hours = 15.25 if pos != "velitel" else 16.25
            detail_text = (
                f"Ranná: {morning['prichod'].strftime('%H:%M')} - {morning['odchod'].strftime('%H:%M')} "
                f"({morning['hours']} h)\n"
                f"Poobedná: {afternoon['prichod'].strftime('%H:%M')} - {afternoon['odchod'].strftime('%H:%M')} "
                f"({afternoon['hours']} h)"
            )
        elif len(valid_shifts) == 1:
            morning = valid_shifts.iloc[0]
            total_hours = morning["hours"]
            status = "✅ Ranná OK" if morning["prichod"].time() < time(12, 0) else "✅ Poobedná OK"
            detail_text = (
                f"Príchod: {morning['prichod']}, Odchod: {morning['odchod']} "
                f"({morning['hours']} h)"
            )
        else:
            # Ak chýba príchod alebo odchod
            pr = pos_df[pos_df["action"].str.lower() == "prichod"]["timestamp"]
            od = pos_df[pos_df["action"].str.lower() == "odchod"]["timestamp"]
            pr_time = pr.min() if not pr.empty else None
            od_time = od.max() if not od.empty else None
            missing = "⚠ chýba príchod" if pr.empty else "⚠ chýba odchod" if od.empty else "⚠ neúplné dáta"
            detail_text = f"Príchod: {pr_time}, Odchod: {od_time}"
            total_hours = 0
            status = missing

        result[pos] = {
            "status": status,
            "hours": total_hours,
            "detail": detail_text
        }
    return result

# ---------- TÝŽDENNÝ PREHĽAD ----------
today = datetime.now().date()
monday = today - timedelta(days=today.weekday())
sunday = monday + timedelta(days=6)
week_df = df[(df["date"] >= monday) & (df["date"] <= sunday)]

summary_week = []
for day, df_day in week_df.groupby("date"):
    day_sum = summarize_day(df_day)
    for pos, info in day_sum.items():
        summary_week.append({
            "Dátum": day.strftime("%d.%m.%Y"),
            "Pozícia": pos,
            "Stav": info["status"],
            "Hodiny": info["hours"],
            "Detail": info["detail"]
        })
df_summary_week = pd.DataFrame(summary_week)

# ---------- DENNÝ PREHĽAD ----------
st.sidebar.title("🗓 Výber dňa")
selected_day = st.sidebar.date_input(
    "Denný prehľad - vyber deň",
    value=today,
    min_value=monday,
    max_value=sunday
)

if selected_day < monday or selected_day > sunday:
    st.error("⚠ Rozsah nie je k dispozícii.")
    st.stop()

df_day = df[df["date"] == selected_day]
if df_day.empty:
    st.warning("⚠ Pre tento deň nie sú dáta k dispozícii.")
    st.stop()

day_summary = summarize_day(df_day)

st.markdown(f"## 🕒 Denný prehľad — {selected_day.strftime('%d.%m.%Y')}")
for pos, info in day_summary.items():
    color = "green" if "✅" in info["status"] else "red"
    st.markdown(
        f"<div style='border:1px solid {color};border-radius:8px;padding:10px;margin:5px;'>"
        f"<b>{pos}</b><br>"
        f"{info['status']} — <b>{info['hours']} h</b><br>"
        f"<small>{info['detail']}</small>"
        f"</div>", unsafe_allow_html=True
    )

# ---------- EXPORT DO EXCELU ----------
output = BytesIO()
with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
    pd.DataFrame(day_summary).T.to_excel(writer, sheet_name="Denný prehľad")
    df_summary_week.to_excel(writer, sheet_name="Týždenný prehľad", index=False)
    week_df.to_excel(writer, sheet_name="Zdrojové dáta (DB)", index=False)
st.download_button("📤 Exportovať Excel (3 listy)", data=output.getvalue(), file_name="dochadzka_prehlad.xlsx")
