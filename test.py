import atexit
import json
import re
import time
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

BASE = "https://www.e-disclosure.ru"
SEARCH_PAGE = BASE + "/poisk-po-soobshheniyam"
EVENT_URL = BASE + "/portal/event.aspx?EventId={}"
RATING_EVENT = "присвоение или изменение рейтинга эмиссионных ценных бумаг"
RATING_LABEL = "значение присвоенного рейтинга"

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


# ---------------- разбор страницы ----------------
def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def clean_rating(raw):
    """'AAA(RU)' -> 'AAA'; 'AA+(RU) -> AAA(RU)' -> 'AAA'."""
    if not raw:
        return None
    s = raw.strip()
    if re.search(r"\bпосле\b", s, flags=re.I):
        s = re.split(r"\bпосле\b", s, flags=re.I)[-1]
    parts = [p.strip() for p in re.split(r"\s*(?:→|->|=>|;|/|\n| |\t)\s*", s) if p.strip()]
    if not parts:
        return None
    s = parts[-1]
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"^ru(?=[A-Za-z])", "", s)
    s = re.sub(r"\.", "", s)
    s = re.sub(r"\.ru$", "", s, flags=re.I)
    return s.strip(" :-–") or None


def parse_rating(text):
    m = re.search(r"(?<![\d.])2\.4\.?\s*(.*?)(?=(?<![\d.])2\.5\b|(?<![\d.])3\.\s|\Z)",
                  text, flags=re.S)
    block = m.group(1) if m else text
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if RATING_LABEL in _norm(line):
            value = re.split(r"[\t ]", line, maxsplit=1)[1] if re.search(r"[\t ]", line) else ""
            if not value.strip():
                nxt = [l for l in lines[i + 1:] if l.strip()]
                value = nxt[0] if nxt else ""
            return clean_rating(value)
    return None


# ---------------- работа с сайтом ----------------
class EDisclosure:
    def __init__(self, profile_dir="./browser_profile", use_chrome=True):
        self.profile_dir = profile_dir
        self.use_chrome = use_chrome
        self._pw = self.ctx = None

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
        events, page_no, total_pages = [], 1, 1
        while page_no <= total_pages:
            out = self._raw_fetch(self._build_data(inn, d1, d2, page_no, page_size))
            if not self._is_json(out):
                print("Пришёл не JSON, возможно снова капча.")
                self._pass_captcha()
                continue
            js = json.loads(out["text"])
            events.extend(js["foundEventsList"])
            total_pages = js["pagingInfo"]["totalPages"]
            page_no += 1
            time.sleep(pause)
        return events

    def rating_events(self, inn, year):
        fmt = "%d.%m.%Y"
        end = min(date(year, 12, 31), date.today())
        events = self.get_events(inn, f"01.01.{year}", end.strftime(fmt))
        return [e for e in events if RATING_EVENT in _norm(e["eventName"])]

    def latest_rating(self, inn, year):
        """Новый рейтинг из самого свежего рейтингового события за указанный год."""
        events = self.rating_events(inn, int(year))
        if not events:
            return None
        latest = max(events, key=lambda e: e["pubDate"])
        page = self._open_checked(self.event_page,
                                  EVENT_URL.format(latest["pseudoGUID"]), "event.aspx")
        return parse_rating(page.inner_text("body"))


# ---------------- публичная функция ----------------
_session = None


def get_rating(inn, year):
    """Принимает ИНН и год, возвращает рейтинг строкой ('AAA') или None.
    Браузер и капча запускаются только при первом вызове."""
    global _session
    if _session is None or _session.ctx is None:
        _session = EDisclosure()
        _session.start()
        atexit.register(_session.close)
    return _session.latest_rating(str(inn), year)


if __name__ == "__main__":
    print(get_rating("5321029508", 2025))   # -> 'AA'?
    print(get_rating("7702070139", 2025))   # -> 'AAA'
    print(get_rating("7707083893", 2024))
    print(get_rating("7702070139", 2025))  # -> 'AAA'
    print(get_rating("7707083893", 2025))