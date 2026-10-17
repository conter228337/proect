import numpy as np
import requests
import zipfile
import io
import xlrd
import openpyxl
import PyPDF2
import re
from typing import Optional, Dict, Any, List
import io
import zipfile
import pytesseract
from pdf2image import convert_from_bytes
import os

# ---- Настройки OCR: поправьте под свой компьютер ----
# Папка bin из архива Poppler (там лежит pdftoppm.exe). Можно задать и переменной среды POPPLER_PATH.
POPPLER_BIN_PATH_SETTING = os.environ.get("POPPLER_PATH") or r"C:\poppler\Library\bin"
# Полный путь к tesseract.exe. Можно задать и переменной среды TESSERACT_CMD.
TESSERACT_EXE_SETTING = os.environ.get("TESSERACT_CMD") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _existing_dir(path):
    return path if path and os.path.isdir(path) else None


def _existing_file(path):
    return path if path and os.path.isfile(path) else None


def get_bfo_data(org_id: int, year, detailsId):
    """Скачать архив с бухгалтерской отчетностью по ID организации"""
    # url = f"https://bo.nalog.gov.ru/download/bfo/{org_id}?auditReport=true&balance=true&capitalChange=true&clarification=true&targetedFundsUsing=false&detailsId={detailsId}&financialResult=true&fundsMovement=true&type=XLS&period={year}"
    url = f"https://bo.nalog.gov.ru/download/bfo/{detailsId}?auditReport=true&balance=true&capitalChange=true&clarification=true&targetedFundsUsing=false&financialResult=true&fundsMovement=true&type=XLS&period={year}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://bo.nalog.gov.ru/organizations-card/{org_id}"
    }

    response = requests.get(url, headers=headers, timeout=(5, 60))

    if response.status_code == 200:
        return response.content
    else:
        # url = f"https://bo.nalog.gov.ru/download/bfo/{org_id}?auditReport=false&balance=true&capitalChange=false&clarification=true&targetedFundsUsing=false&detailsId={detailsId}&financialResult=true&fundsMovement=false&type=XLS&period={year}"
        url = f"https://bo.nalog.gov.ru/download/bfo/{detailsId}?auditReport=false&balance=true&capitalChange=false&clarification=true&targetedFundsUsing=false&financialResult=true&fundsMovement=false&type=XLS&period={year}"
        response = requests.get(url, headers=headers, timeout=(5, 60))
        if response.status_code == 200:
            return response.content

        print(f"Ошибка: {response.status_code}")
        return None


