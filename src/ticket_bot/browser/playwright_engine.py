"""Playwright + playwright-stealth 引擎實作"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright_stealth import Stealth

from ticket_bot.browser.base import BrowserEngine, ElementHandle, PageWrapper

logger = logging.getLogger(__name__)


class PlaywrightElement(ElementHandle):
    """Playwright 元素封裝"""

    def __init__(self, locator_or_handle, page: Page):
        self._el = locator_or_handle
        self._page = page
        self._cached_text = ""

    async def click(self) -> None:
        await self._el.click()

    async def send_keys(self, text: str) -> None:
        import random
        # 模擬人類打字延遲，避免被防爬蟲機制判定為機器人
        await self._el.type(text, delay=random.randint(50, 150))

    async def query_selector(self, selector: str) -> ElementHandle | None:
        child = await self._el.query_selector(selector)
        if child:
            return PlaywrightElement(child, self._page)
        return None

    @property
    def text(self) -> str:
        # ElementHandle 的 text 需要 async，這裡用 cached 值
        return self._cached_text

    def _set_text(self, text: str) -> None:
        self._cached_text = text


class PlaywrightPage(PageWrapper):
    """Playwright 頁面封裝"""

    def __init__(self, page: Page):
        self._page = page

    async def goto(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded")

    async def current_url(self) -> str:
        return self._page.url

    async def select(self, selector: str) -> ElementHandle | None:
        try:
            handle = await self._page.query_selector(selector)
            if handle:
                el = PlaywrightElement(handle, self._page)
                text = await handle.inner_text() if await handle.is_visible() else ""
                el._set_text(text)
                return el
        except Exception:
            pass
        return None

    async def select_all(self, selector: str) -> list[ElementHandle]:
        handles = await self._page.query_selector_all(selector)
        result = []
        for h in handles:
            el = PlaywrightElement(h, self._page)
            try:
                text = await h.inner_text()
            except Exception:
                text = ""
            el._set_text(text)
            result.append(el)
        return result

    async def evaluate(self, expression: str) -> Any:
        return await self._page.evaluate(expression)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def get_cookies_string(self) -> str:
        return await self._page.evaluate("document.cookie")

    async def get_all_cookies(self) -> list[dict]:
        cookies = await self._page.context.cookies()
        return [{"name": c["name"], "value": c["value"], "domain": c.get("domain", ""),
                 "path": c.get("path", "/"), "httpOnly": c.get("httpOnly", False),
                 "secure": c.get("secure", False)} for c in cookies]

    async def set_cookies(self, cookies: list[dict]) -> None:
        """透過 Playwright context.add_cookies 設定 cookies"""
        formatted = []
        for c in cookies:
            entry = {"name": c["name"], "value": c["value"]}
            if "url" in c:
                entry["url"] = c["url"]
            elif "domain" in c:
                entry["domain"] = c["domain"]
                entry["path"] = c.get("path", "/")
            else:
                entry["url"] = "https://tixcraft.com"
            formatted.append(entry)
        if formatted:
            await self._page.context.add_cookies(formatted)

    async def delete_cookies(self, name: str, domain: str = "") -> None:
        """透過 Playwright context.clear_cookies 刪除指定 cookie"""
        try:
            kwargs = {"name": name}
            if domain:
                kwargs["domain"] = domain
            await self._page.context.clear_cookies(**kwargs)
            logger.debug("已刪除 cookie: name=%s domain=%s", name, domain or "*")
        except Exception as e:
            logger.warning("刪除 cookie 失敗 [%s]: %s", name, e)

    async def block_urls(self, patterns: list[str]) -> None:
        """透過 Playwright route 封鎖追蹤/廣告資源"""
        try:
            substrings = [p.strip("*") for p in patterns if p.strip("*")]

            async def _abort(route):
                await route.abort()

            await self._page.route(
                lambda url: any(s in str(url) for s in substrings),
                _abort,
            )
            logger.info("已封鎖 %d 個追蹤資源 URL pattern", len(patterns))
        except Exception as e:
            logger.warning("封鎖追蹤資源失敗: %s", e)

    def on_response_callback(self, url_pattern: str, callback: callable) -> None:
        import re
        regex = re.compile(url_pattern)

        async def handle_response(response):
            if regex.search(response.url):
                try:
                    body = await response.body()
                    callback(body)
                except Exception as e:
                    logger.debug(f"Intercept response error: {e}")

        self._page.on("response", handle_response)

    def on_response_event(self, url_pattern: str, callback: callable) -> None:
        import re

        regex = re.compile(url_pattern)

        async def handle_response(response):
            if regex.search(response.url):
                try:
                    headers = await response.headers_array()
                    callback(
                        {
                            "url": response.url,
                            "status_code": response.status,
                            "method": getattr(response.request, "method", ""),
                            "headers": [
                                (item["name"], item["value"])
                                for item in headers
                            ],
                        }
                    )
                except Exception as e:
                    logger.debug("Intercept response event error: %s", e)

        self._page.on("response", handle_response)

    async def handle_cloudflare(self, timeout: float = 15.0) -> bool:
        """偵測並通過 Cloudflare Turnstile（Playwright 版）"""
        try:
            has_cf = await self._page.evaluate("""
                (() => {
                    const text = document.body?.innerText || '';
                    const hasCfText = /verify you are human|checking your browser|請確認您是真人|正在執行安全驗證|請啟用 javascript (?:and|與) cookies? 以繼續/i.test(text);
                    const hasCfFrame = !!document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                    return hasCfText || hasCfFrame;
                })()
            """)
            if not has_cf:
                return True

            logger.info("偵測到 Cloudflare 挑戰，嘗試通過...")

            # 找 CF iframe 並點擊 checkbox
            cf_frame = self._page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
            checkbox = cf_frame.locator('input[type="checkbox"], .cb-lb')
            try:
                await checkbox.click(timeout=timeout * 1000)
            except Exception:
                # Fallback: 點擊 iframe 中央
                iframe = self._page.locator('iframe[src*="challenges.cloudflare.com"]')
                if await iframe.count() > 0:
                    await iframe.click()

            # 等待通過
            import asyncio as _asyncio
            elapsed = 0.0
            while elapsed < timeout:
                await _asyncio.sleep(1.0)
                elapsed += 1.0
                still_cf = await self._page.evaluate("""
                    (() => {
                        const text = document.body?.innerText || '';
                        return /verify you are human|checking your browser|正在執行安全驗證|安全驗證/i.test(text);
                    })()
                """)
                if not still_cf:
                    logger.info("Cloudflare 挑戰已通過")
                    return True

            logger.warning("Cloudflare 等待逾時")
            return False
        except Exception as e:
            logger.warning("Cloudflare 處理失敗 (Playwright): %s", e)
            return False

    async def screenshot(self) -> bytes:
        try:
            return await self._page.screenshot()
        except Exception:
            return b""


class PlaywrightEngine(BrowserEngine):
    """Playwright 瀏覽器引擎（Chromium + stealth）"""

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._stealth = Stealth()

    @staticmethod
    def _build_proxy_config(proxy_server: str) -> dict[str, str] | None:
        if not proxy_server:
            return None

        parsed = urlsplit(proxy_server)
        if not parsed.hostname:
            return {"server": proxy_server}

        server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
        if parsed.port:
            server = f"{server}:{parsed.port}"

        config: dict[str, str] = {"server": server}
        if parsed.username:
            config["username"] = parsed.username
        if parsed.password:
            config["password"] = parsed.password
        return config

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
        self._playwright = await async_playwright().start()

        launch_args = list(extra_args or [])
        launch_args.append("--disable-blink-features=AutomationControlled")

        kwargs: dict[str, Any] = dict(
            headless=headless,
            args=launch_args,
        )
        if executable_path:
            kwargs["executable_path"] = executable_path

        proxy_config = None
        if proxy_server:
            proxy_config = self._build_proxy_config(proxy_server)

        if user_data_dir:
            # persistent context = 自帶 user profile
            context_kwargs: dict[str, Any] = dict(
                user_data_dir=user_data_dir,
                locale=lang,
                viewport={"width": 1280, "height": 800},
                **kwargs,
            )
            if proxy_config:
                context_kwargs["proxy"] = proxy_config
            self._context = await self._playwright.chromium.launch_persistent_context(
                **context_kwargs,
            )
            self._browser = None  # persistent context 沒有獨立 browser 物件
        else:
            if proxy_config:
                kwargs["proxy"] = proxy_config
            self._browser = await self._playwright.chromium.launch(**kwargs)
            self._context = await self._browser.new_context(
                locale=lang,
                viewport={"width": 1280, "height": 800},
            )

        logger.info("Playwright 瀏覽器啟動完成")

    async def new_page(self, url: str = "") -> PageWrapper:
        if not self._context:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 launch()")

        page = None
        existing_pages = list(self._context.pages)
        for existing in existing_pages:
            if existing.url in ("", "about:blank"):
                page = existing
                break
        if page is None and len(existing_pages) == 1:
            page = existing_pages[0]
        if page is None:
            page = await self._context.new_page()
        await self._stealth.apply_stealth_async(page)

        if url:
            await page.goto(url, wait_until="domcontentloaded")

        return PlaywrightPage(page)

    async def close(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Playwright 瀏覽器已關閉")
