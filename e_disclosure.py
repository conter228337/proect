import atexit
import csv
import json
import os
import re
import time
from datetime import date, timedelta
from playwright.sync_api import sync_playwright
from logutil import log as print   # print с префиксом [ИНН ...] текущей компании

BASE = "https://www.e-disclosure.ru"
SEARCH_PAGE = BASE + "/poisk-po-soobshheniyam"
EVENT_URL = BASE + "/portal/event.aspx?EventId={}"
# Названия событий, в которых ищем рейтинг (сравнение по вхождению, регистр и пробелы не важны)
RATING_EVENTS = (
    "присвоение или изменение рейтинга эмиссионных ценных бумаг",
    "присвоение или изменение рейтинга эмитента рейтинговым агентством на основании договора",
)
# Куда складывать тексты событий, из которых рейтинг достать не удалось
RATING_NOT_FOUND_DIR = "rating_not_found"
RATING_NOT_FOUND_INDEX = "rating_not_found.csv"
# Сколько предыдущих лет просматривать, если за отчётный год новых рейтинговых событий нет
# (рейтинг мог быть присвоен раньше и с тех пор не менялся). 0 = смотреть только отчётный год.
RATING_LOOKBACK_YEARS = 3
# Сколько раз повторять запрос, если сайт вместо данных отдаёт не JSON (капча/блокировка),
# и сколько компаний подряд может завершиться сбоем, прежде чем e-disclosure отключится на этот запуск
MAX_FETCH_RETRIES = 3
# Пауза перед повтором после ошибки (одинаковая для всех повторов), секунд
RETRY_DELAY_SECONDS = 10
MAX_FAIL_STREAK = 3
# Ключевые слова в названии события, по которым определяем дефолт эмитента
DEFAULT_KEYWORDS = ("дефолт", "неисполнение обязательств", "неисполнении обязательств")
# Следующая ступень после неисполнения: у владельцев облигаций возникло право требовать досрочного погашения.
# Ключевая фраза узкая: просто «досрочное погашение» встречается и в добровольных решениях эмитента.
ACCEL_KEYWORDS = ("права требовать от эмитента досрочного погашения",)

JS_FETCH = """async (data) => {
    const r = await fetch('/api/search/sevents', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest'
        },
        body: new URLSearchParams(data).toString()
    });
    return {status: r.status,
            ctype: r.headers.get('content-type') || '',
            text: await r.text()};
}"""

_RATING_TOKEN = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё])"
    r"(?:[Rr][Uu])?"
    r"(?:"
    r"[AАaа]{1,3}|"
    r"[BВbв]{1,3}|"
    r"[CСcс]{1,3}|"
    r"D|"
    r"WD|NR|SD|R"
    r")"
    r"[+\-]?"
    r"(?:\(RU\)|\.ru)?"
    r"(?![A-Za-zА-Яа-яЁё])",
    flags=re.I,
)

# ---------------- разбор страницы ----------------
def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def clean_rating(raw):
    """'AAA(RU)' / 'ВВ-.ru' / 'BBВ-.ru' -> 'AAA' / 'BB-' / 'BBB-'."""
    if not raw:
        return None
    s = raw.strip()
    if re.search(r"\bпосле\b", s, flags=re.I):
        s = re.split(r"\bпосле\b", s, flags=re.I)[-1]
    parts = [p.strip() for p in re.split(r"\s*(?:→|->|=>|;|/|\n)\s*", s) if p.strip()]
    if not parts:
        return None
    s = parts[-1]
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\.ru$", "", s, flags=re.I)
    s = re.sub(r"^ru(?=[A-Za-zА-Яа-я])", "", s, flags=re.I)
    # кириллица рейтинга -> латиница (А->A, В->B, С->C); смесь BBВ -> BBB
    s = s.translate(str.maketrans("АаВвСс", "AaBbCc"))
    return s.strip(" :").upper() or None


