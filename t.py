import requests
from bs4 import BeautifulSoup


def find_company_url_search_page(ticker):
    """
    Ищет URL компании на Investing.com, парся страницу поиска.

    Args:
        ticker (str): Биржевой код для поиска (например, 'sber')

    Returns:
        str: Полная ссылка на страницу компании или None, если не найдено.
    """
    # Формируем URL страницы поиска
    search_url = f"https://www.investing.com/search?q={ticker}"

    # Обязательные заголовки, чтобы сайт думал, что запрос от браузера
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
    }

    try:
        # Отправляем GET-запрос
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()  # Проверяем, успешен ли запрос

        # Парсим HTML
        soup = BeautifulSoup(response.text, 'html.parser')

        # Ищем секцию "Quotes" - обычно это div с определенным классом
        # Находим первую таблицу с результатами (может быть несколько секций)
        quotes_section = soup.find('div', {'data-test': 'quotes-list'})

        if not quotes_section:
            # Альтернативный поиск: ищем любую ссылку, содержащую '/equities/'
            all_links = soup.find_all('a', href=True)
            for link in all_links:
                href = link['href']
                if '/equities/' in href or '/etfs/' in href:
                    # Проверяем, есть ли тикер в ТЕКСТЕ ссылки или в её родителе
                    link_text = link.get_text(strip=True).lower()
                    parent_text = link.parent.get_text(strip=True).lower() if link.parent else ""

                    if ticker.lower() in link_text or ticker.lower() in parent_text:
                        full_url = f"https://www.investing.com{href}"
                        return full_url
            return None

        # Ищем все строки в теле таблицы результатов
        # Обычно ссылка находится в первом теге <a> внутри строки
        rows = quotes_section.find_all('tr')
        for row in rows:
            # Ищем тег <a> внутри ячейки (обычно первый td)
            link_tag = row.find('a', href=True)
            if link_tag and '/equities/' in link_tag['href']:
                # Проверяем, что текст ссылки или ссылка содержат тикер (для надежности)
                link_text = link_tag.get_text(strip=True).lower()
                if ticker.lower() in link_text or ticker.lower() in link_tag['href']:
                    full_url = f"https://www.investing.com{link_tag['href']}"
                    return full_url

        # Если точное совпадение не найдено, берем первую попавшуюся ссылку на акцию
        first_equity_link = soup.find('a', href=lambda x: x and '/equities/' in x)
        if first_equity_link:
            return f"https://www.investing.com{first_equity_link['href']}"

    except requests.exceptions.RequestException as e:
        print(f"Ошибка сети: {e}")
    except Exception as e:
        print(f"Ошибка при парсинге: {e}")

    return None


def main():
    # Пример использования для тикера 'sber'
    ticker = 'sber'
    company_url = find_company_url_search_page(ticker)

    if company_url:
        print(f"✅ Ссылка на компанию {ticker.upper()}: {company_url}")
        # Реальный результат будет похож на: https://www.investing.com/equities/sberbank-russia
    else:
        print(f"❌ Не удалось найти ссылку для {ticker.upper()}")


if __name__ == "__main__":
    main()
