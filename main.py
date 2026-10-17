"""
Сбор финансовых и рыночных показателей по российским компаниям
Источники:
- MOEX ISS API (рыночные данные: котировки, капитализация)
- disclosure.ru (бухгалтерская отчётность РСБУ в формате XML)
- ФНС bo.nalog.ru (дополнительный источник для отчётности)

Для работы кода необходимы библиотеки:
pip install requests pandas numpy openpyxl lxml

Анализ тональности сохранённых отчётов: pip install transformers torch pymupdf
Обучение годовых прогнозов: pip install scikit-learn
"""
from urllib.parse import urljoin

import requests
import pandas as pd
import numpy as np
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import re
import time
import warnings

from bs4 import BeautifulSoup

# from scraper_rbc_news import RbcScraper
from datetime import datetime

from config import MIN_YEAR, MAX_YEAR
import bo_nalog
from bo_nalog import get_bo_nalog_data, get_bo_nalog_data_all_years
import rfsd
from rfsd import get_rfsd_data, get_rfsd_data_all_years
from e_disclosure import get_rating, get_disclosure_info, get_default_events, get_annual_reports_text
from get_inn import collect_companies_data
from logutil import log, log_error, set_company, clear_company, save_errors
from investing_history import get_history_shares
from investing_id_count_years import get_data_selenium_all_years
from investing_search import find_company_url_search_page
from report_sentiment import analyze_report_paths

# RFSD на Hugging Face открытый, токен не нужен. Если понадобится, задайте переменную среды HF_TOKEN
# (huggingface_hub подхватит её сам), а в код ключи не вставляйте.

# Что собирать. Выключенные блоки не запускаются и не попадают в таблицу.
MARKET_ENABLED = False   # MOEX/Investing: капитализация, цена, волатильность, MTB
TEXTS_ENABLED = True     # пути к сохранённым файлам годового отчёта, аудиторского заключения и пояснительной записки (без OCR)

DEFAULT_EVENTS = []       # все сообщения о неисполнении обязательств (по одной строке на событие)
SKIPPED = []             # компании, которых нет в итоговой таблице, с причиной


def calculate_volatility(price_series, periods_per_year=12):
    """
    Расчёт годовой волатильности акции на основе доходностей за период
    Параметры:
        price_series: Series с ценами закрытия
        periods_per_year: сколько периодов в году (12 - месячные цены, 252 - дневные)
    Возвращает годовую волатильность (доля, не проценты)
    """
    prices = pd.to_numeric(pd.Series(price_series), errors="coerce")
    prices = prices.replace([np.inf, -np.inf], np.nan).dropna()
    if len(prices) < 2:
        return np.nan

    # Доходность не определена при нулевой предыдущей цене.
    returns = prices.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if returns.empty:
        return np.nan

    # Стандартное отклонение доходностей за период
    period_volatility = returns.std()

    # Годовая волатильность: масштабируем на корень из числа периодов в году
    annual_volatility = period_volatility * np.sqrt(periods_per_year)

    return annual_volatility


# ======================== 2. ПОЛУЧЕНИЕ БУХГАЛТЕРСКОЙ ОТЧЁТНОСТИ ========================

def get_financials_from_disclosure(inn, year):
    """
    Получение финансовых показателей из бухгалтерской отчётности (РСБУ)
    через сайт bo.nalog.ru
    Параметры:
        inn: ИНН компании
        year: год отчётности
    Возвращает словарь с финансовыми показателями
    """
    # Формируем URL для поиска отчётности компании по ИНН
    # Примечание: disclosure.ru требует парсинга HTML, это упрощённая версия
    # Для production рекомендуется использовать платные API (СПАРК, Контур.Фокус)

    financials = {
        'total_assets': np.nan,  # Итого активы (строка 1600)
        'total_liabilities': np.nan,  # Итого обязательства (строка 1400 + 1500)
        'equity': np.nan,  # Собственный капитал (строка 1300)
        'revenue': np.nan,  # Выручка (строка 2110)
        'net_income': np.nan,  # Чистая прибыль (строка 2400)
        'ebitda': np.nan,  # EBITDA (приблизительный расчёт)
        'current_assets': np.nan,  # Оборотные активы (строка 1200)
        'current_liabilities': np.nan,  # Краткосрочные обязательства (строка 1500)
        'cash': np.nan  # Денежные средства (строка 1250)
    }

    # financials_full = get_bo_nalog_data(inn, year)
    financials_full = get_rfsd_data(inn, year)
    if not financials_full:
        # ИНН не найден на bo.nalog.ru или отчётности нет: возвращаем пустые значения
        return financials

    # exclude_keys = {'period', 'actual_date', 'has_audit'}
    exclude_keys = {'period', 'has_audit'}
    financials = {k: v for k, v in financials_full.items() if k not in exclude_keys}

    # ПРИМЕЧАНИЕ: В реальном коде здесь был бы парсинг disclosure.ru или запрос к API
    # Для демонстрации используем тестовые данные
    # В production-решении рекомендуется использовать платные API:
    # - СПАРК API (https://www.spark-interfax.ru/)
    # - Контур.Фокус API (https://focus.kontur.ru/)
    # financials["ebitda"] = financials["net_income"] + financials["tax_expense"] + financials["interest_payable"] + financials["depreciation"]

    return financials


