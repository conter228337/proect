import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


def get_rbc_news_via_selenium(company_name, year):
    chrome_options = Options()
    # Подключаемся к вашему уже запущенному системному браузеру
    chrome_options.add_experimental_option(
        "debuggerAddress", "120.0.0.1:9222"
    )

    # Инициализируем драйвер (он подцепит открытое окно)
    driver = webdriver.Chrome(options=chrome_options)

    all_news = []
    offset = 0
    limit = 10

    print(
        f"Начинаем сбор новостей по запросу '{company_name}' за {year} год через ваш системный браузер..."
    )

    while True:
        # Логика строгого ограничения в 10 новостей полностью сохранена
        if len(all_news) >= 10:
            all_news = all_news[:10]
            break

        date_from = f"01.01.{year}"
        date_to = f"31.12.{year}"
        search_url = f"https://rbc.ru{company_name}&dateFrom={date_from}&dateTo={date_to}&offset={offset}"

        try:
            # Вы увидите глазами в окне браузера, как открывается эта страница
            driver.get(search_url)
            time.sleep(4)  # Ждем загрузки динамического контента JavaScript

            news_blocks = driver.find_elements(By.CLASS_NAME, "search-item")

            # Если на странице больше нет блоков с новостями — прерываем цикл
            if not news_blocks:
                break

            for block in news_blocks:
                try:
                    title_element = block.find_element(
                        By.CLASS_NAME, "search-item__title"
                    )
                    link_element = block.find_element(By.TAG_NAME, "a")
                    date_element = block.find_element(
                        By.CLASS_NAME, "search-item__category"
                    )

                    title = title_element.text
                    link = link_element.get_attribute("href")
                    raw_date = date_element.text

                    # Проверяем вхождение года в текстовую дату новости
                    if str(year) in raw_date:
                        if not any(n["link"] == link for n in all_news):
                            all_news.append(
                                {
                                    "date": raw_date,
                                    "title": title,
                                    "link": link,
                                }
                            )
                except Exception:
                    continue

            offset += limit

        except Exception as e:
            print(f"Ошибка при работе браузера: {e}")
            break

    # Метод driver.quit() убран, чтобы не закрывать ваш рабочий браузер по окончании

    if not all_news:
        print(f"Новости за {year} год не получены.")
        return

    print(f"\nУспешно собрано новостей: {len(all_news)}\n")
    for i, news in enumerate(all_news, 1):
        print(f"{i}. [{news['date']}] {news['title']}")
        print(f"   Ссылка: {news['link']}")
        print("-" * 60)


# Запуск парсера
get_rbc_news_via_selenium("Яндекс", 2025)
