from bs4 import BeautifulSoup
# Вместо requests импортируем сессию из curl_cffi
from curl_cffi import requests


def find_company_url_search_page(ticker):
    search_url = f"https://www.investing.com/search?q={ticker}"

    try:
        # Используем impersonate='chrome' для полной имитации браузера Chrome
        response = requests.get(search_url, impersonate="chrome", timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        # Ищем блок с котировками (актуальная структура страницы)
        quotes_wrapper = soup.find('div', class_='js-inner-all-results-quotes-wrapper')

        if not quotes_wrapper:
            # Fallback: ищем любые подходящие ссылки
            all_links = soup.find_all('a', href=True)
            for link in all_links:
                href = link['href']
                if '/equities/' in href or '/etfs/' in href:
                    # Проверяем наличие российского флага в родителе
                    flag = link.find('i', class_=lambda x: x and 'Russian_Federation' in x)
                    if flag and ticker.lower() in link.get_text(strip=True).lower():
                        return f"https://www.investing.com{href}"
            return None

        # Основной путь: перебираем строки с котировками
        rows = quotes_wrapper.find_all('a', class_='js-inner-all-results-quote-item')

        for row in rows:
            # Берём только те, у которых российский флаг
            flag = row.find('i', class_=lambda x: x and 'Russian_Federation' in x)
            if not flag:
                continue

            href = row.get('href')
            if href and ('/equities/' in href or '/etfs/' in href):
                # Дополнительная проверка по тикеру в тексте/ссылке
                if ticker.lower() in row.get_text(strip=True).lower() or ticker.lower() in href.lower():
                    return f"https://www.investing.com{href}"

        return None

    except Exception as e:
        print(f"Ошибка при запросе/парсинге: {e}")
        return None


if __name__ == "__main__":
    ticker = 'HYDR'
    company_url = find_company_url_search_page(ticker)

    if company_url:
        print(f"✅ Ссылка: {company_url}")
    else:
        print("❌ Не удалось найти ссылку")