# def get_bo_nalog_data(inn):


# Новости получить

def calculate_financial_ratios(financials):
    """Безопасно рассчитывает коэффициенты; пропуски и некорректные числа дают NaN."""
    def number(key):
        try:
            value = float(financials.get(key, np.nan))
        except (TypeError, ValueError):
            return np.nan
        return value if np.isfinite(value) else np.nan

    assets = number("total_assets")
    liabilities = number("total_liabilities")
    equity = number("equity")
    net_income = number("net_income")
    ebitda = number("ebitda")
    current_assets = number("current_assets")
    current_liabilities = number("current_liabilities")
    cash = number("cash")

    def ratio(numerator, denominator, scale=1.0):
        if np.isfinite(numerator) and np.isfinite(denominator) and denominator != 0:
            return numerator / denominator * scale
        return np.nan

    return {
        "ROA": ratio(net_income, assets, 100),
        "ROE": ratio(net_income, equity, 100),
        "Debt_to_EBITDA": ratio(liabilities, ebitda),
        "Total_Liabilities_to_Assets": ratio(liabilities, assets),
        "Current_Ratio": ratio(current_assets, current_liabilities),
        "Cash_to_Assets": ratio(cash, assets),
        "Ln_Total_Assets": float(np.log(assets)) if np.isfinite(assets) and assets > 0 else np.nan,
    }


FINANCIAL_KEYS = ('total_assets', 'total_liabilities', 'equity', 'revenue', 'net_income',
                  'ebitda', 'current_assets', 'current_liabilities', 'cash')


def _fetch_all_financials(inn):
    """Отчётность с bo.nalog.ru по всем доступным годам: ({год: financials}, был_ли_сбой_загрузки)."""
    # by_year, error = {}, False
    # for attempt in range(1, 4):
    #     try:
    #         by_year = get_bo_nalog_data_all_years(inn, years=set(range(MIN_YEAR, MAX_YEAR + 1)))
    #         # пусто + сетевая ошибка = не смогли загрузить (а не «отчётности нет»)
    #         error = bool(bo_nalog.LAST_ERROR) and not by_year
    #     except Exception as e:
    #         by_year, error = {}, True
    #         print(f"  Отчётность недоступна для ИНН {inn}: {type(e).__name__}: {e}")
    #     if not error:
    #         break
    #     if attempt < 3:
    #         print(f"  Повтор загрузки отчётности ({attempt + 1}/3) через 5 с...")
    #         time.sleep(5)
    # return by_year, error

    # --- загрузка из RFSD (отключена) ---
    by_year, error = {}, False
    try:
        by_year = get_rfsd_data_all_years(inn, years=set(range(MIN_YEAR, MAX_YEAR + 1)))
        # пусто + ошибка чтения = не смогли загрузить (а не «отчётности нет»)
        error = bool(rfsd.LAST_ERROR) and not by_year
    except Exception as e:
        by_year, error = {}, True
        log_error("RFSD", e)
    return by_year, error


def collect_company_data(ticker, inn, years=None):
    """Обёртка: на время обработки компании весь вывод получает префикс [ИНН ...] (см. logutil),
    поэтому любое сообщение или ошибка из любого источника привязаны к компании."""
    print(f"\n=== ИНН {inn or 'не найден'}{f' (тикер {ticker})' if ticker else ''} ===")
    set_company(inn, ticker)
    try:
        return _collect_company_data(ticker, inn, years)
    finally:
        clear_company()


