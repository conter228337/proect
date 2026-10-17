import datetime
import os
# Изменен коренной импорт под новый пакет t-tech-investments
from t_tech.invest import Client, CandleInterval, InstrumentIdType
from t_tech.invest.utils import quotation_to_decimal

# Переменные конфигурации
TOKEN = os.getenv("T_INVEST_TOKEN", "")  # токен задаётся в переменной окружения
CLASS_CODE = "TQBR"


def get_shares_info_tapi(TICKER, year):
    if not TOKEN:
        raise RuntimeError("Не задан T_INVEST_TOKEN в переменных окружения")
    START_DATE = datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc)
    END_DATE = datetime.datetime(year + 1, 1, 1, tzinfo=datetime.timezone.utc)
    # Используем новый клиент из t_tech
    with Client(TOKEN) as client:
        # 1. Поиск информации об инструменте по тикеру
        instrument_info = client.instruments.share_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=CLASS_CODE,
            id=TICKER
        ).instrument

        uid = instrument_info.uid

        # 4. Получение исторических дневных свечей за период
        # print(f"\nИсторические цены закрытия с {START_DATE.date()} по {END_DATE.date()}:")

        candles = client.market_data.get_candles(
            instrument_id=uid,
            from_=START_DATE,
            to=END_DATE,
            interval=CandleInterval.CANDLE_INTERVAL_MONTH
        ).candles

        results = []
        for candle in candles:
            close_price = quotation_to_decimal(candle.close)
            candle_date = candle.time.strftime("%Y-%m-%d")
            results.append({"date": candle_date, "close": float(close_price), "currency": instrument_info.currency})
            print(f"Дата: {candle_date} | Цена закрытия: {close_price} {instrument_info.currency}")
        return results


if __name__ == "__main__":
    TICKER = "SBER"
    # Период для исторических данных
    START_DATE = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    END_DATE = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)
    year = 2026

    get_shares_info_tapi(TICKER, year)
