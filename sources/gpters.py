"""
지피터스 AI뉴스 크롤러 - Selenium 방식
https://www.gpters.org/news
"""
import hashlib
import logging
import time
from typing import List, Dict

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from sources import BaseSource

log = logging.getLogger(__name__)


def _make_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


class GptersSource(BaseSource):
    def fetch(self) -> List[Dict]:
        driver = None
        try:
            driver = _make_driver()
            driver.get(self.url)
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href]"))
            )
            time.sleep(2)

            links = []
            for a in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
                href = a.get_attribute("href") or ""
                if "gpters.org" in href and "/news/" in href:
                    path = href.split("gpters.org")[-1]
                    if path.count("/") >= 2 and href not in links:
                        links.append(href)

            log.info("지피터스에서 링크 %d개 발견", len(links))

            articles = []
            for url in links[:10]:
                article = self._fetch_article(driver, url)
                if article:
                    articles.append(article)
                time.sleep(1)

            return articles

        except Exception as e:
            log.error("지피터스 크롤링 실패: %s", e)
            return []
        finally:
            if driver:
                driver.quit()

    def _fetch_article(self, driver, url: str) -> Dict | None:
        try:
            driver.get(url)
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "h1"))
            )
            time.sleep(1)

            title = ""
            els = driver.find_elements(By.CSS_SELECTOR, "h1")
            if els:
                title = els[0].text.strip()
            if not title:
                return None

            content = ""
            for sel in ["article", "div.content", "div.post-content", "div.prose", "main"]:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    text = els[0].text.strip()
                    if text and text != title:
                        content = text
                        break

            uid = hashlib.md5(url.encode()).hexdigest()[:12]
            return {
                "id": uid,
                "title": title,
                "content": content,
                "url": url,
            }
        except Exception as e:
            log.warning("지피터스 글 로드 실패 (%s): %s", url, e)
            return None
