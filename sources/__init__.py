"""
소스 베이스 클래스 - 새로운 사이트를 추가할 때 이 클래스를 상속하세요.
"""

from abc import ABC, abstractmethod
from typing import List, Dict


class BaseSource(ABC):
    """
    모든 뉴스 소스의 기반 클래스.

    새 사이트 추가 방법:
        class MySite(BaseSource):
            def fetch(self) -> List[Dict]:
                # 여기서 글 목록을 가져옵니다
                return [
                    {
                        "id": "unique_id",       # 중복 체크용 고유 ID
                        "title": "글 제목",
                        "content": "글 본문",
                        "url": "https://...",
                    }
                ]
    """

    def __init__(self, url: str, name: str, tag: str = ""):
        self.url = url
        self.name = name
        self.tag = tag

    @abstractmethod
    def fetch(self) -> List[Dict]:
        """
        최신 글 목록을 반환합니다.
        각 항목: {"id": str, "title": str, "content": str, "url": str}
        """
        ...
