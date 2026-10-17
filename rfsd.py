""""Бухгалтерская отчётность из RFSD (Russian Financial Statements Database).

Замена bo.nalog.ru: вместо скачивания архивов с сайта данные читаются из локальных
parquet-файлов RFSD (https://huggingface.co/datasets/irlspbru/RFSD), разложенных по папкам
``<RFSD_DIR>/year=<ГОД>/*.parquet``.

Формат результата такой же, как у bo_nalog.get_bo_nalog_data_all_years:
``{год: financials}``, где financials содержит ключи
total_assets, total_liabilities, equity, net_income, ebitda, current_assets,
current_liabilities, cash, revenue, depreciation, period, has_audit, ap.

Что RFSD НЕ даёт (в отличие от PDF-архивов bo.nalog):
  * пояснительную записку (поле 'ap' остаётся пустым);
  * амортизацию (стр. 5240 из пояснений), поэтому depreciation = NaN, а EBITDA считается
    без амортизации: чистая прибыль + проценты к уплате + налог на прибыль;
  * признак обязательного аудита (has_audit = False).

Нужны библиотеки: pip install pyarrow pandas numpy huggingface_hub
"""
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from config import MIN_YEAR, MAX_YEAR
from logutil import log as print   # print с префиксом [ИНН ...] текущей компании

# Папка с данными RFSD. Можно задать переменной среды RFSD_DIR.
RFSD_DIR = Path(os.environ.get("RFSD_DIR") or (Path(__file__).resolve().parent / "rfsd_data"))

# Скачивать недостающие годы с Hugging Face автоматически (нужен интернет и huggingface_hub).
# Файлы RFSD большие (по несколько сотен МБ на год).
AUTO_DOWNLOAD = True
HF_REPO_ID = "irlspbru/RFSD"

# Коды строк отчётности, которые нужны для расчётов (в RFSD колонки называются line_<код>).
LINE_CODES = {
    'total_assets': 1600,
    'equity': 1300,
    'current_assets': 1200,
    'cash': 1250,
    'long_term_liabilities': 1400,
    'current_liabilities': 1500,
    'revenue': 2110,
    'interest_expense': 2330,
    'income_tax': 2410,
    'net_income': 2400,
}

# True, если при последней загрузке произошла ошибка чтения данных (а не «отчётности нет»).
LAST_ERROR = False

_CACHE = {}  # ИНН -> {год: financials}
_NOT_AVAILABLE = set()  # годы, которых нет на Hugging Face (чтобы не качать повторно)


# ---------------------------------------------------------------- доступ к файлам

def _year_dirs():
    """{год: путь} для всех найденных папок year=YYYY внутри RFSD_DIR."""
    found = {}
    if RFSD_DIR.exists():
        for p in RFSD_DIR.rglob("year=*"):
            # служебные папки Hugging Face (.cache/...) содержат только метаданные — пропускаем
            if p.is_dir() and not any(part.startswith(".") for part in p.relative_to(RFSD_DIR).parts):
                try:
                    found[int(p.name.split("=", 1)[1])] = p
                except ValueError:
                    pass
    return found


def _ensure_downloaded(years):
    """Скачать с Hugging Face годы, которых нет локально."""
    # год считается скачанным, только если в его папке есть .parquet (пустые папки — недокачка)
    have = {y for y, d in _year_dirs().items()
            if any(f.suffix.lower() == ".parquet" for f in d.rglob("*") if f.is_file())}
    missing = [y for y in years if y not in have and y not in _NOT_AVAILABLE]
    if not missing or not AUTO_DOWNLOAD:
        return
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("  RFSD: не установлен huggingface_hub, автоскачивание пропущено "
              "(pip install huggingface_hub)")
        return
    print(f"  RFSD: скачиваю годы {missing} в {RFSD_DIR} ...")
    try:
        snapshot_download(
            repo_id=HF_REPO_ID,
            repo_type="dataset",
            local_dir=str(RFSD_DIR),
            allow_patterns=[f"RFSD/year={y}/*" for y in missing],
        )
    except Exception as e:
        print(f"  RFSD: сбой скачивания: {type(e).__name__}: {e}")
    # то, что после попытки так и не появилось, на сервере отсутствует — повторно не качаем
    have_now = {y for y, d in _year_dirs().items()
                if any(f.suffix.lower() == ".parquet" for f in d.rglob("*") if f.is_file())}
    _NOT_AVAILABLE.update(y for y in missing if y not in have_now)


