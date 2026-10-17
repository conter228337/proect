from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import time
import re
import json
from logutil import log as print   # print с префиксом [ИНН ...] текущей компании


def get_instrument_id(driver):
    """
    Получение instrument_id со страницы Investing.com
    """
    try:
        # Способ 1: Из JavaScript переменных
        instrument_id = driver.execute_script(r"""
            // Проверяем глобальные переменные
            if (window.instrumentId) return window.instrumentId;
            if (window.InstrumentId) return window.InstrumentId;
            if (window.currentInstrument) return window.currentInstrument.id;
            if (window.instrument) return window.instrument.id;
            if (window.pairId) return window.pairId;

            // Ищем в data-атрибутах
            const body = document.body;
            const dataAttr = body.getAttribute('data-instrument-id') || 
                            body.getAttribute('data-pair-id') ||
                            body.getAttribute('data-instrument');
            if (dataAttr) return dataAttr;

            // Ищем в мета-тегах
            const metaTags = document.querySelectorAll('meta');
            for (let meta of metaTags) {
                const content = meta.getAttribute('content');
                if (meta.getAttribute('name') === 'instrument_id' && content) return content;
                if (meta.getAttribute('property') === 'instrument:id' && content) return content;
            }

            // Ищем в скриптах
            const scripts = document.querySelectorAll('script');
            for (let script of scripts) {
                const scriptContent = script.textContent;
                if (scriptContent) {
                    // Ищем instrumentId в JSON
                    const match = scriptContent.match(/instrumentId["']?\s*:\s*["']?(\d+)["']?/);
                    if (match) return match[1];

                    // Ищем pairId
                    const pairMatch = scriptContent.match(/pairId["']?\s*:\s*["']?(\d+)["']?/);
                    if (pairMatch) return pairMatch[1];
                }
            }

            return null;
        """)

        if instrument_id:
            # print(f"✅ Найден instrument_id: {instrument_id}")
            return instrument_id

        # Способ 2: Из URL API запросов
        instrument_id = driver.execute_script(r"""
            // Ищем в сетевых запросах (если доступно)
            if (window.performance && window.performance.getEntries) {
                const entries = performance.getEntries();
                for (let entry of entries) {
                    if (entry.name && entry.name.includes('get HistoricalData')) {
                        const match = entry.name.match(/[&?]pair_id[=:](\d+)/);
                        if (match) return match[1];

                        const instrumentMatch = entry.name.match(/[&?]instrument_id[=:](\d+)/);
                        if (instrumentMatch) return instrumentMatch[1];
                    }
                }
            }
            return null;
        """)

        if instrument_id:
            # print(f"✅ Найден instrument_id из сетевых запросов: {instrument_id}")
            return instrument_id

        # Способ 3: Из URL страницы
        current_url = driver.current_url
        url_match = re.search(r'/(\d+)(?:-|$)', current_url)
        if url_match:
            potential_id = url_match.group(1)
            if len(potential_id) > 3:  # ID обычно длинный
                # print(f"✅ Найден instrument_id из URL: {potential_id}")
                return potential_id

    except Exception as e:
        print(f"⚠️ Ошибка при получении instrument_id: {e}")

    return None


