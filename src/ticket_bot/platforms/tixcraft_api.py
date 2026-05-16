"""tixcraft API 級別搶票模組 — 瀏覽器僅用於登入，其餘全 httpx 直送"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Callable, Awaitable
from urllib.parse import urlsplit

from curl_cffi import requests as curl_requests

from ticket_bot.platforms.tixcraft import TixcraftBot
from ticket_bot.platforms.tixcraft_parser import (
    parse_game_list,
    parse_verify_page,
    parse_area_list,
    detect_coming_soon,
    detect_login_required,
    matches_any_keyword,
    split_match_keywords,
)
from ticket_bot.proxy.manager import ProxyManager

logger = logging.getLogger(__name__)

BASE = "https://tixcraft.com"
DEFAULT_API_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_SESSION_CHECK_INTERVAL = 30.0
MIN_MULTI_TARGET_GAP = 0.5
FORBIDDEN_SESSION_REBUILD_STREAK = 3
FORBIDDEN_PROXY_ROTATE_STREAK = 5
FORBIDDEN_BROWSER_REFRESH_STREAK = 7
MAX_FORBIDDEN_BACKOFF = 8.0
BLOCKED_STATUS_CODES = (401, 403)
SESSION_FAILOVER_BLOCK_STREAK = 20
SESSION_FAILOVER_PROXY_ERROR_STREAK = 3


class LoginExpiredError(RuntimeError):
    """cookie 過期且無法自動恢復時拋出，讓上層（TG bot）通知使用者"""
    pass


class SessionFailoverRequiredError(RuntimeError):
    """目前 session 長時間被擋或 proxy 異常，需切換到下一組 session。"""
    pass


class TixcraftApiBot(TixcraftBot):
    """
    繼承 TixcraftBot，以 curl_cffi 取代瀏覽器 DOM 操作。
    瀏覽器僅用於：登入、Cloudflare 挑戰、驗證碼圖片展示（可選）。

    api_mode:
      - "checkout": 只有最後結帳用 API（原有行為）
      - "full": 全流程 API（場次→驗證→區域→結帳）
    """

    def __init__(self, *args,
                 captcha_callback: Callable[[bytes], Awaitable[str]] | None = None,
                 notify_callback: Callable[[str], Awaitable[None]] | None = None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self._http: curl_requests.AsyncSession | None = None
        self._captcha_callback = captcha_callback  # 雲端模式：TG 推送驗證碼
        self._notify_callback = notify_callback    # 雲端模式：TG 推送狀態
        self.last_error: str = ""  # 最近一次失敗原因，供 TG bot 讀取
        self.last_success_info: str = ""  # 搶票成功時的票券資訊（區域、價格等）
        self.last_submit_timing: dict[str, float | str] = {}
        self._last_session_check_at = 0.0
        self._session_check_interval = DEFAULT_SESSION_CHECK_INTERVAL
        self._game_block_streak = 0
        self._proxy_error_streak = 0
        self._session_failover_enabled = False
        self._session_failover_block_streak = SESSION_FAILOVER_BLOCK_STREAK
        self._session_failover_proxy_error_streak = SESSION_FAILOVER_PROXY_ERROR_STREAK
        # ── watch 統計 ──
        self._watch_stats: dict[str, dict] = {}  # key=interval, value={ok, blocked, total, latencies}
        self._watch_stats_interval: float = 0.0  # 目前使用的 interval

    # ── HTTP client 管理 ──────────────────────────────────────

    async def _init_http(self) -> None:
        """從瀏覽器提取所有 cookie（含 HttpOnly），建立 curl_cffi session"""
        if self._http:
            try:
                if hasattr(self._http, "aclose"):
                    await self._http.aclose()
                elif hasattr(self._http, "close"):
                    close_result = self._http.close()
                    if asyncio.iscoroutine(close_result):
                        await close_result
            except Exception:
                logger.debug("關閉既有 HTTP client 失敗", exc_info=True)
            finally:
                self._http = None

        all_cookies = await self.page.get_all_cookies()
        browser_jar = {}
        for c in all_cookies:
            if c.get("value"):  # 跳過空值（加密解不開的）
                browser_jar[c["name"]] = c["value"]

        # 永遠用匯出的 JSON 補齊缺少的 cookie，但保留瀏覽器已持有的最新值。
        jar = self._load_cookies_from_json(
            browser_jar,
            cookie_file=self._resolve_cookie_file(),
        )
        missing_browser_cookies = [
            {"name": k, "value": v, "url": BASE}
            for k, v in jar.items()
            if k not in browser_jar
        ]
        if missing_browser_cookies:
            await self.page.set_cookies(missing_browser_cookies)
        await self._refresh_browser_frontend_after_cookie_sync(
            force_reload=bool(missing_browser_cookies),
        )

        ua = await self._resolve_browser_user_agent()
        session_kwargs = {
            "impersonate": "chrome124",  # 指定模擬的 Chrome 版本
            "cookies": jar,
            "headers": {
                "User-Agent": str(ua),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": BASE,
                "Connection": "keep-alive",
                "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
            },
            "allow_redirects": False,
            "timeout": 15,
        }
        if self._proxy_server:
            session_kwargs["proxy"] = self._proxy_server
            logger.info("API session 使用 proxy: %s", self._proxy_server)
        # 使用 curl_cffi 的 impersonate 功能偽裝成真實 Chrome
        self._http = curl_requests.AsyncSession(**session_kwargs)
        logger.info("API curl_cffi session 初始化完成 (cookies: %d, impersonate=chrome124)", len(all_cookies))

    async def _refresh_browser_frontend_after_cookie_sync(self, *, force_reload: bool = False) -> None:
        """補齊 cookie 後，將前台從第三方登入頁拉回 tixcraft。"""
        if not self.page:
            return

        try:
            current_url = await self._current_url()
        except Exception:
            current_url = ""

        if (
            not force_reload
            and current_url.startswith(BASE)
            and not detect_login_required("", current_url)
        ):
            return

        target_url = self.event.url if self.event and self.event.url else BASE
        logger.info(
            "瀏覽器前台目前停在非 tixcraft 已登入頁 (%s)，導回 %s 以套用最新 session",
            current_url or "<unknown>",
            target_url,
        )
        try:
            await self.page.goto(target_url)
            await self.page.sleep(1.0)
        except Exception:
            logger.debug("補 cookie 後回跳前台頁面失敗", exc_info=True)
        self._mark_session_checked()

    async def _resolve_browser_user_agent(self, retries: int = 8, delay: float = 0.5) -> str:
        """頁面跳轉中 evaluate 可能失敗；重試後仍失敗則回退到固定 UA。"""
        for attempt in range(1, retries + 1):
            try:
                ua = await self.page.evaluate("navigator.userAgent")
                if ua:
                    return str(ua)
            except Exception as exc:
                if "Execution context was destroyed" not in str(exc):
                    logger.warning("讀取瀏覽器 UA 失敗，改用 fallback UA: %s", exc)
                    return DEFAULT_API_USER_AGENT
                logger.info("瀏覽器頁面仍在跳轉，等待後重試 UA 讀取 (%d/%d)", attempt, retries)
            await asyncio.sleep(delay)

        logger.warning("讀取瀏覽器 UA 逾時，改用 fallback UA")
        return DEFAULT_API_USER_AGENT

    async def _wait_for_browser_session_ready(self, timeout: float = 20.0) -> None:
        """等待瀏覽器拿到可用 session cookie，再初始化 API session。"""
        required_names = {"BID", "_csrf", "TIXUISID", "eps_sid"}
        deadline = asyncio.get_running_loop().time() + timeout
        last_names: set[str] = set()

        while asyncio.get_running_loop().time() < deadline:
            all_cookies = await self.page.get_all_cookies()
            names = {c.get("name", "") for c in all_cookies if c.get("value")}
            last_names = names
            if len(names) > 1 and names & required_names:
                logger.info(
                    "瀏覽器 session 已就緒 (cookies=%d, 命中=%s)",
                    len(names),
                    ", ".join(sorted(names & required_names)),
                )
                return
            await asyncio.sleep(0.5)

        logger.warning(
            "等待瀏覽器 session cookie 逾時，使用目前 cookie 繼續 (cookies=%d, names=%s)",
            len(last_names),
            ", ".join(sorted(last_names)) or "<none>",
        )

    def enable_session_failover(
        self,
        enabled: bool = True,
        *,
        block_streak: int | None = None,
        proxy_error_streak: int | None = None,
    ) -> None:
        self._session_failover_enabled = enabled
        if block_streak is not None:
            self._session_failover_block_streak = max(1, int(block_streak))
        if proxy_error_streak is not None:
            self._session_failover_proxy_error_streak = max(1, int(proxy_error_streak))

    def _mark_session_checked(self) -> None:
        self._last_session_check_at = asyncio.get_running_loop().time()

    def _session_check_is_due(self, *, force: bool = False) -> bool:
        if force or self._last_session_check_at <= 0:
            return True
        return (
            asyncio.get_running_loop().time() - self._last_session_check_at
            >= self._session_check_interval
        )

    @staticmethod
    def _watch_sleep_seconds(interval: float, target_count: int) -> float:
        return interval

    def _record_watch_hit(self, status_code: int, latency_ms: float) -> None:
        """記錄 watch 單次請求結果"""
        key = f"{self._watch_stats_interval:.1f}"
        if key not in self._watch_stats:
            self._watch_stats[key] = {"ok": 0, "blocked": 0, "total": 0, "latencies": []}
        s = self._watch_stats[key]
        s["total"] += 1
        if status_code in BLOCKED_STATUS_CODES:
            s["blocked"] += 1
        else:
            s["ok"] += 1
        s["latencies"].append(latency_ms)

    def _dump_watch_stats(self, force: bool = False) -> None:
        """每 50 輪輸出一次統計摘要，並存檔"""
        total_all = sum(s["total"] for s in self._watch_stats.values())
        if not force and total_all % 50 != 0:
            return
        if not self._watch_stats:
            return

        lines = ["Watch 間隔統計:"]
        for key in sorted(self._watch_stats.keys(), key=float):
            s = self._watch_stats[key]
            rate = s["ok"] / s["total"] * 100 if s["total"] > 0 else 0
            avg_lat = sum(s["latencies"]) / len(s["latencies"]) if s["latencies"] else 0
            lines.append(f"  {key}s: {rate:.0f}% ok ({s['ok']}/{s['total']}), avg {avg_lat:.0f}ms")
        logger.info("\n".join(lines))

        # 存檔
        import json
        from pathlib import Path
        out = Path("data/watch_stats.json")
        out.parent.mkdir(exist_ok=True)
        dump = {}
        for key, s in self._watch_stats.items():
            dump[key] = {
                "ok": s["ok"],
                "blocked": s["blocked"],
                "total": s["total"],
                "success_rate": round(s["ok"] / s["total"], 3) if s["total"] > 0 else 0,
                "avg_latency_ms": round(sum(s["latencies"]) / len(s["latencies"]), 1) if s["latencies"] else 0,
            }
        try:
            existing = {}
            if out.exists():
                try:
                    existing = json.loads(out.read_text())
                except Exception:
                    pass
            existing.update(dump)
            out.write_text(json.dumps(existing, indent=2))
        except Exception:
            pass

    @staticmethod
    def _forbidden_backoff_seconds(streak: int, watch_delay: float) -> float:
        base = min(MAX_FORBIDDEN_BACKOFF, 0.5 * (2 ** min(max(streak - 1, 0), 4)))
        return max(watch_delay, base + random.uniform(0.0, 0.3))

    @staticmethod
    def _target_label(target: dict) -> str:
        return str(target.get("text") or target.get("keyword") or "watch-target")

    @staticmethod
    def _clear_forbidden_streak(target: dict) -> None:
        target["forbidden_streak"] = 0

    @staticmethod
    def _blocked_status_label(status_code: int) -> str:
        if status_code == 401:
            return "401 identify"
        return str(status_code)

    @staticmethod
    def _is_proxy_transport_error(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        return (
            "proxy" in name
            or "proxy" in message
            or "connect tunnel failed" in message
        )

    def _raise_session_failover(self, reason: str) -> None:
        self.last_error = reason
        raise SessionFailoverRequiredError(reason)

    @staticmethod
    def _build_submit_timing(
        *,
        ticket_url: str,
        ticket_started_at: float,
        ticket_page_loaded_at: float,
        captcha_solved_at: float,
        post_started_at: float,
        post_completed_at: float,
    ) -> dict[str, float | str]:
        return {
            "ticket_url": ticket_url,
            "ticket_page_get_ms": round((ticket_page_loaded_at - ticket_started_at) * 1000, 2),
            "captcha_solve_ms": round((captcha_solved_at - ticket_page_loaded_at) * 1000, 2),
            "ticket_entry_to_submit_ms": round((post_started_at - ticket_started_at) * 1000, 2),
            "submit_post_ms": round((post_completed_at - post_started_at) * 1000, 2),
            "ticket_entry_to_post_response_ms": round((post_completed_at - ticket_started_at) * 1000, 2),
        }

    def _record_submit_timing(
        self,
        *,
        ticket_url: str,
        ticket_started_at: float,
        ticket_page_loaded_at: float,
        captcha_solved_at: float,
        post_started_at: float,
        post_completed_at: float,
    ) -> None:
        timing = self._build_submit_timing(
            ticket_url=ticket_url,
            ticket_started_at=ticket_started_at,
            ticket_page_loaded_at=ticket_page_loaded_at,
            captcha_solved_at=captcha_solved_at,
            post_started_at=post_started_at,
            post_completed_at=post_completed_at,
        )
        self.last_submit_timing = timing
        logger.info(
            "API timing: ticket_page_get=%.2f ms | captcha_solve=%.2f ms | entry_to_submit=%.2f ms | submit_post=%.2f ms | entry_to_post_response=%.2f ms | ticket=%s",
            timing["ticket_page_get_ms"],
            timing["captcha_solve_ms"],
            timing["ticket_entry_to_submit_ms"],
            timing["submit_post_ms"],
            timing["ticket_entry_to_post_response_ms"],
            ticket_url,
        )

    @staticmethod
    def _mask_proxy_server(proxy_server: str) -> str:
        if not proxy_server:
            return "<direct>"
        parsed = urlsplit(proxy_server)
        if parsed.hostname:
            host = parsed.hostname
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return f"{parsed.scheme or 'http'}://{host}"
        return proxy_server.split("@")[-1]

    async def _rebuild_api_session(self) -> bool:
        try:
            await self._wait_for_browser_session_ready(timeout=5.0)
            await self._init_http()

            # proxy 輪替後 IP 可能與瀏覽器不同，驗證 session 是否仍然有效
            if self._proxy_server != self._browser_proxy:
                if not await self._verify_api_session_after_proxy_change():
                    return False

            return True
        except Exception:
            logger.exception("重建 API session 失敗")
            return False

    async def _verify_api_session_after_proxy_change(self) -> bool:
        """proxy 輪替後驗證 session：若 cookie 被 IP 綁定則回退到瀏覽器 proxy。"""
        try:
            resp = await self._http.get(
                f"{BASE}/user/changeLanguage/lang/zh_tw",
                timeout=5, allow_redirects=False,
            )
            self._trace_api_response(resp, note="proxy_change_verify")
            loc = self._absolute_url(resp.headers.get("Location", ""))

            if resp.status_code in BLOCKED_STATUS_CODES:
                logger.warning(
                    "Proxy 輪替後收到 %s，cookie 可能綁定 IP，回退瀏覽器 proxy: %s",
                    resp.status_code,
                    self._mask_proxy_server(self._browser_proxy),
                )
                self._proxy_server = self._browser_proxy
                await self._init_http()
                return True  # 回退成功，session 仍可用

            if resp.status_code in (301, 302) and ("login" in loc or "facebook.com" in loc):
                logger.warning(
                    "Proxy 輪替後 session 失效（需登入），回退瀏覽器 proxy: %s",
                    self._mask_proxy_server(self._browser_proxy),
                )
                self._proxy_server = self._browser_proxy
                await self._init_http()
                return True

            logger.info(
                "Proxy 輪替後 session 驗證通過 (browser=%s, api=%s)",
                self._mask_proxy_server(self._browser_proxy),
                self._mask_proxy_server(self._proxy_server),
            )
            return True
        except Exception as exc:
            logger.warning(
                "Proxy 輪替後驗證失敗 (%s)，回退瀏覽器 proxy: %s",
                exc,
                self._mask_proxy_server(self._browser_proxy),
            )
            self._proxy_server = self._browser_proxy
            try:
                await self._init_http()
            except Exception:
                logger.exception("回退瀏覽器 proxy 後重建 session 也失敗")
                return False
            return True

    def _rotate_api_proxy(self) -> bool:
        if self.session and self.session.proxy_server:
            logger.info("略過 proxy 輪替：session 已指定固定 proxy")
            return False

        manager = getattr(self, "_proxy_manager", None) or ProxyManager(self.config.proxy)
        if not manager.available:
            logger.info("略過 proxy 輪替：未設定可用 proxy")
            return False

        previous = self._proxy_server
        next_proxy = previous
        server_count = len(manager.servers)
        for _ in range(max(server_count, 1)):
            candidate = manager.next() or previous
            next_proxy = candidate
            if candidate != previous:
                break
        self._proxy_server = next_proxy
        logger.warning(
            "API proxy 重新取號: %s -> %s (瀏覽器仍在 %s)",
            self._mask_proxy_server(previous),
            self._mask_proxy_server(next_proxy),
            self._mask_proxy_server(self._browser_proxy),
        )
        return next_proxy != previous

    async def _handle_watch_forbidden(
        self,
        game_url: str,
        target: dict,
        *,
        status_code: int = 403,
        round_num: int,
        watch_delay: float,
        multi_date_watch: bool,
    ) -> tuple[dict, float]:
        streak = int(target.get("forbidden_streak", 0)) + 1
        target["forbidden_streak"] = streak
        label = self._target_label(target)
        action = ""

        if streak % FORBIDDEN_PROXY_ROTATE_STREAK == 0:
            rotated = self._rotate_api_proxy()
            rebuilt = await self._rebuild_api_session()
            if rotated and rebuilt:
                action = "已輪替 proxy 並重建 session"
            elif rotated:
                action = "已輪替 proxy，但 session 重建失敗"
            elif rebuilt:
                action = "proxy 未變更，已重建 session"
            else:
                action = "proxy 未變更，session 重建失敗"
        elif streak >= FORBIDDEN_BROWSER_REFRESH_STREAK and streak % 2 == 1:
            refresh_url = target.get("href") if status_code == 401 else ""
            refreshed = await self._refresh_session(challenge_url=refresh_url or game_url)
            action = "已刷新瀏覽器 session" if refreshed else "瀏覽器 session 刷新失敗"
            if multi_date_watch:
                target["href"] = None
                refreshed_target = await self._refresh_watch_target(game_url, target)
                target.update(refreshed_target)
        elif streak % FORBIDDEN_SESSION_REBUILD_STREAK == 0:
            rebuilt = await self._rebuild_api_session()
            action = "重建 API session" if rebuilt else "API session 重建失敗"

        backoff = self._forbidden_backoff_seconds(streak, watch_delay)
        logger.warning(
            "API watch: [%s] 第 %d 輪收到 %s (連續 %d 次)%s，%.1f 秒後重試",
            label[:24],
            round_num,
            self._blocked_status_label(status_code),
            streak,
            f"，{action}" if action else "",
            backoff,
        )
        return target, backoff

    async def _handle_game_page_blocked(self, game_url: str, status_code: int) -> None:
        self._game_block_streak += 1
        streak = self._game_block_streak
        action = ""

        if streak % FORBIDDEN_PROXY_ROTATE_STREAK == 0:
            rotated = self._rotate_api_proxy()
            rebuilt = await self._rebuild_api_session()
            if rotated and rebuilt:
                action = "已輪替 proxy 並重建 session"
            elif rotated:
                action = "已輪替 proxy，但 session 重建失敗"
            elif rebuilt:
                action = "proxy 未變更，已重建 session"
            else:
                action = "proxy 未變更，session 重建失敗"
        elif streak >= FORBIDDEN_BROWSER_REFRESH_STREAK and streak % 2 == 1:
            refresh_url = game_url if status_code == 401 else ""
            refreshed = await self._refresh_session(challenge_url=refresh_url)
            action = "已刷新瀏覽器 session" if refreshed else "瀏覽器 session 刷新失敗"
        elif streak % FORBIDDEN_SESSION_REBUILD_STREAK == 0:
            rebuilt = await self._rebuild_api_session()
            action = "重建 API session" if rebuilt else "API session 重建失敗"

        logger.warning(
            "API: 步驟1 場次頁收到 %s (連續 %d 次)%s: %s",
            self._blocked_status_label(status_code),
            streak,
            f"，{action}" if action else "",
            game_url,
        )
        if self._session_failover_enabled and streak >= self._session_failover_block_streak:
            self._raise_session_failover(
                f"[{self._session_label}] 步驟1 場次頁持續收到 {self._blocked_status_label(status_code)} "
                f"({streak} 次)，切換到下一組 session"
            )

    async def _handle_proxy_transport_error(self, url: str, exc: Exception) -> None:
        self._proxy_error_streak += 1
        streak = self._proxy_error_streak
        rotated = self._rotate_api_proxy()
        rebuilt = await self._rebuild_api_session()

        if rotated and rebuilt:
            action = "已輪替 proxy 並重建 session"
        elif rotated:
            action = "已輪替 proxy，但 session 重建失敗"
        elif rebuilt:
            action = "proxy 未變更，已重建 session"
        else:
            action = "proxy 未變更，session 重建失敗"

        logger.warning(
            "API: proxy 連線錯誤 (連續 %d 次，%s): %s | %s",
            streak,
            action,
            url,
            exc,
        )

        if self._session_failover_enabled and streak >= self._session_failover_proxy_error_streak:
            self._raise_session_failover(
                f"[{self._session_label}] proxy 連線持續異常 ({streak} 次)，切換到下一組 session"
            )

    @staticmethod
    def _absolute_url(url: str) -> str:
        if url.startswith("/"):
            return f"{BASE}{url}"
        return url

    async def _api_get(
        self,
        url: str,
        referer: str = "",
        *,
        follow_redirects: bool = False,
    ) -> curl_requests.Response:
        """GET 請求，自動加 Referer；需要時才 follow 302。"""
        headers = {}
        if referer:
            headers["Referer"] = referer
        resp = await self._http.get(url, headers=headers)
        self._trace_api_response(resp, note="initial")
        if follow_redirects and resp.status_code in (301, 302):
            loc = self._absolute_url(resp.headers.get("Location", ""))
            if loc:
                resp = await self._http.get(loc, headers={"Referer": url})
                self._trace_api_response(resp, note="follow_redirect")
        return resp

    async def _api_post(self, url: str, data: dict, referer: str = "") -> curl_requests.Response:
        """POST 請求"""
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if referer:
            headers["Referer"] = referer
        resp = await self._http.post(url, data=data, headers=headers)
        self._trace_api_response(resp, note="post")
        return resp

    def _resolve_cookie_file(self):
        from pathlib import Path

        if self.session and self.session.cookie_file:
            return Path(self.session.cookie_file)

        if len(self.config.sessions) > 1 and self.session and self.session.name != "default":
            logger.info(
                "[%s] 未設定 session 專屬 cookie_file，略過全域 tixcraft_cookies.json 補檔以避免多帳號混用",
                self._session_label,
            )
            return None

        return Path("tixcraft_cookies.json")

    @staticmethod
    def _load_cookies_from_json(existing: dict, cookie_file=None) -> dict:
        """從 cookie JSON 載入明文 cookie（跨機器 profile 同步用）"""
        import json
        if cookie_file is None:
            return existing
        if not cookie_file.exists():
            logger.warning("找不到 %s，無法補充 cookie", cookie_file)
            return existing
        try:
            data = json.loads(cookie_file.read_text())
            jar = dict(existing)
            added = 0
            for c in data:
                name = c.get("name")
                value = c.get("value")
                if not name or not value or name in jar:
                    continue
                jar[name] = value
                added += 1
            logger.info(
                "從 %s 補充 %d 個 cookie（瀏覽器原有 %d 個，合併後 %d 個）",
                cookie_file,
                added,
                len(existing),
                len(jar),
            )
            return jar
        except Exception as e:
            logger.warning("載入 %s 失敗: %s", cookie_file, e)
            return existing

    async def _notify(self, msg: str) -> None:
        """推送通知到 TG（如果有設定 callback）"""
        if self._notify_callback:
            try:
                await self._notify_callback(msg)
            except Exception:
                pass

    async def _refresh_session(self, challenge_url: str = "") -> bool:
        """嘗試用瀏覽器刷新 session 並重新提取 cookie。

        回傳 True 表示恢復成功，False 表示需要手動重新登入。
        """
        logger.info(
            "嘗試透過瀏覽器刷新 session%s...",
            f"（先訪問挑戰頁: {challenge_url}）" if challenge_url else "",
        )
        try:
            if challenge_url:
                await self.page.goto(challenge_url)
                await self.page.sleep(3)

            # 導航瀏覽器到語系頁，觸發 cookie 自動刷新
            await self.page.goto(f"{BASE}/user/changeLanguage/lang/zh_tw")
            await self.page.sleep(2)

            url = await self._current_url()
            if detect_login_required("", url):
                logger.warning("瀏覽器 session 也已過期，無法自動恢復")
                return False

            await self._wait_for_browser_session_ready(timeout=10.0)
            # 瀏覽器仍有效，重新提取 cookie
            await self._init_http()
            logger.info("Session 刷新成功，已重新提取 cookie")
            return True
        except Exception:
            logger.exception("刷新 session 失敗")
            return False

    def _raise_login_expired(self) -> None:
        """拋出 LoginExpiredError，讓上層（TG bot 錯誤處理）通知使用者"""
        raise LoginExpiredError(
            "tixcraft 登入已過期，瀏覽器 session 也無法恢復。"
            "請在本機執行 login → sync profile 到雲端後重新啟動。"
        )

    async def _ensure_session(self, *, force: bool = False) -> None:
        """檢查 API session 是否有效，無效則嘗試恢復。失敗時拋出 LoginExpiredError。"""
        if not self._session_check_is_due(force=force):
            return
        try:
            resp = await self._http.get(f"{BASE}/user/changeLanguage/lang/zh_tw",
                                        timeout=5, allow_redirects=False)
            self._trace_api_response(resp, note="session_check")
            loc = self._absolute_url(resp.headers.get("Location", ""))
            if resp.status_code in (301, 302) and ("login" in loc or "facebook.com" in loc):
                logger.warning("API cookie 已過期，嘗試恢復...")
                if not await self._refresh_session():
                    self._raise_login_expired()
                return
            self._mark_session_checked()
        except LoginExpiredError:
            raise
        except Exception:
            logger.warning("Session 檢查失敗，嘗試刷新...")
            if not await self._refresh_session():
                self._raise_login_expired()

    async def _write_checkout_relay(self, checkout_url: str) -> None:
        """寫出 checkout relay 檔案，供本機 relay 腳本接力結帳"""
        import json
        import os
        from datetime import datetime, timezone, timedelta
        try:
            tz = timezone(timedelta(hours=8))
            cookies = []
            if self._http:
                for k, v in self._http.cookies.items():
                    cookies.append({"name": k, "value": v, "domain": ".tixcraft.com", "path": "/"})
            relay_data = {
                "timestamp": datetime.now(tz).isoformat(),
                "checkout_url": checkout_url,
                "cookies": cookies,
                "event_name": self.event.name if self.event else "",
                "area": getattr(self, "_selected_area_text", ""),
                "timing": self.last_submit_timing,
            }
            relay_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "checkout_relay.json")
            relay_path = os.path.normpath(relay_path)
            with open(relay_path, "w") as f:
                json.dump(relay_data, f, ensure_ascii=False)
            logger.info("已寫出 checkout relay: %s (%d cookies)", relay_path, len(cookies))
        except Exception:
            logger.exception("寫出 checkout relay 失敗")

    async def _sync_cookies_to_browser(self) -> None:
        """將 HTTP client 的 cookie 同步回瀏覽器（引擎無關）"""
        if not self._http or not self.page:
            return
        cookies = [{"name": k, "value": v, "url": "https://tixcraft.com"}
                   for k, v in self._http.cookies.items()]
        if cookies:
            logger.info("正在同步 %d 個 Cookie 至瀏覽器...", len(cookies))
            await self.page.set_cookies(cookies)

    def _trace_api_response(self, response: curl_requests.Response, *, note: str = "") -> None:
        request = getattr(response, "request", None)
        self._trace_logger.trace_response(
            source="api",
            method=getattr(request, "method", ""),
            url=str(getattr(response, "url", "") or getattr(request, "url", "")),
            status_code=response.status_code,
            headers=response.headers,
            remote_ip=getattr(response, "primary_ip", ""),
            protocol=str(getattr(response, "http_version", "")),
            note=note,
        )

    # ── Full API 模式 run() ───────────────────────────────────

    def _build_area_blacklist_re(self) -> re.Pattern | None:
        """從 event.area_blacklist_keywords 編譯黑名單 regex；空 list 回傳 None 表示不過濾。"""
        blacklist = self.event.area_blacklist_keywords or []
        if not blacklist:
            return None
        pattern = "|".join(re.escape(k) for k in blacklist if k)
        return re.compile(pattern, re.IGNORECASE) if pattern else None

    async def prepare_api_session(self) -> None:
        """確認登入＋抽出瀏覽器 cookie 建立 httpx session。
        前提：caller 已經跑過 start_browser + pre_warm（或 _open_page）。
        countdown 模式會在售票前 10 分鐘呼叫這個方法，把所有準備工作做完，
        T=0 直接 _api_loop() 開搶。
        """
        url = await self._current_url()
        if detect_login_required("", url):
            if self.config.browser.headless:
                self._raise_login_expired()
            logger.warning("需要登入，請在瀏覽器中手動登入...")
            await self._wait_for_login()

        await self._wait_for_browser_session_ready()
        await self._init_http()

    async def run(self) -> bool:
        """全流程：瀏覽器登入 → API 搶票"""
        if self.config.browser.api_mode != "full":
            # checkout 模式：沿用瀏覽器流程，只覆寫 _fill_ticket_form
            return await super().run()

        # ── Full API 模式 ──
        await self.start_browser()

        # 預熱 + 確認登入
        if self.config.browser.pre_warm:
            await self.pre_warm()
        else:
            await self._open_page(self.event.url)

        await self.prepare_api_session()

        # API 搶票迴圈
        try:
            return await self._api_loop()
        except LoginExpiredError:
            raise  # 讓上層處理通知
        except Exception:
            logger.exception("API 搶票流程發生錯誤")
            return False

    async def _api_loop(self) -> bool:
        """API 搶票主迴圈"""
        initial_url = self.event.url
        if "/activity/detail/" in initial_url:
            slug = initial_url.rstrip("/").split("/")[-1]
            initial_url = f"{BASE}/activity/game/{slug}"

        for attempt in range(200):
            await self._ensure_session()
            # 判斷起始 URL 是場次頁還是區域頁
            if "/ticket/area/" in initial_url:
                area_url = initial_url
            elif "/ticket/ticket/" in initial_url:
                ticket_url = initial_url
                area_url = None # 直接跳過前面的步驟
            else:
                # ── Step 1: 場次選擇 ──
                area_url = await self._select_game_api(initial_url)
                if not area_url:
                    # API 模式下，未開賣時的輪詢可以極快 (0.1 ~ 0.3 秒)
                    await asyncio.sleep(random.uniform(0.1, 0.3))
                    continue

            if area_url:
                # ── Step 2: 驗證頁 ──
                if "/verify/" in area_url:
                    area_url = await self._handle_verify_api(area_url)
                    if not area_url:
                        continue

                # ── Step 3: 區域選擇 ──
                ticket_url = await self._select_area_api(area_url)
                if not ticket_url:
                    continue

            # ── Step 4: 結帳 (全 API 直送) ──
            logger.info("進入結帳流程：%s", ticket_url)
            success = await self._fill_ticket_form_api(ticket_url)
            if success:
                logger.info("🎉 搶票成功！請盡速完成付款")
                return True
            
            logger.warning("API 結帳失敗，回退至場次頁重試")
            await self.page.sleep(0.5)

        logger.error("API 搶票超過最大嘗試次數")
        return False

    # ── Step 1: 場次選擇 (API) ────────────────────────────────

    async def _select_game_api(self, game_url: str) -> str | None:
        """GET 場次頁，解析可用場次，回傳 area URL"""
        candidates = await self._select_game_candidates_api(game_url)
        if not candidates:
            return None

        target = candidates[0]
        href = target["href"]
        logger.info("API: 選擇場次: %s → %s", target["text"][:40], href)
        return href

    async def _select_game_candidates_api(
        self, game_url: str, date_keyword: str | None = None
    ) -> list[dict]:
        """GET 場次頁，解析所有匹配的場次，回傳 [{text, href}, ...]。"""
        try:
            resp = await self._api_get(game_url, follow_redirects=True)
            self._proxy_error_streak = 0
        except SessionFailoverRequiredError:
            raise
        except Exception as exc:
            if self._is_proxy_transport_error(exc):
                await self._handle_proxy_transport_error(game_url, exc)
                return []
            raise

        status_code = getattr(resp, "status_code", 200)
        if status_code in BLOCKED_STATUS_CODES:
            await self._handle_game_page_blocked(game_url, status_code)
            return []

        self._game_block_streak = 0
        html = resp.text

        if detect_coming_soon(html):
            logger.info("API: 即將開賣，等待刷新...")
            return []

        game_info = parse_game_list(html)
        available = game_info["available"]
        sold_out = game_info["sold_out"]

        if sold_out:
            logger.info("API: 售完場次 (%d): %s", len(sold_out),
                        "; ".join(s[:30] for s in sold_out[:3]))

        if not available:
            self.last_error = f"步驟1 場次：所有場次已售完或未開賣 ({game_info['total']} 場)"
            logger.warning("API: %s", self.last_error)
            return []

        # 找匹配 date_keyword 的場次；支援 2026/09/11|2026/09/12 這種多日期寫法
        candidates = []
        date_kw = date_keyword if date_keyword is not None else self.event.date_keyword
        if date_kw:
            for g in available:
                if matches_any_keyword(g["text"], date_kw):
                    candidates.append(g)

        if not candidates:
            candidates = [available[0]]

        normalized = []
        for target in candidates:
            href = target["href"]
            if href.startswith("/"):
                href = f"{BASE}{href}"
            normalized.append({"text": target["text"], "href": href})
        return normalized

    # ── Step 2: 驗證頁 (API) ─────────────────────────────────

    async def _handle_verify_api(self, verify_url: str) -> str | None:
        """處理驗證頁，回傳下一步 URL"""
        resp = await self._api_get(verify_url, follow_redirects=True)
        html = resp.text

        info = parse_verify_page(html)
        answer = info.get("answer")

        if not answer and self.event.presale_code:
            answer = self.event.presale_code
            logger.info("API: 使用 presale_code：%s***", answer[:3])
        elif answer:
            logger.info("API: 驗證答案：%s", answer)
        else:
            logger.warning("API: 無法取得驗證答案")
            return None

        # POST 驗證碼（tixcraft 用 AJAX POST 到 check-code endpoint）
        csrf = info.get("csrf", "")
        # check-code URL: 把 verify 改成 check-code
        check_url = verify_url
        if "/activity/verify/" in verify_url:
            check_url = verify_url.replace("/activity/verify/", "/activity/check-code/")
        elif "/ticket/verify/" in verify_url:
            check_url = verify_url.replace("/ticket/verify/", "/ticket/check-code/")

        post_data = {"_csrf": csrf, "checkCode": answer}
        resp = await self._api_post(check_url, post_data, referer=verify_url)

        if resp.status_code in (301, 302):
            loc = resp.headers.get("Location", "")
            if loc.startswith("/"):
                loc = f"{BASE}{loc}"
            logger.info("API: 驗證成功，導向: %s", loc)
            return loc

        # AJAX 回應可能是 JSON
        try:
            data = resp.json()
            if data.get("message"):
                logger.warning("API: 驗證失敗: %s", data["message"])
            elif data.get("url"):
                logger.info("API: 驗證成功，導向: %s", data["url"])
                url = data["url"]
                if url.startswith("/"):
                    url = f"{BASE}{url}"
                return url
        except Exception:
            pass

        logger.warning("API: 驗證回應異常 (status=%d)", resp.status_code)
        return None

    # ── Step 3: 區域選擇 (API) ────────────────────────────────

    async def _select_area_api(self, area_url: str) -> str | None:
        """GET 區域頁，選擇可用區域，回傳 ticket URL"""
        resp = await self._api_get(area_url, follow_redirects=True)
        html = resp.text

        area_info = parse_area_list(html)
        _skip_re = self._build_area_blacklist_re()
        if _skip_re:
            available = [a for a in area_info["available"] if not _skip_re.search(a["text"])]
        else:
            available = list(area_info["available"])

        if not available:
            self.last_error = f"步驟2 區域：所有區域已售完 ({area_info['total']} 區)"
            logger.warning("API: %s", self.last_error)
            return None

        # 找匹配 area_keyword 的區域
        target = None
        area_kw = self.event.area_keyword
        if area_kw:
            for a in available:
                if matches_any_keyword(a["text"], area_kw):
                    target = a
                    break

        if not target:
            target = available[0]

        href = target["href"]
        if href.startswith("/"):
            href = f"{BASE}{href}"
        logger.info("API: 選擇區域: %s → %s", target["text"][:40], href)
        return href

    # ── Step 4: 結帳 (全 API POST) ───────────────────────────

    async def _fill_ticket_form_api(self, ticket_url: str) -> bool:
        """完全使用 API 提交訂票單，不依賴瀏覽器 UI"""
        ticket_started_at = time.perf_counter()
        # 1. GET 訂票頁，取得表單欄位
        resp = await self._api_get(ticket_url, follow_redirects=True)
        ticket_page_loaded_at = time.perf_counter()
        html = resp.text

        # debug: 存 ticket 頁 HTML
        try:
            with open("ticket_page_debug.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

        from ticket_bot.platforms.tixcraft_parser import parse_ticket_form
        form_info = parse_ticket_form(html)
        form_data = form_info["fields"]

        if not form_data.get("_csrf"):
            # 精確診斷失敗原因
            import re as _diag_re
            if 'sold out' in html.lower() or '已售完' in html:
                self.last_error = "步驟3：此區已售完 (sold out)"
            elif 'alert("This event' in html:
                alert_match = _diag_re.search(r'alert\("([^"]+)"\)', html)
                self.last_error = f"步驟3：tixcraft alert — {alert_match.group(1) if alert_match else 'unknown'}"
            elif detect_login_required(html, str(resp.url)):
                self.last_error = "步驟3：cookie 過期，需要重新登入"
            elif 'Browsing Activity' in html or 'unusual behavior' in html:
                self.last_error = "步驟3：IP 被封鎖 (Browsing Activity Paused)"
            elif 'cf-browser-verification' in html or 'challenge-platform' in html:
                self.last_error = "步驟3：Cloudflare challenge"
            elif '/ticket/area/' in html and 'TicketForm' not in html:
                self.last_error = "步驟3：被踢回選區頁（非票頁）"
            else:
                self.last_error = f"步驟3：未知錯誤（HTML {len(html)} bytes，無 CSRF）"
            logger.warning("API: %s", self.last_error)
            return False

        # 2. 設定票數（並驗證是否有票可選）
        import re as _re
        count = str(self.event.ticket_count)
        ticket_set = False
        # 檢查 HTML 中的 select options 是否有目標票數
        if form_info["select_name"]:
            sel_name = _re.escape(form_info["select_name"])
            sel_match = _re.search(f'name=["\']?{sel_name}["\']?[^>]*>(.*?)</select>', html, _re.DOTALL)
            if sel_match:
                options = _re.findall(r'value=["\'](\d+)["\']', sel_match.group(1))
                if count in options:
                    form_data[form_info["select_name"]] = count
                    ticket_set = True
                elif options and max(int(o) for o in options) > 0:
                    # 沒有剛好的數量但有其他非零選項，用最大的
                    best = str(max(int(o) for o in options))
                    form_data[form_info["select_name"]] = best
                    logger.warning("API: 目標票數 %s 不可用，改用 %s", count, best)
                    ticket_set = True
                else:
                    self.last_error = f"步驟3 票頁：此座位已無票可選 (可選數量: {options})"
                    logger.warning("API: %s", self.last_error)
                    return False
            else:
                form_data[form_info["select_name"]] = count
                ticket_set = True
        if not ticket_set:
            for key in form_data.keys():
                if "ticketCount" in key or "ticketPrice" in key:
                    form_data[key] = count
                    ticket_set = True
                    break

        # 3. 取得並辨識驗證碼
        captcha_text = await self._solve_captcha_api(ticket_url)
        captcha_solved_at = time.perf_counter()
        if not captcha_text:
            self.last_error = "步驟3 票頁：驗證碼取得或辨識失敗"
            return False

        form_data['TicketForm[verifyCode]'] = captcha_text

        # 4. POST 送單
        post_started_at = time.perf_counter()
        logger.info("API 送單 POST -> %s | 驗證碼: %s", ticket_url, captcha_text)
        post_resp = await self._api_post(ticket_url, form_data, referer=ticket_url)
        post_completed_at = time.perf_counter()
        self._record_submit_timing(
            ticket_url=ticket_url,
            ticket_started_at=ticket_started_at,
            ticket_page_loaded_at=ticket_page_loaded_at,
            captcha_solved_at=captcha_solved_at,
            post_started_at=post_started_at,
            post_completed_at=post_completed_at,
        )

        # 5. 判斷結果
        if post_resp.status_code in (301, 302):
            redirect_url = post_resp.headers.get("Location", "")
            if redirect_url:
                if redirect_url.startswith("/"):
                    redirect_url = f"{BASE}{redirect_url}"
                logger.info("API 送單跳轉至: %s", redirect_url)

                # 被踢回 = 驗證碼錯誤或選位失敗
                fail_patterns = ["/ticket/ticket/", "/ticket/area/", "/activity/game/", "/activity/detail/"]
                if any(p in redirect_url for p in fail_patterns):
                    self.last_error = f"步驟3 送單：驗證碼錯誤或選位失敗，被踢回 {redirect_url.split('/')[-1]}"
                    logger.warning("API: %s", self.last_error)
                    return False
                # 被踢回首頁
                if redirect_url.rstrip("/").endswith("tixcraft.com"):
                    self.last_error = "步驟3 送單：被踢回首頁（票可能已售完或 session 過期）"
                    logger.warning("API: %s", self.last_error)
                    return False

                # 跳到結帳頁 → 切回瀏覽器處理（Sit tight 用 JS 動態更新，API GET 拿不到付款表單）
                if "/ticket/order" in redirect_url or "/ticket/checkout" in redirect_url:
                    logger.info("API 送單成功！切回瀏覽器處理 Sit tight + 付款...")
                    # 寫出 checkout relay 檔案，讓本機可以接力結帳
                    await self._write_checkout_relay(redirect_url)
                    await self._sync_cookies_to_browser()
                    try:
                        await self.page.goto(redirect_url)
                    except Exception:
                        pass
                    # 用瀏覽器版的 _handle_order（繼承自 TixcraftBot）
                    await self._handle_order()
                    await self.page.sleep(2.0)
                    try:
                        final_url = await self._current_url()
                    except Exception:
                        logger.info("🎉 搶票流程完成，請完成付款！")
                        return True
                    # 判斷結果
                    fail_back = ["/ticket/area/", "/ticket/ticket/", "/activity/game/"]
                    if any(p in final_url for p in fail_back) or final_url.rstrip("/").endswith("tixcraft.com"):
                        logger.warning("Sit tight 後被踢回: %s", final_url)
                        return False
                    # /order（票夾）但不是 /ticket/order → Sit tight 失敗，訂單未完成付款
                    if final_url.rstrip("/").endswith("/order") and "/ticket/order" not in final_url:
                        logger.warning("Sit tight 後跳到票夾（非結帳頁）: %s，訂單未完成付款", final_url)
                        return False
                    # 跳到登入頁或外部頁面 → session 過期，不算成功
                    if "/login" in final_url or "tixcraft.com" not in final_url:
                        logger.warning("Sit tight 後跳到非 tixcraft 頁面（session 過期？）: %s", final_url)
                        return False
                    # 確認是付款相關頁面才算成功
                    success_patterns = ["/ticket/order", "/ticket/checkout", "/ticket/payment"]
                    if any(p in final_url for p in success_patterns):
                        logger.info("🎉 搶票流程完成 (URL: %s)，請完成付款！", final_url)
                        return True
                    # 未知 tixcraft 頁面 → 保守判定失敗
                    logger.warning("Sit tight 後停在未知頁面: %s，不確定是否成功", final_url)
                    return False

                # 跳到 /order（票夾）= 直接成功（罕見）
                if "/order" in redirect_url and "/ticket/" not in redirect_url:
                    logger.info("🎉 API 直接跳到票夾: %s", redirect_url)
                    await self._sync_cookies_to_browser()
                    try:
                        await self.page.goto(redirect_url)
                    except Exception:
                        pass
                    return True

                # 其他未知 URL → 不算成功
                self.last_error = f"步驟3 送單：跳轉到未知頁面 {redirect_url}"
                logger.warning("API: %s", self.last_error)
                return False

        # 非 302 → 解析錯誤訊息
        import re
        msg_match = re.search(r'class=["\']help-block["\'][^>]*>(.*?)</div>', post_resp.text)
        if msg_match:
            self.last_error = f"步驟3 送單：{re.sub(r'<[^>]+>', '', msg_match.group(1)).strip()[:80]}"
        else:
            self.last_error = f"步驟3 送單：HTTP {post_resp.status_code}（非 redirect，可能驗證碼錯誤）"
        logger.warning("API: %s", self.last_error)
        return False

    # ── 結帳流程：order → checkout → 票夾 ────────────────────

    def _parse_order_form(self, html: str) -> dict | None:
        """解析結帳頁 HTML，回傳表單資料。找不到付款選項回傳 None。"""
        import re

        csrf_match = re.search(r'name=["\']_csrf["\']\s*value=["\']([^"\']+)', html)
        if not csrf_match:
            return None
        csrf = csrf_match.group(1)

        # 偵測 Sit tight / 處理中
        if re.search(r'sit tight|securing your|請稍候|處理中|processing', html, re.IGNORECASE):
            return None  # 還在 loading

        form_data = {"_csrf": csrf}

        # 解析所有 radio（paymentId, shipmentId 等）
        # 通用方式：找所有 CheckoutForm[xxx] 的 radio
        radio_groups: dict[str, list[dict]] = {}
        for m in re.finditer(
            r'<input[^>]+type=["\']radio["\'][^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']+)["\'][^>]*/?>',
            html, re.IGNORECASE
        ):
            name, value = m.group(1), m.group(2)
            # 取這個 input 後面到 </label> 之間的文字當 label
            after = html[m.end():m.end() + 300]
            label_text = re.sub(r'<[^>]+>', ' ', after.split('</label>')[0] if '</label>' in after else '').strip().lower()
            if name not in radio_groups:
                radio_groups[name] = []
            radio_groups[name].append({"value": value, "label": label_text})

        # 也嘗試 input value 在 name 之前的順序
        for m in re.finditer(
            r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']([^"\']+)["\'][^>]+type=["\']radio["\'][^>]*/?>',
            html, re.IGNORECASE
        ):
            value, name = m.group(1), m.group(2)
            after = html[m.end():m.end() + 300]
            label_text = re.sub(r'<[^>]+>', ' ', after.split('</label>')[0] if '</label>' in after else '').strip().lower()
            if name not in radio_groups:
                radio_groups[name] = []
            # 避免重複
            if not any(r["value"] == value for r in radio_groups[name]):
                radio_groups[name].append({"value": value, "label": label_text})

        if not radio_groups:
            # 沒有 radio → 可能是確認頁，只需 CSRF + submit
            # 但必須有 Checkout 按鈕才算有效表單
            if re.search(r'checkout|結帳|確認付款|確認|送出', html, re.IGNORECASE):
                return form_data
            return None

        # 對每個 radio group 選擇最佳值
        for name, options in radio_groups.items():
            logger.info("API 結帳 radio [%s]: %s", name,
                        [(o["value"], o["label"][:30]) for o in options])
            # 付款：ATM > ibon > 第一個
            if "payment" in name.lower():
                pay_kw = ['atm', '虛擬帳號', '轉帳', '匯款', 'virtual', 'ibon', '超商繳費']
                selected = self._select_radio_by_keywords(options, pay_kw)
                form_data[name] = selected
                logger.info("API 選擇付款: %s", selected)
            # 取票：ibon > 超商 > 第一個
            elif "shipment" in name.lower() or "delivery" in name.lower():
                deliv_kw = ['ibon', '超商', '便利商店', '7-eleven', '7-11']
                selected = self._select_radio_by_keywords(options, deliv_kw)
                form_data[name] = selected
                logger.info("API 選擇取票: %s", selected)
            else:
                # 未知 radio → 選第一個
                form_data[name] = options[0]["value"]

        # 同意條款 checkbox
        for m in re.finditer(r'<input[^>]+type=["\']checkbox["\'][^>]+name=["\']([^"\']+)["\']', html):
            form_data[m.group(1)] = "1"

        return form_data

    @staticmethod
    def _select_radio_by_keywords(options: list[dict], keywords: list[str]) -> str:
        """根據關鍵字優先序從 radio options 中選擇"""
        for kw in keywords:
            for opt in options:
                if kw in opt["label"]:
                    return opt["value"]
        return options[0]["value"]

    async def _handle_order_api(self, order_url: str) -> bool:
        """API 結帳完整流程：Sit tight → 選付款/取票 → Checkout → 確認 → 票夾

        流程對照本機瀏覽器版：
        1. GET /ticket/order → 輪詢等 Sit tight 消失、付款選項出現
        2. POST 選好的付款/取票方式
        3. 302 → /ticket/checkout → GET → POST（確認）
        4. 302 → /order（票夾）= 真正成功
        """
        import re

        current_url = order_url
        referer = order_url

        for step in range(5):  # 最多 5 步（order → checkout → 確認 ...）
            logger.info("API 結帳步驟 %d: %s", step + 1, current_url)

            # ── 輪詢等 Sit tight 完成 ──
            form_data = None
            for poll in range(100):  # 最多 30 秒 (100 * 0.3s)
                resp = await self._api_get(current_url, referer=referer)
                html = resp.text

                # 被踢回 → 失敗
                if resp.status_code in (301, 302):
                    loc = resp.headers.get("Location", "")
                    if loc.startswith("/"):
                        loc = f"{BASE}{loc}"
                    fail_patterns = ["/ticket/area/", "/ticket/ticket/", "/activity/game/"]
                    if any(p in loc for p in fail_patterns):
                        self.last_error = f"步驟4 Sit tight：票被搶走，被踢回 {loc.split('/')[-1]}"
                        logger.warning("API: %s", self.last_error)
                        return False
                    # 跳到 /order（票夾）= 成功
                    if "/order" in loc and "/ticket/" not in loc:
                        logger.info("🎉 API 訂單已成立！票夾: %s", loc)
                        await self._sync_cookies_to_browser()
                        try:
                            await self.page.goto(loc)
                        except Exception:
                            pass
                        return True
                    # 跳到下一步 checkout
                    if "/ticket/checkout" in loc or "/ticket/order" in loc:
                        referer = current_url
                        current_url = loc
                        break

                form_data = self._parse_order_form(html)
                if form_data is not None:
                    break  # 表單就緒

                if poll % 10 == 9:
                    logger.info("API 等待 Sit tight... (%.1f 秒)", (poll + 1) * 0.3)
                if poll == 0:
                    # 第一次 dump Sit tight 頁面內容
                    try:
                        with open("order_sittight_debug.html", "w", encoding="utf-8") as f:
                            f.write(html)
                        logger.info("已存 Sit tight HTML (%d bytes)", len(html))
                    except Exception:
                        pass
                await asyncio.sleep(0.3)
            else:
                self.last_error = "步驟4 Sit tight：等待付款選項逾時 (30秒)，票可能被搶走"
                logger.warning("API: %s", self.last_error)
                return False

            if form_data is None:
                # 是 redirect，loop 繼續下一步
                continue

            # ── POST 結帳表單 ──
            logger.info("API 送出結帳表單: %s", {k: v for k, v in form_data.items() if k != "_csrf"})
            post_resp = await self._api_post(current_url, form_data, referer=current_url)

            if post_resp.status_code not in (301, 302):
                # 非 redirect → 可能還在同一頁（錯誤或多步驟）
                err_match = re.search(r'class=["\'](?:alert|error)["\'][^>]*>(.*?)</div>', post_resp.text, re.DOTALL)
                if err_match:
                    err_text = re.sub(r'<[^>]+>', '', err_match.group(1)).strip()[:100]
                    self.last_error = f"步驟5 Checkout：{err_text}"
                    logger.warning("API: %s", self.last_error)
                    return False
                logger.warning("API 結帳 POST 非 redirect (HTTP %d)，重試本步驟", post_resp.status_code)
                continue

            next_url = post_resp.headers.get("Location", "")
            if next_url.startswith("/"):
                next_url = f"{BASE}{next_url}"
            logger.info("API 結帳跳轉: %s", next_url)

            # 被踢回 = 失敗
            fail_patterns = ["/ticket/area/", "/ticket/ticket/", "/activity/game/"]
            if any(p in next_url for p in fail_patterns) or next_url.rstrip("/").endswith("tixcraft.com"):
                self.last_error = f"步驟5 Checkout 後被踢回: {next_url.split('tixcraft.com')[-1]}"
                logger.warning("API: %s", self.last_error)
                return False

            # 到了 /order（票夾）= 真正成功！
            if "/order" in next_url and "/ticket/" not in next_url:
                logger.info("🎉 API 訂單已成立！票夾: %s", next_url)
                await self._sync_cookies_to_browser()
                return True

            # 下一步（/ticket/order 或 /ticket/checkout）
            if "/ticket/order" in next_url or "/ticket/checkout" in next_url:
                referer = current_url
                current_url = next_url
                continue

            # 未知頁面
            self.last_error = f"步驟5 Checkout 跳到未知頁面: {next_url}"
            logger.warning("API: %s", self.last_error)
            return False

        self.last_error = "步驟5 結帳超過最大步驟數 (5 步)"
        logger.warning("API: %s", self.last_error)
        return False

    async def _solve_captcha_api(self, referer_url: str) -> str:
        """取得並辨識驗證碼"""
        if not self._http:
            await self._init_http()

        async def fetch_captcha_image() -> bytes:
            resp = await self._http.get(
                f"{BASE}/ticket/captcha",
                params={"refresh": str(random.random())}, # 增加隨機性
                headers={"Referer": referer_url},
            )
            if resp.status_code != 200:
                return b""
            ct = resp.headers.get("content-type", "")
            if "json" in ct:
                data = resp.json()
                img_url = data.get("url", "")
                if not img_url:
                    return b""
                img_resp = await self._http.get(f"{BASE}{img_url}")
                return img_resp.content
            return resp.content

        # 如果有 TG 回調（雲端模式），推送驗證碼圖片
        if self._captcha_callback:
            img = await fetch_captcha_image()
            if img:
                return await self._captcha_callback(img)
            return ""

        # 自動辨識 (使用我們之前訓練的高準確率模型)
        text = await self.solver.asolve_with_retry(fetch_captcha_image)
        logger.info("API 自動辨識驗證碼：%s", text)
        return text

    # ── API Watch 模式 ─────────────────────────────────────────

    async def watch(self, interval: float = 5.0) -> bool:
        """API 版釋票監測：全 HTTP 輪詢，不經瀏覽器渲染"""
        if self.config.browser.api_mode != "full":
            return await super().watch(interval=interval)

        # 瀏覽器僅用於登入 + 提取 cookie
        await self.start_browser()
        if self.config.browser.pre_warm:
            await self.pre_warm()

        url = await self._current_url()
        if detect_login_required("", url):
            if self.config.browser.headless:
                self._raise_login_expired()
            logger.warning("需要登入，請在瀏覽器中手動登入...")
            await self._wait_for_login()

        await self._wait_for_browser_session_ready()
        await self._init_http()

        # 取得 area URL
        game_url = self.event.url
        if "/activity/detail/" in game_url:
            slug = game_url.rstrip("/").split("/")[-1]
            game_url = f"{BASE}/activity/game/{slug}"

        date_keywords = split_match_keywords(self.event.date_keyword)
        multi_date_watch = len(date_keywords) > 1
        if multi_date_watch:
            watch_targets = await self._build_multi_date_watch_targets(game_url, date_keywords)
            watch_delay = self._watch_sleep_seconds(interval, len(watch_targets))
            logger.info(
                "API watch: 同時監測 %d 個場次 (單一場次約每 %.1f 秒、target 間隔 %.1f 秒): %s",
                len(watch_targets),
                interval,
                watch_delay,
                "; ".join(t["keyword"][:40] for t in watch_targets),
            )
        else:
            area_url = await self._navigate_to_area_api(game_url)
            while area_url is None:
                logger.warning("API watch: 無法進入區域頁，%.1f 秒後重試...", interval)
                await asyncio.sleep(interval)
                area_url = await self._navigate_to_area_api(game_url)
            watch_targets = [{"text": self.event.date_keyword or "第一個可用", "href": area_url}]
            watch_delay = interval

        self._watch_stats_interval = interval
        logger.info("API watch: 開始監測釋票 (間隔 %.1f 秒)", interval)

        round_num = 0
        consecutive_errors = 0
        watch_index = 0

        while True:
            round_num += 1
            current_target = watch_targets[watch_index]
            current_target["visits"] = current_target.get("visits", 0) + 1
            visit_num = current_target["visits"]
            if multi_date_watch:
                current_target = await self._ensure_watch_target(game_url, current_target)
                watch_targets[watch_index] = current_target
                if not current_target.get("href"):
                    if visit_num % 10 == 1:
                        logger.info(
                            "[%s] [第 %d 輪/%d 次] 尚未取得區域頁，持續輪巡場次頁...",
                            current_target["keyword"][:24],
                            round_num,
                            visit_num,
                        )
                    await asyncio.sleep(watch_delay)
                    watch_index = (watch_index + 1) % len(watch_targets)
                    continue

            area_url = current_target["href"]
            try:
                await self._ensure_session()
                _t0 = time.perf_counter()
                resp = await self._api_get(area_url)
                _latency = (time.perf_counter() - _t0) * 1000
                self._record_watch_hit(resp.status_code, _latency)
                self._dump_watch_stats()
                self._proxy_error_streak = 0
                consecutive_errors = 0
                loc = self._absolute_url(resp.headers.get("Location", ""))

                if resp.status_code in BLOCKED_STATUS_CODES:
                    current_target, backoff = await self._handle_watch_forbidden(
                        game_url,
                        current_target,
                        status_code=resp.status_code,
                        round_num=round_num,
                        watch_delay=watch_delay,
                        multi_date_watch=multi_date_watch,
                    )
                    watch_targets[watch_index] = current_target
                    await asyncio.sleep(backoff)
                    watch_index = (watch_index + 1) % len(watch_targets)
                    continue

                self._clear_forbidden_streak(current_target)

                # 被導向非 area 頁（登入過期等）
                if resp.status_code in (301, 302):
                    if detect_login_required("", loc):
                        logger.warning("API watch: cookie 過期，嘗試自動恢復...")
                        if not await self._refresh_session():
                            self._raise_login_expired()  # 拋出 → TG bot 通知使用者
                    elif loc and "/ticket/area/" in loc:
                        current_target["href"] = loc
                    else:
                        # 重新取得 area URL / watch targets
                        if multi_date_watch:
                            current_target["href"] = None
                            current_target = await self._refresh_watch_target(game_url, current_target)
                            watch_targets[watch_index] = current_target
                        else:
                            new_area = await self._navigate_to_area_api(game_url)
                            if new_area:
                                watch_targets[0]["href"] = new_area
                    await asyncio.sleep(watch_delay)
                    watch_index = (watch_index + 1) % len(watch_targets)
                    continue

                html = resp.text
                if detect_login_required(html, loc):
                    logger.warning("API watch: 收到登入頁，嘗試自動恢復...")
                    if not await self._refresh_session():
                        self._raise_login_expired()
                    await asyncio.sleep(watch_delay)
                    watch_index = (watch_index + 1) % len(watch_targets)
                    continue

                area_info = parse_area_list(html)
                all_available = area_info["available"]

                # 過濾黑名單區（預設身障票/站區；可在 config.yaml events[].area_blacklist_keywords 自訂）
                _skip_re = self._build_area_blacklist_re()
                if _skip_re:
                    available = [a for a in all_available if not _skip_re.search(a["text"])]
                    disabled_only = [a for a in all_available if _skip_re.search(a["text"])]
                else:
                    available = list(all_available)
                    disabled_only = []

                if not available and disabled_only:
                    if visit_num % 10 == 1:
                        prefix = f"[{current_target['text'][:24]}] " if multi_date_watch else ""
                        logger.info(
                            "%s[第 %d 輪/%d 次] 只剩身障票 (%d 區)，%.0f 秒後刷新等釋票...",
                            prefix,
                            round_num,
                            visit_num,
                            len(disabled_only),
                            interval,
                        )
                    await asyncio.sleep(watch_delay)
                    watch_index = (watch_index + 1) % len(watch_targets)
                    continue

                if available:
                    logger.info("API watch: 偵測到 %d 個可用區域！", len(available))
                    for a in available:
                        logger.info("  → %s", a["text"][:50])

                    # 選區域
                    target = None
                    area_kw = self.event.area_keyword
                    if area_kw:
                        for a in available:
                            if matches_any_keyword(a["text"], area_kw):
                                target = a
                                break
                    if not target:
                        target = available[0]

                    href = target["href"]
                    if href.startswith("/"):
                        href = f"{BASE}{href}"

                    # 記錄選中的區域資訊
                    self._selected_area_text = target["text"][:60]

                    # 結帳
                    success = await self._fill_ticket_form_api(href)
                    if success:
                        self.last_success_info = (
                            f"場次: {current_target['text'][:60]}\n"
                            f"區域: {self._selected_area_text}\n"
                            f"張數: {self.event.ticket_count}"
                        )
                        logger.info("🎉 API watch: 釋票搶票成功！\n%s", self.last_success_info)
                        return True

                    await self._notify(f"❌ 結帳失敗：{self.last_error}\n繼續監測中...")
                    logger.warning("API watch: 結帳失敗 (%s)，繼續監測...", self.last_error)
                else:
                    if visit_num % 10 == 1:
                        prefix = f"[{current_target['text'][:24]}] " if multi_date_watch else ""
                        logger.info(
                            "%s[第 %d 輪/%d 次] 尚無可用票券，持續監測中...",
                            prefix,
                            round_num,
                            visit_num,
                        )

            except SessionFailoverRequiredError:
                raise
            except LoginExpiredError:
                raise  # 讓 TG bot 處理通知
            except Exception as exc:
                if self._is_proxy_transport_error(exc):
                    await self._handle_proxy_transport_error(area_url, exc)
                    await asyncio.sleep(watch_delay)
                    watch_index = (watch_index + 1) % len(watch_targets)
                    continue
                consecutive_errors += 1
                if consecutive_errors <= 3:
                    logger.exception("[第 %d 輪] API watch 錯誤，重試...", round_num)
                elif consecutive_errors == 5:
                    logger.error("[第 %d 輪] 連續 %d 次錯誤，嘗試刷新 session...",
                                 round_num, consecutive_errors)
                    if await self._refresh_session():
                        consecutive_errors = 0
                    else:
                        self._raise_login_expired()

            await asyncio.sleep(watch_delay)
            watch_index = (watch_index + 1) % len(watch_targets)

    async def _navigate_to_area_api(self, game_url: str) -> str | None:
        """API 版：從場次頁取得 area URL"""
        try:
            area_url = await self._select_game_api(game_url)
            if not area_url:
                return None
            # 處理驗證頁
            if "/verify/" in area_url:
                area_url = await self._handle_verify_api(area_url)
            return area_url
        except (LoginExpiredError, SessionFailoverRequiredError):
            raise
        except Exception:
            logger.exception("API: 導航到區域頁失敗")
            return None

    async def _navigate_to_area_candidates_api(self, game_url: str) -> list[dict]:
        """API 版：從場次頁取得所有匹配的 area URLs。"""
        try:
            candidates = await self._select_game_candidates_api(game_url)
            results = []
            for candidate in candidates:
                area_url = candidate["href"]
                if "/verify/" in area_url:
                    area_url = await self._handle_verify_api(area_url)
                if area_url:
                    results.append({"text": candidate["text"], "href": area_url})
            return results
        except (LoginExpiredError, SessionFailoverRequiredError):
            raise
        except Exception:
            logger.exception("API: 導航到多個區域頁失敗")
            return []

    async def _build_multi_date_watch_targets(
        self, game_url: str, date_keywords: list[str]
    ) -> list[dict]:
        targets = [
            {"keyword": keyword, "text": keyword, "href": None, "visits": 0}
            for keyword in date_keywords
        ]
        for index, target in enumerate(targets):
            targets[index] = await self._refresh_watch_target(game_url, target)
            if targets[index].get("href"):
                logger.info(
                    "API watch: 場次 %s 已連到區域頁: %s",
                    targets[index]["keyword"][:24],
                    targets[index]["text"][:60],
                )
            else:
                logger.info(
                    "API watch: 場次 %s 目前尚未取得區域頁，後續會持續回場次頁重探",
                    targets[index]["keyword"][:24],
                )
        return targets

    async def _refresh_watch_target(self, game_url: str, target: dict) -> dict:
        keyword = target["keyword"]
        refreshed = {
            "keyword": keyword,
            "text": keyword,
            "href": None,
            "visits": target.get("visits", 0),
        }
        candidates = await self._select_game_candidates_api(game_url, keyword)
        if not candidates:
            return refreshed

        selected = candidates[0]
        area_url = selected["href"]
        if "/verify/" in area_url:
            area_url = await self._handle_verify_api(area_url)
        if not area_url:
            return refreshed

        refreshed["text"] = selected["text"]
        refreshed["href"] = area_url
        return refreshed

    async def _ensure_watch_target(self, game_url: str, target: dict) -> dict:
        if target.get("href"):
            return target
        return await self._refresh_watch_target(game_url, target)

    async def _fill_ticket_form(self) -> None:
        """覆寫：API 高速結帳（相容舊有流程）"""
        if self.config.browser.api_mode == "off":
            return await super()._fill_ticket_form()
        
        url = await self.page.current_url()
        success = await self._fill_ticket_form_api(url)
        if not success:
            logger.warning("API 結帳失敗，回退至瀏覽器手動模式")
            await super()._fill_ticket_form()

    async def close(self):
        """關閉 HTTP client + 瀏覽器"""
        if self._http:
            if hasattr(self._http, "close"):
                close_result = self._http.close()
                if asyncio.iscoroutine(close_result):
                    await close_result
            self._http = None
        await super().close()
