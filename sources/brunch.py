"""
브런치 크롤러 - Selenium 방식 (JavaScript 렌더링 대응)
"""
import hashlib
import logging
import time
from typing import List, Dict

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
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


class BrunchSource(BaseSource):
    def fetch(self) -> List[Dict]:
        driver = None
        try:
            driver = _make_driver()
            driver.get(self.url)
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href]"))
            )
            time.sleep(2)

            links = []
            for a in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
                href = a.get_attribute("href") or ""
                if "brunch.co.kr/@" in href:
                    parts = href.rstrip("/").split("/")
                    try:
                        int(parts[-1])
                        if href not in links:
                            links.append(href)
                    except ValueError:
                        continue

            log.info(f"브런치에서 링크 {len(links)}개 발견")

            articles = []
            for url in links[:5]:
                article = self._fetch_article(driver, url)
                if article:
                    articles.append(article)
                time.sleep(1)

            return articles

        except Exception as e:
            log.error(f"브런치 크롤링 실패: {e}")
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
            for sel in ["h1.cover_title", "h1.article-head-title", "h1"]:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    title = els[0].text.strip()
                    break

            if not title:
                return None

            published_date = ""
            for sel in ["time", ".wrap_info .etc_date", ".article-sub-info time", "span.txt_date", ".article_date"]:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    dt = els[0].get_attribute("datetime") or ""
                    if dt:
                        published_date = dt[:10]
                        break
                    text = els[0].text.strip()
                    if text:
                        published_date = text
                        break

            uid = hashlib.md5(url.encode()).hexdigest()[:12]
            return {
                "id": uid,
                "title": title,
                "content": title,
                "url": url,
                "published_date": published_date,
            }
        except Exception as e:
            log.warning(f"글 로드 실패 ({url}): {e}")
            return None