def get_data_selenium_all_years(link):
    """
    Получение Diluted Weighted Average Shares Outstanding за все доступные годы
    + instrument_id
    """
    if link is None:
        print("Ссылка не найдена")
        return None

    options = Options()
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)

    # Опционально: отключаем уведомления
    prefs = {
        "profile.default_content_setting_values.notifications": 2
    }
    options.add_experimental_option("prefs", prefs)

    # Автоматическая установка драйвера
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    try:
        # print("🔄 Открываем страницу...")
        if link.find("etfs") == -1:
            driver.get(link + '-income-statement')
        else:
            driver.get(link)

        # Ждём загрузки таблицы
        wait = WebDriverWait(driver, 15)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table, .genTbl, [data-test='table']")))

        # Даём время на полную загрузку данных
        time.sleep(5)

        # Получаем instrument_id
        instrument_id = get_instrument_id(driver)

        # Выполняем скрипт для поиска данных
        result = driver.execute_script(r"""
            // Функция для очистки чисел
            function cleanNumber(value) {
                if (!value || value === 'aa.aa' || value === '-' || value === 'N/A') return null;
                // Удаляем пробелы, запятые и другие разделители
                let cleaned = value.replace(/,/g, '').replace(/\s/g, '').replace(/\$/g, '');
                // Проверяем, является ли число
                if (/^-?\d+(\.\d+)?$/.test(cleaned)) {
                    return cleaned;
                }
                return null;
            }

            // Ищем все таблицы
            const tables = document.querySelectorAll('table');
            let targetRow = null;
            let targetTable = null;

            // Ищем строку с Diluted Weighted Average Shares
            for (let table of tables) {
                const rows = table.querySelectorAll('tr');
                for (let row of rows) {
                    const firstCell = row.querySelector('td:first-child, th:first-child');
                    if (firstCell && firstCell.innerText.includes('Diluted Weighted Average Shares')) {
                        targetRow = row;
                        targetTable = table;
                        break;
                    }
                }
                if (targetRow) break;
            }

            if (!targetRow) {
                // Пробуем поиск по тексту во всей странице
                const elements = document.evaluate(
                    "//*[contains(text(), 'Diluted Weighted Average Shares')]",
                    document,
                    null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE,
                    null
                ).singleNodeValue;

                if (elements) {
                    targetRow = elements.closest('tr');
                    if (targetRow) targetTable = targetRow.closest('table');
                }
            }

            if (!targetRow) {
                return { error: "Строка с Diluted Weighted Average Shares не найдена" };
            }

            // Получаем значения из строки
            const cells = targetRow.querySelectorAll('td');
            const values = [];

            for (let cell of cells) {
                const value = cell.innerText.trim();
                const cleaned = cleanNumber(value);
                if (cleaned !== null) {
                    values.push(cleaned);
                }
            }

            // Ищем заголовки таблицы (года)
            let years = [];

            // Пробуем найти заголовки в thead
            if (targetTable) {
                const thead = targetTable.querySelector('thead');
                if (thead) {
                    const headerRows = thead.querySelectorAll('tr');
                    for (let headerRow of headerRows) {
                        const headers = headerRow.querySelectorAll('th');
                        for (let i = 1; i < headers.length; i++) {
                            let yearText = headers[i].innerText.trim();
                            const yearMatch = yearText.match(/\d{4}/);
                            if (yearMatch) {
                                years.push(yearMatch[0]);
                            }
                        }
                        if (years.length > 0) break;
                    }
                }
            }

            // Если не нашли в thead, ищем в первой строке таблицы
            if (years.length === 0 && targetTable) {
                const firstRow = targetTable.querySelector('tr:first-child');
                if (firstRow) {
                    const firstRowCells = firstRow.querySelectorAll('th, td');
                    for (let i = 1; i < firstRowCells.length; i++) {
                        let yearText = firstRowCells[i].innerText.trim();
                        const yearMatch = yearText.match(/\d{4}/);
                        if (yearMatch) {
                            years.push(yearMatch[0]);
                        }
                    }
                }
            }

            // Если всё ещё нет годов, ищем любой текст с датами вверху таблицы
            if (years.length === 0 && targetTable) {
                const allElements = targetTable.querySelectorAll('th, td, div, span');
                for (let elem of allElements) {
                    const text = elem.innerText;
                    const dateMatch = text.match(/\d{2}[/.]\d{2}[/.]\d{4}/);
                    if (dateMatch) {
                        const yearMatch = dateMatch[0].match(/\d{4}/);
                        if (yearMatch && !years.includes(yearMatch[0])) {
                            years.push(yearMatch[0]);
                        }
                    }
                }
                // Убираем дубликаты и сортируем
                years = [...new Set(years)].sort();
            }

            // Если годов меньше чем значений, возможно значения за кварталы
            // Пробуем найти информацию о периодах
            if (values.length > 0 && years.length === 0) {
                // Ищем рядом с таблицей текст с годами
                const container = targetTable.closest('div');
                if (container) {
                    const dateSpans = container.querySelectorAll('span, div');
                    for (let span of dateSpans) {
                        const text = span.innerText;
                        const yearMatch = text.match(/20\d{2}/);
                        if (yearMatch && !years.includes(yearMatch[0])) {
                            years.push(yearMatch[0]);
                        }
                    }
                }
            }

            return {
                values: values,
                years: years,
                rowText: targetRow.innerText
            };
        """)

        if 'error' in result:
            # Строки с акциями нет (например, у ETF), но instrument_id нужен для истории цен
            if instrument_id:
                print(f"⚠️ {result['error']} (цены всё равно можно получить, id={instrument_id})")
                return {
                    'instrument_id': instrument_id,
                    'diluted_shares_by_year': {},
                    'all_values': [],
                    'years_found': [],
                    'row_text': ''
                }
            print(f"❌ {result['error']}")
            return {'error': result['error']}

        # Формируем результат
        diluted_shares_by_year = {}

        if result['years'] and len(result['years']) > 0:
            # Сопоставляем года со значениями
            years_with_know_value = result['years'][-len(result['values']):]
            for i in range(min(len(years_with_know_value), len(result['values']))):
                year = years_with_know_value[i]
                value = result['values'][i]
                if value:
                    # Конвертируем в миллионы
                    try:
                        num_value = float(value)
                        diluted_shares_by_year[year] = num_value / 1000000 if num_value > 1000000 else num_value
                    except:
                        diluted_shares_by_year[year] = value
        else:
            # Если годов нет, создаём свои
            current_year = 2025
            for i, value in enumerate(result['values']):
                diluted_shares_by_year[str(current_year - i)] = value

        return {
            'instrument_id': instrument_id,
            'diluted_shares_by_year': diluted_shares_by_year,
            'all_values': result['values'],
            'years_found': result['years'],
            'row_text': result['rowText']
        }

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        return {'error': str(e)}

    finally:
        driver.quit()


