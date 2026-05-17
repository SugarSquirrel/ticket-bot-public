"""tixcraft 自動搶票核心邏輯 — 支援 NoDriver / Playwright 雙引擎"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from typing import Callable, Awaitable

import random

import httpx

from ticket_bot.browser import BrowserEngine, PageWrapper, create_engine
from ticket_bot.captcha.solver import CaptchaSolver
from ticket_bot.config import AppConfig, EventConfig, SessionConfig
from ticket_bot.network_trace import BROWSER_TRACE_URL_PATTERN, TixcraftTraceLogger
from ticket_bot.platforms.tixcraft_parser import matches_any_keyword
from ticket_bot.proxy.manager import ProxyManager

logger = logging.getLogger(__name__)

# ── CSS 選擇器 ──────────────────────────────────────────────
SEL_GAME_ROWS = "#gameList > table > tbody > tr"
SEL_GAME_BTN = "button[data-href]"  # tixcraft 用 data-href + JS 跳轉
SEL_AREA_ZONE = ".zone"
SEL_TICKET_AGREE = "#TicketForm_agree"
SEL_TICKET_COUNT = "select[id^='TicketForm_ticketPrice_'], select.mobile-select"
SEL_TICKET_CAPTCHA = "#TicketForm_verifyCode"
SEL_TICKET_SUBMIT = "button.btn-primary"
SEL_VERIFY_ZONE = ".zone-verify"
SEL_VERIFY_INPUT = "#checkCode"
SEL_VERIFY_SUBMIT = "#submitButton"

# ── 封鎖追蹤/廣告資源，加速頁面載入 ─────────────────────────
BLOCKED_URL_PATTERNS = [
    "*google-analytics.com*",
    "*googletagmanager.com*",
    "*googlesyndication.com*",
    "*google.com/pagead*",
    "*adservice.google.com*",
    "*doubleclick.net*",
    "*facebook.com/tr*",
    "*hotjar.com*",
    "*clarity.ms*",
    "*cdn.segment.io*",
    "*cdn.segment.com*",
    "*cdn.amplitude.com*",
    "*sentry.io*",
    "*newrelic.com*",
    "*amazon-adsystem.com*",
    "*criteo.com*",
]

# ── 即將開賣偵測 ─────────────────────────────────────────────
COMING_SOON_JS = """
(() => {
    const text = document.body?.innerText || '';
    return /coming soon|即將開賣|尚未開賣|即将开卖|まもなく販売開始/i.test(text);
})()
"""


async def inject_tixcraft_sid(page: PageWrapper, sid: str) -> bool:
    """注入 TIXUISID cookie，跳過手動登入。

    先刪除舊的 TIXUISID + legacy SID cookie，再 set 新值，避免 profile
    殘留蓋過新注入（與 ticket_hunter 行為一致）。
    需先 page.goto 至 tixcraft.com，回傳是否成功。
    """
    if not sid or len(sid) < 2:
        return False
    try:
        # 1. 先清掉 profile 殘留 / server 之前 set 的舊 cookie
        for cookie_name in ("TIXUISID", "SID"):
            await page.delete_cookies(cookie_name, ".tixcraft.com")
        # 2. set 新的 SID
        await page.set_cookies([
            {
                "name": "TIXUISID",
                "value": sid,
                "domain": ".tixcraft.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }
        ])
        logger.info("已注入 TIXUISID cookie (length=%d，舊值已清除)", len(sid))
        return True
    except Exception as e:
        logger.warning("注入 TIXUISID 失敗: %s", e)
        return False


class TixcraftBot:
    """tixcraft 全自動搶票機器人"""

    def __init__(self, config: AppConfig, event: EventConfig, session: SessionConfig | None = None,
                 captcha_callback: Callable[[bytes], Awaitable[str]] | None = None):
        self.config = config
        self.event = event
        self.session = session
        self.engine: BrowserEngine = create_engine(config.browser.engine)
        self.page: PageWrapper | None = None
        self.solver = CaptchaSolver(config.captcha)
        self._session_label = session.name if session else "default"
        self._pre_solved_captcha = None
        self._captcha_callback = captcha_callback  # 雲端模式：TG 推送驗證碼
        # 持久化 HTTP 客戶端：連線復用 + HTTP/2 多工
        self._http: httpx.AsyncClient | None = None
        self._http_cookies: dict[str, str] = {}
        self._http_headers: dict[str, str] = {}
        self._failed_areas: set[str] = set()  # Sit tight 失敗的區域 URL，下次跳過
        self._trace_logger = TixcraftTraceLogger(config.trace)
        self._browser_trace_registered = False
        self._proxy_manager = ProxyManager(config.proxy)
        self._proxy_server = ""
        if self.session and self.session.proxy_server:
            self._proxy_server = self.session.proxy_server
        else:
            self._proxy_server = self._proxy_manager.next() or ""
        self._browser_proxy = self._proxy_server  # 瀏覽器啟動後 proxy 不可變
        # Gemma 4 客戶端（頁面理解 fallback）
        self._gemma = None
        if config.gemma.enabled:
            from ticket_bot.gemma_client import GemmaClient
            self._gemma = GemmaClient(config.gemma)

    async def start_browser(self) -> None:
        """啟動瀏覽器"""
        # session 設定覆蓋全域設定
        user_data_dir = self.session.user_data_dir if self.session else self.config.browser.user_data_dir

        await self.engine.launch(
            headless=self.config.browser.headless,
            user_data_dir=user_data_dir,
            executable_path=self.config.browser.executable_path,
            lang=self.config.browser.lang,
            proxy_server=self._proxy_server,
        )
        logger.info("[%s] 瀏覽器啟動完成 (engine=%s)", self._session_label, self.config.browser.engine)

    async def pre_warm(self) -> None:
        """預熱：載入活動頁面 + DNS 預解析 + HTTP 預連線"""
        url = self.event.url
        # 自動將 detail URL 轉為 game URL（省 1-2 秒）
        if "/activity/detail/" in url:
            game_name = url.rstrip("/").split("/")[-1]
            url = f"https://tixcraft.com/activity/game/{game_name}"

        # DNS 預解析（在背景做，不阻塞頁面載入）
        dns_task = asyncio.get_event_loop().run_in_executor(
            None, lambda: socket.getaddrinfo("tixcraft.com", 443)
        )

        sid = (self.session.tixcraft_sid if self.session else "") or ""
        if sid:
            # 先導到首頁建立 CDP target → 注入 cookie → 再導到活動頁
            await self._open_page("https://tixcraft.com/")
            await inject_tixcraft_sid(self.page, sid)
            await self.page.goto(url)
        else:
            await self._open_page(url)

        # 等 DNS 完成
        try:
            await dns_task
        except Exception:
            pass

        # 建立持久化 HTTP 客戶端 + 預連線（HTTP/2 多工）
        await self._init_http_client()

        logger.info("Session 預熱完成：%s (HTTP/2 已預連線)", url)

    async def _current_url(self) -> str:
        """取得目前頁面 URL"""
        return await self.page.current_url()

    async def _wait_for_navigation(self, timeout: float = 10.0) -> None:
        """等待頁面導航完成（URL 變化）"""
        old_url = await self._current_url()
        elapsed = 0.0
        while elapsed < timeout:
            await self.page.sleep(0.2)
            elapsed += 0.2
            new_url = await self._current_url()
            if new_url != old_url:
                logger.debug("導航完成: %s → %s", old_url, new_url)
                await self.page.sleep(0.15)  # 等 DOM 穩定
                return
        logger.warning("等待導航逾時 (%.1fs)，URL 仍為 %s", timeout, old_url)

    async def _wait_for_login(self, timeout: float = 120.0) -> None:
        """等待使用者在瀏覽器中完成登入，最多等 timeout 秒"""
        elapsed = 0.0
        while elapsed < timeout:
            await self.page.sleep(2.0)
            elapsed += 2.0
            url = await self._current_url()
            if "tixcraft.com" in url and "login" not in url and "facebook.com" not in url and "accounts.google.com" not in url:
                logger.info("登入成功！目前頁面: %s", url)
                return
            if int(elapsed) % 10 == 0:
                logger.info("等待登入中... (已等 %.0f 秒)", elapsed)
        logger.error("等待登入逾時 (%.0f 秒)", timeout)

    async def run(self) -> bool:
        """執行完整搶票流程，回傳是否成功進入結帳"""
        # 如果已經預熱過（countdown 模式），跳過瀏覽器啟動
        if self.page is None:
            await self.start_browser()

            if self.config.browser.pre_warm:
                await self.pre_warm()
            else:
                await self._open_page(self.event.url)
        else:
            logger.info("使用已預熱的瀏覽器，直接開始搶票")
            # 立即刷新 game 頁面以取得最新狀態
            url = await self._current_url()
            if "/activity/game/" in url:
                await self.page.goto(url)
                await self.page.sleep(0.2)

        consecutive_errors = 0
        try:
            # 依序處理每個頁面，根據當前 URL 決定動作
            for _ in range(500):  # 防止無限迴圈（含即將開賣自動刷新）
                try:
                    url = await self._current_url()
                    consecutive_errors = 0
                except Exception:
                    consecutive_errors += 1
                    if consecutive_errors >= 5:
                        logger.error("連續 %d 次 WebSocket 錯誤，重啟瀏覽器...", consecutive_errors)
                        try:
                            await self._restart_browser()
                            consecutive_errors = 0
                            logger.info("瀏覽器重啟成功，繼續搶票")
                        except Exception:
                            logger.exception("瀏覽器重啟失敗")
                            await asyncio.sleep(5.0)
                    else:
                        logger.warning("WebSocket 斷線 (%d/5)，重新連接頁面...", consecutive_errors)
                        await self.page.sleep(2.0)
                    continue

                if "/activity/game/" in url:
                    await self._select_game()
                elif "/activity/verify/" in url or "/ticket/verify/" in url:
                    await self._handle_verify()
                    await self._wait_for_navigation()
                elif "/ticket/area/" in url:
                    await self._select_area()
                    # 選完區域後檢查是否成功進入下一步
                    await self.page.sleep(0.3)
                    post_url = await self._current_url()
                    if "/ticket/area/" in post_url:
                        # 還在 area 頁面 → 可能選到售完區域被導回，重試
                        logger.warning("仍在區域頁面，可能選到售完區域，重試...")
                        continue
                elif "/ticket/order" in url or "/ticket/checkout" in url:
                    await self._handle_order()
                    # 點完 Checkout 後等頁面跳轉，確認訂單是否成立
                    await self.page.sleep(2.0)
                    try:
                        post_url = await self._current_url()
                    except Exception:
                        logger.info("搶票流程完成，請在 10 分鐘內完成付款！")
                        return True
                    logger.info("Checkout 後 URL: %s", post_url)
                    if post_url and ("/ticket/order" in post_url or "/ticket/checkout" in post_url):
                        # 還在 order/checkout 頁 → 多步驟，繼續處理
                        continue
                    elif post_url and ("/ticket/area/" in post_url or "/ticket/ticket/" in post_url or "/activity/game/" in post_url):
                        # 被踢回 area/ticket/game 頁 = 訂單失敗，記錄失敗區域，重試
                        logger.warning("訂單處理失敗，被導回 %s，重新搶票...", post_url)
                        # 記錄這次用的 ticket URL，下次跳過這個區域
                        if hasattr(self, '_last_ticket_url') and self._last_ticket_url:
                            self._failed_areas.add(self._last_ticket_url)
                            logger.info("已標記失敗區域: %s（共 %d 個）", self._last_ticket_url, len(self._failed_areas))
                        continue
                    elif post_url and "/order" in post_url and "/ticket/" not in post_url:
                        # /order（票夾頁）= 訂單成立
                        logger.info("訂單已成立！請在 10 分鐘內完成付款！")
                        return True
                    else:
                        logger.info("頁面跳轉至: %s，請確認訂單狀態", post_url)
                        return True
                elif "/ticket/ticket/" in url:
                    await self._fill_ticket_form()
                    # 等待跳轉
                    await self.page.sleep(1.0)
                    try:
                        post_url = await self._current_url()
                    except Exception:
                        logger.info("搶票流程完成，請在 10 分鐘內完成付款！")
                        return True
                    if "/ticket/ticket/" in post_url or "/ticket/area/" in post_url or "/activity/game/" in post_url:
                        logger.warning("驗證碼可能錯誤或被導回，重新嘗試...")
                        continue
                    # 可能已跳到 order 頁或其他頁，讓下一輪迴圈處理
                    continue
                elif "/activity/detail/" in url:
                    # 被導回活動詳情頁 → 重新進入場次頁
                    logger.warning("被導回活動詳情頁，重新進入場次頁...")
                    game_name = url.rstrip("/").split("/")[-1]
                    await self.page.goto(f"https://tixcraft.com/activity/game/{game_name}")
                    await self.page.sleep(0.3)
                elif "login" in url or "facebook.com" in url or "accounts.google.com" in url:
                    logger.warning("偵測到登入頁面，請在瀏覽器中手動登入 tixcraft...")
                    await self._wait_for_login()
                    await self.page.goto(self.event.url)
                    await self.page.sleep(0.3)
                else:
                    # 可能是 Cloudflare 挑戰頁
                    cf_passed = await self.page.handle_cloudflare()
                    if cf_passed:
                        logger.info("等待頁面載入... (%s)", url)
                    else:
                        # Cloudflare 未通過，嘗試 Gemma 4 理解頁面
                        advice = await self._gemma_understand_page(url)
                        if advice:
                            logger.info("Gemma 頁面理解: %s", advice)
                        else:
                            logger.warning("未知頁面: %s", url)
                    await self.page.sleep(0.5)

            logger.error("搶票流程超過最大步驟數")
            return False

        except Exception:
            logger.exception("搶票流程發生錯誤")
            return False

    # ── Gemma 4 頁面理解 ────────────────────────────────────

    async def _gemma_understand_page(self, url: str) -> str | None:
        """用 Gemma 4 理解未知頁面的狀態，回傳建議行動或 None"""
        if not self._gemma:
            return None
        try:
            if not await self._gemma.is_available():
                return None
            # 擷取頁面文字（限制長度避免 prompt 過大）
            page_text = await self.page.evaluate("""
                (() => {
                    return (document.body?.innerText || '').substring(0, 1500);
                })()
            """)
            if not page_text or len(page_text.strip()) < 10:
                return None

            result = await self._gemma.structured_chat(
                prompt=f"""分析這個購票網站的頁面狀態。
