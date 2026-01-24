import pandas as pd
from datetime import timedelta, time

# =========================
# KONŠTANTY
# =========================
SHIFT_END_STANDARD = time(23, 0)
MIDNIGHT_LIMIT = time(3, 0)

HOURS_VELITEL = 16.25
HOURS_SBS = 15.25
EXTRA_HOURS = 3.45

# =========================
# NAČÍTANIE DÁT
# =========================
def load_data(path):
    df = pd.read_csv(path, parse_dates=["timestamp"])
    return df.sort_values("timestamp")

# =========================
# POSUN DO PRECH. DŇA
# =========================
def adjust_day(ts):
    if ts.time() < MIDNIGHT_LIMIT:
        return (ts - timedelta(days=1)).date()
    return ts.date()

# =========================
# HLAVNÉ SPRACOVANIE
# =========================
def process_data(df):
    df["work_date"] = df["timestamp"].apply(adjust_day)

    results = {}

    for date, day_df in df.groupby("work_date"):
        day_result = {}
        extra_velitel = 0
        extra_sbs = 0

        for position, pos_df in day_df.groupby("position"):
            arrivals = pos_df[pos_df["action"] == "Príchod"]
            departures = pos_df[pos_df["action"] == "Odchod"]

            if departures.empty:
                continue

            last_departure = departures["timestamp"].max().time()

            is_velitel = position.lower() == "veliteľ"

            if last_departure <= SHIFT_END_STANDARD:
                day_result[position] = HOURS_VELITEL if is_velitel else HOURS_SBS
            else:
                day_result[position] = HOURS_VELITEL if is_velitel else HOURS_SBS
                if is_velitel:
                    extra_velitel += EXTRA_HOURS
                else:
                    extra_sbs += EXTRA_HOURS

        if extra_velitel > 0:
            day_result["Extra1 – Veliteľ"] = extra_velitel
        if extra_sbs > 0:
            day_result["Extra2 – SBS"] = extra_sbs

        results[date] = day_result

    return results

# =========================
# EXPORT DO EXCELU
# =========================
def export_excel(results, path):
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        weekly_sum = {}

        for date, data in results.items():
            df = pd.DataFrame.from_dict(data, orient="index", columns=["Hodiny"])
            df.to_excel(writer, sheet_name=str(date))

            for k, v in data.items():
                weekly_sum[k] = weekly_sum.get(k, 0) + v

        weekly_df = pd.DataFrame.from_dict(weekly_sum, orient="index", columns=["Spolu"])
        weekly_df.to_excel(writer, sheet_name="Týždeň")

# =========================
# SPUSTENIE
# =========================
if __name__ == "__main__":
    df = load_data("dochadzka.csv")
    results = process_data(df)
    export_excel(results, "prehľad.xlsx")
