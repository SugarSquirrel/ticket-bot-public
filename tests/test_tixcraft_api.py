"""tixcraft_api cookie merge 測試"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ticket_bot.config import AppConfig, BrowserConfig, EventConfig, ProxyConfig, SessionConfig
from ticket_bot.platforms.tixcraft_api import (
    DEFAULT_API_USER_AGENT,
    SessionFailoverRequiredError,
    TixcraftApiBot,
)
from ticket_bot.platforms.tixcraft_parser import matches_any_keyword, split_match_keywords


def test_load_cookies_from_json_preserves_browser_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tixcraft_cookies.json").write_text(
        """[
  {"name": "__cflb", "value": "json-cflb"},
  {"name": "eps_sid", "value": "json-eps"},
  {"name": "_csrf", "value": ""}
]""",
        encoding="utf-8",
    )

    merged = TixcraftApiBot._load_cookies_from_json(
        {"__cflb": "browser-cflb", "BID": "browser-bid"},
        cookie_file=tmp_path / "tixcraft_cookies.json",
    )

    assert merged["__cflb"] == "browser-cflb"
    assert merged["BID"] == "browser-bid"
    assert merged["eps_sid"] == "json-eps"
    assert "_csrf" not in merged


@pytest.mark.asyncio
async def test_init_http_merges_exported_cookies_even_when_browser_cookie_count_is_high(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tixcraft_cookies.json").write_text(
        """[
  {"name": "eps_sid", "value": "json-eps"},
  {"name": "__cflb", "value": "json-cflb"}
]""",
        encoding="utf-8",
    )

    class FakePage:
        def __init__(self):
            self.synced = []

        async def get_all_cookies(self):
            return [{"name": f"cookie_{i}", "value": f"value_{i}"} for i in range(29)]

        async def set_cookies(self, cookies):
            self.synced.extend(cookies)

        async def evaluate(self, expression):
            assert expression == "navigator.userAgent"
            return "Mozilla/5.0 Test"

    class FakeAsyncSession:
        def __init__(self, **kwargs):
            self.cookies = kwargs["cookies"]

        async def head(self, url, timeout=5):
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(
        "ticket_bot.platforms.tixcraft_api.curl_requests.AsyncSession",
        FakeAsyncSession,
    )

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="ITZY", platform="tixcraft", url="https://tixcraft.com/activity/game/26_itzy")],
    )
    event = config.events[0]
    bot = TixcraftApiBot(config, event)
    bot.page = FakePage()

    await bot._init_http()

    assert bot._http.cookies["eps_sid"] == "json-eps"
    assert bot._http.cookies["__cflb"] == "json-cflb"
    assert len(bot._http.cookies) == 31
    assert {cookie["name"] for cookie in bot.page.synced} == {"eps_sid", "__cflb"}


@pytest.mark.asyncio
async def test_init_http_passes_proxy_to_api_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tixcraft_cookies.json").write_text("[]", encoding="utf-8")

    class FakePage:
        async def get_all_cookies(self):
            return [{"name": "BID", "value": "browser-bid"}]

        async def set_cookies(self, cookies):
            raise AssertionError(f"unexpected cookie sync: {cookies}")

        async def evaluate(self, expression):
            assert expression == "navigator.userAgent"
            return "Mozilla/5.0 Test"

    class FakeAsyncSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.cookies = kwargs["cookies"]

        async def head(self, url, timeout=5):
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(
        "ticket_bot.platforms.tixcraft_api.curl_requests.AsyncSession",
        FakeAsyncSession,
    )

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        proxy=ProxyConfig(enabled=True, rotate=False, servers=["http://proxy.example:3128"]),
        events=[EventConfig(name="ITZY", platform="tixcraft", url="https://tixcraft.com/activity/game/26_itzy")],
    )
    event = config.events[0]
    bot = TixcraftApiBot(config, event)
    bot.page = FakePage()

    await bot._init_http()

    assert bot._proxy_server == "http://proxy.example:3128"
    assert bot._http.kwargs["proxy"] == "http://proxy.example:3128"


@pytest.mark.asyncio
async def test_init_http_uses_session_cookie_file_when_provided(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cookies_a.json").write_text(
        """[
  {"name": "eps_sid", "value": "json-eps"}
]""",
        encoding="utf-8",
    )

    class FakePage:
        async def get_all_cookies(self):
            return [{"name": "BID", "value": "browser-bid"}]

        async def set_cookies(self, cookies):
            return None

        async def evaluate(self, expression):
            assert expression == "navigator.userAgent"
            return "Mozilla/5.0 Test"

    class FakeAsyncSession:
        def __init__(self, **kwargs):
            self.cookies = kwargs["cookies"]

    monkeypatch.setattr(
        "ticket_bot.platforms.tixcraft_api.curl_requests.AsyncSession",
        FakeAsyncSession,
    )

    sessions = [
        SessionConfig(name="帳號A", user_data_dir="./profile_a", cookie_file="./cookies_a.json"),
        SessionConfig(name="帳號B", user_data_dir="./profile_b"),
    ]
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        sessions=sessions,
        events=[EventConfig(name="ITZY", platform="tixcraft", url="https://tixcraft.com/activity/game/26_itzy")],
    )
    bot = TixcraftApiBot(config, config.events[0], session=sessions[0])
    bot.page = FakePage()

    await bot._init_http()

    assert bot._http.cookies["eps_sid"] == "json-eps"


@pytest.mark.asyncio
async def test_init_http_skips_global_cookie_file_for_multi_session_without_cookie_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tixcraft_cookies.json").write_text(
        """[
  {"name": "eps_sid", "value": "json-eps"}
]""",
        encoding="utf-8",
    )

    class FakePage:
        async def get_all_cookies(self):
            return [{"name": "BID", "value": "browser-bid"}]

        async def set_cookies(self, cookies):
            return None

        async def evaluate(self, expression):
            assert expression == "navigator.userAgent"
            return "Mozilla/5.0 Test"

    class FakeAsyncSession:
        def __init__(self, **kwargs):
            self.cookies = kwargs["cookies"]

    monkeypatch.setattr(
        "ticket_bot.platforms.tixcraft_api.curl_requests.AsyncSession",
        FakeAsyncSession,
    )

    sessions = [
        SessionConfig(name="帳號A", user_data_dir="./profile_a"),
        SessionConfig(name="帳號B", user_data_dir="./profile_b"),
    ]
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        sessions=sessions,
        events=[EventConfig(name="ITZY", platform="tixcraft", url="https://tixcraft.com/activity/game/26_itzy")],
    )
    bot = TixcraftApiBot(config, config.events[0], session=sessions[0])
    bot.page = FakePage()

    await bot._init_http()

    assert "eps_sid" not in bot._http.cookies


@pytest.mark.asyncio
async def test_init_http_closes_existing_client_before_replacing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tixcraft_cookies.json").write_text("[]", encoding="utf-8")

    class FakePage:
        async def get_all_cookies(self):
            return [{"name": "BID", "value": "browser-bid"}]

        async def set_cookies(self, cookies):
            return None

        async def evaluate(self, expression):
            assert expression == "navigator.userAgent"
            return "Mozilla/5.0 Test"

    class ExistingClient:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    class FakeAsyncSession:
        def __init__(self, **kwargs):
            self.cookies = kwargs["cookies"]

        async def head(self, url, timeout=5):
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(
        "ticket_bot.platforms.tixcraft_api.curl_requests.AsyncSession",
        FakeAsyncSession,
    )

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="ITZY", platform="tixcraft", url="https://tixcraft.com/activity/game/26_itzy")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot.page = FakePage()
    old_client = ExistingClient()
    bot._http = old_client

    await bot._init_http()

    assert old_client.closed is True
    assert isinstance(bot._http, FakeAsyncSession)


@pytest.mark.asyncio
async def test_init_http_awaits_async_close_when_close_returns_coroutine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tixcraft_cookies.json").write_text("[]", encoding="utf-8")

    class FakePage:
        async def get_all_cookies(self):
            return [{"name": "BID", "value": "browser-bid"}]

        async def set_cookies(self, cookies):
            return None

        async def evaluate(self, expression):
            assert expression == "navigator.userAgent"
            return "Mozilla/5.0 Test"

    class ExistingClient:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class FakeAsyncSession:
        def __init__(self, **kwargs):
            self.cookies = kwargs["cookies"]

    monkeypatch.setattr(
        "ticket_bot.platforms.tixcraft_api.curl_requests.AsyncSession",
        FakeAsyncSession,
    )

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="ITZY", platform="tixcraft", url="https://tixcraft.com/activity/game/26_itzy")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot.page = FakePage()
    old_client = ExistingClient()
    bot._http = old_client

    await bot._init_http()

    assert old_client.closed is True
    assert isinstance(bot._http, FakeAsyncSession)


@pytest.mark.asyncio
async def test_wait_for_browser_session_ready_waits_for_required_cookies():
    class FakePage:
        def __init__(self):
            self.calls = 0

        async def get_all_cookies(self):
            self.calls += 1
            if self.calls < 3:
                return [{"name": "cf_clearance", "value": "abc"}]
            return [
                {"name": "cf_clearance", "value": "abc"},
                {"name": "BID", "value": "bid"},
                {"name": "_csrf", "value": "csrf"},
            ]

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="ITZY", platform="tixcraft", url="https://tixcraft.com/activity/game/26_itzy")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot.page = FakePage()

    await bot._wait_for_browser_session_ready(timeout=2.0)

    assert bot.page.calls == 3


@pytest.mark.asyncio
async def test_refresh_session_visits_challenge_page_before_language_refresh():
    class FakePage:
        def __init__(self):
            self.gotos = []
            self.sleeps = []

        async def goto(self, url):
            self.gotos.append(url)

        async def sleep(self, seconds):
            self.sleeps.append(seconds)

        async def get_all_cookies(self):
            return [
                {"name": "BID", "value": "bid"},
                {"name": "_csrf", "value": "csrf"},
            ]

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="EXO", platform="tixcraft", url="https://tixcraft.com/activity/game/26_exotp")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot.page = FakePage()

    async def fake_current_url():
        return "https://tixcraft.com/activity/game/26_exotp"

    async def fake_init_http():
        return None

    bot._current_url = fake_current_url
    bot._init_http = fake_init_http

    refreshed = await bot._refresh_session("https://tixcraft.com/activity/game/26_exotp")

    assert refreshed is True
    assert bot.page.gotos == [
        "https://tixcraft.com/activity/game/26_exotp",
        "https://tixcraft.com/user/changeLanguage/lang/zh_tw",
    ]
    assert bot.page.sleeps == [3, 2]


@pytest.mark.asyncio
async def test_ensure_session_skips_recent_check():
    class FakeHttp:
        def __init__(self):
            self.calls = 0

        async def get(self, url, timeout=5, allow_redirects=False):
            self.calls += 1
            return SimpleNamespace(status_code=200, headers={})

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="ITZY", platform="tixcraft", url="https://tixcraft.com/activity/game/26_itzy")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot._http = FakeHttp()
    bot._session_check_interval = 999
    bot._last_session_check_at = asyncio.get_running_loop().time()

    await bot._ensure_session()

    assert bot._http.calls == 0


@pytest.mark.asyncio
async def test_ensure_session_force_bypasses_recent_check():
    class FakeHttp:
        def __init__(self):
            self.calls = 0

        async def get(self, url, timeout=5, allow_redirects=False):
            self.calls += 1
            return SimpleNamespace(status_code=200, headers={})

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="ITZY", platform="tixcraft", url="https://tixcraft.com/activity/game/26_itzy")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot._http = FakeHttp()
    bot._session_check_interval = 999
    bot._last_session_check_at = asyncio.get_running_loop().time()

    await bot._ensure_session(force=True)

    assert bot._http.calls == 1


@pytest.mark.asyncio
async def test_resolve_browser_user_agent_retries_on_navigation_reset():
    class FakePage:
        def __init__(self):
            self.calls = 0

        async def evaluate(self, expression):
            assert expression == "navigator.userAgent"
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
            return "Mozilla/5.0 Tokyo"

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="ITZY", platform="tixcraft", url="https://tixcraft.com/activity/game/26_itzy")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot.page = FakePage()

    ua = await bot._resolve_browser_user_agent(retries=4, delay=0)

    assert ua == "Mozilla/5.0 Tokyo"
    assert bot.page.calls == 3


def test_split_match_keywords_supports_multiple_separators():
    assert split_match_keywords("2026/09/11|2026/09/12, A區；B區") == [
        "2026/09/11",
        "2026/09/12",
        "A區",
        "B區",
    ]


def test_watch_sleep_seconds_distributes_multi_date_targets():
    assert TixcraftApiBot._watch_sleep_seconds(3.0, 1) == pytest.approx(3.0)
    assert TixcraftApiBot._watch_sleep_seconds(3.0, 2) == pytest.approx(1.5)
    assert TixcraftApiBot._watch_sleep_seconds(1.0, 3) == pytest.approx(0.5)


def test_forbidden_backoff_never_shorter_than_watch_delay(monkeypatch):
    monkeypatch.setattr("ticket_bot.platforms.tixcraft_api.random.uniform", lambda a, b: 0.0)

    assert TixcraftApiBot._forbidden_backoff_seconds(1, 1.5) == pytest.approx(1.5)
    assert TixcraftApiBot._forbidden_backoff_seconds(3, 1.5) == pytest.approx(2.0)
    assert TixcraftApiBot._forbidden_backoff_seconds(6, 1.5) == pytest.approx(8.0)


def test_build_submit_timing_computes_expected_durations():
    timing = TixcraftApiBot._build_submit_timing(
        ticket_url="https://tixcraft.com/ticket/ticket/26_ive/22286/1/1",
        ticket_started_at=10.0,
        ticket_page_loaded_at=10.25,
        captcha_solved_at=11.0,
        post_started_at=11.1,
        post_completed_at=11.45,
    )

    assert timing == {
        "ticket_url": "https://tixcraft.com/ticket/ticket/26_ive/22286/1/1",
        "ticket_page_get_ms": 250.0,
        "captcha_solve_ms": 750.0,
        "ticket_entry_to_submit_ms": 1100.0,
        "submit_post_ms": 350.0,
        "ticket_entry_to_post_response_ms": 1450.0,
    }


def test_matches_any_keyword_matches_any_date():
    assert matches_any_keyword("2026/09/12 (Sat.) 18:00 IVE", "2026/09/11|2026/09/12") is True
    assert matches_any_keyword("2026/09/13 (Sun.) 18:00 IVE", "2026/09/11|2026/09/12") is False


def test_matches_any_keyword_quoted_preserves_comma():
    # Quoted JSON format protects commas inside prices (ticket_hunter style)
    assert matches_any_keyword("搖滾區 NT$7,880 售完", '"NT$7,880"') is True
    assert matches_any_keyword("搖滾區 NT$8,800 售完", '"NT$7,880"') is False


def test_matches_any_keyword_quoted_or_across_groups():
    text = "VIP A1 NT$8,800"
    assert matches_any_keyword(text, '"NT$7,880","NT$8,800"') is True
    assert matches_any_keyword("NT$5,000", '"NT$7,880","NT$8,800"') is False


def test_matches_any_keyword_quoted_and_within_group():
    # Space inside a quoted string = AND (all tokens must appear)
    assert matches_any_keyword("VIP 1F 搖滾區", '"VIP 1F"') is True
    assert matches_any_keyword("VIP 2F 搖滾區", '"VIP 1F"') is False
    assert matches_any_keyword("1F 看台區", '"VIP 1F"') is False


def test_matches_any_keyword_quoted_empty_matches_all():
    assert matches_any_keyword("anything", '""') is True


def test_matches_any_keyword_falls_back_to_legacy_when_unquoted():
    assert matches_any_keyword("VIP 搖滾區", "VIP|搖滾") is True
    assert matches_any_keyword("看台區", "VIP|搖滾") is False


@pytest.mark.asyncio
async def test_select_game_api_supports_multi_date_keywords():
    html = """
    <table id="gameList"><tbody>
      <tr>
        <td>2026/09/11 (Fri.) 19:00</td>
        <td>IVE WORLD TOUR</td>
        <td></td>
        <td><button data-href="/ticket/area/26_ive/22286"></button></td>
      </tr>
      <tr>
        <td>2026/09/12 (Sat.) 18:00</td>
        <td>IVE WORLD TOUR</td>
        <td></td>
        <td><button data-href="/ticket/area/26_ive/22287"></button></td>
      </tr>
    </tbody></table>
    """

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[
            EventConfig(
                name="IVE",
                platform="tixcraft",
                url="https://tixcraft.com/activity/game/26_ive",
                date_keyword="2026/09/12|2026/09/13",
            )
        ],
    )
    bot = TixcraftApiBot(config, config.events[0])

    async def fake_api_get(url, follow_redirects=True):
        return SimpleNamespace(text=html)

    bot._api_get = fake_api_get

    area_url = await bot._select_game_api("https://tixcraft.com/activity/game/26_ive")

    assert area_url == "https://tixcraft.com/ticket/area/26_ive/22287"


@pytest.mark.asyncio
async def test_select_game_candidates_api_can_filter_single_keyword():
    html = """
    <table id="gameList"><tbody>
      <tr>
        <td>2026/09/11 (Fri.) 19:00</td>
        <td>IVE WORLD TOUR</td>
        <td></td>
        <td><button data-href="/ticket/area/26_ive/22286"></button></td>
      </tr>
      <tr>
        <td>2026/09/12 (Sat.) 18:00</td>
        <td>IVE WORLD TOUR</td>
        <td></td>
        <td><button data-href="/ticket/area/26_ive/22287"></button></td>
      </tr>
    </tbody></table>
    """

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[
            EventConfig(
                name="IVE",
                platform="tixcraft",
                url="https://tixcraft.com/activity/game/26_ive",
                date_keyword="2026/09/11|2026/09/12",
            )
        ],
    )
    bot = TixcraftApiBot(config, config.events[0])

    async def fake_api_get(url, follow_redirects=True):
        return SimpleNamespace(text=html)

    bot._api_get = fake_api_get

    candidates = await bot._select_game_candidates_api(
        "https://tixcraft.com/activity/game/26_ive",
        "2026/09/12",
    )

    assert candidates == [
        {
            "text": "2026/09/12 (Sat.) 18:00 IVE WORLD TOUR",
            "href": "https://tixcraft.com/ticket/area/26_ive/22287",
        }
    ]


@pytest.mark.asyncio
async def test_build_multi_date_watch_targets_keeps_missing_dates():
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[
            EventConfig(
                name="IVE",
                platform="tixcraft",
                url="https://tixcraft.com/activity/game/26_ive",
                date_keyword="2026/09/11|2026/09/12",
            )
        ],
    )
    bot = TixcraftApiBot(config, config.events[0])

    async def fake_refresh(game_url, target):
        if target["keyword"] == "2026/09/11":
            return {
                "keyword": target["keyword"],
                "text": "2026/09/11 (Fri.) 19:00 IVE WORLD TOUR",
                "href": "https://tixcraft.com/ticket/area/26_ive/22286",
                "visits": target.get("visits", 0),
            }
        return {
            "keyword": target["keyword"],
            "text": target["keyword"],
            "href": None,
            "visits": target.get("visits", 0),
        }

    bot._refresh_watch_target = fake_refresh

    targets = await bot._build_multi_date_watch_targets(
        "https://tixcraft.com/activity/game/26_ive",
        ["2026/09/11", "2026/09/12"],
    )

    assert targets == [
        {
            "keyword": "2026/09/11",
            "text": "2026/09/11 (Fri.) 19:00 IVE WORLD TOUR",
            "href": "https://tixcraft.com/ticket/area/26_ive/22286",
            "visits": 0,
        },
        {
            "keyword": "2026/09/12",
            "text": "2026/09/12",
            "href": None,
            "visits": 0,
        },
    ]


@pytest.mark.asyncio
async def test_ensure_watch_target_keeps_existing_href():
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[
            EventConfig(
                name="IVE",
                platform="tixcraft",
                url="https://tixcraft.com/activity/game/26_ive",
                date_keyword="2026/09/11|2026/09/12",
            )
        ],
    )
    bot = TixcraftApiBot(config, config.events[0])
    target = {
        "keyword": "2026/09/11",
        "text": "2026/09/11 (Fri.) 19:00 IVE WORLD TOUR",
        "href": "https://tixcraft.com/ticket/area/26_ive/22286",
        "visits": 3,
    }

    async def fake_refresh(game_url, target):
        raise AssertionError("unexpected refresh")

    bot._refresh_watch_target = fake_refresh

    result = await bot._ensure_watch_target(
        "https://tixcraft.com/activity/game/26_ive",
        target,
    )

    assert result == target


@pytest.mark.asyncio
async def test_ensure_watch_target_refreshes_missing_href():
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[
            EventConfig(
                name="IVE",
                platform="tixcraft",
                url="https://tixcraft.com/activity/game/26_ive",
                date_keyword="2026/09/11|2026/09/12",
            )
        ],
    )
    bot = TixcraftApiBot(config, config.events[0])
    target = {
        "keyword": "2026/09/11",
        "text": "2026/09/11",
        "href": None,
        "visits": 0,
    }
    refreshed = {
        "keyword": "2026/09/11",
        "text": "2026/09/11 (Fri.) 19:00 IVE WORLD TOUR",
        "href": "https://tixcraft.com/ticket/area/26_ive/22286",
        "visits": 0,
    }

    async def fake_refresh(game_url, incoming_target):
        assert incoming_target == target
        return refreshed

    bot._refresh_watch_target = fake_refresh

    result = await bot._ensure_watch_target(
        "https://tixcraft.com/activity/game/26_ive",
        target,
    )

    assert result == refreshed


@pytest.mark.asyncio
async def test_handle_watch_forbidden_rebuilds_session_on_third_strike(monkeypatch):
    monkeypatch.setattr("ticket_bot.platforms.tixcraft_api.random.uniform", lambda a, b: 0.0)

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="IVE", platform="tixcraft", url="https://tixcraft.com/activity/game/26_ive")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    target = {
        "keyword": "2026/09/11",
        "text": "2026/09/11 (Fri.) 19:00 IVE WORLD TOUR",
        "href": "https://tixcraft.com/ticket/area/26_ive/22286",
        "forbidden_streak": 2,
    }
    calls: list[str] = []

    async def fake_rebuild():
        calls.append("rebuild")
        return True

    bot._rebuild_api_session = fake_rebuild

    updated, backoff = await bot._handle_watch_forbidden(
        "https://tixcraft.com/activity/game/26_ive",
        target,
        round_num=9,
        watch_delay=1.5,
        multi_date_watch=True,
    )

    assert updated["forbidden_streak"] == 3
    assert calls == ["rebuild"]
    assert backoff == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_handle_watch_forbidden_rotates_proxy_on_fifth_strike(monkeypatch):
    monkeypatch.setattr("ticket_bot.platforms.tixcraft_api.random.uniform", lambda a, b: 0.0)

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        proxy=ProxyConfig(enabled=True, rotate=False, servers=["http://proxy.example:3128"]),
        events=[EventConfig(name="IVE", platform="tixcraft", url="https://tixcraft.com/activity/game/26_ive")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    target = {
        "keyword": "2026/09/11",
        "text": "2026/09/11 (Fri.) 19:00 IVE WORLD TOUR",
        "href": "https://tixcraft.com/ticket/area/26_ive/22286",
        "forbidden_streak": 4,
    }
    calls: list[str] = []

    def fake_rotate():
        calls.append("rotate")
        return True

    async def fake_rebuild():
        calls.append("rebuild")
        return True

    bot._rotate_api_proxy = fake_rotate
    bot._rebuild_api_session = fake_rebuild

    updated, backoff = await bot._handle_watch_forbidden(
        "https://tixcraft.com/activity/game/26_ive",
        target,
        round_num=11,
        watch_delay=1.5,
        multi_date_watch=True,
    )

    assert updated["forbidden_streak"] == 5
    assert calls == ["rotate", "rebuild"]
    assert backoff == pytest.approx(8.0)


@pytest.mark.asyncio
async def test_handle_watch_forbidden_rotates_again_on_tenth_strike(monkeypatch):
    monkeypatch.setattr("ticket_bot.platforms.tixcraft_api.random.uniform", lambda a, b: 0.0)

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        proxy=ProxyConfig(enabled=True, rotate=True, servers=["http://proxy1:3128", "http://proxy2:3128"]),
        events=[EventConfig(name="IVE", platform="tixcraft", url="https://tixcraft.com/activity/game/26_ive")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    target = {
        "keyword": "2026/09/11",
        "text": "2026/09/11 (Fri.) 19:00 IVE WORLD TOUR",
        "href": "https://tixcraft.com/ticket/area/26_ive/22286",
        "forbidden_streak": 9,
    }
    calls: list[str] = []

    def fake_rotate():
        calls.append("rotate")
        return True

    async def fake_rebuild():
        calls.append("rebuild")
        return True

    bot._rotate_api_proxy = fake_rotate
    bot._rebuild_api_session = fake_rebuild

    updated, backoff = await bot._handle_watch_forbidden(
        "https://tixcraft.com/activity/game/26_ive",
        target,
        round_num=21,
        watch_delay=1.5,
        multi_date_watch=True,
    )

    assert updated["forbidden_streak"] == 10
    assert calls == ["rotate", "rebuild"]
    assert backoff == pytest.approx(8.0)


def test_rotate_api_proxy_advances_to_next_server():
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        proxy=ProxyConfig(
            enabled=True,
            rotate=True,
            servers=[
                "http://proxy1.example:10001",
                "http://proxy2.example:10002",
                "http://proxy3.example:10003",
            ],
        ),
        events=[EventConfig(name="IVE", platform="tixcraft", url="https://tixcraft.com/activity/game/26_ive")],
    )
    bot = TixcraftApiBot(config, config.events[0])

    assert bot._proxy_server == "http://proxy1.example:10001"
    assert bot._rotate_api_proxy() is True
    assert bot._proxy_server == "http://proxy2.example:10002"
    assert bot._rotate_api_proxy() is True
    assert bot._proxy_server == "http://proxy3.example:10003"


@pytest.mark.asyncio
async def test_handle_game_page_blocked_rotates_again_on_tenth_strike():
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        proxy=ProxyConfig(enabled=True, rotate=True, servers=["http://proxy1:3128", "http://proxy2:3128"]),
        events=[EventConfig(name="IVE", platform="tixcraft", url="https://tixcraft.com/activity/game/26_ive")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot._game_block_streak = 9
    calls: list[str] = []

    def fake_rotate():
        calls.append("rotate")
        return True

    async def fake_rebuild():
        calls.append("rebuild")
        return True

    bot._rotate_api_proxy = fake_rotate
    bot._rebuild_api_session = fake_rebuild

    await bot._handle_game_page_blocked("https://tixcraft.com/activity/game/26_ive", 403)

    assert bot._game_block_streak == 10
    assert calls == ["rotate", "rebuild"]


@pytest.mark.asyncio
async def test_handle_game_page_blocked_raises_session_failover_when_enabled():
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="IVE", platform="tixcraft", url="https://tixcraft.com/activity/game/26_ive")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot.enable_session_failover(True, block_streak=2)
    bot._game_block_streak = 1

    with pytest.raises(SessionFailoverRequiredError, match="切換到下一組 session"):
        await bot._handle_game_page_blocked("https://tixcraft.com/activity/game/26_ive", 403)


@pytest.mark.asyncio
async def test_handle_game_page_blocked_refreshes_identify_via_game_page():
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="EXO", platform="tixcraft", url="https://tixcraft.com/activity/game/26_exotp")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot._game_block_streak = 6
    calls = []

    async def fake_refresh_session(challenge_url=""):
        calls.append(challenge_url)
        return True

    bot._refresh_session = fake_refresh_session

    await bot._handle_game_page_blocked("https://tixcraft.com/activity/game/26_exotp", 401)

    assert bot._game_block_streak == 7
    assert calls == ["https://tixcraft.com/activity/game/26_exotp"]


@pytest.mark.asyncio
async def test_handle_watch_forbidden_treats_401_as_blocked(monkeypatch):
    monkeypatch.setattr("ticket_bot.platforms.tixcraft_api.random.uniform", lambda a, b: 0.0)

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="IVE", platform="tixcraft", url="https://tixcraft.com/activity/game/26_ive")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    target = {
        "keyword": "2026/09/11",
        "text": "2026/09/11 (Fri.) 19:00 IVE WORLD TOUR",
        "href": "https://tixcraft.com/ticket/area/26_ive/22286",
        "forbidden_streak": 2,
    }
    calls: list[str] = []

    async def fake_rebuild():
        calls.append("rebuild")
        return True

    bot._rebuild_api_session = fake_rebuild

    updated, backoff = await bot._handle_watch_forbidden(
        "https://tixcraft.com/activity/game/26_ive",
        target,
        status_code=401,
        round_num=9,
        watch_delay=1.5,
        multi_date_watch=True,
    )

    assert updated["forbidden_streak"] == 3
    assert calls == ["rebuild"]
    assert backoff == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_handle_watch_forbidden_refreshes_identify_via_target_href(monkeypatch):
    monkeypatch.setattr("ticket_bot.platforms.tixcraft_api.random.uniform", lambda a, b: 0.0)

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="EXO", platform="tixcraft", url="https://tixcraft.com/activity/game/26_exotp")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    target = {
        "keyword": "2026/05/09",
        "text": "2026/05/09 (Sat.) 19:00 EXO PLANET",
        "href": "https://tixcraft.com/ticket/area/26_exotp/12345",
        "forbidden_streak": 6,
    }
    calls = []

    async def fake_refresh_session(challenge_url=""):
        calls.append(challenge_url)
        return True

    bot._refresh_session = fake_refresh_session

    updated, backoff = await bot._handle_watch_forbidden(
        "https://tixcraft.com/activity/game/26_exotp",
        target,
        status_code=401,
        round_num=9,
        watch_delay=1.5,
        multi_date_watch=False,
    )

    assert updated["forbidden_streak"] == 7
    assert calls == ["https://tixcraft.com/ticket/area/26_exotp/12345"]
    assert backoff == pytest.approx(8.0)


@pytest.mark.asyncio
async def test_select_game_candidates_treats_401_as_blocked(monkeypatch):
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="IVE", platform="tixcraft", url="https://tixcraft.com/activity/game/26_ive")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    calls: list[int] = []

    async def fake_api_get(url, follow_redirects=False):
        return SimpleNamespace(status_code=401, text='{"response":"identify"}')

    async def fake_handle_blocked(game_url, status_code):
        calls.append(status_code)

    bot._api_get = fake_api_get
    bot._handle_game_page_blocked = fake_handle_blocked

    result = await bot._select_game_candidates_api("https://tixcraft.com/activity/game/26_ive")

    assert result == []
    assert calls == [401]


@pytest.mark.asyncio
async def test_select_game_candidates_handles_proxy_error_without_crashing():
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="IVE", platform="tixcraft", url="https://tixcraft.com/activity/game/26_ive")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    calls: list[str] = []

    async def fake_api_get(url, follow_redirects=False):
        raise RuntimeError("CONNECT tunnel failed, response 502")

    async def fake_handle_proxy_error(url, exc):
        calls.append(f"{url}|{exc}")

    bot._api_get = fake_api_get
    bot._handle_proxy_transport_error = fake_handle_proxy_error

    result = await bot._select_game_candidates_api("https://tixcraft.com/activity/game/26_ive")

    assert result == []
    assert calls == [
        "https://tixcraft.com/activity/game/26_ive|CONNECT tunnel failed, response 502"
    ]


@pytest.mark.asyncio
async def test_select_game_candidates_raises_session_failover_on_repeated_proxy_error():
    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="IVE", platform="tixcraft", url="https://tixcraft.com/activity/game/26_ive")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot.enable_session_failover(True, proxy_error_streak=2)
    bot._proxy_error_streak = 1

    async def fake_api_get(url, follow_redirects=False):
        raise RuntimeError("CONNECT tunnel failed, response 502")

    async def fake_rebuild():
        return True

    bot._api_get = fake_api_get
    bot._rebuild_api_session = fake_rebuild

    with pytest.raises(SessionFailoverRequiredError, match="proxy 連線持續異常"):
        await bot._select_game_candidates_api("https://tixcraft.com/activity/game/26_ive")


@pytest.mark.asyncio
async def test_resolve_browser_user_agent_falls_back_on_other_errors():
    class FakePage:
        async def evaluate(self, expression):
            assert expression == "navigator.userAgent"
            raise RuntimeError("page already closed")

    config = AppConfig(
        browser=BrowserConfig(engine="nodriver"),
        events=[EventConfig(name="ITZY", platform="tixcraft", url="https://tixcraft.com/activity/game/26_itzy")],
    )
    bot = TixcraftApiBot(config, config.events[0])
    bot.page = FakePage()

    ua = await bot._resolve_browser_user_agent(retries=2, delay=0)

    assert ua == DEFAULT_API_USER_AGENT
