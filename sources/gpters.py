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
            time.sleep(5)

            # execute_script avoids StaleElementReferenceException from SPA re-renders
            all_hrefs = driver.execute_script(
                "return Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
            )
            seen_urls = set()
            links = []
            for href in (all_hrefs or []):
                if (
                    href
                    and "gpters.org/news/post/" in href
                    and href not in seen_urls
                ):
                    seen_urls.add(href)
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

            title_els = driver.execute_script(
                "return Array.from(document.querySelectorAll('h1')).map(e => e.innerText.trim())"
            )
            title = next((t for t in (title_els or []) if t), "")
            if not title:
                return None

            date_result = driver.execute_script("""
                var el = document.querySelector('time');
                if (el) return el.getAttribute('datetime') || el.innerText.trim();
                var els = document.querySelectorAll('[class*="date"], [class*="time"], [class*="publish"]');
                for (var i = 0; i < els.length; i++) {
                    var t = els[i].innerText.trim();
                    if (t) return t;
                }
                return null;
            """)
            published_date = ""
            if date_result:
                published_date = str(date_result).strip()[:20]

            uid = hashlib.md5(url.encode()).hexdigest()[:12]
            return {
                "id": uid,
                "title": title,
                "content": title,
                "url": url,
                "published_date": published_date,
            }
        except Exception as e:
            log.warning("지피터스 글 로드 실패 (%s): %s", url, e)
            return None
