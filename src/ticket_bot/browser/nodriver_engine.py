"""NoDriver (undetected-chromedriver) 引擎實作"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import nodriver as uc

from ticket_bot.browser.base import BrowserEngine, ElementHandle, PageWrapper

logger = logging.getLogger(__name__)

# ── 反偵測 stealth 腳本 ──────────────────────────────────────
STEALTH_JS = """
(() => {
    // 1. 隱藏 webdriver 屬性
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. 偽造 plugins（正常瀏覽器有 plugin）
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', filename: 'internal-nacl-plugin' },
        ],
    });

    // 3. 偽造 languages
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-TW', 'zh', 'en-US', 'en'] });

    // 4. 隱藏 chrome.runtime（CDP 痕跡）
    if (window.chrome) {
        window.chrome.runtime = undefined;
    }

    // 5. 修正 permissions API（headless 特徵）
    const origQuery = window.navigator.permissions?.query?.bind(window.navigator.permissions);
    if (origQuery) {
        window.navigator.permissions.query = (params) => {
            if (params.name === 'notifications') {
                return Promise.resolve({ state: Notification.permission });
            }
            return origQuery(params);
        };
    }

    // 6. 隱藏 automation 相關屬性
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;

    // 7. WebGL vendor/renderer 正常化（避免 headless 特徵）
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter.call(this, param);
    };
})();
"""


class NodriverElement(ElementHandle):
    """NoDriver 元素封裝"""

    def __init__(self, element):
        self._el = element

    async def click(self) -> None:
        await self._el.click()

    async def send_keys(self, text: str) -> None:
        import asyncio
        # 模擬人類打字，逐字輸入並加入隨機延遲
        for char in text:
            await self._el.send_keys(char)
            await asyncio.sleep(random.uniform(0.05, 0.15))

    async def query_selector(self, selector: str) -> ElementHandle | None:
        child = await self._el.query_selector(selector)
        return NodriverElement(child) if child else None

    @property
    def text(self) -> str:
        return self._el.text or ""


class NodriverPage(PageWrapper):
    """NoDriver 頁面封裝"""

    def __init__(self, page):
        self._page = page

    async def goto(self, url: str) -> None:
        await self._page.get(url)

    async def current_url(self) -> str:
        result = await self.evaluate("window.location.href")
        return str(result)

    async def select(self, selector: str) -> ElementHandle | None:
        el = await self._page.select(selector)
        return NodriverElement(el) if el else None

    async def select_all(self, selector: str) -> list[ElementHandle]:
        elements = await self._page.select_all(selector)
        return [NodriverElement(el) for el in elements]

    async def evaluate(self, expression: str) -> Any:
        # nodriver 的 deep serialization 對複雜物件不可靠，
        # 用 JSON.stringify 在 JS 端序列化，Python 端反序列化
        # 對 void 操作（如 location.reload()）用 try-catch 避免錯誤
        wrapped = f"""
            (() => {{
                try {{
                    const __r = ({expression});
                    return JSON.stringify(__r === undefined ? null : __r);
                }} catch(e) {{
                    return null;
                }}
            }})()
        """
        try:
            raw = await self._page.evaluate(wrapped, return_by_value=True)
        except Exception:
            return None
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return raw
        # 如果 raw 還是 RemoteObject，嘗試取 .value
        val = getattr(raw, "value", raw)
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return val
        return val

    async def sleep(self, seconds: float) -> None:
        await self._page.sleep(seconds)

    async def get_cookies_string(self) -> str:
        result = await self.evaluate("document.cookie")
        return str(result)

    async def get_all_cookies(self) -> list[dict]:
        import nodriver.cdp.network as cdp_net
        cookies = await self._page.send(cdp_net.get_all_cookies())
        return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                 "httpOnly": c.http_only, "secure": c.secure} for c in cookies]

    async def delete_cookies(self, name: str, domain: str = "") -> None:
        """透過 CDP Network.deleteCookies 刪除指定 cookie"""
        import nodriver.cdp.network as cdp_net
        try:
            params = {"name": name}
            if domain:
                params["domain"] = domain
            await self._page.send(cdp_net.delete_cookies(**params))
            logger.debug("已刪除 cookie: name=%s domain=%s", name, domain or "*")
        except Exception as e:
            logger.warning("刪除 cookie 失敗 [%s]: %s", name, e)

    async def set_cookies(self, cookies: list[dict]) -> None:
        """透過 CDP Network.setCookie 設定 cookies"""
        import nodriver.cdp.network as cdp_net
        success_count = 0
        for c in cookies:
            params = {
                "name": c["name"],
                "value": c["value"],
            }
            if c.get("url"):
                params["url"] = c["url"]
            elif c.get("domain"):
                params["domain"] = c["domain"]
                params["path"] = c.get("path", "/")
                params["secure"] = c.get("secure")
                params["http_only"] = c.get("httpOnly")
            else:
                params["url"] = "https://tixcraft.com"
            try:
                await self._page.send(cdp_net.set_cookie(
                    **{k: v for k, v in params.items() if v is not None}
                ))
                success_count += 1
            except Exception as e:
                logger.warning("設定 cookie 失敗 [%s]: %s", c.get("name", "?"), e)
        if cookies:
            logger.info("已同步 %d/%d 個 cookies 至瀏覽器", success_count, len(cookies))

    async def block_urls(self, patterns: list[str]) -> None:
        """透過 CDP Network.setBlockedURLs 封鎖追蹤/廣告資源"""
        try:
            import nodriver.cdp.network as cdp_net
            await self._page.send(cdp_net.enable())
            await self._page.send(cdp_net.set_blocked_ur_ls(urls=patterns))
            logger.info("已封鎖 %d 個追蹤資源 URL pattern", len(patterns))
        except Exception as e:
            logger.warning("封鎖追蹤資源失敗: %s", e)

    def on_response_callback(self, url_pattern: str, callback: callable) -> None:
        import re
        import asyncio
        import nodriver.cdp.network as cdp_net
        regex = re.compile(url_pattern)

        async def _response_handler(event: cdp_net.ResponseReceived):
            if regex.search(event.response.url):
                try:
                    # Nodriver 獲取 body 需要另發一個 CDP 指令
                    body_info = await self._page.send(
                        cdp_net.get_response_body(request_id=event.request_id)
                    )

                    body_data = body_info[0]
                    is_base64 = body_info[1]

                    if is_base64:
                        import base64
                        data = base64.b64decode(body_data)
                    else:
                        data = body_data.encode('utf-8')

                    # 背景執行 callback 避免阻塞 CDP 事件迴圈
                    asyncio.create_task(asyncio.to_thread(callback, data))
                except Exception as e:
                    logger.debug(f"CDP intercept response error: {e}")

        self._page.add_handler(cdp_net.ResponseReceived, _response_handler)

    def on_response_event(self, url_pattern: str, callback: callable) -> None:
        import re
        import asyncio
        import nodriver.cdp.network as cdp_net

        regex = re.compile(url_pattern)

        async def _response_handler(event: cdp_net.ResponseReceived):
            if regex.search(event.response.url):
                try:
                    payload = {
                        "url": event.response.url,
                        "status_code": event.response.status,
                        "headers": event.response.headers_text or event.response.headers,
                        "remote_ip": event.response.remote_ip_address or "",
                        "protocol": event.response.protocol or "",
                    }
                    asyncio.create_task(asyncio.to_thread(callback, payload))
                except Exception as e:
                    logger.debug("CDP intercept response event error: %s", e)

        self._page.add_handler(cdp_net.ResponseReceived, _response_handler)

    async def handle_cloudflare(self, timeout: float = 15.0) -> bool:
        """偵測並通過 Cloudflare Turnstile 挑戰

        使用 NoDriver 內建的 verify_cf() — 透過模板匹配找到
        Cloudflare checkbox 位置，模擬滑鼠點擊。
        """
        try:
            # 先檢查是否有 CF 挑戰
            has_cf = await self.evaluate("""
                (() => {
                    const text = document.body?.innerText || '';
                    const hasCfText = /verify you are human|checking your browser|請確認您是真人|正在執行安全驗證|請啟用 javascript (?:and|與) cookies? 以繼續/i.test(text);
                    const hasCfFrame = !!document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                    return hasCfText || hasCfFrame;
                })()
            """)
            if not has_cf:
                return True  # 無 CF 挑戰

            logger.info("偵測到 Cloudflare 挑戰，嘗試通過...")

            # 使用 NoDriver 內建模板匹配 + 點擊
            await self._page.verify_cf()

            # 等待挑戰完成（URL 改變或 CF 元素消失）
            elapsed = 0.0
            while elapsed < timeout:
                await asyncio.sleep(1.0)
                elapsed += 1.0
                still_cf = await self.evaluate("""
                    (() => {
                        const text = document.body?.innerText || '';
                        return /verify you are human|checking your browser|請確認您是真人|正在執行安全驗證|安全驗證/i.test(text);
                    })()
                """)
                if not still_cf:
                    logger.info("Cloudflare 挑戰已通過")
                    return True

            logger.warning("Cloudflare 挑戰等待逾時 (%.0fs)", timeout)
            return False

        except Exception as e:
            logger.warning("Cloudflare 處理失敗: %s", e)
            # Fallback: 嘗試 CDP DOM 穿透找 checkbox
            return await self._cf_fallback_cdp()

    async def _cf_fallback_cdp(self) -> bool:
        """Fallback: 用 CDP DOM pierce 穿透 shadow DOM 找 Cloudflare checkbox"""
        try:
            import nodriver.cdp.dom as cdp_dom

            # 取得完整 DOM（含 shadow root 和 iframe）
            doc = await self._page.send(cdp_dom.get_document(depth=-1, pierce=True))

            # 遞迴搜尋 challenges.cloudflare.com iframe 中的 checkbox
            def find_cf_checkbox(node, depth=0):
                """遞迴搜尋 CF checkbox node"""
                if depth > 20:
                    return None
                attrs = node.attributes or []
                # 找 input[type=checkbox] 或帶有 cf-turnstile 相關屬性的元素
                attr_dict = {}
                for i in range(0, len(attrs) - 1, 2):
                    attr_dict[attrs[i]] = attrs[i + 1]

                if node.node_name.lower() == "input" and attr_dict.get("type") == "checkbox":
                    return node.node_id

                # 搜尋子節點
                children = node.children or []
                if node.shadow_roots:
                    children = list(children) + list(node.shadow_roots)
                if node.content_document:
                    children = list(children) + [node.content_document]

                for child in children:
                    result = find_cf_checkbox(child, depth + 1)
                    if result:
                        return result
                return None

            checkbox_id = find_cf_checkbox(doc)
            if checkbox_id:
                # 取得座標並點擊
                box = await self._page.send(cdp_dom.get_box_model(node_id=checkbox_id))
                if box and box.content:
                    # content quad: [x1,y1, x2,y2, x3,y3, x4,y4]
                    points = box.content
                    cx = (points[0] + points[4]) / 2
                    cy = (points[1] + points[5]) / 2
                    await self._page.mouse_click(cx, cy)
                    logger.info("CDP fallback: 已點擊 CF checkbox (%d, %d)", cx, cy)
                    await asyncio.sleep(3)
                    return True

            logger.warning("CDP fallback: 找不到 CF checkbox")
            return False
        except Exception as e:
            logger.warning("CDP fallback 失敗: %s", e)
            return False

    async def screenshot(self) -> bytes:
        """截圖回傳 PNG bytes"""
        try:
            return await self._page.get_screenshot()
        except Exception:
            return b""


class NodriverEngine(BrowserEngine):
    """NoDriver 瀏覽器引擎"""

    def __init__(self):
        self._browser: uc.Browser | None = None

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
        import platform
        browser_args = list(extra_args or [])
        browser_args.extend([
            "--disable-blink-features=AutomationControlled",
            "--disable-features=AutomationControlled",
            "--disable-hang-monitor",           # 避免「頁面無回應」彈窗
            "--disable-popup-blocking",          # 允許彈窗（搶票可能用到）
            "--disable-prompt-on-repost",        # 避免 POST 重新提交確認
            "--disable-background-networking",   # 減少背景流量特徵
            "--disable-client-side-phishing-detection",
            "--disable-default-apps",
            "--no-default-browser-check",
            "--window-size=1440,900",            # 正常桌面解析度
        ])
        # Linux 雲端環境需要額外參數
        if platform.system() == "Linux":
            browser_args.extend([
                "--no-sandbox",
                "--disable-dev-shm-usage",   # /dev/shm 太小會 crash
                "--disable-gpu",              # headless 不需要 GPU
            ])
        if proxy_server:
            browser_args.append(f"--proxy-server={proxy_server}")

        kwargs: dict[str, Any] = dict(
            headless=headless,
            browser_args=browser_args,
            lang=lang,
            no_sandbox=True,
        )
        if user_data_dir:
            kwargs["user_data_dir"] = user_data_dir
            # 清理 Chrome profile lock（上次 crash 可能殘留）
            import pathlib
            for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                lock_file = pathlib.Path(user_data_dir) / lock
                if lock_file.exists() or lock_file.is_symlink():
                    lock_file.unlink(missing_ok=True)
                    logger.debug("已清理殘留 lock: %s", lock_file)
        if executable_path:
            kwargs["browser_executable_path"] = executable_path

        try:
            self._browser = await uc.start(**kwargs)
        except FileNotFoundError:
            raise FileNotFoundError(
                "找不到 Chrome/Chromium。請安裝 Chrome 或在 config.yaml 設定 "
                "browser.executable_path 指向瀏覽器執行檔路徑。\n"
                "macOS: brew install --cask google-chrome"
            )
        except Exception as e:
            # 連線失敗時確保清理殘留的瀏覽器進程
            if self._browser:
                try:
                    self._browser.stop()
                except Exception:
                    pass
                self._browser = None
            raise RuntimeError(f"瀏覽器啟動失敗：{e}") from e
        logger.info("NoDriver 瀏覽器啟動完成")

    async def new_page(self, url: str = "") -> PageWrapper:
        if not self._browser:
            raise RuntimeError("瀏覽器尚未啟動，請先呼叫 launch()")
        page = await self._browser.get(url or "about:blank")
        # 注入反偵測腳本
        await self._inject_stealth(page)
        return NodriverPage(page)

    async def _inject_stealth(self, page) -> None:
        """注入 stealth JS，降低被偵測為機器人的機率"""
        try:
            import nodriver.cdp.page as cdp_page
            await page.send(cdp_page.add_script_to_evaluate_on_new_document(source=STEALTH_JS))
            # 也立即在當前頁面執行一次
            await page.evaluate(STEALTH_JS, return_by_value=True)
        except Exception as e:
            logger.debug("stealth 注入失敗: %s", e)

    async def close(self) -> None:
        if self._browser:
            pid = getattr(self._browser, '_process_pid', None)
            try:
                self._browser.stop()
            except Exception:
                pass
            # 確保 chromium 主進程被殺（stop 可能不夠）
            if pid:
                try:
                    import os
                    import signal
                    os.kill(pid, signal.SIGKILL)
                    logger.debug("已強制殺 chromium PID %d", pid)
                except (OSError, ProcessLookupError):
                    pass
            self._browser = None
            logger.info("NoDriver 瀏覽器已關閉")
