import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import pytz
import io

# ========== KONFIGURÁCIA ==========
st.set_page_config(page_title="Dochádzka SBS", layout="wide")
hide_streamlit_style = """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    </style>
"""
st.markdown(hide_streamlit_style, unsafe_allow_html=True)

tz = pytz.timezone("Europe/Bratislava")

# ========== NAČÍTANIE DÁT ==========
@st.cache_data
def load_data():
    # Tu si pripoj DB (alebo pre test CSV)
    df = pd.read_csv("dochadzka.csv")
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["action"] = df["action"].str.replace("Ă", "Á").str.replace("ľ", "ľ").str.strip()
    return df

df = load_data()

# ========== POMOCNÉ FUNKCIE ==========
def calculate_hours(row_group):
    if len(row_group) < 2:
        return 0
    times = row_group["timestamp"].sort_values().tolist()
    if len(times) % 2 != 0:
        times = times[:-1]  # odstráň neúplné záznamy
    total = sum([(times[i+1]-times[i]).total_seconds()/3600 for i in range(0, len(times), 2)])
    return round(total, 2)

def calculate_hours_matrix(df_week, monday):
    matrix = {}
    for pos in sorted(df_week["position"].unique()):
        matrix[pos] = []
        for i in range(7):
            day = monday + timedelta(days=i)
            day_records = df_week[(df_week["timestamp"].dt.date == day.date()) & (df_week["position"] == pos)]
            total_hours = calculate_hours(day_records)
            # Logika pre r+P smenu
            if 15 < total_hours < 16.3:
                total_hours = 16.25
            elif total_hours >= 7 and total_hours < 8:
                total_hours = 7.5
            elif total_hours > 8 and total_hours < 15:
                total_hours = 15.0
            matrix[pos].append(total_hours)
    df_matrix = pd.DataFrame(matrix, index=["Pondelok","Utorok","Streda","Štvrtok","Piatok","Sobota","Nedeľa"]).T
    df_matrix["SUM"] = df_matrix.sum(axis=1)
    return df_matrix

def highlight_hours(val):
    if val == 0:
        color = 'lightcoral'
    elif val in (7.5, 15, 16.25):
        color = 'lightgreen'
    else:
        color = 'khaki'
    return f'background-color: {color}'

# ========== VÝBER TÝŽDŇA ==========
st.sidebar.header("Nastavenie")
today = datetime.now(tz).date()
week_offset = st.sidebar.number_input("Posuň týždeň (-1 = minulý, 0 = aktuálny, 1 = budúci)", -10, 10, 0)
monday = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)

selected_day = st.sidebar.date_input(
    "Denný prehľad - vyber deň",
    value=today,
    min_value=monday,
    max_value=monday + timedelta(days=6)
)

df_week = df[(df["timestamp"].dt.date >= monday) & (df["timestamp"].dt.date <= monday + timedelta(days=6))]

if df_week.empty:
    st.warning("📅 Dáta pre tento týždeň nie sú k dispozícii.")
else:
    df_day = df_week[df_week["timestamp"].dt.date == selected_day]
    if df_day.empty:
        st.warning("📅 Dáta pre tento deň nie sú k dispozícii.")
    else:
        st.header(f"Denný prehľad - {selected_day.strftime('%d.%m.%Y')}")

        for position in sorted(df_day["position"].unique()):
            pos_data = df_day[df_day["position"] == position]
            total = calculate_hours(pos_data)
            if 15 < total < 16.3:
                total = 16.25
            elif total >= 7 and total < 8:
                total = 7.5
            elif total > 8 and total < 15:
                total = 15.0
            st.markdown(f"**{position}** — {total} h")

    # ========== TÝŽDENNÁ TABUĽKA ==========
    st.header("📊 Týždenný súhrn hodín podľa pozícií")
    hours_matrix = calculate_hours_matrix(df_week, monday)
    st.dataframe(hours_matrix.style.applymap(highlight_hours))

    # ========== EXPORT DO EXCELU ==========
    st.subheader("📤 Export do Excelu")
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
        df_day.to_excel(writer, sheet_name="Denný_prehlad", index=False)
        hours_matrix.to_excel(writer, sheet_name="Týždenný_súhrn")
        writer.close()
    st.download_button(
        label="⬇️ Stiahnuť Excel report",
        data=buffer.getvalue(),
        file_name=f"report_{monday}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