def extract_depreciation_from_pdf(pdf_content: bytes) -> Optional[Dict[str, Optional[float]]]:
    """
    Извлекает значения незавершенных капитальных вложений (код 5240) из PDF-файла

    Возвращает словарь с:
    - 'start_of_year': значение на начало года (None если прочерк)
    - 'end_of_period': значение на конец периода (None если прочерк)
    - 'difference': изменение за период (конец - начало)
    """
    try:
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_content))

        start_value = None  # На начало года
        end_value = None  # На конец периода

        if len(pdf_reader.pages) == 0:
            print("  PDF файл пуст")
            return None

        for page_num, page in enumerate(pdf_reader.pages):
            text = page.extract_text()
            if not text:
                continue

            lines = text.split('\n')

            for i, line in enumerate(lines):
                # Ищем строку с кодом 5240
                if re.search(r'5240', line):
                    # print(f"  Найдена строка с кодом 5240 на странице {page_num + 1}")
                    # print(f"    Содержимое: {line[:200]}...")

                    # Функция для проверки, является ли значение прочерком/пустым
                    def is_empty_value(val_str):
                        val_str = val_str.strip()
                        return val_str in ['', '-', '—', '–', '  ', '   ', 'х', 'Х', 'x', 'X', 'н/д', 'Н/Д', '(', ')',
                                           '( - )', '(-)', '-']

                    # Функция для парсинга числа
                    def parse_number(val_str):
                        if is_empty_value(val_str):
                            return None
                        # Убираем пробелы и заменяем запятую на точку
                        clean_num = val_str.replace(' ', '').replace(',', '.')
                        # Убираем возможные скобки для отрицательных чисел
                        if clean_num.startswith('(') and clean_num.endswith(')'):
                            clean_num = '-' + clean_num[1:-1]
                        try:
                            # Проверяем, что это не просто код строки
                            if clean_num in ['5240', '5240.0']:
                                return None
                            value = float(clean_num)
                            return value
                        except ValueError:
                            return None

                    # Разбиваем строку на части
                    parts = re.split(r'[\s\t]+', line)

                    # Ищем позицию кода 5240
                    code_pos = -1
                    for idx, part in enumerate(parts):
                        if part.strip() == 'всего5240':
                            code_pos = idx
                            break

                    start_value = None
                    end_value = None
                    values_found = []
                    if code_pos != -1:
                        # Начинаем поиск значений сразу после "г.1"
                        start_idx = code_pos + 4

                        # Собираем все последующие элементы, пока не найдем 2 значения
                        for idx in range(start_idx, len(parts)):
                            candidate = parts[idx]

                            # Проверяем, содержит ли элемент прочерк или число
                            # При этом пропускаем только явные текстовые маркеры
                            if candidate.find("г.1") != -1:
                                candidate = candidate[3:]

                            # Обрабатываем значение
                            if '(' in candidate and ')' in candidate:
                                # Извлекаем содержимое скобок
                                inner = candidate.strip('()')
                                if is_empty_value(inner):
                                    values_found.append(None)
                                else:
                                    parsed = parse_number(inner)
                                    if parsed is not None:
                                        values_found.append(parsed)
                                    else:
                                        # Если внутри скобок не число и не прочерк, пропускаем
                                        continue
                            elif is_empty_value(candidate):
                                values_found.append(0)
                            else:
                                parsed = parse_number(candidate)
                                if parsed is not None:
                                    values_found.append(parsed)
                                else:
                                    # Если это не число и не прочерк, пропускаем
                                    continue

                            # Если нашли 2 значения - останавливаемся
                            if len(values_found) >= 2:
                                break

                    # Если нашли оба значения
                    if len(values_found) >= 2:
                        start_value = values_found[0]
                        end_value = values_found[1]

                    # Альтернативный поиск: если не нашли через позицию кода
                    if start_value is None and end_value is None:
                        # Ищем все числа в строке и следующих строках
                        number_pattern = r'-?\d{1,3}(?:[ \.]?\d{3})*(?:[.,]\d+)?'
                        all_numbers = re.findall(number_pattern, line)

                        parsed_numbers = []
                        for num_str in all_numbers:
                            clean_num = num_str.replace(' ', '').replace(',', '.')
                            if clean_num in ['5240', '5240.0']:
                                continue
                            try:
                                value = float(clean_num)
                                if abs(value) > 0:
                                    parsed_numbers.append(value)
                            except ValueError:
                                continue

                        # Проверяем следующие строки
                        for j in range(1, 6):
                            if i + j < len(lines) and len(parsed_numbers) < 2:
                                next_line = lines[i + j]
                                # Пропускаем строки с подстроками "в том числе" и т.д.
                                if any(keyword in next_line for keyword in ['в том числе', 'группа', 'объект']):
                                    continue
                                next_numbers = re.findall(number_pattern, next_line)
                                for num_str in next_numbers:
                                    clean_num = num_str.replace(' ', '').replace(',', '.')
                                    if clean_num in ['5240', '5240.0']:
                                        continue
                                    try:
                                        value = float(clean_num)
                                        if abs(value) > 0:
                                            parsed_numbers.append(value)
                                    except ValueError:
                                        continue

                        if len(parsed_numbers) >= 2:
                            start_value = parsed_numbers[0]
                            end_value = parsed_numbers[1]
                        elif len(parsed_numbers) == 1:
                            end_value = parsed_numbers[0]
                            start_value = 0
                        break

            if start_value is not None or end_value is not None:
                break

        # Формируем результат
        # print(f"\n    На начало года: {start_value if start_value is not None else 'Прочерк (-)'}")
        # print(f"    На конец периода: {end_value if end_value is not None else 'Прочерк (-)'}")

        # Вычисляем изменение
        difference = None
        if start_value is not None and end_value is not None:
            difference = end_value - start_value
            # print(f"    Изменение за период: {difference:,.0f}")
        # elif start_value is not None and end_value is None:
            # print("    На конец периода - прочерк, изменение = None")
        # elif start_value is None and end_value is not None:
            # print("    На начало года - прочерк, изменение = None")
        else:
            # print("    Оба значения - прочерк, изменение = 0")
            difference = 0

        raw_lines = []
        for pg in pdf_reader.pages:
            t = pg.extract_text() or ''
            ls = t.split('\n')
            for k, ln in enumerate(ls):
                if '5240' in ln:
                    raw_lines.append(' | '.join(ls[k:k + 3])[:300])
        return {
            'start_of_year': start_value,
            'end_of_period': end_value,
            'difference': difference,
            'raw': raw_lines,
        }

    except Exception as e:
        print(f"  Ошибка при обработке PDF: {e}")
        import traceback
        traceback.print_exc()
        return None