def _collect_company_data(ticker, inn, years=None):
    """
    Сбор всех показателей по одной компании за ВСЕ доступные годы
    Параметры:
        ticker: тикер акции (обязательный)
        inn: ИНН компании
        years: необязательный список годов (None = все, за которые есть хоть какие-то данные)
    Возвращает список словарей - по одной строке на каждый год
    """
    if inn is None:
        inn = ''

    # ===== 1. Источники, которые отдают сразу все годы (запрашиваем один раз на компанию) =====
    # Любая ошибка с рынком (нет страницы на investing, сбой selenium) не должна выкидывать
    # компанию: рейтинг, дефолт и отчётность всё равно соберём.
    count_shares = None
    try:
        if not MARKET_ENABLED:
            raise ValueError('MARKET_ENABLED=False')
        link = find_company_url_search_page(ticker)
        if link is None:
            raise ValueError("страница компании на investing.com не найдена")
        for attempt in (1, 2):
            count_shares = get_data_selenium_all_years(link)
            if count_shares and 'instrument_id' in count_shares:
                break
            if attempt == 1:
                log("investing.com: повторная попытка загрузки...")
                time.sleep(5)
        if not (count_shares and 'instrument_id' in count_shares):
            count_shares = None
    except Exception as e:
        count_shares = None
        if MARKET_ENABLED:
            log_error("investing.com", e)
    if count_shares:
        log(f"investing.com: данные по акциям получены, годы {sorted(map(str, count_shares['diluted_shares_by_year']))}")

    if not inn or inn == 'Не найден':
        SKIPPED.append({'ticker': ticker, 'inn': inn, 'reason': 'ИНН не найден'})
        log("пропуск: ИНН не найден")
        return []

    financials_by_year, fin_error = _fetch_all_financials(inn)
    if financials_by_year:
        log(f"RFSD: отчётность найдена за годы {sorted(financials_by_year)}")
    elif fin_error:
        log_error("RFSD", "отчётность не загружена (сбой чтения данных)")
    else:
        log("RFSD: компании нет в датасете - пропускаю")
    if not financials_by_year and not fin_error:
        SKIPPED.append({'ticker': ticker, 'inn': inn,
                        'reason': 'нет в RFSD (банк/страховщик/фонд/иностранная компания или ИНН не совпал)'})
        return []

    # ===== 2. Какие годы брать =====
    # Объединение годов, за которые есть отчётность (bo.nalog) или данные по количеству акций (investing)
    shares_by_year = {}
    if count_shares:
        for y, v in count_shares['diluted_shares_by_year'].items():
            try:
                if int(y) >= MIN_YEAR:
                    shares_by_year[int(y)] = v
            except (TypeError, ValueError):
                pass
    available = set(financials_by_year) | set(shares_by_year)
    # Всегда берём ВСЕ годы MIN_YEAR..MAX_YEAR (2018-2026), а не только те, по которым сайты что-то отдали
    available |= set(range(MIN_YEAR, MAX_YEAR + 1))
    available = {y for y in available if MIN_YEAR <= y <= MAX_YEAR}

    # ===== 2a. Годовые отчёты с e-disclosure + Tesseract =====
    # Берём ВСЕ доступные годовые отчёты. Они не ограничиваются REPORT_YEARS:
    # входом является ИНН, а годы определяются непосредственно на странице "Годовая".
    annual_reports_by_year = {}
    # Файлы сохраняются на диск как есть (PDF/ZIP), распознавание Tesseract отключено (см. annual_reports_ocr.py)
    if TEXTS_ENABLED:
        try:
            annual_reports_by_year = get_annual_reports_text(inn, candidate_years=available)
            available |= {y for y in annual_reports_by_year if MIN_YEAR <= y <= MAX_YEAR}
        except Exception as e:
            log_error("e-disclosure: документы", e)
        else:
            log("e-disclosure документы: годовых отчётов {}, аудиторских заключений {}, пояснительных записок {}".format(
                *(sum(1 for v in annual_reports_by_year.values() if v.get(k)) for k in
                  ('annual_path', 'audit_path', 'explanatory_path'))))

    if years is not None:
        available &= set(years)
    year_list = sorted(available)
    log(f"Годы: {year_list if year_list else 'не найдены'}")

    if not year_list:
        # Ни bo.nalog, ни investing годов не дали (например, ETF): берём последний полный год,
        # чтобы рейтинг/дефолт (e-disclosure) и признак «публичного отчёта» всё равно заполнились
        fallback = (max(min(years), MIN_YEAR) if years else datetime.now().year - 1)
        year_list = [fallback]
        log(f"Годы не определены, беру {fallback}")

    # ===== 3. Расчёт по каждому году =====
    rows = []
    disclosure_ok = True  # после первого сбоя e-disclosure для этой компании остальные годы не мучаем
    for year in year_list:
        report_paths = annual_reports_by_year.get(year, {})
        result = {'ticker': ticker, 'inn': inn, 'company_name': '', 'isin': '', 'year': year,
                  'annual_report_path': report_paths.get('annual_path', '') if isinstance(report_paths, dict) else '',
                  'audit_report_path': report_paths.get('audit_path', '') if isinstance(report_paths, dict) else '',
                  'explanatory_note_path': report_paths.get('explanatory_path', '') if isinstance(report_paths, dict) else ''}

        # Тональность годового отчёта и пояснительной записки; аудиторское заключение
        # не включаем, поскольку оно отражает мнение аудитора, а не текст эмитента.
        sentiment_paths = [
            result['annual_report_path'],
            result['explanatory_note_path'],
        ]
        try:
            sentiment = analyze_report_paths([p for p in sentiment_paths if p])
        except Exception as e:
            log_error("анализ тональности отчётов", e, year=year)
            sentiment = {
                'sentiment_score': np.nan,
                'sentiment_positive': np.nan,
                'sentiment_neutral': np.nan,
                'sentiment_negative': np.nan,
                'sentiment_chunks': 0,
                'sentiment_text_chars': 0,
                'sentiment_document_count': len([p for p in sentiment_paths if p]),
                'sentiment_model': '',
                'sentiment_status': f'error: {type(e).__name__}',
            }
        result.update(sentiment)

        # --- рыночные показатели ---
        market_cap = np.nan
        result['market_cap_million_rub'] = np.nan
        result['stock_volatility'] = np.nan
        result['current_price'] = np.nan
        if count_shares and year is not None:
            try:
                history_price = get_history_shares(count_shares['instrument_id'], str(year))

                # Средняя по месяцам цена закрытия
                if history_price is None or history_price.empty or 'last_close' not in history_price.columns:
                    raise ValueError('история не содержит last_close')
                last_close = pd.to_numeric(
                    history_price['last_close'].astype(str).str.replace(',', '', regex=False),
                    errors='coerce',
                ).dropna()
                if last_close.empty:
                    raise ValueError('нет корректных цен закрытия')
                avg_price = last_close.mean()

                shares = pd.to_numeric(pd.Series([shares_by_year.get(year, np.nan)]), errors='coerce').iloc[0]
                market_cap = (avg_price * shares) / 1_000_000
                result['market_cap_million_rub'] = market_cap

                if not last_close.empty:
                    result['stock_volatility'] = calculate_volatility(last_close)
                    result['current_price'] = last_close.iloc[-1]
            except Exception as e:
                log_error("investing.com", e, year=year)

        # --- отчётность ---
        raw = financials_by_year.get(year)
        if raw:
            financials = {k: v for k, v in raw.items() if k not in {'period', 'has_audit'}}
        else:
            financials = {k: np.nan for k in FINANCIAL_KEYS}

        # --- рейтинг и дефолт (e-disclosure) ---
        disclosure_info = {'rating': None, 'default': np.nan}
        if disclosure_ok and year is not None:
            try:
                disclosure_info = get_disclosure_info(inn, year)
            except Exception as e:
                disclosure_ok = False
                log_error("e-disclosure", e, year=year)
                log("e-disclosure отключён для остальных лет этой компании")
        result['rating'] = disclosure_info['rating']
        result['default'] = disclosure_info['default']
        result['default_events'] = disclosure_info.get('default_events', '')
        result['default_dates'] = disclosure_info.get('default_dates', '')
        result['default_bond_n'] = disclosure_info.get('default_bond_n', np.nan)
        result['default_share_n'] = disclosure_info.get('default_share_n', np.nan)
        result['default_other_n'] = disclosure_info.get('default_other_n', np.nan)
        result['default_obligation'] = disclosure_info.get('default_obligation', '')
        result['default_unpaid_rub'] = disclosure_info.get('default_unpaid_rub', np.nan)
        result['default_accel_n'] = disclosure_info.get('default_accel_n', np.nan)
        result['default_hard_n'] = disclosure_info.get('default_hard_n', np.nan)

        # Публичный отчёт: 1 - отчётность за год найдена; 0 - у компании её нет;
        # пусто - не удалось загрузить (сбой сети), значение неизвестно
        if pd.notna(financials.get('filed')):
            result['public_report'] = int(financials['filed'])   # по флагу RFSD
        elif pd.notna(financials.get('total_assets')):
            result['public_report'] = 1
        elif fin_error:
            result['public_report'] = np.nan
        else:
            result['public_report'] = 0

        ratios = calculate_financial_ratios(financials)
        result.update(financials)  # Исходные финансовые данные
        result.update(ratios)  # Рассчитанные коэффициенты

        # MTB (Market-to-Book) = Рыночная капитализация / Собственный капитал
        if not np.isnan(market_cap) and not np.isnan(result.get('equity', np.nan)) and result['equity'] != 0:
            result['MTB'] = (market_cap * 1_000_000) / result['equity']
        else:
            result['MTB'] = np.nan

        rows.append(result)

    # Таблица событий: все годы MIN_YEAR..текущий (включая год, которого нет в панели признаков),
    # чтобы дефолты последних месяцев не терялись. Из неё потом строится целевая переменная.
    if disclosure_ok:
        for y in range(MIN_YEAR, MAX_YEAR + 1):
            try:
                for ev in get_default_events(inn, y):
                    ev['ticker'] = ticker
                    DEFAULT_EVENTS.append(ev)
            except Exception as e:
                log_error("e-disclosure: события дефолта", e, year=y)
                break

    return rows


