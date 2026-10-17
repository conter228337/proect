import requests
from typing import Dict, Any, List, Optional
import numpy as np

from bo_nalog_excel import extract_financials_from_bfo, get_bfo_data
from config import MIN_YEAR

# True, если при последнем вызове get_bo_nalog_data был сбой сети/сервера
# (чтобы отличать «отчётности нет» от «не смогли загрузить»)
LAST_ERROR = False


def _set_error():
    global LAST_ERROR
    LAST_ERROR = True


class BoNalogClient:
    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://bo.nalog.gov.ru"
        self._init_session()

    def _init_session(self):
        """Инициализация сессии с получением свежих кук"""
        # Заходим на главную страницу для получения базовых кук
        self.session.get(f"{self.base_url}/", timeout=10)

        # Устанавливаем стандартные заголовки
        self.session.headers.update({
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Referer": self.base_url,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin"
        })

    def search_by_inn(self, inn: str) -> Optional[Dict[str, Any]]:
        """Поиск организации по ИНН (получение ID)"""
        try:
            url = f"{self.base_url}/advanced-search/organizations/search"
            params = {"query": inn, "page": 0, "size": 20}

            response = self.session.get(url, params=params, timeout=10)

            if response.status_code == 200:
                data = response.json()
                if data.get('content') and len(data['content']) > 0:
                    return data['content'][0]
                else:
                    print(f"Организация с ИНН {inn} не найдена")
                    return None
            else:
                _set_error()
                print(f"Ошибка поиска: {response.status_code}")
                return None

        except requests.exceptions.RequestException as e:
            _set_error()
            print(f"Ошибка запроса: {e}")
            return None

    def get_organization_id(self, inn: str) -> Optional[int]:
        """Получить ID организации по ИНН"""
        org = self.search_by_inn(inn)
        return org.get('id') if org else None

    def get_detailsId(self, org_id: int, year: int) -> Optional[Dict[str, Any]]:
        """Получить данные бухгалтерской отчетности по ID организации"""
        try:
            url = f"{self.base_url}/nbo/organizations/{org_id}/bfo/"

            # Обновляем Referer для этого запроса
            self.session.headers.update({
                "Referer": f"{self.base_url}/organizations-card/{org_id}"
            })

            response = self.session.get(url, timeout=10)

            if response.status_code == 200:
                for e in response.json():
                    if e['period'] == str(year):
                        return e["typeCorrections"][0]["correction"]["id"]

            if response.status_code != 200:
                _set_error()
            print(f"Ошибка получения BFO данных: {response.status_code}")
            return None

        except requests.exceptions.RequestException as e:
            _set_error()
            print(f"Ошибка запроса BFO: {e}")
            return None

    def get_periods(self, org_id: int) -> Dict[int, Any]:
        """Все годы, за которые на bo.nalog есть отчётность: {год: detailsId}"""
        periods: Dict[int, Any] = {}
        try:
            url = f"{self.base_url}/nbo/organizations/{org_id}/bfo/"
            self.session.headers.update({
                "Referer": f"{self.base_url}/organizations-card/{org_id}"
            })
            response = self.session.get(url, timeout=10)
            if response.status_code != 200:
                _set_error()
                print(f"Ошибка получения списка периодов BFO: {response.status_code}")
                return periods

            for e in response.json():
                try:
                    year = int(e['period'])
                    periods[year] = e["typeCorrections"][0]["correction"]["id"]
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
        except requests.exceptions.RequestException as e:
            _set_error()
            print(f"Ошибка запроса списка периодов BFO: {e}")
        return periods

    def get_full_organization_data(self, inn: str, year) -> Optional[Dict[str, Any]]:
        """Получить полные данные об организации (реквизиты + отчетность)"""
        # 1. Получаем основную информацию и ID
        org_info = self.search_by_inn(inn)
        if not org_info:
            return None

        org_id = org_info.get('id')
        if not org_id:
            print("ID организации не найден")
            return None

        # 2. Получаем данные бухгалтерской отчетности
        detailsId = self.get_detailsId(org_id, year)
        bfo_data = get_bfo_data(org_id, year, detailsId)

        # 3. Объединяем данные
        result = {
            'id': org_id,
            'inn': org_info.get('inn', '').replace('<strong>', '').replace('</strong>', ''),
            'short_name': org_info.get('shortName'),
            'ogrn': org_info.get('ogrn'),
            'status': org_info.get('statusCode'),
            'status_date': org_info.get('statusDate'),
            'okved': org_info.get('okved2'),
            'address': self._format_address(org_info),
            'bfo': bfo_data  # Данные отчетности
        }

        return result

    def _format_address(self, org: Dict) -> str:
        """Форматирование адреса из полей"""
        parts = []

        if org.get('region'):
            parts.append(org.get('region'))
        if org.get('street'):
            parts.append(f"ул. {org.get('street')}")
        if org.get('house'):
            parts.append(f"д. {org.get('house')}")
        if org.get('building'):
            parts.append(f"корп. {org.get('building')}")
        if org.get('office'):
            parts.append(f"оф. {org.get('office')}")

        # Если нет деталей адреса, пробуем взять полный адрес
        if not parts and org.get('index'):
            parts.append(f"индекс {org.get('index')}")

        return ", ".join(parts) if parts else "Адрес не указан"


