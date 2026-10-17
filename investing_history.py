import requests
import pandas as pd
import json


def get_history_shares(instrument_id, year):
    START_DATE = str(year) + "-01-01"
    END_DATE = str(year) + "-12-31"
    url = f"https://api.investing.com/api/financialdata/historical/{instrument_id}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Domain-Id": "www",
        "Origin": "https://www.investing.com",
        "Referer": "https://www.investing.com/",
    }
    with requests.Session() as session:
        session.headers.update(headers)
        for timeframe in ("Monthly", "MONTHLY", "monthly", "1mo", "M"):
            params = {
                "start-date": START_DATE,
                "end-date": END_DATE,
                "time-frame": timeframe,
                "add-missing-rows": "false",
            }
            try:
                response = session.get(url, params=params, timeout=(5, 20))
                if response.status_code in (401, 403, 429):
                    return None
                response.raise_for_status()
                data = response.json()
                rows = data.get("data") if isinstance(data, dict) else None
                if rows:
                    df = pd.DataFrame(rows)
                    if "last_close" in df.columns:
                        df["last_close"] = pd.to_numeric(
                            df["last_close"].astype(str).str.replace(",", "", regex=False),
                            errors="coerce",
                        )
                    return df
            except (requests.RequestException, ValueError) as exc:
                print(f"Investing.com ({timeframe}): {exc}")
    return None


def main():
    instrument_id = "13711"
    # START_DATE = "2024-05-31"
    # END_DATE = "2026-05-31"
    year = 2026
    get_history_shares(instrument_id, year)


if __name__ == "__main__":
    main()