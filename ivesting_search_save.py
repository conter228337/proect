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

        # Далее ваш код парсинга остается без изменений...
        quotes_section = soup.find('div', {'data-test': 'quotes-list'})
        if not quotes_section:
            all_links = soup.find_all('a', href=True)
            for link in all_links:
                href = link['href']
                if '/equities/' in href or '/etfs/' in href:
                    link_text = link.get_text(strip=True).lower()
                    if ticker.lower() in link_text:
                        return f"https://www.investing.com{href}"
            return None

        rows = quotes_section.find_all('tr')
        for row in rows:
            link_tag = row.find('a', href=True)
            if link_tag and '/equities/' in link_tag['href']:
                if ticker.lower() in link_tag['href']:
                    return f"https://www.investing.com{link_tag['href']}"

    except Exception as e:
        print(f"Ошибка при запросе/парсинге: {e}")

    return None


if __name__ == "__main__":
    ticker = 'sber'
    company_url = find_company_url_search_page(ticker)
    if company_url:
        print(f"✅ Ссылка: {company_url}")
    else:
        print("❌ Не удалось найти ссылку")
