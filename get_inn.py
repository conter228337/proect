"""Список компаний для анализа.

Раньше ИНН собирались с Мосбиржи (по тикерам); теперь берутся ВСЕ ИНН компаний из RFSD
(Russian Financial Statements Database) за годы MIN_YEAR..MAX_YEAR. Тикеров и названий в RFSD нет,
поэтому в возвращаемых записях 'ticker' и 'name' пустые, а основной идентификатор компании - 'inn'.
Прежний сбор с Мосбиржи сохранён в collect_companies_data_moex().
"""
import time

import rfsd

OUTPUT_FILENAME = "rfsd_all_inns.csv"


def collect_companies_data(years=None, limit=None, save_to=OUTPUT_FILENAME):
    """-> [{'ticker': '', 'inn': '7707083893', 'name': ''}, ...] по всем ИНН из RFSD.

    years  - ограничить годы RFSD (по умолчанию MIN_YEAR..MAX_YEAR из config.py);
    limit  - взять только первые N ИНН (для отладки);
    save_to - CSV со списком ИНН и годами присутствия в RFSD (None = не сохранять).
    """
    print("1. Сбор списка ИНН из RFSD...")
    df = rfsd.list_inns(years)
    if df.empty:
        print("Ошибка: в RFSD не найдено ни одного ИНН.")
        return []
    print(f"Найдено {len(df)} уникальных ИНН в RFSD.")

    if save_to:
        try:
            # CSV, а не Excel: ИНН может быть больше миллиона (лимит строк Excel - 1 048 576)
            df.to_csv(save_to, index=False, sep=";", encoding="utf-8-sig")
            print(f"Список ИНН сохранён в файл: {save_to}")
        except PermissionError:
            print(f"[ОШИБКА!] Закройте файл {save_to} и запустите программу заново.")

    if limit:
        df = df.head(int(limit))
    return [{"ticker": "", "inn": inn, "name": ""} for inn in df["inn"]]


def collect_companies_data_moex():
    """Прежний способ: тикеры и ИНН с Мосбиржи (ISS API). Нужны requests, apimoex, pandas."""
    import pandas as pd
    import requests
    import apimoex

    start_year, end_year = 2018, 2026
    years = list(range(start_year, end_year + 1))
    status_by_year = {}
    with requests.Session() as session:
        print("1. Сбор базового списка акций с Мосбиржи...")
        securities = apimoex.get_board_securities(session, board='TQBR', market='shares', engine='stock')
        if not securities:
            print("Ошибка: не удалось получить данные от MOEX ISS.")
            return []
        df_sec = pd.DataFrame(securities)
        ticker_dict = dict(zip(df_sec['SECID'], df_sec['SHORTNAME']))
        print(f"Найдено {len(ticker_dict)} уникальных тикеров. Начинаем сбор ИНН...")
        for idx, (ticker, shortname) in enumerate(ticker_dict.items(), 1):
            inn = "Не найден"
            try:
                url = f"https://iss.moex.com/iss/securities.json?q={ticker}&iss.meta=off&iss.only=securities"
                response = session.get(url, timeout=5)
                if response.status_code == 200:
                    raw = response.json()
                    cols = [c.lower() for c in raw['securities']['columns']]
                    if 'secid' in cols and 'emitent_inn' in cols:
                        si, ii = cols.index('secid'), cols.index('emitent_inn')
                        for r in raw['securities']['data']:
                            if r[si] == ticker and r[ii] and str(r[ii]).strip() not in ["", "nan", "None"]:
                                inn = str(r[ii]).strip()
                                break
            except Exception as e:
                print(f"[{idx}/{len(ticker_dict)}] {ticker}: ИНН не получен ({type(e).__name__}: {e})")
            status_by_year[ticker] = {'Название': shortname, 'ИНН': inn}
            print(f"[{idx}/{len(ticker_dict)}] ИНН {inn} (тикер {ticker}, {shortname})")
            time.sleep(0.04)
    return [{'ticker': t, 'inn': d['ИНН'], 'name': d['Название']} for t, d in status_by_year.items()]


if __name__ == "__main__":
    result = collect_companies_data(limit=1000)
    print("\nРезультат функции (первые 3 компании):")
    print(result[:3])
