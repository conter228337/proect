"""Сбор годовых отчётов, аудиторских заключений и пояснительных записок.

Модуль рассчитан на работу с уже существующей Playwright-сессией из e_disclosure.py.
Для каждой компании по ИНН открываются разделы e-disclosure.ru "Годовая" (type=2) и
"Бухгалтерская отчётность" (type=3); файлы (PDF/ZIP/изображения) СОХРАНЯЮТСЯ НА ДИСК как есть,
а в таблицу попадают пути к ним.

Распознавание Tesseract сейчас ОТКЛЮЧЕНО (закомментировано). Чтобы вернуть OCR, раскомментируйте
помеченные "OCR:" строки: импорт pytesseract, настройку TESSERACT_CMD и вызовы download_and_ocr в collect().
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urljoin

# OCR: распознавание Tesseract отключено; для включения раскомментируйте импорт
# import pytesseract

from config import MIN_YEAR, MAX_YEAR
from logutil import log as print   # print с префиксом [ИНН ...] текущей компании

try:
    from bo_nalog import BoNalogClient
    from bo_nalog_excel import get_bfo_data
except Exception:  # bo.nalog is an optional source; e-disclosure remains fallback
    BoNalogClient = None
    get_bfo_data = None

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

BASE = "https://www.e-disclosure.ru"
ANNUAL_FILES_URL = BASE + "/portal/files.aspx?id={company_id}&type=2"
AUDIT_FILES_URL = BASE + "/portal/files.aspx?id={company_id}&type=3"

# Можно переопределить переменными окружения.
TESSERACT_CMD = os.environ.get("TESSERACT_CMD", "")
OCR_LANG = os.environ.get("OCR_LANG", "rus+eng")
OCR_DPI = int(os.environ.get("OCR_DPI", "200"))
OCR_MAX_FILE_MB = int(os.environ.get("OCR_MAX_FILE_MB", "300"))
# Корень, куда сохраняются скачанные файлы (и OCR-тексты, если включить распознавание). На Windows по умолчанию C:/ap.
OCR_OUTPUT_ROOT = os.environ.get("ANNUAL_OCR_ROOT", r"C:/ap")

# OCR: настройка пути к Tesseract (нужна только при включённом распознавании)
# if TESSERACT_CMD:
#     pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


class AnnualReportsOCR:
    def __init__(self, session):
        self.session = session
        self.company_id_cache: dict[str, str] = {}
        self.report_cache: dict[tuple[str, int], str] = {}
        self.bo_client = None
        self.bo_periods_cache: dict[str, dict[int, int]] = {}

    # ---------- company id ----------
    def _company_id_from_page(self, page) -> str | None:
        html = page.content()
        m = re.search(r"/portal/company\.aspx\?id=(\d+)", html, flags=re.I)
        return m.group(1) if m else None

    def _company_id_from_events(self, inn: str, candidate_years=None) -> str | None:
        years = sorted({int(y) for y in (candidate_years or []) if str(y).isdigit()}, reverse=True)
        # Обычно достаточно одного-двух последних годов: любой event содержит ссылку на карточку компании.
        for year in years[:5]:
            try:
                events = self.session.year_events(inn, year)
            except Exception:
                continue
            for event in events[:10]:
                guid = event.get("pseudoGUID")
                if not guid:
                    continue
                try:
                    self.session.event_page.goto(
                        self.session.EVENT_URL.format(guid) if hasattr(self.session, "EVENT_URL")
                        else f"{BASE}/portal/event.aspx?EventId={guid}"
                    )
                    self.session.event_page.wait_for_load_state("load")
                    cid = self._company_id_from_page(self.session.event_page)
                    if cid:
                        return cid
                except Exception:
                    pass
        return None

    def _company_id_from_search(self, inn: str) -> str | None:
        """Резервный поиск через страницу поиска компаний.

        DOM на сайте периодически меняется, поэтому пробуем несколько вариантов
        заполнения формы, а затем ищем company.aspx?id=... в текущем документе.
        """
        page = self.session.search_page
        try:
            page.goto(BASE + "/poisk-po-kompaniyam", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
        except Exception:
            return None

        # На странице обычно несколько текстовых input'ов. Сначала пробуем input,
        # который выглядит как поле поиска по коду/названию.
        inputs = page.locator("input[type='text']")
        count = inputs.count()
        for i in range(count):
            try:
                el = inputs.nth(i)
                placeholder = (el.get_attribute("placeholder") or "").lower()
                name = (el.get_attribute("name") or "").lower()
                if any(x in (placeholder + " " + name) for x in ("инн", "огрн", "окпо", "код", "назван", "поиск")):
                    el.fill(str(inn))
                    el.press("Enter")
                    page.wait_for_timeout(1500)
                    cid = self._company_id_from_page(page)
                    if cid:
                        return cid
            except Exception:
                continue

        # Последняя попытка: заполнить первый видимый текстовый input.
        for i in range(count):
            try:
                el = inputs.nth(i)
                if el.is_visible():
                    el.fill(str(inn))
                    el.press("Enter")
                    page.wait_for_timeout(1500)
                    cid = self._company_id_from_page(page)
                    if cid:
                        return cid
            except Exception:
                continue
        return None

    def get_company_id(self, inn: str, candidate_years=None) -> str | None:
        inn = str(inn)
        if inn in self.company_id_cache:
            return self.company_id_cache[inn]

        cid = self._company_id_from_events(inn, candidate_years)
        if not cid:
            cid = self._company_id_from_search(inn)
        if cid:
            self.company_id_cache[inn] = cid
        return cid

    # ---------- annual report table ----------
    @staticmethod
    def _row_document_info(row, document_kind: str) -> tuple[int, str] | None:
        try:
            cells = row.locator("td")
            n = cells.count()
            if n < 3:
                return None
            texts = [re.sub(r"\s+", " ", cells.nth(i).inner_text()).strip() for i in range(n)]
            # В таблице: № | Тип документа | Отчётный год | ... | Файл
            doc_type = texts[1].lower()
            if document_kind == "annual":
                if doc_type != "годовой отчет":
                    return None
            elif document_kind == "audit":
                if "аудиторское заключение" not in doc_type:
                    return None
            elif document_kind == "explanatory":
                if "пояснительн" not in doc_type:
                    return None
            else:
                return None
            year = None
            for txt in texts[2:5]:
                m = re.search(r"\b(19\d{2}|20\d{2})\b", txt)
                if m:
                    year = int(m.group(1))
                    break
            if year is None:
                return None

            links = row.locator("a")
            for j in range(links.count()):
                href = links.nth(j).get_attribute("href") or ""
                text = re.sub(r"\s+", " ", links.nth(j).inner_text()).strip().lower()
                if href and ("zip" in text or href.lower().endswith((".zip", ".pdf", ".jpg", ".jpeg", ".png")) or "/content/" in href.lower()):
                    return year, urljoin(BASE, href)
        except Exception:
            return None
        return None

    def _extract_document_rows(self, page, document_kind: str) -> dict[int, str]:
        documents: dict[int, str] = {}
        rows = page.locator("table tr")
        for i in range(rows.count()):
            item = self._row_document_info(rows.nth(i), document_kind)
            if item:
                year, href = item
                # На сайте встречаются несколько публикаций одного года.
                # Таблица обычно идёт от самой свежей к старой, поэтому берём первую.
                documents.setdefault(year, href)
        return documents

    def _get_document_links(self, company_id: str, document_kind: str) -> dict[int, str]:
        """{год: ссылка на файл}. Годовой отчёт ищем в type=2, аудит в type=3,
        пояснительную записку в type=3, а если там нет - в type=2."""
        urls = {"annual": [ANNUAL_FILES_URL],
                "audit": [AUDIT_FILES_URL],
                "explanatory": [AUDIT_FILES_URL, ANNUAL_FILES_URL]}[document_kind]
        links: dict[int, str] = {}
        for tpl in urls:
            for year, href in self._scan_files_page(tpl.format(company_id=company_id), document_kind).items():
                links.setdefault(year, href)
        return links

    def _scan_files_page(self, url: str, document_kind: str) -> dict[int, str]:
        page = self.session.event_page
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)

        reports = self._extract_document_rows(page, document_kind)

        # Если раздел разбит на страницы, пробуем последующие ссылки пагинации.
        seen_urls = {page.url}
        for _ in range(20):
            candidates = []
            links = page.locator("a")
            for i in range(links.count()):
                try:
                    href = links.nth(i).get_attribute("href") or ""
                    txt = re.sub(r"\s+", " ", links.nth(i).inner_text()).strip().lower()
                    if not href:
                        continue
                    full = urljoin(BASE, href)
                    if full in seen_urls:
                        continue
                    if txt in {"следующая", "далее", ">", "next", "»"} or re.search(r"page=\d+", href, re.I):
                        candidates.append(full)
                except Exception:
                    pass
            if not candidates:
                break
            next_url = candidates[0]
            seen_urls.add(next_url)
            try:
                page.goto(next_url, wait_until="domcontentloaded")
                page.wait_for_timeout(500)
                before = len(reports)
                reports.update(self._extract_document_rows(page, document_kind))
                if len(reports) == before and len(seen_urls) > 20:
                    break
            except Exception:
                break

        return reports

    # ---------- bo.nalog.ru: аудит как приоритетный источник ----------
    def _get_bo_audit_pdf(self, inn: str, year: int) -> tuple[str, bytes] | None:
        """Вернуть (имя_файла, pdf) аудиторского заключения с bo.nalog.ru.

        bo.nalog.ru отдаёт архив BFO с параметром auditReport=true. В архиве
        аудиторское заключение обычно имеет префикс NO_BUHAZ*.pdf. Если его
        нет, считаем, что на bo.nalog.ru заключение отсутствует — тогда
        вызывающий код использует e-disclosure.ru.
        """
        if BoNalogClient is None or get_bfo_data is None:
            return None
        inn = str(inn)
        year = int(year)
        try:
            if self.bo_client is None:
                self.bo_client = BoNalogClient()
            if inn not in self.bo_periods_cache:
                org = self.bo_client.search_by_inn(inn)
                if not org or not org.get("id"):
                    self.bo_periods_cache[inn] = {}
                    return None
                periods = self.bo_client.get_periods(org["id"])
                self.bo_periods_cache[inn] = periods
            details_id = self.bo_periods_cache.get(inn, {}).get(year)
            if not details_id:
                return None
            org_id = self.bo_client.get_organization_id(inn)
            if not org_id:
                return None
            archive = get_bfo_data(org_id, year, details_id)
            if not archive:
                return None
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
                # Сначала официальный префикс ФНС, затем мягкий fallback по имени.
                pdf_names = [n for n in names if n.lower().endswith(".pdf")]
                preferred = [n for n in pdf_names if Path(n).name.upper().startswith("NO_BUHAZ")]
                if not preferred:
                    preferred = [n for n in pdf_names if "BUHAZ" in Path(n).name.upper() or "AUDIT" in Path(n).name.upper()]
                if not preferred:
                    return None
                name = preferred[0]
                return name, z.read(name)
        except Exception as e:
            print(f"    bo.nalog audit {inn}/{year}: {type(e).__name__}: {e}")
            return None

    def _save_bo_audit_if_available(self, inn: str, year: int) -> str:
        """Скачать аудиторское заключение с bo.nalog.ru и сохранить PDF на диск как есть.
        Возвращает путь к файлу или '' (если на bo.nalog.ru заключения нет)."""
        item = self._get_bo_audit_pdf(inn, year)
        if not item:
            return ""
        name, pdf = item
        path = self._save_file(inn, year, "audit", pdf, ".pdf")
        print(f"      bo.nalog.ru: аудит {year} сохранён -> {path}")
        # OCR: распознавание Tesseract отключено
        # text = self._ocr_pdf(pdf, name)
        # text = re.sub(r"[ \t]+", " ", text)
        # text = re.sub(r"\n{3,}", "\n\n", text).strip()
        # if text:
        #     self._save_text(self._output_path(inn, year, "audit"), text)
        return path

    # ---------- download + OCR ----------
    def _download(self, url: str) -> bytes:
        response = self.session.ctx.request.get(url, timeout=120000)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status} при скачивании {url}")
        data = response.body()
        if len(data) > OCR_MAX_FILE_MB * 1024 * 1024:
            raise RuntimeError(f"Файл слишком большой: {len(data) / 1024 / 1024:.1f} MB")
        return data

    @staticmethod
    def _is_image(name: str) -> bool:
        return Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

    def _ocr_image(self, data: bytes) -> str:
        if Image is None:
            raise RuntimeError("Не установлен Pillow: pip install pillow")
        image = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(image, lang=OCR_LANG)

    def _ocr_pdf(self, data: bytes, source_name: str) -> str:
        if fitz is None:
            raise RuntimeError("Не установлен PyMuPDF: pip install pymupdf")
        doc = fitz.open(stream=data, filetype="pdf")
        chunks = []
        scale = OCR_DPI / 72.0
        matrix = fitz.Matrix(scale, scale)
        for page_no, page in enumerate(doc, 1):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            text = pytesseract.image_to_string(img, lang=OCR_LANG)
            chunks.append(f"\n--- Страница {page_no} ---\n{text}")
            if page_no % 10 == 0:
                print(f"      OCR {source_name}: страница {page_no}/{len(doc)}")
        return "".join(chunks).strip()

    def _ocr_blob(self, data: bytes, name: str) -> str:
        lower = name.lower()
        if lower.endswith(".pdf"):
            return self._ocr_pdf(data, name)
        if self._is_image(name):
            return self._ocr_image(data)
        if lower.endswith((".txt", ".html", ".htm")):
            return data.decode("utf-8", errors="replace")
        return ""

    def _ocr_zip(self, data: bytes, zip_name: str) -> str:
        chunks = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = [n for n in z.namelist() if not n.endswith("/")]
            for name in members:
                blob = z.read(name)
                if name.lower().endswith(".zip"):
                    text = self._ocr_zip(blob, name)
                else:
                    text = self._ocr_blob(blob, name)
                if text.strip():
                    chunks.append(f"\n===== {name} =====\n{text.strip()}")
        return "".join(chunks).strip()

    def download_and_ocr(self, url: str) -> str:
        data = self._download(url)
        name = url.split("?")[0].rsplit("/", 1)[-1] or "report"
        if data[:4] == b"PK\x03\x04" or name.lower().endswith(".zip"):
            return self._ocr_zip(data, name)
        return self._ocr_blob(data, name)

    @staticmethod
    def _safe_inn(inn: str) -> str:
        return re.sub(r"[^0-9A-Za-z_-]", "_", str(inn).strip())

    def _output_path(self, inn: str, year: int, document_kind: str) -> Path:
        """Одинаковое имя <год>.txt, но в отдельных папках annual/audit."""
        company_dir = Path(OCR_OUTPUT_ROOT) / self._safe_inn(inn) / document_kind
        company_dir.mkdir(parents=True, exist_ok=True)
        return company_dir / f"{int(year)}.txt"

    @staticmethod
    def _save_text(path: Path, text: str) -> None:
        # utf-8-sig удобно для открытия русского текста в Windows.
        path.write_text(text, encoding="utf-8-sig")

    # ---------- сохранение файлов на диск без OCR ----------
    @staticmethod
    def _detect_ext(data: bytes, url: str) -> str:
        if data[:4] == b"PK\x03\x04":
            return ".zip"
        if data[:5] == b"%PDF-":
            return ".pdf"
        suffix = Path(url.split("?")[0]).suffix.lower()
        return suffix if suffix and len(suffix) <= 5 else ".bin"

    def _find_saved(self, inn: str, year: int, document_kind: str) -> str:
        """Путь к уже сохранённому файлу <год>.* (любое расширение) или ''."""
        d = Path(OCR_OUTPUT_ROOT) / self._safe_inn(inn) / document_kind
        if d.is_dir():
            for f in sorted(d.glob(f"{int(year)}.*")):
                if f.is_file() and f.stat().st_size > 0:
                    return str(f)
        return ""

    def _save_file(self, inn: str, year: int, document_kind: str, data: bytes, ext: str) -> str:
        path = self._output_path(inn, year, document_kind).with_suffix(ext)
        path.write_bytes(data)
        return str(path)

    def download_and_save(self, inn: str, year: int, document_kind: str, url: str) -> str:
        """Скачивает файл по ссылке и сохраняет его на диск как есть. Возвращает путь."""
        data = self._download(url)
        return self._save_file(inn, year, document_kind, data, self._detect_ext(data, url))

    def collect(self, inn: str, candidate_years=None) -> dict[int, dict[str, str]]:
        """ИНН -> {год: {annual_path, audit_path, explanatory_path}} для ВСЕХ годов MIN_YEAR..MAX_YEAR.

        Годовые отчёты берутся из type=2, аудиторские заключения (сначала bo.nalog.ru, затем e-disclosure)
        и пояснительные записки - из type=3 (запасной вариант type=2).
        Файлы сохраняются на диск как есть, без OCR:
          C:/ap/<ИНН>/annual|audit|explanatory/<год>.<pdf|zip|...>
        Если документа за год нет, путь пустой.
        """
        inn = str(inn)
        cid = self.get_company_id(inn, candidate_years)
        if not cid:
            print(f"  e-disclosure: не найден company id для ИНН {inn}")
            return {}

        print(f"  e-disclosure: company id {cid}, ищу годовые отчёты, аудиторские заключения и пояснительные записки")
        links = {kind: self._get_document_links(cid, kind) for kind in ("annual", "audit", "explanatory")}

        # прогреваем список периодов bo.nalog (нужен для поиска аудита)
        try:
            if BoNalogClient is not None:
                if self.bo_client is None:
                    self.bo_client = BoNalogClient()
                org = self.bo_client.search_by_inn(inn)
                if org and org.get("id"):
                    self.bo_periods_cache[inn] = self.bo_client.get_periods(org["id"])
        except Exception as e:
            print(f"  bo.nalog: список аудиторских документов недоступен: {type(e).__name__}: {e}")

        result: dict[int, dict[str, str]] = {}
        for year in range(MIN_YEAR, MAX_YEAR + 1):
            result[year] = {"annual_path": "", "audit_path": "", "explanatory_path": ""}
            for kind, field in (("annual", "annual_path"), ("audit", "audit_path"),
                                ("explanatory", "explanatory_path")):
                key = (inn, year, kind)
                path = self.report_cache.get(key) or self._find_saved(inn, year, kind)
                try:
                    # аудит: сначала bo.nalog.ru, если там нет PDF - e-disclosure
                    if not path and kind == "audit":
                        path = self._save_bo_audit_if_available(inn, year)
                    if not path and year in links[kind]:
                        url = links[kind][year]
                        print(f"    {kind} {year}: {url}")
                        path = self.download_and_save(inn, year, kind, url)
                        print(f"      сохранено: {path}")
                        # OCR: распознавание Tesseract отключено
                        # text = self.download_and_ocr(url)
                        # text = re.sub(r"[ \t]+", " ", text)
                        # text = re.sub(r"\n{3,}", "\n\n", text).strip()
                        # self._save_text(self._output_path(inn, year, kind), text)
                except Exception as e:
                    print(f"    {kind} {year} не сохранён: {type(e).__name__}: {e}")
                if path:
                    self.report_cache[key] = path
                    result[year][field] = path
        return result