def _extract_rating_token(s):
    """Из строки достаёт последнее рейтинговое обозначение (AAA, AA+, bb, wd…)."""
    if not s:
        return None
    # однобуквенные строчные (а/в/с/a/b/c) — не рейтинг, а предлоги/слова
    _skip = set("aаbвcс")
    last = None
    for m in _RATING_TOKEN.finditer(s):
        tok = m.group(0)
        core = re.sub(r"(?i)^ru", "", tok)
        core = re.sub(r"(?i)(\([Rr][Uu]\)|\.ru)$", "", core)
        core = re.sub(r"[+\-]$", "", core)
        if len(core) == 1 and core in _skip:
            continue
        last = tok
    return clean_rating(last) if last else None


def _section(text, num, next_nums):
    """Текст пункта `num` (например '2.4') до начала любого из пунктов `next_nums` или до '3.'.
    None, если такого пункта в тексте нет."""
    nxt = "|".join(re.escape(n) for n in next_nums)
    m = re.search(rf"(?<![\d.]){re.escape(num)}\.?\s*(.*?)(?=(?<![\d.])(?:{nxt})\b|(?<![\d.])3\.\s|\Z)",
                  text, flags=re.S)
    return m.group(1) if m else None


def parse_rating(text):
    """Ищет рейтинг: сначала в п. 2.4 (последнее упоминание AA/A/bb/wd и т.п. в блоке),
    а если там не нашли - тем же способом в п. 2.3.
    Если в тексте нет ни 2.4, ни 2.3, разбирается весь текст целиком."""
    sec24 = _section(text, "2.4", ("2.5",))
    sec23 = _section(text, "2.3", ("2.4", "2.5"))
    if sec24 is None and sec23 is None:
        return _extract_rating_token(text)
    for block in (sec24, sec23):
        if block:
            rating = _extract_rating_token(block)
            if rating:
                return rating
    return None