# ======================== 4. ЗАПУСК СБОРА ДАННЫХ ========================

def dedupe_by_inn(companies):
    """Одна строка на ИНН; остальные тикеры того же эмитента сохраняются в also_tickers."""
    seen, out = {}, []
    for c in companies:
        inn = c['inn']
        if inn and inn != 'Не найден' and inn in seen:
            seen[inn]['also_tickers'].append(c['ticker'])
            continue
        c = dict(c, also_tickers=[])
        out.append(c)
        if inn and inn != 'Не найден':
            seen[inn] = c
    return out


def main():
    """
    Основная функция для сбора данных по списку компаний
    """

    # Список компаний для анализа (тикер + ИНН)
    # Для реального использования можно загрузить список из файла или через MOEX API
    companies_to_analyze = dedupe_by_inn(collect_companies_data())
#     companies_to_analyze = [
#     {'ticker': 'ABIO', 'inn': '7702508905', 'name': 'iАРТГЕН ао'},
#     {'ticker': 'ABRD', 'inn': '7727620673', 'name': 'АбрауДюрсо'},
#     {'ticker': 'AFKS', 'inn': '7703104630', 'name': 'Система ао'},
#     {'ticker': 'AFLT', 'inn': '7712040126', 'name': 'Аэрофлот'},
#     {'ticker': 'AKAI', 'inn': '7728142469', 'name': 'AKAI ETF'},
#     {'ticker': 'AKBC', 'inn': '7728142469', 'name': 'AKBC ETF'},
#     {'ticker': 'AKCN', 'inn': '7728142469', 'name': 'AKCN ETF'},
#     {'ticker': 'AKFB', 'inn': '7728142469', 'name': 'AKFB ETF'},
#     {'ticker': 'AKFN', 'inn': '7728142469', 'name': 'AKFN ETF'},
#     {'ticker': 'AKGD', 'inn': '7728142469', 'name': 'AKGD ETF'},
#     {'ticker': 'AKGP', 'inn': '7728142469', 'name': 'AKGP ETF'},
#     {'ticker': 'AKHT', 'inn': '7728142469', 'name': 'AKHT ETF'},
#     {'ticker': 'AKIE', 'inn': '7728142469', 'name': 'ETF AKIE'},
#     {'ticker': 'AKMB', 'inn': '7728142469', 'name': 'ETF AKMB'},
#     {'ticker': 'AKMC', 'inn': '7728142469', 'name': 'AKMC ETF'},
#     {'ticker': 'AKME', 'inn': '7728142469', 'name': 'ETF AKME'},
#     {'ticker': 'AKMM', 'inn': '7728142469', 'name': 'AKMM ETF'},
#     {'ticker': 'AKMP', 'inn': '7728142469', 'name': 'AKMP ETF'},
#     {'ticker': 'AKPP', 'inn': '7728142469', 'name': 'AKPP ETF'},
#     {'ticker': 'AKQU', 'inn': '7728142469', 'name': 'AKQU ETF'},
#     {'ticker': 'AKRN', 'inn': '5321029508', 'name': 'Акрон'},
#     {'ticker': 'AKUP', 'inn': '7728142469', 'name': 'AKUP ETF'},
#     {'ticker': 'ALRS', 'inn': '1433000147', 'name': 'АЛРОСА ао'},
#     {'ticker': 'AMEZ', 'inn': '7401000473', 'name': 'АшинскийМЗ'},
#     {'ticker': 'AMFL', 'inn': '7701253764', 'name': 'AMFL ETF'},
#     {'ticker': 'AMGB', 'inn': '7701253764', 'name': 'AMGB ETF'},
#     {'ticker': 'AMGL', 'inn': '7701253764', 'name': 'AMGL ETF'},
#     {'ticker': 'AMIX', 'inn': '7701253764', 'name': 'AMIX ETF'},
#     {'ticker': 'AMNR', 'inn': '7701253764', 'name': 'AMNR ETF'},
#     {'ticker': 'AMNY', 'inn': '7701253764', 'name': 'AMNY ETF'},
#     {'ticker': 'AMRE', 'inn': '7701253764', 'name': 'AMRE ETF'},
#     {'ticker': 'AMRH', 'inn': '7701253764', 'name': 'AMRH ETF'},
#     {'ticker': 'AMVD', 'inn': '7701253764', 'name': 'AMVD ETF'},
#     {'ticker': 'AMVY', 'inn': '7701253764', 'name': 'AMVY ETF'},
# ]

    # companies_to_analyze = [

    # {'ticker': 'AKRN', 'inn': '5321029508', 'name': 'Акрон'},
        # {'ticker': 'ABIO', 'inn': '7702508905', 'name': 'iАРТГЕН ао'},
        # {'ticker': 'AMFL', 'inn': '5410779580', 'name': 'ОР ГРУПП'},
        # {'ticker': 'ASTR', 'inn': '7707083893', 'name': 'Сбербанк'},
        # {'ticker': 'AVAN', 'inn': '7736050003', 'name': 'Газпром'},
        # {'ticker': 'BANE', 'inn': '7710000000', 'name': 'Лукойл'},
        # {'ticker': 'BANEP', 'inn': '7706107510', 'name': 'Роснефть'},
        # {'ticker': 'BLNG', 'inn': '1645000000', 'name': 'Татнефть'},
        # {'ticker': 'BISVP', 'inn': '1645000000', 'name': 'Татнефть'},
        # {'ticker': 'CARM', 'inn': '1645000000', 'name': 'Татнефть'},
        # {'ticker': 'ELMT', 'inn': '1645000000', 'name': 'Татнефть'},
        # {'ticker': 'FEES', 'inn': '1645000000', 'name': 'Татнефть'},
    #     {'ticker': 'TATN', 'inn': '1645000000', 'name': 'Татнефть'},
    # ]

    # Годы отчётности: None = все доступные для каждой компании.
    # Для отладки можно ограничить, например: REPORT_YEARS = range(2018, 2026)  или  [2024, 2025]
    REPORT_YEARS = None

    # True = не обрабатывать ETF (в названии есть 'ETF'): у них нет отчётности и данных по акциям
    SKIP_ETF = True

    # Предзагрузка отчётности RFSD пачками по RFSD_BATCH компаний: ИНН может быть миллионы, и держать
    # отчётность всех сразу в памяти нельзя. Перед каждой пачкой кэш предыдущей очищается.
    RFSD_BATCH = 2000
    def _is_etf(c):
        return SKIP_ETF and re.search(r'\bETF\b', c['name'], flags=re.I)

    # Сбор данных по всем компаниям
    all_data = []

    partial_file = f'company_analysis_partial_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'

    total = len(companies_to_analyze)
    for idx, company in enumerate(companies_to_analyze):
        if idx % RFSD_BATCH == 0:
            batch = [c['inn'] for c in companies_to_analyze[idx:idx + RFSD_BATCH] if not _is_etf(c)]
            print(f"\n##### Компании {idx + 1}-{min(idx + RFSD_BATCH, total)} из {total}: предзагрузка RFSD #####")
            rfsd.clear_cache()
            rfsd.preload(batch)
        if _is_etf(company):
            print(f"Пропускаю ИНН {company['inn'] or 'не найден'} (тикер {company['ticker']}, {company['name']}): ETF, отчётности и данных по акциям нет")
            continue
        try:
            rows = collect_company_data(
                ticker=company['ticker'],
                inn=company['inn'],
                years=REPORT_YEARS
            )
            for row in rows:
                row['company_name'] = company['name']
                row['also_tickers'] = ','.join(company.get('also_tickers', []))
            all_data.extend(rows)

            # Чекпоинт: при падении на длинном прогоне собранное не теряется
            pd.DataFrame(all_data).to_csv(partial_file, index=False, sep=';', decimal=',',
                                          encoding='utf-8-sig')

            # Задержка между запросами для соблюдения этики
            time.sleep(1)

        except KeyboardInterrupt:
            print("Остановлено вручную: сохраняю то, что уже собрано.")
            break
        except Exception as e:
            log_error("обработка компании", e, inn=company['inn'], ticker=company['ticker'])
            continue

    # Создаём DataFrame с результатами
    df_results = pd.DataFrame(all_data)

    # Выводим результаты в консоль
    print("\n" + "=" * 80)
    print("РЕЗУЛЬТАТЫ АНАЛИЗА КОМПАНИЙ")
    print("=" * 80)

    # Выбираем только основные показатели для отображения
    display_columns = [
        'inn', 'ticker', 'also_tickers', 'company_name', 'year',
        'market_cap_million_rub', 'stock_volatility', 'current_price', 'total_assets',
        'total_liabilities', 'equity', 'revenue', 'net_income', 'ebitda', 'current_assets',
        'current_liabilities', 'cash', 'ROA', 'ROE', 'Debt_to_EBITDA', 'Total_Liabilities_to_Assets',
        'Current_Ratio', 'Cash_to_Assets', 'Ln_Total_Assets', 'MTB',
        'filed', 'outlier', 'rating', 'default', 'default_bond_n', 'default_share_n', 'default_other_n', 'default_obligation', 'default_unpaid_rub', 'default_accel_n', 'default_hard_n', 'default_events', 'default_dates',
        'annual_report_path', 'audit_report_path', 'explanatory_note_path',
        'sentiment_score', 'sentiment_positive', 'sentiment_neutral',
        'sentiment_negative', 'sentiment_chunks', 'sentiment_text_chars',
        'sentiment_document_count', 'sentiment_model', 'sentiment_status'
    ]
    if not MARKET_ENABLED:
        display_columns = [c for c in display_columns if c not in
                           {'market_cap_million_rub', 'stock_volatility', 'current_price', 'MTB'}]
    if not TEXTS_ENABLED:
        display_columns = [c for c in display_columns if c not in
                           {'annual_report_path', 'audit_report_path', 'explanatory_note_path'}]

    # Фильтруем существующие колонки
    # display_columns = [col for col in display_columns if col in df_results.columns]
    #
    # print(df_results[display_columns].to_string(index=False, float_format='%.2f'))

    # Оригинальные названия и их русские соответствия
    column_names_ru = {
        'ticker': 'Тикер',
        'also_tickers': 'Др. тикеры эмитента',
        'inn': 'ИНН',
        'filed': 'filed (1=подана)',
        'outlier': 'outlier',
        'default_events': 'дефолт_события',
        'default_obligation': 'дефолт_обязательство (coupon/principal/offer)',
        'default_unpaid_rub': 'дефолт_сумма_неисп, руб.',
        'default_accel_n': 'право_досрочного_погашения (выпусков)',
        'default_hard_n': 'дефолт без техн. стадии (выпусков)',
        'default_bond_n': 'неисп_облигации (шт.)',
        'default_share_n': 'неисп_акции/дивиденды (шт.)',
        'default_other_n': 'неисп_прочее/не разобрано (шт.)',
        'default_dates': 'дефолт_даты',
        'company_name': 'Компания',
        'isin': 'isin какой-то',
        'year': 'Год',
        'market_cap_million_rub': 'Капитализация, млн ₽',
        'stock_volatility': 'Волатильность',
        'current_price': 'Цена акций',
        'total_assets': 'Актив итого (1600)',
        'total_liabilities': 'Обязательства (1400 + 1500)',
        'equity': 'Капитал (1300)',
        'revenue': 'Выручка (2110)',
        'net_income': 'Чистая прибыть (2400)',
        'ebitda': 'EBIT упрощ. (ЧП+проценты+налог, без амортизации)',
        'current_assets': 'Оборотные активы (1200)',
        'current_liabilities': 'Краткосрочные обязательства (1500)',
        'cash': 'Денежные средства (1250)',
        'ROA': 'ROA, %',
        'ROE': 'ROE, %',
        'Debt_to_EBITDA': 'Обязательства/EBIT упрощ.',
        'Total_Liabilities_to_Assets': 'Total_Liabilities_to_Assets',
        'Current_Ratio': 'Тек. ликвидность',
        'Cash_to_Assets': 'Денежные ср-ва/Активы',
        'Ln_Total_Assets': 'Ln(Активы)',
        'MTB': 'MTB',
        'ap': 'ap',
        'public_report': 'публичный_отчёт',
        'rating': 'рейтинг',
        'default': 'дефолт (по облигациям)',
        'annual_report_path': 'Путь к файлу годового отчёта',
        'audit_report_path': 'Путь к файлу аудиторского заключения',
        'explanatory_note_path': 'Путь к файлу пояснительной записки',
        'sentiment_score': 'Тональность отчёта (RuBERT, positive - negative)',
        'sentiment_positive': 'Тональность: positive',
        'sentiment_neutral': 'Тональность: neutral',
        'sentiment_negative': 'Тональность: negative',
        'sentiment_chunks': 'Тональность: фрагментов текста',
        'sentiment_text_chars': 'Тональность: символов текста',
        'sentiment_document_count': 'Тональность: документов',
        'sentiment_model': 'Тональность: модель',
        'sentiment_status': 'Тональность: статус'
    }

    # Фильтруем существующие колонки
    display_columns = [col for col in display_columns if col in df_results.columns]

    # Создаем копию для отображения с русскими названиями
    df_display = df_results[display_columns].rename(columns=column_names_ru)

    # Выводим с русскими названиями
    print(df_display.to_string(index=False, float_format='%.2f'))

    # Сохраняем результаты в Excel файл
    # CSV: разделитель ';' и запятая в дробных числах - так файл корректно открывается
    # в русском Excel двойным щелчком; utf-8-sig нужен, чтобы не ломалась кириллица.
    # Для чтения в pandas: pd.read_csv(file, sep=';', decimal=',', encoding='utf-8-sig')
    output_file = f'company_analysis_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    df_display.to_csv(output_file, index=False, sep=';', decimal=',',
                      float_format='%.2f', encoding='utf-8-sig')
    # print(f"\nРезультаты сохранены в файл: {output_file}")

    if DEFAULT_EVENTS:
        pd.DataFrame(DEFAULT_EVENTS).to_csv('default_events.csv', index=False, sep=';', decimal=',', encoding='utf-8-sig')
        print(f"События неисполнения обязательств: {len(DEFAULT_EVENTS)} (default_events.csv)")
    save_errors('errors.csv')
    if SKIPPED:
        pd.DataFrame(SKIPPED).to_csv('skipped_companies.csv', index=False, sep=';', encoding='utf-8-sig')
        print(f"Пропущено компаний: {len(SKIPPED)} (список в skipped_companies.csv)")

    # Выводим статистику по собранным данным
    print("\n" + "=" * 80)
    print("СТАТИСТИКА ПО СОБРАННЫМ ДАННЫМ")
    print("=" * 80)
    print(f"Всего компаний (по ИНН): {df_results['inn'].nunique() if not df_results.empty else 0}, строк (компания-год): {len(df_results)}")
    print(f"Доступность данных (%):")
    for col in display_columns[2:]:  # пропускаем inn и ticker
        if col in df_results.columns:
            pct = (df_results[col].notna().sum() / len(df_results)) * 100
            print(f"  {col}: {pct:.1f}%")

    return df_results


