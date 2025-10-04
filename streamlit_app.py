import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import pytz
from io import BytesIO
from supabase import create_client

# =====================================
# DB CONNECTION
# =====================================
DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
ADMIN_PASS = st.secrets.get("ADMIN_PASS", "")
databaze = create_client(DATABAZA_URL, DATABAZA_KEY)

tz = pytz.timezone("Europe/Bratislava")

# =====================================
# SKRYTIE HLAVIČKY STREAMLIT
# =====================================
st.markdown("""
    <style>
        #MainMenu {visibility: hidden;}
        header {visibility: hidden;}
        footer {visibility: hidden;}
    </style>
""", unsafe_allow_html=True)

# =====================================
# FUNKCIE
# =====================================
@st.cache_data(ttl=60)
def load_attendance():
    """Načíta údaje z DB attendance"""
    resp = databaze.table("attendance").select("*").execute()
    df = pd.DataFrame(resp.data)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["date"] = df["timestamp"].dt.date
    df = df[df["valid"] == True]
    return df

def calculate_hours(prichod, odchod, pozicia):
    """Výpočet hodín pre danú dvojicu príchod/odchod"""
    if pd.isnull(prichod) or pd.isnull(odchod):
        return 0
    duration = (odchod - prichod).total_seconds() / 3600

    # Veliteľ – špeciálna logika
    if pozicia.lower() == "veliteľ":
        if duration >= 15:
            return 16.25
        elif duration >= 7:
            return 7.5
        else:
            return round(duration, 2)

    # ostatní
    if duration >= 14:
        return 15.25
    elif duration >= 7:
        return 7.5
    else:
        return round(duration, 2)

def process_day(df_day):
    """Spracuje denné dáta a vráti prehľad s hodinami"""
    results = []
    for pos, group in df_day.groupby("position"):
        prichody = group[group["action"] == "Príchod"].sort_values("timestamp")
        odchody = group[group["action"] == "Odchod"].sort_values("timestamp")

        # Párovanie príchod – odchod
        records = []
        for i in range(min(len(prichody), len(odchody))):
            pr = prichody.iloc[i]["timestamp"]
            od = odchody.iloc[i]["timestamp"]
            hodiny = calculate_hours(pr, od, pos)
            records.append((pr, od, hodiny))

        total_hodiny = sum(r[2] for r in records)
        pr_text = " | ".join(f"{r[0].strftime('%H:%M')} - {r[1].strftime('%H:%M')}" for r in records)

        results.append({
            "Pozícia": pos,
            "Zmeny": pr_text if pr_text else "—",
            "Hodiny": round(total_hodiny, 2)
        })
    return pd.DataFrame(results)

def process_week(df_week):
    """Súhrn hodín za celý týždeň"""
    df_week["hodiny"] = 0.0
    for idx, row in df_week.iterrows():
        df_week.at[idx, "hodiny"] = 0

    positions = []
    for pos, group in df_week.groupby(["position", "date"]):
        pozicia, date = pos
        prichody = group[group["action"] == "Príchod"].sort_values("timestamp")
        odchody = group[group["action"] == "Odchod"].sort_values("timestamp")
        hodiny = 0
        for i in range(min(len(prichody), len(odchody))):
            pr = prichody.iloc[i]["timestamp"]
            od = odchody.iloc[i]["timestamp"]
            hodiny += calculate_hours(pr, od, pozicia)
        positions.append({"pozicia": pozicia, "date": date, "hodiny": hodiny})

    df_hours = pd.DataFrame(positions)
    if df_hours.empty:
        return pd.DataFrame()

    pivot = df_hours.pivot_table(index="pozicia", columns="date", values="hodiny", aggfunc="sum", fill_value=0)
    pivot["SUM"] = pivot.sum(axis=1)
    return pivot.round(2)

def export_to_excel(daily, weekly):
    """Export 2 sheetov do Excelu"""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        daily.to_excel(writer, index=False, sheet_name="Denný prehľad")
        weekly.to_excel(writer, sheet_name="Týždenný súhrn hodín")
    return output.getvalue()

# =====================================
# APLIKÁCIA
# =====================================
st.sidebar.title("📅 Dochádzkový prehľad – SBS")

data = load_attendance()
if data.empty:
    st.warning("🔸 Rozsah nie je k dispozícii.")
    st.stop()

today = datetime.now(tz).date()
monday = today - timedelta(days=today.weekday())
week_options = [monday - timedelta(weeks=i) for i in range(5)]
selected_week = st.sidebar.selectbox(
    "Vyber týždeň:",
    week_options,
    format_func=lambda d: f"Týždeň od {d.strftime('%d.%m.%Y')}"
)
selected_day = st.sidebar.date_input(
    "Vyber deň",
    value=today,
    min_value=selected_week,
    max_value=selected_week + timedelta(days=6)
)

start_date = selected_week
end_date = selected_week + timedelta(days=7)
df_week = data[(data["date"] >= start_date) & (data["date"] < end_date)]

if df_week.empty:
    st.warning("🔸 Rozsah nie je k dispozícii pre vybraný týždeň.")
    st.stop()

# Denný prehľad
df_day = df_week[df_week["date"] == selected_day]
st.subheader(f"📋 Denný prehľad – {selected_day.strftime('%A %d.%m.%Y')}")

if df_day.empty:
    st.info("Žiadne záznamy pre tento deň.")
    daily_summary = pd.DataFrame()
else:
    daily_summary = process_day(df_day)
    st.dataframe(daily_summary, use_container_width=True)

# Týždenný súhrn
st.subheader("📊 Týždenný súhrn hodín")
weekly_summary = process_week(df_week)

if weekly_summary.empty:
    st.info("Žiadne údaje pre daný týždeň.")
else:
    st.dataframe(weekly_summary, use_container_width=True)

# Export
excel_data = export_to_excel(daily_summary, weekly_summary)
st.download_button(
    "⬇️ Exportovať do Excelu",
    data=excel_data,
    file_name=f"dochadzka_{selected_week.strftime('%Y-%m-%d')}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