def print_results(result):
    """Красивый вывод результатов"""
    if 'error' in result:
        print(f"\n❌ Ошибка: {result['error']}")
        return

    print("\n✅ Результат:")

    # Выводим instrument_id
    if result.get('instrument_id'):
        print(f"\n🆔 Instrument ID: {result['instrument_id']}")
    else:
        print("\n⚠️ Instrument ID не найден")

    print(f"\n📈 Diluted Weighted Average Shares Outstanding по годам:")
    print("-" * 50)

    if result['diluted_shares_by_year']:
        for year, value in result['diluted_shares_by_year'].items():
            # Форматируем число
            try:
                if isinstance(value, (int, float)):
                    if value >= 1000:
                        formatted_value = f"{value:,.0f}".replace(',', ' ')
                    else:
                        formatted_value = f"{value:,.2f}".replace(',', ' ')
                    print(f"  {year}: {formatted_value} млн акций")
                else:
                    print(f"  {year}: {value} млн акций")
            except:
                print(f"  {year}: {value}")
    else:
        print("  Данные не найдены")

    print(f"\n📊 Все значения (сырые данные): {result['all_values']}")
    print(f"📅 Найденные года: {result['years_found']}")

    # Показываем instrument_id в разных форматах для использования
    if result.get('instrument_id'):
        print("\n🔧 Instrument ID можно использовать в:")
        print(f"   • API запросах: pair_id={result['instrument_id']}")
        print(f"   • Прямых ссылках: https://www.investing.com/equities/sberbank_rts-{result['instrument_id']}")
        print(
            f"   • Financial Summary: https://www.investing.com/api/financialdata/{result['instrument_id']}/financial-summary")


# Запуск
if __name__ == "__main__":
    print("=" * 60)
    print("📊 Получение Diluted Weighted Average Shares за все года")
    print("=" * 60)

    # Основной метод
    result = get_data_selenium_all_years("https://www.investing.com/equities/sberbank_rts")

    # Выводим результаты
    print_results(result)