def find_code_columns(sheet):
    """Найти колонки с кодами строк и значениями в листе"""
    code_col = None
    value_col = None

    # Проходим по первой строке (заголовки)
    first_row = None
    max_col = 30
    first_nrow = 0
    end = False

    # Получаем первую строку
    for row_idx in range(min(15, sheet.max_row)):  # Проверяем первые 5 строк
        for col_idx in range(max_col):
            cell = sheet.cell(row=row_idx + 1, column=col_idx + 1)
            if cell.value:
                val_str = str(cell.value).strip()
                if 'код' in val_str.lower() or 'код строки' in val_str.lower() or val_str == 'Код строки':
                    code_col = col_idx
                    # print(f"Найдена колонка кодов: {code_col} (значение: {val_str})")
                elif 'За ' in val_str:
                    if not first_row and row_idx != first_nrow:
                        first_row = True
                        first_nrow = row_idx + 1
                        continue
                    else:
                        if end:
                            continue
                        end = True
                    value_col = col_idx
                    # print(f"Найдена колонка значений за 2025: {value_col} (значение: {val_str})")
                elif 'на 31 декабря' in val_str.lower():
                    if not first_row and row_idx != first_nrow:
                        first_row = True
                        first_nrow = row_idx + 1
                        continue
                    else:
                        if end:
                            continue
                        end = True
                    value_col = col_idx
                    # print(f"Найдена колонка значений за 2025: {value_col} (значение: {val_str})")

    # Если не нашли, пробуем найти по позиции из примера
    if code_col is None:
        code_col = 7  # Колонка H (индекс 7) - предположительно
        # print(f"Используем колонку кодов по умолчанию: {code_col}")

    if value_col is None:
        value_col = 11  # Колонка L (индекс 11) - предположительно
        # print(f"Используем колонку значений по умолчанию: {value_col}")

    return code_col, value_col


def get_sheet_data(sheet):
    """Преобразовать лист в удобный для поиска формат"""
    data = []
    for row in sheet.iter_rows(values_only=True):
        data.append([str(cell).strip() if cell else '' for cell in row])
    return data