# def extract_financials_from_bfo(bfo_data: List[Dict[str, Any]], period: Optional[str] = None) -> Dict[str, Any]:
#     """
#     Извлекает финансовые показатели из данных BFO
#
#     Args:
#         bfo_data: Список отчетных периодов от API bo.nalog.gov.ru
#         period: Год периода (например '2024'). Если None - берет последний доступный
#
#     Returns:
#         Словарь с финансовыми показателями
#     """
#     financials = {
#         'total_assets': np.nan,  # Итого активы (строка 1600)
#         'total_liabilities': np.nan,  # Итого обязательства (строка 1400 + 1500)
#         'equity': np.nan,  # Собственный капитал (строка 1300)
#         'revenue': np.nan,  # Выручка (строка 2110)
#         'net_income': np.nan,  # Чистая прибыль (строка 2400)
#         'ebitda': np.nan,  # EBITDA (приблизительный расчёт)
#         'current_assets': np.nan,  # Оборотные активы (строка 1200)
#         'current_liabilities': np.nan,  # Краткосрочные обязательства (строка 1500)
#         'cash': np.nan,  # Денежные средства (строка 1250)
#         'period': None,  # Год отчета
#         'actual_date': None,  # Дата актуальности
#         'has_audit': False,  # Наличие аудиторского заключения
#     }
#
#     if not bfo_data:
#         print("Нет данных BFO")
#         return financials
#
#     # Находим нужный период
#     selected_report = None
#     for report in bfo_data:
#         if period and report.get('period') == period:
#             selected_report = report
#             break
#
#     # Если период не указан или не найден - берем последний (первый в списке)
#     if not selected_report:
#         selected_report = bfo_data[0]  # API возвращает от новейших к старым
#
#     if not selected_report:
#         return financials
#
#     # Извлекаем основные поля
#     financials['period'] = selected_report.get('period')
#     financials['actual_date'] = selected_report.get('actualBfoDate')
#     financials['has_audit'] = selected_report.get('hasAz', False)
#
#     # Получаем данные из typeCorrections
#     type_corrections = selected_report.get('typeCorrections', [])
#     if not type_corrections:
#         print("Нет данных typeCorrections")
#         return financials
#
#     correction = type_corrections[0].get('correction', {})
#
#     # 1. Данные из баланса (balance)
#     balance = correction.get('balance', {})
#     if balance:
#         # Строка 1600 - Итого активы
#         if balance.get('current1600') is not None:
#             financials['total_assets'] = float(balance['current1600'])
#
#         # Строка 1200 - Оборотные активы
#         if balance.get('current1200') is not None:
#             financials['current_assets'] = float(balance['current1200'])
#
#         # Строка 1250 - Денежные средства
#         if balance.get('current1250') is not None:
#             financials['cash'] = float(balance['current1250'])
#
#         # Строка 1300 - Собственный капитал
#         if balance.get('current1300') is not None:
#             financials['equity'] = float(balance['current1300'])
#
#         # Строка 1500 - Краткосрочные обязательства
#         if balance.get('current1500') is not None:
#             financials['current_liabilities'] = float(balance['current1500'])
#
#         # Строка 1400 - Долгосрочные обязательства
#         long_term_liabilities = balance.get('current1400', 0)
#         if long_term_liabilities is None:
#             long_term_liabilities = 0
#
#         # Итого обязательства = Долгосрочные + Краткосрочные
#         financials['total_liabilities'] = long_term_liabilities + financials['current_liabilities']
#
#         # Если total_liabilities все еще NaN, пробуем через 1700 (пассив)
#         if np.isnan(financials['total_liabilities']) and balance.get('current1700') is not None:
#             financials['total_liabilities'] = float(balance['current1700'])
#
#     # 2. Данные из отчета о финансовых результатах (financialResult)
#     financial_result = correction.get('financialResult', {})
#     if financial_result:
#         # Строка 2110 - Выручка
#         if financial_result.get('current2110') is not None:
#             financials['revenue'] = float(financial_result['current2110'])
#         # В некоторых версиях API выручка может быть в другом месте
#         elif financial_result.get('current2100') is not None:
#             financials['revenue'] = float(financial_result['current2100'])
#
#         # Строка 2400 - Чистая прибыль
#         if financial_result.get('current2400') is not None:
#             financials['net_income'] = float(financial_result['current2400'])
#
#     # 3. Расчет EBITDA (приблизительно)
#     # EBITDA ≈ Чистая прибыль + Проценты + Налоги + Амортизация
#     # В данных есть ограниченный набор полей, делаем упрощенный расчет
#     if not np.isnan(financials['net_income']):
#         # Пробуем получить проценты к уплате (строка 2330)
#         interest = financial_result.get('current2330', 0)
#         if interest is None:
#             interest = 0
#
#         # Пробуем получить текущий налог на прибыль (строка 2410)
#         tax = financial_result.get('current2410', 0)
#         if tax is None:
#             tax = 0
#
#         # Амортизация может быть в составе расходов, но отдельно не выделена
#         # В балансе можно посмотреть изменение ОС (строка 1150), но это неточно
#         amortization = 0
#         if balance:
#             # Изменение основных средств как очень грубая оценка амортизации
#             current_os = balance.get('current1150', 0) or 0
#             previous_os = balance.get('previous1150', 0) or 0
#             if current_os < previous_os:
#                 amortization = previous_os - current_os
#
#         financials['ebitda'] = (financials['net_income'] + interest + tax + amortization)
#
#         # Если EBITDA все еще 0 или отрицательная, а чистая прибыль положительная
#         if financials['ebitda'] <= 0 and financials['net_income'] > 0:
#             financials['ebitda'] = financials['net_income'] * 1.3  # Примерная оценка
#
#     return financials