URL: {url}
頁面文字內容（前 1500 字）:
{page_text}

用 JSON 回答：
{{
    "page_type": "sold_out" | "coming_soon" | "queue" | "error" | "captcha" | "maintenance" | "cloudflare" | "unknown",
    "action": "建議的下一步動作（中文）",
    "should_wait": true/false,
    "wait_seconds": 建議等待秒數
}}""",
                system="你是購票網站頁面狀態分析器。根據頁面內容判斷頁面類型並建議行動。",
                temperature=0.1,
            )

            if result:
                page_type = result.get("page_type", "unknown")
                action = result.get("action", "")
                wait = result.get("wait_seconds", 1)
                logger.info("Gemma 頁面分析: type=%s action=%s wait=%s", page_type, action, wait)

                # 根據分析結果採取行動
                if page_type == "coming_soon":
                    logger.info("即將開賣（Gemma 判斷），%.1f 秒後刷新...", max(0.3, wait))
                    await self.page.sleep(max(0.3, min(wait, 5)))
                    await self.page.goto(url)
                elif page_type == "queue":
                    logger.info("排隊中（Gemma 判斷），等待 %.1f 秒...", max(2, wait))
                    await self.page.sleep(max(2, min(wait, 10)))
                elif page_type == "sold_out":
                    logger.warning("已售完（Gemma 判斷）: %s", action)
                elif page_type in ("error", "maintenance"):
                    logger.warning("錯誤/維護頁面（Gemma 判斷）: %s", action)
                    await self.page.sleep(max(2, min(wait, 10)))
                    await self.page.goto(self.event.url)

                return f"{page_type}: {action}"
            return None
        except Exception as e:
            logger.debug("Gemma 頁面理解失敗: %s", e)
            return None

    # ── 步驟一：場次選擇 ────────────────────────────────────

    async def _select_game_http(self, game_url: str) -> str | None:
        """用 HTTP GET 抓 game 頁 HTML，解析出 area URL（比瀏覽器載入快 5-10 倍）"""
        if not self._http:
            return None
        try:
            resp = await self._http.get(game_url)
            self._trace_httpx_response(resp, source="browser-http")
            if resp.status_code != 200:
                return None
            html = resp.text
            import re as _re
            date_kw = self.event.date_keyword
            logger.info("HTTP game 頁: status=%d, len=%d, has_gameList=%s",
                       resp.status_code, len(html), 'gameList' in html)
            # debug: 存 game HTML
            try:
                with open("game_page_debug.html", "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass
            # 找所有場次的 data-href（完整 URL 或相對路徑）
            matches = _re.findall(r'data-href="((?:https?://tixcraft\.com)?/ticket/area/[^"]+)"', html)
            if not matches:
                return None

            def _ensure_full_url(u):
                return u if u.startswith("http") else f"https://tixcraft.com{u}"

            if date_kw:
                rows = _re.findall(r'<tr[^>]*>(.*?)</tr>', html, _re.DOTALL)
                for row in rows:
                    if matches_any_keyword(row, date_kw):
                        m = _re.search(r'data-href="((?:https?://tixcraft\.com)?/ticket/area/[^"]+)"', row)
                        if m:
                            return _ensure_full_url(m.group(1))
            return _ensure_full_url(matches[0])
        except Exception as e:
            logger.debug("HTTP 抓 game 頁失敗: %s", e)
            return None

    async def _select_area_http(self, area_url: str) -> str | None:
        """用 HTTP GET 抓 area 頁 HTML，從 areaUrlList JS 變數解析 ticket URL"""
        if not self._http:
            return None
        try:
            resp = await self._http.get(area_url)
            self._trace_httpx_response(resp, source="browser-http")
            logger.info("HTTP area 頁: status=%d, len=%d", resp.status_code, len(resp.text))
            if resp.status_code != 200:
                return None
            html = resp.text
            import json as _json
            import re as _re

            # 方法1：解析 areaUrlList JS 變數（最可靠）
            # 格式: var areaUrlList = {"22198_1":"/ticket/ticket/26_della/21450/1/59", ...};
            m = _re.search(r'var\s+areaUrlList\s*=\s*(\{[^}]+\})', html)
            if m:
                try:
                    url_map = _json.loads(m.group(1))
                    if url_map:
                        area_kw = self.event.area_keyword
                        # 有關鍵字 → 找匹配的區域 ID
                        if area_kw:
                            # 找 area ID 對應的文字
                            for aid, ticket_path in url_map.items():
                                # 在 HTML 裡找這個 ID 附近的文字
                                pattern = f'id="{aid}"[^>]*>[^<]*([^<]*)'
                                txt_match = _re.search(pattern, html)
                                if txt_match and area_kw in txt_match.group(0):
                                    logger.info("HTTP areaUrlList 匹配: %s → %s", aid, ticket_path)
                                    return ticket_path if ticket_path.startswith("http") else f"https://tixcraft.com{ticket_path}"
                        # 沒關鍵字 → 第一個（排除身障票和已知失敗區域）
                        _skip_re = _re.compile(r'身心障礙|身障|輪椅|wheelchair|殘障', _re.IGNORECASE)
                        for aid, ticket_path in url_map.items():
                            full = ticket_path if ticket_path.startswith("http") else f"https://tixcraft.com{ticket_path}"
                            if full in self._failed_areas:
                                continue
                            # 檢查 area ID 附近文字是否含身障
                            ctx = _re.search(f'id="{aid}"[^>]*>([^<]{{0,80}})', html)
                            if ctx and _skip_re.search(ctx.group(1)):
                                logger.info("HTTP 跳過身障區: %s", ctx.group(1)[:30])
                                continue
                            logger.info("HTTP areaUrlList 選區: %s", full)
                            return full
                        # 全部都失敗過 → 清空重試
                        self._failed_areas.clear()
                        first_path = next(iter(url_map.values()))
                        return first_path if first_path.startswith("http") else f"https://tixcraft.com{first_path}"
                except Exception:
                    pass

            # 方法2：直接找 /ticket/ticket/ URL
            ticket_matches = _re.findall(r'(/ticket/ticket/[^"\'\\]+)', html)
            if ticket_matches:
                return f"https://tixcraft.com{ticket_matches[0]}"
            return None
        except Exception as e:
            logger.debug("HTTP 抓 area 頁失敗: %s", e)
            return None

    async def _select_game(self) -> None:
        """在 /activity/game/ 頁面選擇場次 — 優先 HTTP 快速解析，失敗才用瀏覽器"""
        logger.info("場次選擇頁面")

        # 偵測排隊 / 即將開賣（只能在瀏覽器做）
        url = await self._current_url()
        if "queue" in url.lower() or "wait" in url.lower():
            logger.warning("偵測到排隊等待室，靜待跳轉...")
            await self.page.sleep(5.0)
            return

        is_waiting = await self.page.evaluate("""
            () => {
                const text = document.body.innerText;
                return /排隊中|請稍候|Waiting Room/i.test(text);
            }
        """)
        if is_waiting:
            logger.warning("偵測到排隊等待室，靜待跳轉...")
            await self.page.sleep(5.0)
            return

        is_coming_soon = await self.page.evaluate(COMING_SOON_JS)
        if is_coming_soon:
            logger.info("偵測到「即將開賣」頁面，0.3 秒後刷新...")
            await self.page.sleep(0.3)
            await self.page.goto(url)
            await self.page.sleep(0.15)
            return

        # 快速路徑：HTTP 直接抓 game → area URL
        area_url = await self._select_game_http(url)
        if area_url:
            logger.info("HTTP 快速選場: → %s", area_url)
            # 再用 HTTP 抓 area → ticket URL
            ticket_url = await self._select_area_http(area_url)
            if ticket_url:
                logger.info("HTTP 快速選區: → %s", ticket_url)
                self._last_ticket_url = ticket_url
                await self.page.goto(ticket_url)
                return
            # area HTTP 失敗，瀏覽器載入 area 頁
            await self.page.goto(area_url)
            await self.page.sleep(0.2)
            return

        # 慢速路徑：瀏覽器 JS 解析（fallback）
        date_kw = self.event.date_keyword
        game_info = await self.page.evaluate(f"""
            (() => {{
                const rows = document.querySelectorAll('{SEL_GAME_ROWS}');
                const result = {{ available: [], sold_out: [], total: rows.length }};
                const soldOutPattern = /選購一空|已售完|sold out|暫無|no tickets|完売/i;
                for (const row of rows) {{
                    const btn = row.querySelector('button[data-href]');
                    const text = row.textContent.trim();
                    const shortText = text.substring(0, 60).replace(/\\s+/g, ' ');
                    if (!btn) {{
                        result.sold_out.push(shortText);
                        continue;
                    }}
                    result.available.push({{
                        text: shortText,
                        href: btn.getAttribute('data-href')
                    }});
                }}
                return result;
            }})()
        """)

        available = game_info.get("available", [])
        if not available:
            delay = random.uniform(2.0, 5.0)
            logger.warning("所有場次已售完或未開賣 (%d 場)，%.1f 秒後刷新...",
                           game_info.get("total", 0), delay)
            await self.page.sleep(delay)
            await self.page.goto(url)
            await self.page.sleep(random.uniform(0.5, 1.5))
            return

        target = None
        if date_kw:
            for g in available:
                if matches_any_keyword(g["text"], date_kw):
                    target = g
                    break
        if not target:
            target = available[0]

        href = target["href"]
        logger.info("選擇場次 (瀏覽器): %s → %s", target["text"][:40], href)
        if href.startswith("/"):
            href = f"https://tixcraft.com{href}"
        await self.page.goto(href)
        await self.page.sleep(0.2)

    # ── 步驟二：驗證頁面 ────────────────────────────────────

    async def _handle_verify(self) -> None:
        """處理驗證頁面（/activity/verify/ 選擇題 or /ticket/verify/ 卡號驗證）"""
        logger.info("偵測到驗證頁面")
        await self.page.sleep(0.3)

        # 嘗試 .zone-verify（activity/verify 選擇題）
        zone = await self.page.select(SEL_VERIFY_ZONE)
        if zone:
            raw_text = zone.text
            text = raw_text.replace("「", "【").replace("」", "】")
            match = re.search(r"【(.+?)】", text)
            if match:
                answer = match.group(1)
                logger.info("驗證答案：%s", answer)
            elif self.event.presale_code:
                answer = self.event.presale_code
                logger.info("使用 presale_code 作為驗證碼：%s***", answer[:3])
            else:
                logger.warning("無法從驗證頁面擷取答案，也沒有設定 presale_code：%s", raw_text)
                return
        elif self.event.presale_code:
            # /ticket/verify/ 卡號驗證頁（無 .zone-verify）
            answer = self.event.presale_code
            logger.info("卡號驗證頁，使用 presale_code：%s***", answer[:3])
        else:
            logger.warning("驗證頁面找不到 .zone-verify，也沒有設定 presale_code")
            return

        # 用 JS 填入驗證碼
        filled = await self.page.evaluate(f"""
            (() => {{
                const code = "{answer}";
                const input = document.querySelector('#checkCode')
                    || document.querySelector('input[name="checkCode"]')
                    || document.querySelector('input[type="text"]');
                if (!input) return false;
                input.focus();
                input.value = '';
                input.value = code;
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                input.dispatchEvent(new KeyboardEvent('keyup', {{ bubbles: true }}));
                return true;
            }})()
        """)

        if not filled:
            logger.warning("找不到驗證碼輸入欄位")
            return
        logger.info("已填入驗證碼")

        # tixcraft verify 表單用 jQuery AJAX 提交，
        # 會 alert() 錯誤訊息，需要先攔截 alert
        submitted = await self.page.evaluate("""
            (() => {
                // 攔截 alert 並記錄訊息
                window.__verifyAlert = null;
                const origAlert = window.alert;
                window.alert = function(msg) { window.__verifyAlert = msg; };
                // 同理攔截 confirm（AJAX 成功時用到）
                window.__verifyConfirm = null;
                const origConfirm = window.confirm;
                window.confirm = function(msg) { window.__verifyConfirm = msg; return true; };

                if (typeof jQuery !== 'undefined') {
                    jQuery('#form-ticket-verify').submit();
                    return 'jquery.submit';
                }
                const btn = document.querySelector('#form-ticket-verify .btn-primary');
                if (btn) { btn.click(); return 'btn.click'; }
                return null;
            })()
        """)
        logger.info("驗證頁送出方式：%s", submitted)

        # 等 AJAX 回應
        await self.page.sleep(2.0)

        # 檢查 alert / confirm 訊息
        verify_msg = await self.page.evaluate("""
            (() => {
                return {
                    alert: window.__verifyAlert,
                    confirm: window.__verifyConfirm,
                    url: location.href
                };
            })()
        """)
        if verify_msg:
            if verify_msg.get("alert"):
                logger.warning("驗證頁回應：%s", verify_msg["alert"])
            if verify_msg.get("confirm"):
                logger.info("驗證確認：%s", verify_msg["confirm"])

    # ── 步驟三：區域選擇 ────────────────────────────────────

    async def _select_area(self) -> None:
        """在 /ticket/area/ 頁面選擇座位區域

        偵測售完區域並跳過，全部售完時返回場次頁選下一場。
        """
        logger.info("區域選擇頁面")

        # 用 JS 掃描所有區域，找出可用的 <a> 並直接點擊
        # tixcraft 的 area <a> 沒有 href，靠 JS click handler 導航
        area_kw = self.event.area_keyword
        click_result = await self.page.evaluate(f"""
            (() => {{
                const zones = document.querySelectorAll('.zone');
                const keyword = '{area_kw}';
                let firstLink = null;
                let firstName = '';
                let available = 0;
                let soldOut = 0;

                for (const zone of zones) {{
                    // 找所有 <a>，排除 disabled 和不可見的
                    const links = zone.querySelectorAll('a');
                    for (const link of links) {{
                        if (link.classList.contains('disabled')) {{ soldOut++; continue; }}
                        // 檢查可見性（opacity > 0 且不是 display:none）
                        const style = window.getComputedStyle(link);
                        if (style.display === 'none' || style.opacity === '0') {{ soldOut++; continue; }}

                        const text = link.textContent.trim();
                        // 跳過身障/輪椅區（用 zone 層級文字判斷，因為身障標籤可能不在 <a> 上）
                        const zoneText = zone.textContent.trim();
                        if (/身心障礙|身障|輪椅|wheelchair|殘障/i.test(zoneText)) {{ soldOut++; continue; }}
                        available++;

                        // 有關鍵字 → 匹配就直接點
                        if (keyword && (text.includes(keyword) || zoneText.includes(keyword))) {{
                            if (typeof areaUrlList !== 'undefined' && areaUrlList[link.id]) {{
                                window.location.href = areaUrlList[link.id];
                            }} else {{
                                link.click();
                            }}
                            return {{ clicked: true, area: text.substring(0, 50), available, soldOut }};
                        }}
                        if (!firstLink) {{ firstLink = link; firstName = text; }}
                    }}
                }}

                // 沒關鍵字或沒匹配 → 點第一個可用
                if (firstLink) {{
                    if (typeof areaUrlList !== 'undefined' && areaUrlList[firstLink.id]) {{
                        window.location.href = areaUrlList[firstLink.id];
                    }} else {{
                        firstLink.click();
                    }}
                    return {{ clicked: true, area: firstName.substring(0, 50), available, soldOut }};
                }}
                return {{ clicked: false, available: 0, soldOut }};
            }})()
        """)

        if click_result and click_result.get("clicked"):
            logger.info("已點擊區域：%s (可用%d/售完%d)",
                        click_result.get("area", "")[:40],
                        click_result.get("available", 0),
                        click_result.get("soldOut", 0))
            if not self.config.browser.turbo_mode:
                await self.page.sleep(0.3)
        else:
            logger.warning("所有區域已售完，返回場次頁...")
            await self._go_back_to_game()

    async def _go_back_to_game(self) -> None:
        """返回場次頁面，準備選下一場"""
        url = self.event.url
        if "/activity/game/" not in url:
            # 從 area URL 推導 game URL
            url = await self._current_url()
            # /ticket/area/EVENT/ID → /activity/game/EVENT
            parts = url.split("/")
            for i, p in enumerate(parts):
                if p == "area" and i + 1 < len(parts):
                    event_slug = parts[i + 1]
                    url = f"https://tixcraft.com/activity/game/{event_slug}"
                    break
            else:
                url = self.event.url
        logger.info("返回場次頁: %s", url)
        await self.page.goto(url)
        await self.page.sleep(0.3)

    async def _handle_order(self) -> None:
        """處理訂單結帳頁面 (/ticket/order)，自動選取票/付款方式並送出

        掃描頁面所有 select/radio，用 option 文字匹配，不依賴 name/id。
        付款優先：ATM > ibon。取票優先：ibon > 超商。
        """
        logger.info("進入結帳頁面...")
        # 等頁面穩定
        await self.page.sleep(1.0)

        # 截圖存檔（debug 用）
        try:
            img = await self.page.screenshot()
            if img:
                with open("order_screenshot.png", "wb") as f:
                    f.write(img)
                logger.info("Order 頁截圖: order_screenshot.png (%d bytes)", len(img))
        except Exception as e:
            logger.warning("截圖失敗: %s", e)

        # dump 所有文字內容看有沒有表單相關文字
        page_text = await self.page.evaluate("document.body?.innerText?.substring(0, 3000) || ''")
        logger.info("Order 頁文字內容: %s", page_text[:500] if page_text else "EMPTY")

        # 等待付款選項或 Checkout 按鈕出現（不只是任意 form 元素）
        # 攔截 alert/confirm 訊息（tixcraft 可能用 alert 顯示錯誤後跳轉）
        try:
            await self.page.evaluate("""
                (() => {
                    window.__orderAlerts = [];
                    window.alert = (msg) => { window.__orderAlerts.push('alert: ' + msg); };
                    window.confirm = (msg) => { window.__orderAlerts.push('confirm: ' + msg); return true; };
                })()
            """)
        except Exception:
            pass

        # 監控 "Sit tight" 過程中頁面是否跳轉（= 訂單失敗）
        logger.info("等待 Sit tight 處理...")
        for wait in range(200):  # 最多 60 秒
            state = await self.page.evaluate("""
                (() => {
                    const text = document.body?.innerText || '';
                    const loading = /sit tight|securing your|請稍候|處理中/i.test(text);
                    // 找付款相關 radio 或 Checkout 按鈕
                    const payRadios = document.querySelectorAll('input[type="radio"][name*="payment"], input[type="radio"][name*="Payment"]').length;
                    const checkoutBtn = document.querySelector('.btn-primary.btn-lg, button.btn-primary');
                    const btnText = checkoutBtn ? checkoutBtn.textContent.toLowerCase() : '';
                    const hasCheckout = /checkout|確認|結帳/.test(btnText);
                    const allForms = document.querySelectorAll('select, input[type="radio"], input[type="checkbox"]').length;
                    // 抓取任何錯誤訊息
                    const alerts = window.__orderAlerts || [];
                    const errEl = document.querySelector('.alert, .error, .warning, [class*="error"], [class*="alert"]');
                    const errText = errEl ? errEl.textContent.trim().substring(0, 100) : '';
                    return { loading, payRadios, hasCheckout, allForms, alerts, errText };
                })()
            """)
            if state and (state.get("payRadios", 0) > 0 or state.get("hasCheckout")):
                logger.info("結帳表單已就緒 (付款選項: %d, Checkout: %s)",
                           state.get("payRadios", 0), state.get("hasCheckout"))
                break
            # 有攔截到 alert 訊息
            if state and state.get("alerts"):
                logger.warning("Order 頁 alert 訊息: %s", state["alerts"])
            if state and state.get("errText"):
                logger.warning("Order 頁錯誤元素: %s", state["errText"])
            # 檢查是否已被踢回其他頁面（= 訂單失敗）
            try:
                cur = await self._current_url()
                if cur and "/ticket/order" not in cur and "/ticket/checkout" not in cur:
                    # 頁面已跳走 = "Sit tight" 失敗，嘗試抓跳轉後的錯誤訊息
                    diag = await self.page.evaluate("""
                        (() => {
                            const alerts = window.__orderAlerts || [];
                            const errEl = document.querySelector('.alert, .error, [class*="error"], [class*="alert"]');
                            const errText = errEl ? errEl.textContent.trim().substring(0, 200) : '';
                            const mainText = (document.querySelector('.main, .content, #content, main') || document.body)
                                ?.innerText?.substring(0, 300) || '';
                            return { alerts, errText, mainText };
                        })()
                    """)
                    # 精確診斷 Sit tight 失敗原因
                    main_text = diag.get("mainText", "")
                    if "/ticket/area/" in cur:
                        reason = "座位被搶走，踢回選區頁"
                    elif cur.rstrip("/").endswith("/order"):
                        reason = "跳到票夾（訂單可能已過期或被取消）"
                    elif "Browsing Activity" in main_text:
                        reason = "IP 被封鎖 (Browsing Activity Paused)"
                    elif "sold out" in main_text.lower():
                        reason = "已售完 (sold out)"
                    elif diag.get("alerts"):
                        reason = f"alert: {diag['alerts']}"
                    elif diag.get("errText"):
                        reason = diag["errText"][:100]
                    else:
                        reason = f"未知原因，頁面: {main_text[:100]}"
                    logger.warning("Sit tight 失敗: %s (URL: %s)", reason, cur)
                    return  # 跳出 _handle_order，讓主迴圈重試
            except Exception:
                pass
            if wait % 10 == 9:
                logger.info("等待中... (loading=%s, payRadios=%d, allForms=%d)",
                           state.get("loading") if state else "?",
                           state.get("payRadios", 0) if state else 0,
                           state.get("allForms", 0) if state else 0)
            await self.page.sleep(0.3)
        else:
            logger.warning("等待結帳表單逾時 (60秒)，放棄此次結帳")
            return

        logger.info("自動選擇取票/付款方式...")

        # debug: dump 找到的表單元素
        form_debug = await self.page.evaluate("""
            (() => {
                const selects = [...document.querySelectorAll('select')].map(s => ({
                    name: s.name, id: s.id, display: getComputedStyle(s).display,
                    options: [...s.options].map(o => o.text.trim().substring(0, 30))
                }));
                const radios = [...document.querySelectorAll('input[type="radio"]')].map(r => ({
                    name: r.name, value: r.value, checked: r.checked,
                    label: (r.closest('label')?.textContent || r.parentElement?.textContent || '').trim().substring(0, 40)
                }));
                const checkboxes = [...document.querySelectorAll('input[type="checkbox"]')].map(c => ({
                    name: c.name, id: c.id, checked: c.checked
                }));
                return { selects, radios, checkboxes };
            })()
        """)
        logger.info("表單元素: %s", form_debug)

        # 全頁掃描：所有 select 用 option 文字匹配（含 display:none 的被 jQuery selectBox 隱藏的）
        result = await self.page.evaluate("""
            (() => {
                const log = [];
                const allSelects = [...document.querySelectorAll('select')];
                const allRadios = [...document.querySelectorAll('input[type="radio"]')];

                // 付款關鍵字優先序：ATM > ibon
                const payKw = ['atm', '虛擬帳號', '轉帳', '匯款', 'ibon', '超商繳費', '超商付款'];
                // 取票關鍵字優先序：ibon > 超商 > 郵寄
                const delivKw = ['ibon', '超商', '便利商店', '7-eleven', '7-11', '郵寄'];

                // === 處理所有 select ===
                for (const sel of allSelects) {
                    const options = [...sel.options].filter(o => o.value);
                    if (options.length <= 1) continue;
                    const optTexts = options.map(o => o.text.toLowerCase());

                    // 判斷這個 select 是付款還是取票
                    const looksLikePay = optTexts.some(t => /atm|信用卡|付款|繳費|匯款/.test(t));
                    const looksLikeDeliv = optTexts.some(t => /ibon|超商|郵寄|取票|現場/.test(t));

                    const keywords = looksLikePay ? payKw : (looksLikeDeliv ? delivKw : [...payKw, ...delivKw]);

                    for (const kw of keywords) {
                        const match = options.find(o => o.text.toLowerCase().includes(kw));
                        if (match && sel.value !== match.value) {
                            sel.value = match.value;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            log.push('select [' + (sel.name || sel.id) + ']: ' + match.text.trim());
                            break;
                        }
                    }
                }

                // === 處理所有 radio（按 name 分組）===
                const radioGroups = {};
                for (const r of allRadios) {
                    if (!radioGroups[r.name]) radioGroups[r.name] = [];
                    radioGroups[r.name].push(r);
                }
                for (const [name, radios] of Object.entries(radioGroups)) {
                    const labels = radios.map(r =>
                        (r.closest('label')?.textContent || r.parentElement?.textContent || '').toLowerCase()
                    );
                    const looksLikePay = labels.some(l => /atm|信用卡|付款|繳費/.test(l));
                    const keywords = looksLikePay ? payKw : delivKw;

                    let clicked = false;
                    for (const kw of keywords) {
                        for (let i = 0; i < radios.length; i++) {
                            if (labels[i].includes(kw)) {
                                radios[i].click();
                                log.push('radio [' + name + ']: ' + labels[i].trim().substring(0, 30));
                                clicked = true;
                                break;
                            }
                        }
                        if (clicked) break;
                    }
                }

                // === 勾選所有 checkbox（同意條款）===
                const cbs = document.querySelectorAll('input[type="checkbox"]');
                for (const cb of cbs) {
                    if (!cb.checked && !cb.disabled) {
                        cb.click();
                        log.push('checkbox: ' + (cb.name || cb.id));
                    }
                }

                return log;
            })()
        """)
        if result:
            for msg in result:
                logger.info("  結帳: %s", msg)
        else:
            logger.warning("結帳頁面未偵測到可操作選項")

        # 等 AJAX 更新
        await self.page.sleep(0.5)

        # debug: 列出所有可見的可點擊元素
        all_clickables = await self.page.evaluate("""
            (() => {
                const result = [];
                const els = document.querySelectorAll('button, input[type="submit"], a, [role="button"], [onclick]');
                for (const el of els) {
                    if (el.offsetParent === null && el.tagName !== 'A') continue;
                    const text = (el.textContent || el.value || '').trim();
                    if (!text || text.length > 50) continue;
                    const tag = el.tagName;
                    const href = el.href || '';
                    const cls = el.className || '';
                    if (/nav|footer|lang|country|search|login|logout|sign/i.test(text + cls + href)) continue;
                    result.push({ tag, text: text.substring(0, 30), class: cls.substring(0, 40), href: href.substring(0, 60) });
                }
                return result;
            })()
        """)
        logger.info("可點擊元素: %s", all_clickables)

        # 點擊送出（掃描所有可能的按鈕，含 a.btn-next, a.btn-primary 等）
        clicked = await self.page.evaluate("""
            (() => {
                // 1. 找所有 button 和 submit input
                const candidates = [...document.querySelectorAll('button, input[type="submit"], a.btn, a.btn-primary, a.btn-next')];
                for (const btn of candidates) {
                    const text = (btn.textContent || btn.value || '').trim().toLowerCase();
                    if (btn.disabled || btn.offsetParent === null) continue;
                    if (/checkout|結帳|確認付款|確認|送出|submit|確定|下一步|完成|next|proceed/i.test(text) && !/continue ordering|繼續購物|cancel/i.test(text)) {
                        btn.click();
                        return text.substring(0, 20);
                    }
                }
                // 2. fallback: 任何可見的 btn-primary / btn-next
                for (const sel of ['.btn-primary', '.btn-next', 'button[type="submit"]', '#submitButton']) {
                    const el = document.querySelector(sel);
                    if (el && !el.disabled && el.offsetParent !== null) {
                        el.click();
                        return 'fallback: ' + sel;
                    }
                }
                return null;
            })()
        """)
        if clicked:
            logger.info("已點擊按鈕: %s", clicked)

            # 點完 Checkout 後等頁面跳轉
            await self.page.sleep(2.0)
            try:
                next_url = await self._current_url()
            except Exception:
                return
            if next_url and "/ticket/order" in next_url:
                logger.info("Order 頁多步驟：進入下一步...")
                # 遞迴處理下一步（付款選擇等）
                await self._handle_order()
            elif next_url and "/ticket/checkout" not in next_url and "tixcraft.com" in next_url and "/order" not in next_url:
                # 跳到刷卡頁面（外部支付或 tixcraft 內部支付頁）→ 停下來讓使用者手動完成
                logger.info("💳 刷卡頁面已開啟: %s", next_url)
                logger.info("請在瀏覽器中完成刷卡付款！Bot 不再操作此頁面。")
            elif next_url and ("tixcraft.com" not in next_url):
                # 跳到外部支付閘道 → 絕對不能動
                logger.info("💳 外部刷卡頁面已開啟: %s", next_url[:100])
                logger.info("請在瀏覽器中完成刷卡付款！Bot 不再操作此頁面。")
        else:
            logger.warning("結帳頁面找不到送出按鈕，請手動完成")

    # ── 步驟四：訂票頁面 ────────────────────────────────────

    async def _fill_ticket_form(self) -> None:
        """在 /ticket/ticket/ 頁面填表

        自動完成：勾同意、選票數、填入驗證碼並送出。
        驗證碼預抓取：進頁面就開始抓，不等表單操作完。
        """
        logger.info("訂票頁面：開始全自動填表...")

        # 攔截 alert（tixcraft 可能在送出後用 alert 顯示錯誤）
        try:
            await self.page.evaluate("""
                (() => {
                    window.__ticketAlerts = [];
                    const orig = window.alert;
                    window.alert = (msg) => { window.__ticketAlerts.push(msg); };
                })()
            """)
        except Exception:
            pass

        # 等待表單 DOM 就緒（最多 5 秒）
        for _w in range(50):
            ready = await self.page.evaluate("""
                (() => {
                    const agree = document.querySelector('#TicketForm_agree');
                    const sel = document.querySelector("select[id^='TicketForm_ticketPrice_']");
                    return !!(agree || sel);
                })()
            """)
            if ready:
                break
            await self.page.sleep(0.1)
        else:
            logger.warning("等待訂票表單逾時，仍嘗試填入...")

        # 診斷：dump ticket 頁所有表單元素
        ticket_form = await self.page.evaluate("""
            (() => {
                const selects = [...document.querySelectorAll('select')].map(s => ({
                    name: s.name, id: s.id, value: s.value,
                    options: [...s.options].map(o => ({ text: o.text.trim().substring(0, 30), value: o.value }))
                }));
                const radios = [...document.querySelectorAll('input[type="radio"]')].map(r => ({
                    name: r.name, value: r.value, checked: r.checked,
                    label: (r.closest('label')?.textContent || r.parentElement?.textContent || '').trim().substring(0, 40)
                }));
                const checkboxes = [...document.querySelectorAll('input[type="checkbox"]')].map(c => ({
                    name: c.name, id: c.id, checked: c.checked
                }));
                return { selects, radios, checkboxes };
            })()
        """)
        logger.info("Ticket 頁表單元素: %s", ticket_form)

        # 1. 等驗證碼圖片 DOM 出現再抓（HTTP 直取會拿到不同的驗證碼，不能用）
        async def _wait_and_fetch():
            for _ in range(15):  # 最多等 1.5 秒
                ready = await self.page.evaluate("""
                    (() => {
                        const img = document.querySelector('#TicketForm_verifyCode-image') ||
                                    document.querySelector('img[src*="captcha"]');
                        return img && img.complete && img.naturalWidth > 0;
                    })()
                """)
                if ready:
                    return await self._fetch_captcha_image()
                await asyncio.sleep(0.1)
            return await self._fetch_captcha_image()
        captcha_prefetch = asyncio.create_task(_wait_and_fetch())

        # 2. 原子化預處理：一邊抓圖，一邊把「勾同意」和「選票數」同時做完
        count = self.event.ticket_count
        fill_result = await self.page.evaluate(f"""
            (() => {{
                const log = [];
                // 勾選同意
                const chk = document.querySelector('{SEL_TICKET_AGREE}');
                if (chk) {{
                    if (!chk.checked) chk.click();
                    log.push('agree: ✓');
                }} else {{
                    log.push('agree: NOT FOUND');
                }}

                // 選擇票數（含被 jQuery selectBox 隱藏的 display:none select）
                const selects = document.querySelectorAll("{SEL_TICKET_COUNT}");
                let filled = false;
                for (const sel of selects) {{
                    if (sel.options && sel.options.length > 0) {{
                        sel.value = '{count}';
                        sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                        log.push('count: ' + sel.id + ' → {count}');
                        filled = true;
                        break;
                    }}
                }}
                if (!filled) {{
                    // fallback: 找頁面上所有 select
                    const allSel = document.querySelectorAll('select');
                    for (const sel of allSel) {{
                        const opts = [...sel.options].map(o => o.value);
                        if (opts.includes('{count}')) {{
                            sel.value = '{count}';
                            sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                            log.push('count fallback: ' + (sel.name || sel.id) + ' → {count}');
                            filled = true;
                            break;
                        }}
                    }}
                }}
                if (!filled) log.push('count: NO SELECT FOUND (' + selects.length + ' matched)');
                return log;
            }})()
        """)
        logger.info("表單填入: %s", fill_result)

        # 3. 執行辨識並提交
        turbo = self.config.browser.turbo_mode
        mode_str = "極速模式" if turbo else "安全模式"
        logger.info("正在以 %s 辨識並送出驗證碼...", mode_str)
        await self._solve_and_fill_captcha(prefetch_task=captcha_prefetch)

        # 4. 等待頁面跳轉（驗證碼送出後），檢查 alert
        try:
            await self.page.sleep(0.5)
            alerts = await self.page.evaluate("window.__ticketAlerts || []")
            if alerts:
                logger.warning("驗證碼送出後 alert: %s", alerts)
        except Exception:
            pass

    async def _wait_for_manual_submit(self, timeout: float = 120.0) -> None:
        """等待使用者手動輸入驗證碼並送出，偵測離開 /ticket/ticket/ 頁面"""
        elapsed = 0.0
        while elapsed < timeout:
            await self.page.sleep(1.0)
            elapsed += 1.0
            try:
                url = await self.page.current_url()
            except Exception:
                # 頁面跳轉導致 WebSocket 斷線 → 可能送出成功了
                logger.info("偵測到頁面跳轉")
                return
            if "/ticket/ticket/" not in url:
                logger.info("使用者已送出，頁面跳轉至: %s", url)
                return
        logger.warning("等待手動送出逾時 (%.0f 秒)", timeout)

    async def _select_ticket_count(self) -> None:
        """選擇票數，確保選到第一個可用的下拉選單"""
        count = self.event.ticket_count
        # 使用 JS 選取所有符合條件的 select 並設置第一個非 0 的為目標票數
        await self.page.evaluate(
            f"""
            (() => {{
                const selects = document.querySelectorAll("{SEL_TICKET_COUNT}");
                let selected = false;
                for (const sel of selects) {{
                    if (sel.offsetParent !== null) {{
                        sel.value = '{count}';
                        sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                        selected = true;
                        break;
                    }}
                }}
                return selected;
            }})()
            """
        )
        logger.info("已嘗試選擇 %d 張票", count)

    async def _init_http_client(self) -> None:
        """建立/更新持久化 HTTP 客戶端，復用連線 + HTTP/2"""
        # 從瀏覽器同步 cookies（使用 get_all_cookies 取得含 HttpOnly 的完整 cookie）
        all_cookies = await self.page.get_all_cookies()
        self._http_cookies = {}
        for c in all_cookies:
            name = c.get("name", "")
            value = c.get("value", "")
            if name and value:
                self._http_cookies[name] = value
        logger.info("HTTP 客戶端 cookie 同步完成 (%d 個，含 HttpOnly)", len(self._http_cookies))

        self._http_headers = {
            "User-Agent": await self.page.evaluate("navigator.userAgent"),
            "Referer": "https://tixcraft.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }

        # 關閉舊客戶端
        if self._http:
            try:
                await self._http.aclose()
            except Exception:
                pass

        # 建立新客戶端：HTTP/2 + 連線復用（h2 沒裝則降級為 HTTP/1.1）
        try:
            self._http = httpx.AsyncClient(
                http2=True,
                cookies=self._http_cookies,
                headers=self._http_headers,
                timeout=10,
            )
        except ImportError:
            logger.info("h2 未安裝，使用 HTTP/1.1（建議 pip install httpx[http2]）")
            self._http = httpx.AsyncClient(
                cookies=self._http_cookies,
                headers=self._http_headers,
                timeout=10,
            )

        # 預連線：發一個輕量請求建立 TCP+TLS
        try:
            resp = await self._http.head("https://tixcraft.com/ticket/captcha")
            self._trace_httpx_response(resp, source="browser-http", note="prewarm")
            logger.debug("HTTP/2 預連線完成")
        except Exception:
            pass

    async def _open_page(self, url: str = "") -> None:
        """建立新頁面並掛上常用 hook。"""
        self.page = await self.engine.new_page()
        self._browser_trace_registered = False
        self._install_browser_trace()
        await self.page.block_urls(BLOCKED_URL_PATTERNS)
        if url:
            await self.page.goto(url)

    def _install_browser_trace(self) -> None:
        if not self.page or self._browser_trace_registered or not self._trace_logger.enabled:
            return
        self.page.on_response_event(BROWSER_TRACE_URL_PATTERN, self._handle_browser_trace_event)
        self._browser_trace_registered = True
        logger.info("已啟用 Tixcraft live trace: %s", self.config.trace.log_path)

    def _handle_browser_trace_event(self, payload: dict) -> None:
        self._trace_logger.trace_response(
            source="browser",
            method=str(payload.get("method", "")),
            url=str(payload.get("url", "")),
            status_code=int(payload.get("status_code", 0)),
            headers=payload.get("headers"),
            remote_ip=str(payload.get("remote_ip", "")),
            protocol=str(payload.get("protocol", "")),
        )

    def _trace_httpx_response(
        self,
        response: httpx.Response,
        *,
        source: str,
        note: str = "",
    ) -> None:
        request = getattr(response, "request", None)
        self._trace_logger.trace_response(
            source=source,
            method=getattr(request, "method", ""),
            url=str(response.url),
            status_code=response.status_code,
            headers=response.headers,
            protocol=getattr(response, "http_version", ""),
            note=note,
        )

    async def _fetch_captcha_image(self) -> bytes:
        """直接從瀏覽器 DOM 提取驗證碼圖片，確保與 Session 一致"""
        try:
            # 找到驗證碼圖片元素
            captcha_el = await self.page.select('#TicketForm_verifyCode-image')
            if not captcha_el:
                captcha_el = await self.page.select('img[src*="captcha"]')
            
            if captcha_el:
                import base64
                # 確保元素已可見
                await self.page.sleep(0.15)
                # 利用 JS 將圖片繪製到 canvas 再匯出 base64 (最穩定，避免 CDP 0 width 問題)
                b64_data = await self.page.evaluate("""
                    (() => {{
                        const img = document.querySelector('#TicketForm_verifyCode-image') || document.querySelector('img[src*="captcha"]');
                        if (!img || !img.complete || img.naturalWidth === 0) return null;
                        const canvas = document.createElement('canvas');
                        canvas.width = img.naturalWidth;
                        canvas.height = img.naturalHeight;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0);
                        return canvas.toDataURL('image/png').split(',')[1];
                    }})()
                """)
                if b64_data:
                    return base64.b64decode(b64_data)
            
            logger.warning("找不到驗證碼圖片元素或圖片未載入")
            return b""
        except Exception as e:
            logger.warning("從瀏覽器提取驗證碼失敗: %s", e)
            return b""

    async def _solve_and_fill_captcha(self, prefetch_task: asyncio.Task[bytes] | None = None) -> None:
        """取得驗證碼圖片、辨識、填入，支援自動刷新"""
        # 稍微等待確保圖片已載入
        await self.page.sleep(0.1)
        count = str(self.event.ticket_count)
        for attempt in range(3):  # 最多嘗試刷新 3 次
            if prefetch_task and attempt == 0:
                # 第一輪嘗試，使用傳入的並行任務結果
                img_bytes = await prefetch_task
            else:
                img_bytes = await self._fetch_captcha_image()
            
            if not img_bytes:
                # 若抓圖失敗，嘗試直接從 DOM 抓一次（Fallback）
                img_bytes = await self._fetch_captcha_image()
                if not img_bytes:
                    return

            text = ""
            try:
                ocr_text, confidence = self.solver.solve(img_bytes)
                if len(ocr_text) == 4 and confidence >= self.config.captcha.confidence_threshold:
                    text = ocr_text
                    logger.info("自動辨識成功：%s (confidence=%.2f)", text, confidence)
                elif attempt < 2:
                    # 辨識不佳，點擊圖片刷新
                    logger.warning("辨識結果不佳 (%s, %.2f)，刷新驗證碼...", ocr_text, confidence)
                    captcha_el = await self.page.select('#TicketForm_verifyCode-image')
                    if captcha_el:
                        await captcha_el.click()
                        await self.page.sleep(0.8) # 等待新圖片載入
                        continue
                
                # 若刷了三次還是沒好，就用最後一次的結果
                text = ocr_text
            except Exception as e:
                logger.error("辨識過程出錯: %s", e)

            if text:
                turbo = self.config.browser.turbo_mode
                if turbo:
                    # 送出前確認票數真的有設到（option 不存在時 select.value 不會變）
                    count_check = await self.page.evaluate(f"""
                        (() => {{
                            const selects = document.querySelectorAll("{SEL_TICKET_COUNT}");
                            for (const sel of selects) {{
                                if (sel.value === '{count}' && sel.value !== '0') return {{ ok: true, val: sel.value, name: sel.name }};
                            }}
                            // 檢查是否有任何 >0 的選項
                            let maxAvail = 0;
                            for (const sel of selects) {{
                                for (const opt of sel.options) {{
                                    const v = parseInt(opt.value);
                                    if (v > maxAvail) maxAvail = v;
                                }}
                            }}
                            return {{ ok: false, maxAvail, selCount: selects.length }};
                        }})()
                    """)
                    if not count_check or not count_check.get("ok"):
                        max_avail = count_check.get("maxAvail", 0) if count_check else 0
                        logger.warning("票數設定失敗！最大可選: %d（票可能已被搶走），跳過送出", max_avail)
                        return  # 不送出，回去監測

                    # 【極速模式】原子化操作：填入 + 立即送出，完全消滅間隙
                    logger.info("驗證碼極速原子化提交：%s", text)
                    await self.page.evaluate(f"""
                        (() => {{
                            const el = document.querySelector('{SEL_TICKET_CAPTCHA}');
                            if (el) {{
                                el.value = '{text}';
                                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            }}
                            // 用 button click 提交（觸發 onsubmit），不用 form.submit()（會跳過 onsubmit）
                            const btn = document.querySelector('#form-ticket-ticket .btn-primary') ||
                                        document.querySelector('button[type="submit"]');
                            if (btn) {{
                                btn.click();
                            }} else {{
                                const form = document.querySelector('#form-ticket-ticket');
                                if (form) form.requestSubmit();
                            }}
                        }})()
                    """)
                else:
                    # 【安全模式】模擬人類打字行為
                    input_el = await self.page.select(SEL_TICKET_CAPTCHA)
                    if input_el:
                        await self.page.evaluate(f"document.querySelector('{SEL_TICKET_CAPTCHA}').value = '';")
                        logger.info("正在模擬人類輸入驗證碼：%s", text)
                        await input_el.send_keys(text)
                        await self.page.sleep(random.uniform(0.1, 0.3))
                        submit_btn = await self.page.select('#form-ticket-ticket .btn-primary')
                        if submit_btn:
                            await submit_btn.click()
                break # 填完就跳出

        # ── 步驟四：訂票頁面 ────────────────────────────────────

    async def _watch_post_submit(self) -> bool:
        """驗證碼送出後的後續處理：偵測 order 頁並完成結帳"""
        await self.page.sleep(0.5)
        try:
            url = await self._current_url()
        except Exception:
            logger.info("搶票流程完成，請完成付款！")
            return True

        logger.info("驗證碼送出後 URL: %s", url)

        # 被踢回首頁/ticket 頁/area 頁/game 頁 → 失敗
        fail_patterns = ["/ticket/ticket/", "/ticket/area/", "/activity/game/", "/activity/detail/"]
        if any(p in url for p in fail_patterns):
            logger.warning("送出後被導回: %s", url)
            return False
        # 被踢回首頁（不含 /ticket/ 或 /order 路徑）
        if url.rstrip("/") == "https://tixcraft.com" or url.rstrip("/").endswith("tixcraft.com"):
            logger.warning("送出後被導回首頁: %s（可能驗證碼錯誤或 session 問題）", url)
            return False

        # 進入 order/checkout 頁 → 自動選付款方式並送出
        if "/ticket/order" in url or "/ticket/checkout" in url:
            logger.info("進入結帳頁面，自動處理付款...")
            await self._handle_order()
            await self.page.sleep(2.0)
            try:
                final = await self._current_url()
            except Exception:
                logger.info("搶票流程完成，請完成付款！")
                return True
            # Sit tight 失敗被踢回 → 回去監測
            if "/ticket/area/" in final or "/ticket/ticket/" in final or "/activity/game/" in final:
                logger.warning("Sit tight 後被踢回: %s，繼續監測...", final)
                return False
            # 還在 order 頁可能是多步驟，再處理一次
            if "/ticket/order" in final or "/ticket/checkout" in final:
                await self._handle_order()
                await self.page.sleep(2.0)
                try:
                    final2 = await self._current_url()
                except Exception:
                    logger.info("搶票流程完成，請完成付款！")
                    return True
                if "/ticket/area/" in final2 or "/ticket/ticket/" in final2:
                    logger.warning("結帳後被踢回: %s，繼續監測...", final2)
                    return False
            logger.info("搶票流程完成，請完成付款！")
            return True

        # 其他頁面（成功跳轉）
        logger.info("搶票流程完成 (URL: %s)，請完成付款！", url)
        return True

    async def watch(self, interval: float = 5.0) -> bool:
        """
        釋票監測：持續刷新區域頁面，有票時自動進入搶票流程。

        Args:
            interval: 刷新間隔（秒）
        Returns:
            是否成功搶到票
        """
        await self.start_browser()
        if self.config.browser.pre_warm:
            await self.pre_warm()

        # 先進入場次頁選對應場次，取得 area URL（未開賣時持續重試，保持瀏覽器開啟）
        area_url = await self._navigate_to_area()
        while area_url is None:
            delay = random.uniform(3.0, 6.0)
            logger.warning("無法進入區域選擇頁面，%.1f 秒後重試...", delay)
            await self.page.sleep(delay)
            try:
                url = await self._current_url()
                if "/activity/game/" not in url:
                    await self._go_back_to_game()
            except Exception:
                pass
            area_url = await self._navigate_to_area()

        # 如果 HTTP 快速路徑已經把瀏覽器帶到 ticket 頁，立即填表
        try:
            cur = await self._current_url()
            if "/ticket/ticket/" in cur:
                logger.info("已在訂票頁面，直接填表！")
                await self._fill_ticket_form()
                result = await self._watch_post_submit()
                if result:
                    return True
                logger.warning("首次填表未成功，切換到監測模式...")
        except Exception:
            pass

        logger.info("開始監測釋票... (間隔 %.1f 秒)", interval)
        round_num = 0
        consecutive_errors = 0
        MAX_CONSECUTIVE_ERRORS = 5  # 連續錯誤超過此數 → 重啟瀏覽器

        while True:
            round_num += 1
            try:
                # 刷新區域頁面
                await self.page.goto(area_url)
                await self.page.sleep(0.3)

                url = await self._current_url()
                consecutive_errors = 0  # 成功操作，重設計數

                # 檢查是否在 area 頁面
                if "/ticket/area/" not in url:
                    logger.warning("[第 %d 輪] 被導向: %s", round_num, url)
                    if "login" in url or "facebook.com" in url:
                        logger.warning("需要登入，請在瀏覽器中手動登入...")
                        await self._wait_for_login()
                    # 被導回場次頁時重新取得 area URL
                    if "/activity/game/" in url:
                        new_area = await self._navigate_to_area()
                        if new_area:
                            area_url = new_area
                    await self.page.sleep(interval)
                    continue

                # 用 JS 掃描區域：同時偵測 + 過濾身障 + 直接點擊，一次完成
                # Step 1: 偵測可用區域（不導航，只回傳資料）
                area_kw = self.event.area_keyword or ""
                scan_result = await self.page.evaluate(f"""
                    (() => {{
                        const skipRe = /身心障礙|身障|輪椅|wheelchair|殘障/i;
                        const keyword = '{area_kw}';
                        const urlList = (typeof areaUrlList !== 'undefined') ? areaUrlList : {{}};

                        const zones = document.querySelectorAll('.zone');
                        let available = 0, disabledOnly = 0;
                        let targetId = null, targetText = '', targetUrl = '';

                        for (const zone of zones) {{
                            const links = zone.querySelectorAll('a');
                            for (const link of links) {{
                                if (link.classList.contains('disabled')) continue;
                                const style = window.getComputedStyle(link);
                                if (style.display === 'none' || style.opacity === '0') continue;
                                const linkUrl = urlList[link.id] || link.href || '';
                                if (!linkUrl) continue;

                                const zoneText = zone.textContent || '';
                                const linkText = link.textContent.trim();
                                if (skipRe.test(zoneText) || skipRe.test(linkText)) {{
                                    disabledOnly++;
                                    continue;
                                }}

                                available++;
                                if (keyword && !targetId && (linkText.includes(keyword) || zoneText.includes(keyword))) {{
                                    targetId = link.id; targetText = linkText.substring(0, 60); targetUrl = linkUrl;
                                }}
                                if (!targetId) {{
                                    targetId = link.id; targetText = linkText.substring(0, 60); targetUrl = linkUrl;
                                }}
                            }}
                        }}
                        return {{ available, disabledOnly, targetId, targetText, targetUrl }};
                    }})()
                """)

                available = scan_result.get("available", 0) if scan_result else 0
                disabled_only = scan_result.get("disabledOnly", 0) if scan_result else 0
                target_url = scan_result.get("targetUrl", "") if scan_result else ""
                target_id = scan_result.get("targetId", "") if scan_result else ""

                if not available and disabled_only > 0:
                    if round_num % 10 == 1:
                        logger.info("[第 %d 輪] 只剩身障票 (%d 區)，%.0f 秒後刷新等釋票...", round_num, disabled_only, interval)
                    await self.page.sleep(interval)
                    continue

                if available and target_url:
                    logger.info("watch 選區: %s (可用%d/身障%d)",
                                scan_result.get("targetText", "")[:50], available, disabled_only)

                    # Step 2: 導航（用 try 包住，因為 window.location 會炸掉 CDP context）
                    try:
                        await self.page.evaluate(f"""
                            (() => {{
                                const url = '{target_url}';
                                if (url.startsWith('/')) {{
                                    window.location.href = url;
                                }} else {{
                                    const el = document.getElementById('{target_id}');
                                    if (el) el.click();
                                    else window.location.href = url;
                                }}
                            }})()
                        """)
                    except Exception:
                        pass  # 導航導致 context 被銷毀是正常的

                    # 等待頁面跳轉完成（用 asyncio.sleep 避免 page context 問題）
                    await asyncio.sleep(0.5)

                    try:
                        post_url = await self._current_url()
                    except Exception:
                        await asyncio.sleep(2.0)
                        post_url = await self._current_url()

                    if "/ticket/ticket/" in post_url:
                        await self._fill_ticket_form()
                        result = await self._watch_post_submit()
                        if result:
                            return True
                        logger.warning("驗證碼可能錯誤或被導回，繼續監測...")
                    else:
                        logger.warning("選區後未進入訂票頁 (%s)，繼續監測...", post_url)
                else:
                    if round_num % 10 == 1:
                        logger.info("[第 %d 輪] 尚無可用票券，持續監測中...", round_num)

            except Exception:
                consecutive_errors += 1
                if consecutive_errors <= 3:
                    logger.exception("[第 %d 輪] 監測發生錯誤，重試...", round_num)
                elif consecutive_errors == MAX_CONSECUTIVE_ERRORS:
                    logger.error("[第 %d 輪] 連續 %d 次錯誤，瀏覽器可能已崩潰，嘗試重啟...",
                                 round_num, consecutive_errors)

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    try:
                        await self._restart_browser()
                        new_area = await self._navigate_to_area()
                        if new_area:
                            area_url = new_area
                            consecutive_errors = 0
                            logger.info("瀏覽器重啟成功，繼續監測")
                        else:
                            consecutive_errors = 0
                            logger.warning("重啟成功但場次未開賣，保持瀏覽器繼續重試...")
                            await self.page.sleep(5.0)
                            # 回到場次頁重試取得 area_url
                            await self._go_back_to_game()
                    except Exception:
                        logger.exception("瀏覽器重啟失敗，等待後重試...")
                        import asyncio as _asyncio
                        await _asyncio.sleep(10.0)
                    continue

            await self.page.sleep(interval)

    async def _navigate_to_area(self) -> str | None:
        """從場次頁進入區域頁，回傳 area URL"""
        try:
            url = await self._current_url()
            if "/activity/game/" in url:
                await self._select_game()
                await self.page.sleep(0.5)
                url = await self._current_url()
            if "/ticket/area/" in url:
                return url
            # HTTP 快速路徑可能直接跳到 /ticket/ticket/，從 URL 推導 area URL
            if "/ticket/ticket/" in url:
                # /ticket/ticket/EVENT/GAME/SEAT/XX → /ticket/area/EVENT/GAME
                import re as _re
                m = _re.search(r'(/ticket/ticket/([^/]+)/(\d+))', url)
                if m:
                    area_url = f"https://tixcraft.com/ticket/area/{m.group(2)}/{m.group(3)}"
                    logger.info("從 ticket URL 推導 area URL: %s", area_url)
                    return area_url
        except Exception:
            logger.exception("導航到區域頁失敗")
        return None

    async def _restart_browser(self) -> None:
        """關閉並重新啟動瀏覽器 + 頁面"""
        logger.info("正在重啟瀏覽器...")
        try:
            await self.engine.close()
        except Exception:
            pass
        # 重新建立引擎實例（避免舊狀態殘留）
        from ticket_bot.browser import create_engine
        self.engine = create_engine(self.config.browser.engine)
        await self.start_browser()
        await self._open_page(self.event.url)
        logger.info("瀏覽器重啟完成")

    async def close(self) -> None:
        """關閉瀏覽器和 HTTP 客戶端，確保資源被清理"""
        if self._http:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        try:
            await self.engine.close()
        except Exception:
            pass
        # 額外：清理 Linux 上可能殘留的 chromium 子進程
        import platform
        if platform.system() == "Linux":
            try:
                import subprocess
                subprocess.run(["pkill", "-f", "chrome_profile_cloud.*chromium"],
                               capture_output=True, timeout=5)
            except Exception:
                pass