def extract_financials_from_bfo(zip_content: bytes, period=None) -> Dict[str, Any]:
    """
    Извлекает финансовые показатели из скачанного zip-архива с Excel файлом
    period: отчётный год (попадает в поле 'period' результата)
    """
    interest_expense = 0
    financials = {
        'total_assets': np.nan,
        'total_liabilities': np.nan,
        'equity': np.nan,
        'net_income': np.nan,
        'ebitda': np.nan,
        'current_assets': np.nan,
        'current_liabilities': np.nan,
        'cash': np.nan,
        'revenue': 0,
        'depreciation': np.nan,  # Добавляем поле для амортизации
        'period': str(period) if period is not None else None,
        'has_audit': False,
        'ap': '',
    }

    # Распаковываем zip
    try:
        with zipfile.ZipFile(io.BytesIO(zip_content)) as zip_file:
            # Исправлено условие: ищем файлы, которые содержат 'NO_BUHPZ' ИЛИ 'NO_BUHAZ'
            excel_files = [f for f in zip_file.namelist() if f.endswith(('.xls', '.xlsx'))]
            pdf_files = [f for f in zip_file.namelist() if f.endswith('.pdf') and ('NO_BUHPZ' in f or 'NO_BUHAZ' in f)]

            no_buhaz_content = None
            no_buhpz_content = None

            for pdf_file in pdf_files:
                if 'NO_BUHAZ' in pdf_file:
                    with zip_file.open(pdf_file) as f:
                        no_buhaz_content = f.read()
                elif 'NO_BUHPZ' in pdf_file:
                    with zip_file.open(pdf_file) as f:
                        no_buhpz_content = f.read()

            if not excel_files:
                print("В архиве не найден Excel файл")
                return financials

            excel_file = excel_files[0]

            # Извлекаем текст из NO_BUHAZ (пояснительная записка) через OCR
            # POPPLER_BIN_PATH = _existing_dir(POPPLER_BIN_PATH_SETTING)   # None -> искать в PATH
            # tesseract_exe = _existing_file(TESSERACT_EXE_SETTING)
            # if tesseract_exe:
            #     pytesseract.pytesseract.tesseract_cmd = tesseract_exe     # иначе берётся из PATH

            # if no_buhaz_content:
            #     print("\n=== Извлечение записки из PDF (OCR) ===")
            #     # OCR нужен только для текста пояснительной записки (поле 'ap').
            #     # Если Poppler/Tesseract недоступны, баланс из Excel всё равно разбираем.
            #     try:
            #         pages = convert_from_bytes(
            #             no_buhaz_content,
            #             dpi=200,
            #             poppler_path=POPPLER_BIN_PATH
            #         )

            #         full_text = []
            #         for i, page in enumerate(pages):
            #             print(f"Распознается страница {i + 1} из {len(pages)}...")
            #             page_text = pytesseract.image_to_string(page, lang='rus+eng')
            #             full_text.append(page_text)

            #         financials["ap"] = "\n".join(full_text)
            #         print("Распознавание завершено успешно!")
            #     except Exception as ocr_error:
            #         print(f"  OCR пропущен ({type(ocr_error).__name__}): {ocr_error}")

            if no_buhpz_content:
                print("\n=== Извлечение амортизации из PDF ===")
                depreciation_data = extract_depreciation_from_pdf(no_buhpz_content)
                if depreciation_data:
                    end_val = depreciation_data['end_of_period']
                    raw = depreciation_data.get('raw') or []
                    if not raw:
                        # кода 5240 в тексте PDF нет (скан? другая форма?) — это «нет данных», а не 0
                        print("  Код 5240 в PDF не найден (возможно, скан без текстового слоя) -> амортизация: нет данных")
                    elif end_val is None:
                        print(f"  Код 5240 найден, но значение не распознано -> нет данных. Строки PDF: {raw}")
                    else:
                        financials['depreciation'] = int(end_val)
                        print(f"  Амортизация (стр.5240): {int(end_val):,.0f}   [строки PDF: {raw[:2]}]")
                else:
                    print("  Не удалось найти значение амортизации в PDF")
            else:
                print("\n=== PDF файл с пояснениями не найден ===")

            # Извлекаем амортизацию из PDF, если он есть
            # if no_buhpz_content:
            #     print("\n=== Извлечение амортизации из PDF ===")
            #     depreciation_data = extract_depreciation_from_pdf(no_buhpz_content)
            #     if depreciation_data:
            #         # financials['depreciation_start'] = depreciation_data['start_of_year']
            #         if depreciation_data['end_of_period'] == '-':
            #             depreciation = 0
            #         else:
            #             depreciation = int(depreciation_data['end_of_period'])
            #         financials['depreciation'] = depreciation
                    # financials['depreciation_change'] = depreciation_data['difference']

                # depreciation = extract_depreciation_from_pdf(pdf_content)
                # if depreciation is not None:
                #     financials['depreciation'] = depreciation
            #         print(f"  Амортизация (стр.5240): {depreciation:,.0f}")
            #     else:
            #         print("  Не удалось найти значение амортизации в PDF")
            # else:
            #     print("\n=== PDF файл с пояснениями не найден ===")

            with zip_file.open(excel_file) as f:
                excel_data = f.read()

            # Читаем Excel
            wb = openpyxl.load_workbook(io.BytesIO(excel_data), data_only=True)

            # Читаем лист с бухгалтерским балансом
            if 'Бухгалтерский баланс' in wb.sheetnames:
                sheet = wb['Бухгалтерский баланс']
                # print("\n=== Чтение бухгалтерского баланса ===")

                # Преобразуем лист в данные
                sheet_data = get_sheet_data(sheet)

                # Находим колонки с кодами и значениями
                code_col, value_col = find_code_columns(sheet)

                # Ищем значения по кодам
                for row_idx, row in enumerate(sheet_data):
                    if len(row) > code_col:
                        code = row[code_col].strip()

                        # Проверяем коды строк
                        if code.isdigit():
                            code_num = int(code)

                            # Ищем значение в колонке со значениями
                            if len(row) > value_col:
                                val_str = row[value_col]

                                # Парсим значение
                                if val_str in ['', '-', '—']:
                                    val_str = "0"

                                # Обрабатываем отрицательные числа
                                if val_str.startswith('(') and val_str.endswith(')'):
                                    val_str = '-' + val_str[1:-1]
                                val_str = val_str.replace(' ', '').replace(',', '.')
                                try:
                                    value = float(val_str)

                                    # Маппинг кодов
                                    if code_num == 1600:
                                        financials['total_assets'] = value
                                        # print(f"  Строка 1600 (Актив итого): {value:,.0f}")
                                    elif code_num == 1300:
                                        financials['equity'] = value
                                        # print(f"  Строка 1300 (Капитал итого): {value:,.0f}")
                                    elif code_num == 1200:
                                        financials['current_assets'] = value
                                        # print(f"  Строка 1200 (Оборотные активы): {value:,.0f}")
                                    elif code_num == 1250:
                                        financials['cash'] = value
                                        # print(f"  Строка 1250 (Денежные средства): {value:,.0f}")
                                    elif code_num == 1400:
                                        long_term_liabilities = value
                                        # print(f"  Строка 1400 (Долгосрочные обязательства): {value:,.0f}")
                                    elif code_num == 1500:
                                        financials['current_liabilities'] = value
                                        # print(f"  Строка 1500 (Краткосрочные обязательства): {value:,.0f}")
                                except:
                                    pass

            # Читаем лист с отчетом о финансовых результатах
            if 'Отчет о финансовых результатах' in wb.sheetnames:
                sheet = wb['Отчет о финансовых результатах']
                # print("\n=== Чтение отчета о финансовых результатах ===")

                sheet_data = get_sheet_data(sheet)
                code_col, value_col = find_code_columns(sheet)

                for row_idx, row in enumerate(sheet_data):
                    if len(row) > code_col:
                        code = row[code_col].strip()

                        if code.isdigit():
                            code_num = int(code)

                            if len(row) > value_col:
                                val_str = row[value_col]

                                if val_str in ['', '-', '—']:
                                    val_str = "0"

                                if val_str.startswith('(') and val_str.endswith(')'):
                                    val_str = '-' + val_str[1:-1]
                                val_str = val_str.replace(' ', '').replace(',', '.')
                                try:
                                    value = float(val_str)

                                    if code_num == 2110:
                                        financials['revenue'] = value
                                        # print(f"  Строка 2110 (Выручка): {value:,.0f}")
                                    elif code_num == 2400:
                                        financials['net_income'] = value
                                        # print(f"  Строка 2400 (Чистая прибыль): {value:,.0f}")
                                    elif code_num == 2410:
                                        income_tax = value
                                        # print(f"  Строка 2410 (Налог на прибыль): {value:,.0f}")
                                    elif code_num == 2330:
                                        interest_expense = value
                                        # print(f"  Строка 2330 (Проценты к уплате): {value:,.0f}")
                                except:
                                    pass

                # Расчет EBITDA
                if not np.isnan(financials['net_income']):
                    # Преобразуем отрицательные расходы в положительные для добавления
                    interest = abs(interest_expense) if interest_expense < 0 else interest_expense
                    tax = abs(income_tax) if income_tax < 0 else income_tax

                    # Используем амортизацию из PDF
                    depreciation = financials['depreciation'] if not np.isnan(financials['depreciation']) else 0

                    # print("\n=== Расчёт EBITDA (классический) ===")
                    # print(f"  Чистая прибыль (убыток): {financials['net_income']:,.0f}")
                    # print(f"  Проценты к уплате (+) : {interest:,.0f}")
                    # print(f"  Налог на прибыль (+)   : {tax:,.0f}")
                    # print(f"  Амортизация (+)        : {depreciation:,.0f}")

                    financials['ebitda'] = financials['net_income'] + interest + tax + depreciation

                    # print(f"\n  EBITDA (расчётный): {financials['ebitda']:,.0f}")

                financials['total_liabilities'] = long_term_liabilities + financials['current_liabilities']

            # Читаем информацию об аудите
            if 'Сведения об организации' in wb.sheetnames:
                sheet = wb['Сведения об организации']
                sheet_data = get_sheet_data(sheet)

                for row in sheet_data:
                    for col_idx, cell in enumerate(row):
                        if 'Бухгалтерская отчетность подлежит обязательному аудиту' in str(cell):
                            # Ищем значение в соседней колонке
                            if col_idx + 1 < len(row):
                                audit_val = row[col_idx + 1]
                                financials['has_audit'] = (audit_val == 'Да' or audit_val == 'Yes')
                                # print(f"\n=== Информация об аудите ===")
                                # print(f"  Аудиторское заключение: {'Да' if financials['has_audit'] else 'Нет'}")
                            break

    except Exception as e:
        print(f"Ошибка при обработке: {e}")
        import traceback
        traceback.print_exc()

    return financials


