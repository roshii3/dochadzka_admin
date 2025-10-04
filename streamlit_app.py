import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import pytz
from supabase import create_client, Client
import io

# ---------- CONFIG ----------
DATABAZA_URL = st.secrets["DATABAZA_URL"]
DATABAZA_KEY = st.secrets["DATABAZA_KEY"]
ADMIN_PASS = st.secrets.get("ADMIN_PASS")
databaze: Client = create_client(DATABAZA_URL, DATABAZA_KEY)

# ---------- SETTINGS ----------
tz = pytz.timezone("Europe/Bratislava")
POZICIE = ["Veliteľ", "CCTV", "Brány", "Sklad2", "Sklad3", "Turniket2", "Turniket3", "Plombovac2", "Plombovac3"]
ZMENY = {"ranna": (3, 14), "poobedna": (14, 23.59)}  # v hodinách


# ---------- FUNKCIE ----------
def nacitaj_data():
    try:
        data = databaze.table("attendance").select("*").execute()
        df = pd.DataFrame(data.data)
        if df.empty:
            return pd.DataFrame()
        df["prichod"] = pd.to_datetime(df["prichod"])
        df["odchod"] = pd.to_datetime(df["odchod"])
        df["datum"] = df["prichod"].dt.date
        return df
    except Exception as e:
        st.error(f"❌ Chyba pri načítaní údajov: {e}")
        return pd.DataFrame()


def ziskaj_dennu_zmenu(df, pozicia, datum):
    df_poz = df[(df["pozicia"] == pozicia) & (df["datum"] == datum)]
    if df_poz.empty:
        return "absent", "absent", 0.0

    ranna_ok = False
    poobedna_ok = False
    odprac_hod = 0.0

    for _, r in df_poz.iterrows():
        pr, od = r["prichod"], r["odchod"]
        if pd.isna(pr) or pd.isna(od):
            continue
        start_hour, end_hour = pr.hour + pr.minute / 60, od.hour + od.minute / 60

        # RANNÁ
        if start_hour < 12:
            ranna_ok = True
        # POOBEDNÁ
        if end_hour > 12:
            poobedna_ok = True

        trvanie = (od - pr).total_seconds() / 3600
        odprac_hod += trvanie

    # Logika výpočtu
    if ranna_ok and poobedna_ok:
        hodiny = 15.25
    elif ranna_ok or poobedna_ok:
        hodiny = 7.5
    else:
        hodiny = 0.0

    return (
        "✅" if ranna_ok else "❌",
        "✅" if poobedna_ok else "❌",
        hodiny
    )


def vytvor_tyzdenny_report(df, tyzden_start):
    days = [tyzden_start + timedelta(days=i) for i in range(7)]
    vysledok = []
    for pozicia in POZICIE:
        riadok = {"Pozícia": pozicia}
        sum_h = 0.0
        for d in days:
            _, _, h = ziskaj_dennu_zmenu(df, pozicia, d)
            riadok[d.strftime("%A")] = h
            sum_h += h
        riadok["SUM"] = round(sum_h, 2)
        vysledok.append(riadok)
    return pd.DataFrame(vysledok)


def export_do_excelu(df_tyzden):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_tyzden.to_excel(writer, index=False, sheet_name="Týždenný súhrn")
    output.seek(0)
    return output


# ---------- UI ----------
st.set_page_config(page_title="SBS Dochádzka", layout="wide")
st.markdown("""
    <style>
        header {visibility: hidden;}
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
    </style>
""", unsafe_allow_html=True)

st.title("🕒 Dochádzkový prehľad SBS")

df = nacitaj_data()

if df.empty:
    st.warning("⚠ Dáta nie sú dostupné.")
else:
    # Výber týždňa
    dnes = datetime.now(tz).date()
    monday = dnes - timedelta(days=dnes.weekday())
    selected_monday = st.sidebar.date_input(
        "📅 Vyber začiatok týždňa (pondelok)",
        value=monday
    )

    # Kontrola rozsahu dát
    df_datumy = df["datum"].unique()
    min_d, max_d = df["datum"].min(), df["datum"].max()

    if selected_monday < min_d or selected_monday > max_d:
        st.warning("⚠ Dáta pre zvolený rozsah nie sú k dispozícii.")
    else:
        selected_day = st.sidebar.date_input(
            "📆 Denný prehľad - vyber deň",
            value=dnes,
            min_value=selected_monday,
            max_value=selected_monday + timedelta(days=6)
        )

        # ----------- DENNÝ PREHĽAD -----------
        st.subheader(f"📋 Denný prehľad ({selected_day.strftime('%A %d.%m.%Y')})")
        cols = st.columns(3)
        for i, poz in enumerate(POZICIE):
            ranna, poobedna, h = ziskaj_dennu_zmenu(df, poz, selected_day)
            col = cols[i % 3]
            with col:
                st.markdown(
                    f"**{poz}**  \nRanná: {ranna}  \nPoobedná: {poobedna}  \n🕒 {h} h",
                    unsafe_allow_html=True
                )

        # ----------- TÝŽDENNÝ PREHĽAD -----------
        st.subheader("📊 Týždenný súhrn hodín")
        df_tyzden = vytvor_tyzdenny_report(df, selected_monday)
        st.dataframe(df_tyzden, use_container_width=True)

        # Export
        excel_data = export_do_excelu(df_tyzden)
        st.download_button(
            label="📥 Stiahnuť Excel report",
            data=excel_data,
            file_name=f"dochadzka_tyzden_{selected_monday}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
