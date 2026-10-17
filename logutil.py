"""Вывод в терминал с привязкой к компании.

Основной идентификатор компании - ИНН. Пока в main.collect_company_data обрабатывается компания
(set_company), каждая строка вывода получает префикс «[ИНН 7707083893]», поэтому любое сообщение
(в том числе из e_disclosure, rfsd, annual_reports_ocr, investing) можно привязать к компании.
Ошибки получения данных дополнительно копятся в ERRORS и в конце сохраняются в errors.csv.

В модулях с большим числом print() достаточно `from logutil import log as print`.
"""
import builtins
import csv
import traceback

_ctx = {"inn": "", "ticker": ""}
ERRORS = []          # [{'inn','ticker','source','year','error'}]


def set_company(inn, ticker=""):
    _ctx["inn"] = str(inn or "").strip()
    _ctx["ticker"] = str(ticker or "")


def clear_company():
    _ctx["inn"] = _ctx["ticker"] = ""


def _prefix(inn=None, ticker=None):
    inn = _ctx["inn"] if inn is None else inn
    ticker = _ctx["ticker"] if ticker is None else ticker
    if inn and inn != "Не найден":
        return f"[ИНН {inn}]"
    return f"[ИНН не найден, тикер {ticker}]" if ticker else ""


def log(*args, sep=" ", end="\n", **kwargs):
    """print() с префиксом [ИНН ...] на каждой непустой строке (если задана текущая компания)."""
    msg = sep.join(str(a) for a in args)
    pre = _prefix()
    if pre:
        msg = "\n".join(f"{pre} {ln.strip()}" if ln.strip() else ln for ln in msg.split("\n"))
    builtins.print(msg, end=end, **kwargs)


def log_error(source, error, year=None, inn=None, ticker=None):
    """Печатает ошибку получения данных с указанием компании, источника и года и запоминает её для errors.csv.
    inn/ticker можно передать явно, когда контекст компании уже сброшен."""
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
        tb = traceback.extract_tb(error.__traceback__)
        if tb:
            text += f" ({tb[-1].filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{tb[-1].lineno})"
    else:
        text = str(error)
    inn_v = _ctx["inn"] if inn is None else str(inn)
    ticker_v = _ctx["ticker"] if ticker is None else str(ticker)
    where = f"{source}, {year} г." if year else source
    pre = _prefix(inn_v, ticker_v)
    builtins.print(f"{pre} ОШИБКА [{where}]: {text}".strip())
    ERRORS.append({"inn": inn_v, "ticker": ticker_v, "source": source,
                   "year": year if year else "", "error": text})


def save_errors(path="errors.csv"):
    """Сохраняет все накопленные ошибки в CSV и печатает сводку по компаниям. Возвращает число ошибок."""
    if not ERRORS:
        return 0
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["inn", "ticker", "source", "year", "error"], delimiter=";")
        w.writeheader()
        w.writerows(ERRORS)
    builtins.print(f"\nОшибок при получении данных: {len(ERRORS)} (подробно в {path})")
    for e in ERRORS:
        yr = f", {e['year']} г." if e["year"] else ""
        builtins.print(f"  ИНН {e['inn'] or 'не найден'} ({e['ticker']}): {e['source']}{yr}: {e['error']}")
    return len(ERRORS)