def _year_files(years):
    """{год: [parquet-файлы]} — только реально существующие файлы (любое расширение .parquet)."""
    _ensure_downloaded(years)
    out = {}
    for y, d in _year_dirs().items():
        if y not in years:
            continue
        files = sorted(f for f in d.rglob("*") if f.is_file() and f.suffix.lower() == ".parquet")
        if files:
            out[y] = files
    return out


def _read_raw(inns, years):
    """Прочитать из RFSD нужные колонки по списку ИНН и годам -> DataFrame."""
    import pyarrow as pa
    import pyarrow.dataset as ds

    files_by_year = _year_files(set(years))
    if not files_by_year:
        raise FileNotFoundError(
            f"В {RFSD_DIR} нет ни одного .parquet-файла для годов {list(years)}. "
            f"Скачайте датасет {HF_REPO_ID} (структура RFSD/year=YYYY/*.parquet) "
            f"или задайте путь переменной среды RFSD_DIR. "
            f"Найденные папки годов: {sorted(_year_dirs())}")
    missing = [y for y in years if y not in files_by_year]
    if missing:
        print(f"  RFSD: нет файлов за годы {missing} (в датасете их может не быть) — пропускаю")

    inn_set = [str(i) for i in inns]
    frames = []
    for y, files in sorted(files_by_year.items()):
        dset = ds.dataset([str(f) for f in files], format="parquet")
        names = set(dset.schema.names)
        if 'inn' not in names:
            raise ValueError(f"В файлах за {y} нет колонки 'inn'; есть: {sorted(names)[:15]}")
        inn_type = dset.schema.field('inn').type
        values = inn_set if pa.types.is_string(inn_type) or pa.types.is_large_string(inn_type) \
            else [int(i) for i in inn_set if i.isdigit()]
        cols = ['inn'] + [f'line_{c}' for c in LINE_CODES.values() if f'line_{c}' in names]
        df = dset.to_table(columns=cols, filter=ds.field('inn').isin(values)).to_pandas()
        df['year'] = y
        frames.append(df)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=['inn', 'year'])
    df['inn'] = df['inn'].astype(str)
    df['year'] = df['year'].astype(int)
    return df


# ---------------------------------------------------------------- преобразование

def _num(row, code):
    """Число из колонки line_<код>; NaN, если колонки или значения нет."""
    v = row.get(f'line_{code}', np.nan)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return np.nan
    return v


def _row_to_financials(row, year):
    """Одна строка RFSD (фирма-год) -> словарь показателей в формате bo_nalog_excel."""
    total_assets = _num(row, LINE_CODES['total_assets'])
    equity = _num(row, LINE_CODES['equity'])
    current_assets = _num(row, LINE_CODES['current_assets'])
    cash = _num(row, LINE_CODES['cash'])
    lt_liab = _num(row, LINE_CODES['long_term_liabilities'])
    cur_liab = _num(row, LINE_CODES['current_liabilities'])
    revenue = _num(row, LINE_CODES['revenue'])
    net_income = _num(row, LINE_CODES['net_income'])
    interest = _num(row, LINE_CODES['interest_expense'])
    tax = _num(row, LINE_CODES['income_tax'])

    # Обязательства: как в bo_nalog_excel — стр.1400 + стр.1500 (пустая 1400 = 0)
    if np.isnan(cur_liab) and np.isnan(lt_liab):
        total_liabilities = np.nan
    else:
        total_liabilities = (0 if np.isnan(lt_liab) else lt_liab) + (0 if np.isnan(cur_liab) else cur_liab)

    # EBITDA = чистая прибыль + проценты + налог на прибыль (амортизации в RFSD нет)
    if np.isnan(net_income):
        ebitda = np.nan
    else:
        interest_abs = 0 if np.isnan(interest) else abs(interest)
        tax_abs = 0 if np.isnan(tax) else abs(tax)
        ebitda = net_income + interest_abs + tax_abs

    return {
        'total_assets': total_assets,
        'total_liabilities': total_liabilities,
        'equity': equity,
        'net_income': net_income,
        'ebitda': ebitda,
        'current_assets': current_assets,
        'current_liabilities': cur_liab,
        'cash': cash,
        'revenue': 0 if np.isnan(revenue) else revenue,  # как в bo_nalog_excel: пусто = 0
        'depreciation': np.nan,
        'period': str(year),
        'has_audit': False,
        'ap': '',
    }


def _frame_to_by_inn(df):
    """DataFrame RFSD -> {ИНН: {год: financials}}."""
    out = {}
    if df.empty:
        return out
    # на случай дублей фирма-год оставляем последнюю запись
    df = df.sort_values(['inn', 'year']).drop_duplicates(['inn', 'year'], keep='last')
    for rec in df.to_dict('records'):
        out.setdefault(rec['inn'], {})[int(rec['year'])] = _row_to_financials(rec, int(rec['year']))
    return out


