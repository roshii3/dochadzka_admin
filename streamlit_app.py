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
ADMIN_PASS = st.secrets.get("ADMIN_PASS")
databaze = create_client(DATABAZA_URL, DATABAZA_KEY)

tz = pytz.timezone("Europe/Bratislava")

# =====================================
# SKRYTIE HLAVIČKY
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
def load_data():
    """Načíta údaje z databázy Supabase"""
    response = databaze.table("attendance").select("*").execute()
    df = pd.DataFrame(response.data)
    if df.empty:
        return df
    df["prichod"] = pd.to_datetime(df["prichod"], errors="coerce")
    df["odchod"] = pd.to_datetime(df["odchod"], errors="coerce")
    return df

def calculate_hours(prichod, odchod, pozicia):
    """Výpočet hodín so špeciálnou logikou pre Veliteľa a celodenné zmeny"""
    if pd.isnull(prichod) or pd.isnull(odchod):
        return 0
    duration = (odchod - prichod).total_seconds() / 3600

    if pozicia.lower() == "veliteľ":
        if duration >= 15:
            return 16.25  # Veliteľ - celý deň
        elif duration >= 7:
            return 7.5
        else:
            return round(duration, 2)

    if duration >= 14:
        return 15.25  # Celodenná zmena
    if duration >= 7:
        return 7.5
    return round(duration, 2)

def export_to_excel(daily_df, weekly_pivot):
    """Export do Excelu - 2 sheety"""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        daily_df.to_excel(writer, index=False, sheet_name="Denný prehľad")
        weekly_pivot.to_excel(writer, sheet_name="Týždenný súhrn hodín")
    return output.getvalue()

# =====================================
# APLIKÁCIA
# =====================================
st.sidebar.title("📅 Prehľad dochádzky")

data = load_data()
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

start_date = datetime.combine(selected_week, datetime.min.time()).astimezone(tz)
end_date = start_date + timedelta(days=7)
df_week = data[(data["prichod"] >= start_date) & (data["prichod"] < end_date)]

if df_week.empty:
    st.warning("🔸 Rozsah nie je k dispozícii pre vybraný týždeň.")
    st.stop()

# =====================================
# SPRACOVANIE ÚDAJOV
# =====================================
df_week["den"] = df_week["prichod"].dt.strftime("%A")
df_week["hodiny"] = df_week.apply(
    lambda r: calculate_hours(r["prichod"], r["odchod"], r["pozicia"]),
    axis=1
)

# =====================================
# TÝŽDENNÝ SÚHRN
# =====================================
pivot = pd.pivot_table(
    df_week,
    values="hodiny",
    index="pozicia",
    columns="den",
    aggfunc="sum",
    fill_value=0
)

order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
pivot = pivot.reindex(columns=order, fill_value=0)
pivot.columns = ["Pondelok", "Utorok", "Streda", "Štvrtok", "Piatok", "Sobota", "Nedeľa"]
pivot["SUM"] = pivot.sum(axis=1)
pivot = pivot.round(2)

st.subheader("📊 Týždenný súhrn hodín")
st.dataframe(pivot, use_container_width=True)

# =====================================
# DENNÝ PREHĽAD
# =====================================
df_day = df_week[df_week["prichod"].dt.date == selected_day]

st.subheader(f"📋 Denný prehľad – {selected_day.strftime('%d.%m.%Y')}")

if df_day.empty:
    st.info("Žiadne záznamy pre tento deň.")
else:
    daily_summary = []
    for pozicia, group in df_day.groupby("pozicia"):
        total = group["hodiny"].sum()
        prichody = group["prichod"].dt.strftime("%H:%M").tolist()
        odchody = group["odchod"].dt.strftime("%H:%M").tolist()
        records = [f"{p}-{o}" for p, o in zip(prichody, odchody)]
        daily_summary.append({
            "Pozícia": pozicia,
            "Zmeny": " | ".join(records),
            "Hodiny": total
        })
    df_daily_summary = pd.DataFrame(daily_summary)
    st.dataframe(df_daily_summary, use_container_width=True)

# =====================================
# EXPORT DO EXCELU
# =====================================
excel_data = export_to_excel(df_day, pivot)
st.download_button(
    "⬇️ Exportovať do Excelu",
    data=excel_data,
    file_name=f"prehľad_{selected_week.strftime('%Y-%m-%d')}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