# ======================== 5. ДОПОЛНИТЕЛЬНЫЕ УТИЛИТЫ ========================

def get_company_info_by_ticker(ticker):
    """
    Получение базовой информации о компании по тикеру
    """
    url = f"https://iss.moex.com/iss/securities/{ticker}.json"
    try:
        response = requests.get(url, timeout=(5, 20))
        response.raise_for_status()
        data = response.json()

        if 'securities' in data and data['securities']['data']:
            columns = data['securities']['columns']
            values = data['securities']['data'][0]

            info = {
                'ticker': ticker,
                'name': values[columns.index('SHORTNAME')] if 'SHORTNAME' in columns else '',
                'isin': values[columns.index('ISIN')] if 'ISIN' in columns else '',
                'secid': values[columns.index('SECID')] if 'SECID' in columns else '',
                'lot_size': values[columns.index('LOTSIZE')] if 'LOTSIZE' in columns else '',
            }
            return info
    except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
        print(f'Не удалось получить карточку MOEX для {ticker}: {exc}')
    return None


# def get_all_tickers_from_moex():
#     """
#     Получение всех тикеров акций с Московской биржи
#     """
#     securities_df = get_moex_securities_list()
#     return securities_df['ticker'].tolist()


def rbc_news():
    """Явно сообщает, что прежний RbcScraper не подключён к проекту."""
    raise NotImplementedError(
        "RbcScraper не подключён. Используйте отдельный скрипт tt.py "
        "после настройки параметров поиска и Chrome."
    )


# ======================== ЗАПУСК ПРОГРАММЫ ========================

if __name__ == "__main__":
    # print("Начинаем сбор данных по российским компаниям...")
    # print("Источники: MOEX ISS API, disclosure.ru")
    # print("Внимание: Для получения реальных финансовых данных требуется")
    # print("настройка парсинга disclosure.ru или использование платных API (СПАРК, Контур.Фокус)")
    # print("-" * 80)

    # Запускаем основной процесс сбора данных
    results_df = main()

    print("\nАнализ завершён!")