# ---------------------------------------------------------------- публичный интерфейс

def clear_cache():
    """Очистить кэш отчётности (нужно при обработке компаний пачками, чтобы не копить память)."""
    _CACHE.clear()


def _normalize_inns(col):
    """Колонка inn (строки или числа) -> Series строк-ИНН из 10 или 12 цифр; мусор и пустые отбрасываются."""
    s = pd.Series(col.to_pandas()).dropna()
    numeric = not (pd.api.types.is_string_dtype(s) or s.dtype == object)
    if numeric:
        s = s.astype("int64")
    s = s.astype(str).str.strip()
    if numeric:
        # у чисел пропали ведущие нули: 9 знаков -> 10 (юрлица), 11 знаков -> 12 (ИП)
        n = s.str.len()
        s = s.where(n != 9, s.str.zfill(10)).where(n != 11, s.str.zfill(12))
    return s[s.str.fullmatch(r"\d{10}|\d{12}")]


def list_inns(years=None):
    """Все ИНН, которые есть в RFSD за годы MIN_YEAR..MAX_YEAR (или за `years`).

    Возвращает DataFrame: inn, first_year, last_year, n_years (за сколько лет есть отчётность).
    Читается только колонка inn, поэтому память и время небольшие по сравнению с чтением отчётности."""
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    years = _years_range(years)
    files_by_year = _year_files(set(years))
    if not files_by_year:
        raise FileNotFoundError(
            f"В {RFSD_DIR} нет ни одного .parquet-файла для годов {years}. "
            f"Скачайте датасет {HF_REPO_ID} (структура RFSD/year=YYYY/*.parquet) "
            f"или задайте путь переменной среды RFSD_DIR. Найденные папки годов: {sorted(_year_dirs())}")
    frames = []
    for y, files in sorted(files_by_year.items()):
        dset = ds.dataset([str(f) for f in files], format="parquet")
        if "inn" not in dset.schema.names:
            raise ValueError(f"В файлах за {y} нет колонки 'inn'; есть: {sorted(dset.schema.names)[:15]}")
        uniq = pc.unique(dset.to_table(columns=["inn"]).column("inn"))
        inns = _normalize_inns(uniq).drop_duplicates()
        frames.append(pd.DataFrame({"inn": inns.values, "year": y}))
        print(f"  RFSD: {y} г.: {len(inns)} ИНН")
    all_df = pd.concat(frames, ignore_index=True)
    g = all_df.groupby("inn")["year"].agg(first_year="min", last_year="max", n_years="count").reset_index()
    return g.sort_values("inn", ignore_index=True)


def _years_range(years=None):
    base = set(range(MIN_YEAR, MAX_YEAR + 1))
    return sorted(base & set(years)) if years is not None else sorted(base)


def preload(inns, years=None):
    """Прочитать RFSD сразу по всем ИНН одним проходом (быстрее, чем по одной компании)."""
    global LAST_ERROR
    LAST_ERROR = False
    inns = sorted({str(i) for i in inns if i})
    if not inns:
        return
    try:
        by_inn = _frame_to_by_inn(_read_raw(inns, _years_range(years)))
    except Exception as e:
        LAST_ERROR = True
        print(f"  RFSD: ошибка чтения данных: {type(e).__name__}: {e}")
        return
    for inn in inns:
        _CACHE[inn] = by_inn.get(inn, {})
    print(f"  RFSD: загружено {len(inns)} ИНН, найдено отчётности по {sum(1 for i in inns if _CACHE[i])}")


def get_rfsd_data_all_years(inn, years=None):
    """Отчётность по всем годам (не раньше MIN_YEAR), доступным в RFSD: {год: financials}.

    Пустой словарь — у компании нет отчётности в RFSD (или ошибка чтения, тогда LAST_ERROR = True).
    """
    global LAST_ERROR
    inn = str(inn)
    if inn not in _CACHE:
        LAST_ERROR = False
        try:
            _CACHE[inn] = _frame_to_by_inn(_read_raw([inn], _years_range())).get(inn, {})
        except Exception as e:
            LAST_ERROR = True
            print(f"  RFSD: ошибка чтения данных для ИНН {inn}: {type(e).__name__}: {e}")
            return {}
    else:
        LAST_ERROR = False
    data = _CACHE[inn]
    if years is not None:
        return {y: v for y, v in data.items() if y in set(years)}
    return dict(data)


def get_rfsd_data(inn, year):
    """Отчётность за один год в формате financials; пустой словарь, если данных нет."""
    return get_rfsd_data_all_years(inn).get(int(year), {})