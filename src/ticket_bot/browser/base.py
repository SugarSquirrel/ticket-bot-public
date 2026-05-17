"""瀏覽器引擎抽象介面"""

from __future__ import annotations

import abc
from typing import Any


class ElementHandle(abc.ABC):
    """頁面元素的抽象封裝"""

    @abc.abstractmethod
    async def click(self) -> None: ...

    @abc.abstractmethod
    async def send_keys(self, text: str) -> None: ...

    @abc.abstractmethod
    async def query_selector(self, selector: str) -> ElementHandle | None: ...

    @property
    @abc.abstractmethod
    def text(self) -> str: ...


class PageWrapper(abc.ABC):
    """頁面操作的抽象封裝"""

    @abc.abstractmethod
    async def goto(self, url: str) -> None: ...

    @abc.abstractmethod
    async def current_url(self) -> str: ...

    @abc.abstractmethod
    async def select(self, selector: str) -> ElementHandle | None:
        """選擇單一元素，找不到回傳 None"""

    @abc.abstractmethod
    async def select_all(self, selector: str) -> list[ElementHandle]:
        """選擇所有匹配元素"""

    @abc.abstractmethod
    async def evaluate(self, expression: str) -> Any:
        """執行 JavaScript"""

    @abc.abstractmethod
    async def sleep(self, seconds: float) -> None: ...

    @abc.abstractmethod
    async def get_cookies_string(self) -> str:
        """取得 document.cookie 字串"""

    @abc.abstractmethod
    async def get_all_cookies(self) -> list[dict]:
        """取得所有 cookies（含 HttpOnly），回傳 list of dict"""

    async def block_urls(self, patterns: list[str]) -> None:
        """封鎖指定 URL pattern 的資源載入（預設不做任何事）"""

    def on_response_callback(self, url_pattern: str, callback: callable) -> None:
        """
        註冊網路回應攔截器 (Network Interceptor)。
        當請求網址符合 url_pattern 時，將回應內容 (bytes) 傳給 callback。
        """
        pass

    def on_response_event(self, url_pattern: str, callback: callable) -> None:
        """
        註冊網路回應事件攔截器。
        callback 會收到 dict，包含 url/status_code/headers 等摘要資訊。
        """
        pass

    async def handle_cloudflare(self, timeout: float = 15.0) -> bool:
        """偵測並處理 Cloudflare Turnstile 挑戰，回傳是否成功（預設不做任何事）"""
        return True

    async def set_cookies(self, cookies: list[dict]) -> None:
        """設定 cookies（用於 API→瀏覽器同步）。每個 dict 需有 name, value, url 或 domain"""

    async def delete_cookies(self, name: str, domain: str = "") -> None:
        """刪除指定 cookie（避免舊值蓋過新注入值）。預設不做任何事，由具體 engine 實作"""

    async def screenshot(self) -> bytes:
        """截圖，回傳 PNG bytes（預設空）"""
        return b""


class BrowserEngine(abc.ABC):
    """瀏覽器引擎抽象介面"""

    @abc.abstractmethod
    async def launch(
        self,
        *,
        headless: bool = False,
        user_data_dir: str = "",
        executable_path: str = "",
        lang: str = "zh-TW",
        proxy_server: str = "",
        extra_args: list[str] | None = None,
    ) -> None:
        """啟動瀏覽器"""

    @abc.abstractmethod
    async def new_page(self, url: str = "") -> PageWrapper:
        """開啟新頁面（可選直接導航到 url）"""

    @abc.abstractmethod
    async def close(self) -> None:
        """關閉瀏覽器"""