def main():
    org_id = 8019526  # ИНН организации
    zip_content = get_bfo_data(org_id, 2021, 43061910)

    if zip_content:
        financials = extract_financials_from_bfo(zip_content)

        print("\n" + "=" * 60)
        print("ИТОГОВЫЕ ФИНАНСОВЫЕ ПОКАЗАТЕЛИ")
        print("=" * 60)

        print(f"\nАКТИВЫ:")
        print(f"  Итого активы (стр.1600):           {financials['total_assets']:>18,.0f}" if not np.isnan(
            financials['total_assets']) else "  Итого активы (стр.1600):           Нет данных")
        print(f"  Оборотные активы (стр.1200):       {financials['current_assets']:>18,.0f}" if not np.isnan(
            financials['current_assets']) else "  Оборотные активы (стр.1200):       Нет данных")
        print(f"  Денежные средства (стр.1250):      {financials['cash']:>18,.0f}" if not np.isnan(
            financials['cash']) else "  Денежные средства (стр.1250):      Нет данных")

        print(f"\nКАПИТАЛ И ОБЯЗАТЕЛЬСТВА:")
        print(f"  Собственный капитал (стр.1300):    {financials['equity']:>18,.0f}" if not np.isnan(
            financials['equity']) else "  Собственный капитал (стр.1300):    Нет данных")
        print(f"  Краткосрочные обязательства (стр.1500):{financials['current_liabilities']:>15,.0f}" if not np.isnan(
            financials['current_liabilities']) else "  Краткосрочные обязательства:      Нет данных")

        print(f"\nФИНАНСОВЫЕ РЕЗУЛЬТАТЫ:")
        print(f"  Выручка (стр.2110):               {financials['revenue']:>18,.0f}" if not np.isnan(
            financials['revenue']) else "  Выручка (стр.2110):               Нет данных")
        print(f"  Чистая прибыль (стр.2400):        {financials['net_income']:>18,.0f}" if not np.isnan(
            financials['net_income']) else "  Чистая прибыль (стр.2400):        Нет данных")
        print(f"  Амортизация (стр.5240):           {financials['depreciation']:>18,.0f}" if not np.isnan(
            financials['depreciation']) else "  Амортизация (стр.5240):           Нет данных")
        print(f"  EBITDA (расчетный):               {financials['ebitda']:>18,.0f}" if not np.isnan(
            financials['ebitda']) else "  EBITDA:                           Нет данных")

        print(f"\nПРОЧЕЕ:")
        print(f"  Аудиторское заключение:           {'Да' if financials['has_audit'] else 'Нет'}")
    else:
        print("Не удалось получить данные")


if __name__ == "__main__":
    main()