def get_financials_by_year(bfo_data: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Получить финансовые показатели по всем доступным годам

    Returns:
        Словарь {год: financials}
    """
    result = {}
    for report in bfo_data:
        period = report.get('period')
        if period:
            result[period] = extract_financials_from_bfo([report], period)
    return result


# Пример использования с вашими данными
# def by_year(bfo_response):
    # Ваши данные из ответа API

    # Извлекаем показатели для последнего периода (2025)
    # financials = extract_financials_from_bfo(bfo_response)

    # print("=" * 60)
    # print(f"ФИНАНСОВЫЕ ПОКАЗАТЕЛИ ЗА {financials['period']} ГОД")
    # print("=" * 60)
    # print(f"Итого активы (стр.1600):     {financials['total_assets']:,.2f} тыс. руб.")
    # print(f"Оборотные активы (стр.1200): {financials['current_assets']:,.2f} тыс. руб.")
    # print(f"Денежные средства (стр.1250):{financials['cash']:,.2f} тыс. руб.")
    # print(f"Собственный капитал (1300):   {financials['equity']:,.2f} тыс. руб.")
    # print(f"Краткосрочные обяз-ва (1500): {financials['current_liabilities']:,.2f} тыс. руб.")
    # print(f"Итого обязательства:          {financials['total_liabilities']:,.2f} тыс. руб.")
    # print(f"Выручка (стр.2110):           {financials['revenue']:,.2f} тыс. руб.")
    # print(f"Чистая прибыль (стр.2400):    {financials['net_income']:,.2f} тыс. руб.")
    # print(f"EBITDA (оценка):              {financials['ebitda']:,.2f} тыс. руб.")
    # print(f"\nДата актуальности: {financials['actual_date']}")
    # print(f"Аудиторское заключение: {'Да' if financials['has_audit'] else 'Нет'}")

    # Если нужно получить данные за все годы
    # print("\n" + "=" * 60)
    # print("ДАННЫЕ ПО ВСЕМ ГОДАМ")
    # print("=" * 60)

    # all_years = get_financials_by_year(bfo_response)
    # for year, data in sorted(all_years.items(), reverse=True):
    #     print(
    #         f"\n{year}: Активы={data['total_assets']:,.0f} | Выручка={data['revenue']:,.0f} | ЧП={data['net_income']:,.0f}")
    # return financials


def get_bo_nalog_data_all_years(inn, years=None):
    """Отчётность по ВСЕМ годам, доступным на bo.nalog, за одно обращение к сайту.

    years: необязательный набор годов, чтобы скачать только их (None = все доступные).
    Возвращает {год: financials}. Год, для которого файл не скачался, в словарь не попадает.
    Если организация не найдена или список периодов получить не удалось - пустой словарь
    (при сбое сети LAST_ERROR = True)."""
    global LAST_ERROR
    LAST_ERROR = False
    client = BoNalogClient()

    org_info = client.search_by_inn(inn)
    if not org_info or not org_info.get('id'):
        return {}
    org_id = org_info['id']

    periods = client.get_periods(org_id)
    result = {}
    for year in sorted(periods):
        if year < MIN_YEAR:
            continue
        if years is not None and year not in years:
            continue
        bfo_data = get_bfo_data(org_id, year, periods[year])
        if bfo_data:
            result[year] = extract_financials_from_bfo(bfo_data, year)
    return result


# Использование
def get_bo_nalog_data(inn, year):
    global LAST_ERROR
    LAST_ERROR = False
    # Создаем клиент
    client = BoNalogClient()

    # print(f"Поиск организации с ИНН {inn}...")

    # Получаем полные данные
    data = client.get_full_organization_data(inn, year)

    if data:
        # print("\n" + "=" * 50)
        # print("ОСНОВНАЯ ИНФОРМАЦИЯ")
        # print("=" * 50)
        # print(f"ID: {data['id']}")
        # print(f"ИНН: {data['inn']}")
        # print(f"Название: {data['short_name']}")
        # print(f"ОГРН: {data['ogrn']}")
        # print(f"Статус: {data['status']}")
        # print(f"Дата статуса: {data['status_date']}")
        # print(f"ОКВЭД: {data['okved']}")
        # print(f"Адрес: {data['address']}")

        if data['bfo']:
            # print("\n" + "=" * 50)
            # print("ДАННЫЕ БУХГАЛТЕРСКОЙ ОТЧЕТНОСТИ (BFO)")
            # print("=" * 50)
            # print(data['bfo'])
            return extract_financials_from_bfo(data['bfo'])
        else:
            print("\nДанные отчетности не найдены")
    else:
        print(f"Не удалось получить данные для ИНН {inn}")