def parse_default_event(text):
    """Разбор сообщения «о неисполнении обязательств эмитента перед владельцами его ценных бумаг».

    Работает с двумя форматами (до и после 2024 года: номера пунктов разные, поэтому разделы
    ищутся по тексту заголовков, а не по номерам).
    Возвращает словарь:
      kind        'bond' (облигации = кредитный дефолт) | 'share' (акции: невыплата дивидендов)
                  | 'other' | 'unknown' (не нашли описание ценных бумаг)
      obligation  'coupon' | 'principal' | 'offer' | 'dividend' | 'other' | ''
      unpaid_rub  размер неисполненного обязательства в рублях или None
      event_date  дата наступления события 'ДД.ММ.ГГГГ' или ''  (только в новом формате)
      isin        ISIN выпуска (из п. 2.1) или ''
      due_date    срок исполнения обязательства 'ДД.ММ.ГГГГ' или ''
      stage       'technical' (технический дефолт, идёт льготный срок) | 'default' (дефолт, в т.ч. после
                  истечения льготного срока) | ''.  Одна неуплата может дать 2 сообщения: technical, затем default.
      default_date / grace_end   «Дата наступления (технического) дефолта» и «Дата истечения срока
                  технического дефолта», если указаны в тексте
      basis_type  для права досрочного погашения: 'default' (дефолт/просрочка) | 'material_breach'
                  (существенное нарушение) | 'other'"""
    t = (text or "").replace("\xa0", " ")
    m = re.search(r"(?<![\d.])2\.1\.\s*(.*?)(?=(?<![\d.])2\.2\.)", t, flags=re.S)
    if not m:
        return {"kind": "unknown", "obligation": "", "unpaid_rub": None, "event_date": "",
                "isin": "", "due_date": "", "stage": ""}
    sec = m.group(1).lower()
    if "облигаци" in sec:
        kind = "bond"
    elif re.search(r"\bакци", sec):
        kind = "share"
    else:
        kind = "other"

    obligation = ""
    mo = re.search(r"Содержание обязательства.*?:\s*(.*?)(?=Дата, в которую)", t, flags=re.S | re.I)
    if mo:
        o = mo.group(1).lower()
        if "дивиденд" in o:
            obligation = "dividend"
        elif "оферт" in o or "приобретени" in o or "выкуп" in o:
            obligation = "offer"
        elif "купон" in o or "процент" in o or "доход" in o:
            obligation = "coupon"
        elif "погашени" in o or "номинальн" in o:
            obligation = "principal"
        else:
            obligation = "other"

    unpaid = None
    mu = re.search(r"в котором оно не исполнено:\s*(\d[\d ]*(?:[.,]\d+)?)\s*\(", t)
    if mu:
        try:
            unpaid = float(mu.group(1).replace(" ", "").replace(",", "."))
        except ValueError:
            pass

    md = re.search(r"Дата наступления события[^:]*:\s*(\d{2}\.\d{2}\.\d{4})", t)

    mi = re.search(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b", m.group(1))
    isin = mi.group(0) if mi else ""

    mdue = re.search(r"Дата, в которую обязательство[^\n]*?:\s*(\d{2}\.\d{2}\.\d{4})", t)
    due = mdue.group(1) if mdue else ""

    # стадия: смотрим только тело пункта о неисполнении (после двоеточия в заголовке),
    # потому что в заголовке старого формата стоит шаблон «(технический дефолт, дефолт)»
    stage = ""
    mb = re.search(r"(?:Сведения о неисполнении|Факт неисполнения)[^\n]*?:\s*(.*?)(?=\n\s*(?:2\.\d|3\.)|\Z)",
                   t, flags=re.S)
    if mb:
        b = mb.group(1).lower()
        if "истечени" in b and "технического дефолта" in b:
            stage = "default"      # льготный срок истёк
        elif "технический дефолт" in b or "технического дефолта" in b:
            stage = "technical"
        elif "дефолт" in b:
            stage = "default"
    # для сообщений о праве требовать досрочного погашения: основание и его тип
    basis, basis_type = "", ""
    mbs = re.search(r"Основание, повлекшее[^\n]*?:\s*(.*?)(?=\n\s*2\.3\.|\Z)", t, flags=re.S)
    if mbs:
        full = re.sub(r"\s+", " ", mbs.group(1)).strip()
        basis, low = full[:300], full.lower()
        if "дефолт" in low or "просрочк" in low:
            basis_type = "default"
        elif "существенное нарушение" in low:
            basis_type = "material_breach"
        else:
            basis_type = "other"

    mdd = re.search(r"Дата наступления (?:технического )?дефолта:\s*(\d{2}\.\d{2}\.\d{4})", t, flags=re.I)
    mge = re.search(r"истечени\w+ срока технического дефолта:\s*(\d{2}\.\d{2}\.\d{4})", t, flags=re.I)
    return {"kind": kind, "obligation": obligation, "unpaid_rub": unpaid,
            "event_date": md.group(1) if md else "", "isin": isin, "due_date": due, "stage": stage,
            "default_date": mdd.group(1) if mdd else "", "grace_end": mge.group(1) if mge else "",
            "basis": basis, "basis_type": basis_type}


# ---------------- работа с сайтом ----------------
class EDisclosure:
    def __init__(self, profile_dir="./browser_profile", use_chrome=True):
        self.profile_dir = profile_dir
        self.use_chrome = use_chrome
        self._pw = self.ctx = None
        self._default_cache = {}  # GUID события -> результат parse_default_event
        self._events_cache = {}   # (инн, год) -> события; при переборе всех лет рейтинг ищется и в прошлых годах
        self._rating_cache = {}   # GUID рейтингового события -> распознанный рейтинг (или None)

    def start(self):
        self._pw = sync_playwright().start()
        kwargs = dict(user_data_dir=self.profile_dir, headless=False,
                      args=["--disable-blink-features=AutomationControlled"])
        if self.use_chrome:
            kwargs["channel"] = "chrome"
        self.ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        self.search_page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.event_page = self.ctx.new_page()
        self._pass_captcha()          # единственная ручная капча

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.ctx = self._pw = None

    @staticmethod
    def _build_data(inn, d1, d2, page_no, page_size):
        return {
            "eventTypeTerm": "", "radView": "0",
            "dateStart": d1, "dateFinish": d2,
            "textfieldEvent": "", "radReg": "FederalDistricts",
            "districtsCheckboxGroup": "-1", "regionsCheckboxGroup": "-1",
            "branchesCheckboxGroup": "-1",
            "textfieldCompany": str(inn),
            "lastPageSize": str(page_size), "lastPageNumber": str(page_no),
            "query": str(inn), "queryEvent": "",
        }

    def _raw_fetch(self, data):
        try:
            return self.search_page.evaluate(JS_FETCH, data)
        except Exception:
            return None

    @staticmethod
    def _is_json(out):
        return out is not None and "json" in out["ctype"]

    def _pass_captcha(self):
        try:
            self.search_page.goto(SEARCH_PAGE)
        except Exception:
            pass
        probe = self._build_data("0000000000", "01.01.2026", "02.01.2026", 1, 10)
        while True:
            self.search_page.wait_for_load_state("load")
            time.sleep(2)
            if self._is_json(self._raw_fetch(probe)):
                print("Доступ к сайту есть.")
                return
            input("Пройдите капчу в окне браузера, дождитесь страницы поиска и нажмите Enter...")

    def _open_checked(self, page, url, ok_url_part):
        try:
            page.goto(url)
        except Exception:
            pass
        while True:
            page.wait_for_load_state("load")
            time.sleep(2)
            if "spinner-container" not in page.content() and ok_url_part in page.url:
                return page
            input("Пройдите капчу в окне браузера и нажмите Enter...")

    def get_events(self, inn, d1, d2, page_size=100, pause=1.5):
        events, page_no, total_pages, attempts = [], 1, 1, 0
        while page_no <= total_pages:
            out = self._raw_fetch(self._build_data(inn, d1, d2, page_no, page_size))
            if not self._is_json(out):
                attempts += 1
                if out is None:
                    print("e-disclosure: пришёл не JSON, запрос не выполнился (страница перезагружалась?).")
                else:
                    snippet = re.sub(r"\s+", " ", out["text"])[:150]
                    print(f"e-disclosure: пришёл не JSON (HTTP {out['status']}, {out['ctype'] or 'нет типа'}): {snippet!r}")
                if attempts > MAX_FETCH_RETRIES:
                    raise RuntimeError(
                        f"e-disclosure не отдаёт данные для ИНН {inn} ({d1} - {d2}) "
                        f"после {MAX_FETCH_RETRIES} повторов")
                wait = RETRY_DELAY_SECONDS
                print(f"e-disclosure: повтор {attempts}/{MAX_FETCH_RETRIES} через {wait} с ({d1} - {d2})...")
                time.sleep(wait)
                self._pass_captcha()
                continue
            attempts = 0
            js = json.loads(out["text"])
            events.extend(js["foundEventsList"])
            total_pages = js["pagingInfo"]["totalPages"]
            page_no += 1
            time.sleep(pause)
        return events

    def year_events(self, inn, year):
        key = (str(inn), int(year))
        if key not in self._events_cache:
            fmt = "%d.%m.%Y"
            end = min(date(year, 12, 31), date.today())
            self._events_cache[key] = self.get_events(inn, f"01.01.{year}", end.strftime(fmt))
        return self._events_cache[key]

    @staticmethod
    def _is_rating_event(e):
        name = _norm(e["eventName"])
        return any(k in name for k in RATING_EVENTS)

    def rating_events(self, inn, year):
        return [e for e in self.year_events(inn, year) if self._is_rating_event(e)]

    @staticmethod
    def _save_rating_not_found(inn, event, text):
        """Сохраняет текст события, в котором рейтинг не найден: сам текст в RATING_NOT_FOUND_DIR/<GUID>.txt
        и строку-описание в RATING_NOT_FOUND_INDEX (ИНН, дата, название, ссылка, файл)."""
        guid = event["pseudoGUID"]
        try:
            os.makedirs(RATING_NOT_FOUND_DIR, exist_ok=True)
            path = os.path.join(RATING_NOT_FOUND_DIR, f"{guid}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            new_file = not os.path.exists(RATING_NOT_FOUND_INDEX)
            with open(RATING_NOT_FOUND_INDEX, "a", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                if new_file:
                    w.writerow(["inn", "pub_date", "event_name", "guid", "url", "text_file"])
                w.writerow([inn, event.get("pubDate"), event["eventName"], guid,
                            EVENT_URL.format(guid), path])
        except Exception as ex:
            print(f"  не удалось сохранить текст события {guid}: {type(ex).__name__}: {ex}")

    def _rating_from_events(self, inn, events):
        """-> (найдено ли рейтинговое событие, значение рейтинга или None).
        Берётся самое свежее рейтинговое событие; если рейтинг из него не разобран (ни в п. 2.4, ни в п. 2.3),
        текст события сохраняется отдельно (см. _save_rating_not_found)."""
        rating_events = [e for e in events if self._is_rating_event(e)]
        if not rating_events:
            return False, None
        latest = max(rating_events, key=lambda e: e["pubDate"])
        guid = latest["pseudoGUID"]
        if guid in self._rating_cache:
            return True, self._rating_cache[guid]
        page = self._open_checked(self.event_page, EVENT_URL.format(guid), "event.aspx")
        text = page.inner_text("body")
        rating = parse_rating(text)
        if not rating:
            self._save_rating_not_found(inn, latest, text)
        self._rating_cache[guid] = rating
        return True, rating

    def _classify_default_event(self, e):
        """Открывает страницу события и определяет, чьи это ценные бумаги. Результат кэшируется."""
        guid = e["pseudoGUID"]
        if guid in self._default_cache:
            return self._default_cache[guid]
        try:
            page = self._open_checked(self.event_page, EVENT_URL.format(guid), "event.aspx")
            text = page.inner_text("body")
            try:
                os.makedirs("debug_default", exist_ok=True)
                with open(os.path.join("debug_default", f"{guid}.txt"), "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                pass
            info = parse_default_event(text)
        except Exception as ex:
            print(f"  не удалось прочитать событие {guid}: {type(ex).__name__}: {ex}")
            info = {"kind": "unknown", "obligation": "", "unpaid_rub": None, "event_date": "",
                    "isin": "", "due_date": "", "stage": ""}
        self._default_cache[guid] = info
        return info

    def default_events(self, inn, year):
        """Все сообщения о неисполнении обязательств за год с классификацией (по одному dict на событие)."""
        out = []
        for e in self.year_events(inn, year):
            name = _norm(e["eventName"])
            if any(k in name for k in ACCEL_KEYWORDS):
                event_type = "acceleration"
            elif any(k in name for k in DEFAULT_KEYWORDS):
                event_type = "nonperformance"
            else:
                continue
            info = self._classify_default_event(e)
            out.append({"inn": str(inn), "year": int(year), "guid": e["pseudoGUID"],
                        "pub_date": str(e.get("pubDate")), "event_name": e["eventName"],
                        "event_type": event_type, **info})
        return out

    def disclosure_info(self, inn, year):
        """Рейтинг и признак дефолта за год.
        Дефолт ищется только в отчётном году. Рейтинг: самое свежее рейтинговое событие
        в отчётном году, а если его нет - в предыдущих годах (до RATING_LOOKBACK_YEARS)."""
        year = int(year)
        events = self.year_events(inn, year)
        matched = [e for e in events if any(k in _norm(e["eventName"]) for k in DEFAULT_KEYWORDS)]
        kinds = [self._classify_default_event(e) for e in matched]
        bond_ev = [k for k in kinds if k["kind"] == "bond"]
        default_obligations = ";".join(sorted({k["obligation"] for k in bond_ev if k["obligation"]}))
        # одно неисполнение = выпуск + обязательство + срок; сообщения technical и after_grace считаются один раз
        uniq = {}
        for k in bond_ev:
            uniq.setdefault((k.get("isin", ""), k["obligation"], k.get("due_date", "")), k)
        default_unpaid = sum(k["unpaid_rub"] or 0 for k in uniq.values()) or None
        n_bond = len(uniq)
        n_hard = len({(k.get("isin", ""), k["obligation"], k.get("due_date", ""))
                      for k in bond_ev if k.get("stage") == "default"})
        n_share = sum(k["kind"] == "share" for k in kinds)
        n_other = sum(k["kind"] in ("other", "unknown") for k in kinds)
        # право требовать досрочного погашения: считаем уникальные выпуски (ISIN), только облигации
        accel_ev = [self._classify_default_event(e) for e in events
                    if any(k in _norm(e["eventName"]) for k in ACCEL_KEYWORDS)]
        n_accel = len({k.get("isin") or id(k) for k in accel_ev if k["kind"] == "bond"})
        has_default = n_bond > 0   # кредитный дефолт: облигации. Невыплата дивидендов дефолтом не считается
        # какие именно события сработали и когда опубликованы (для проверки определения и контроля look-ahead)
        default_events = " | ".join(sorted({e["eventName"] for e in matched}))[:300]
        default_dates = ";".join(sorted({str(e.get("pubDate")) for e in matched}))[:100]

        rating, rating_year = None, None
        found, rating = self._rating_from_events(inn, events)
        if found:
            rating_year = year
        else:
            for back in range(1, RATING_LOOKBACK_YEARS + 1):
                found, rating = self._rating_from_events(inn, self.year_events(inn, year - back))
                if found:
                    rating_year = year - back
                    break

        if rating:
            print(f"e-disclosure, {year} г.: рейтинг {rating} (событие {rating_year} г.), дефолт: {int(has_default)}")
        elif found:
            print(f"e-disclosure, {year} г.: рейтинговое событие {rating_year} г. найдено, "
                  f"но значение не распознано (текст сохранён в {RATING_NOT_FOUND_DIR}/)")
        else:
            print(f"e-disclosure, {year} г.: рейтинговых событий не найдено, дефолт: {int(has_default)}")
        return {"rating": rating, "default": int(has_default),
                "default_events": default_events, "default_dates": default_dates,
                "default_bond_n": n_bond, "default_share_n": n_share, "default_other_n": n_other,
                "default_obligation": default_obligations, "default_unpaid_rub": default_unpaid,
                "default_accel_n": n_accel, "default_hard_n": n_hard}


# ---------------- публичная функция ----------------
_session = None
_annual_ocr = None

def get_annual_reports_text(inn, candidate_years=None):
    """ИНН -> {год: {annual_path, audit_path, explanatory_path}} за все годы MIN_YEAR..MAX_YEAR.
    Файлы сохраняются на диск как есть (без OCR).

    Использует ту же Playwright-сессию и профиль браузера, что и e-disclosure.
    """
    global _session, _annual_ocr
    if _session is None or _session.ctx is None:
        _session = EDisclosure()
        _session.start()
        atexit.register(_session.close)
    if _annual_ocr is None or getattr(_annual_ocr, 'session', None) is not _session:
        from annual_reports_ocr import AnnualReportsOCR
        _annual_ocr = AnnualReportsOCR(_session)
    return _annual_ocr.collect(str(inn), candidate_years=candidate_years)


def get_rating(inn, year):
    """Принимает ИНН и год, возвращает рейтинг строкой ('AAA') или None.
    Браузер и капча запускаются только при первом вызове."""
    global _session
    if _session is None or _session.ctx is None:
        _session = EDisclosure()
        _session.start()
        atexit.register(_session.close)
    return _session.disclosure_info(str(inn), year)["rating"]


_fail_streak = 0


def get_disclosure_info(inn, year):
    """ИНН и год -> {'rating': 'AAA' или None, 'default': 1/0}.
    Один запрос событий на компанию вместо двух. При сбое бросает исключение."""
    global _session, _fail_streak
    if _fail_streak >= MAX_FAIL_STREAK:
        raise RuntimeError(f"e-disclosure отключён на этот запуск после {MAX_FAIL_STREAK} сбоев подряд")
    if _session is None or _session.ctx is None:
        _session = EDisclosure()
        _session.start()
        atexit.register(_session.close)
    try:
        info = _session.disclosure_info(str(inn), year)
    except Exception:
        _fail_streak += 1
        raise
    _fail_streak = 0
    return info


def get_default_events(inn, year):
    """ИНН и год -> список классифицированных сообщений о неисполнении обязательств.
    Использует кэш сессии, поэтому после get_disclosure_info за тот же год новых запросов не делает."""
    global _session
    if _fail_streak >= MAX_FAIL_STREAK:
        raise RuntimeError("e-disclosure отключён на этот запуск")
    if _session is None or _session.ctx is None:
        _session = EDisclosure()
        _session.start()
        atexit.register(_session.close)
    return _session.default_events(str(inn), year)


if __name__ == "__main__":
    print(get_rating("7702070139", 2025))   # -> 'AAA'
    print(get_rating("7707083893", 2024))
    print(get_rating("7702070139", 2025))  # -> 'AAA'
    print(get_rating("7707083893", 2025))