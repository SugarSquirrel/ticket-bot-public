"""Click CLI 介面"""

from __future__ import annotations

import asyncio
import logging

import click

from ticket_bot.config import load_config

logger = logging.getLogger(__name__)


def _create_platform_bot(cfg, ev, session, use_api: bool = False):
    if ev.platform == "kktix":
        from ticket_bot.platforms.kktix import KKTIXBot

        return KKTIXBot(cfg, ev, session=session)

    if use_api:
        from ticket_bot.platforms.tixcraft_api import TixcraftApiBot

        return TixcraftApiBot(cfg, ev, session=session)

    from ticket_bot.platforms.tixcraft import TixcraftBot

    return TixcraftBot(cfg, ev, session=session)


async def _notify_all(cfg, event_name: str, url: str, status: str, platform: str = "tixcraft") -> None:
    """根據設定發送所有啟用的通知"""
    if cfg.notifications.telegram.enabled and cfg.notifications.telegram.bot_token:
        from ticket_bot.notifications.telegram import send_telegram

        await send_telegram(
            bot_token=cfg.notifications.telegram.bot_token,
            chat_id=cfg.notifications.telegram.chat_id,
            event_name=event_name,
            url=url,
            status=status,
        )

    if cfg.notifications.discord.enabled and cfg.notifications.discord.webhook_url:
        from ticket_bot.notifications.discord import send_discord

        await send_discord(
            webhook_url=cfg.notifications.discord.webhook_url,
            event_name=event_name,
            url=url,
            status=status,
            platform=platform,
        )


async def _run_single_session(cfg, ev, session, dry_run: bool, use_api: bool = False) -> bool:
    """單一 session 的搶票流程"""
    bot = _create_platform_bot(cfg, ev, session, use_api=use_api)
    label = session.name
    try:
        if dry_run:
            await bot.start_browser()
            if hasattr(bot, "pre_warm"):
                await bot.pre_warm()
            elif hasattr(bot, "open_registration_page"):
                await bot.open_registration_page()
            click.echo(f"[{label}] Dry-run 完成，瀏覽器已預熱")
            return False
        else:
            success = await bot.run()
            if success:
                click.echo(f"[{label}] 搶票成功！瀏覽器保持開啟，請完成付款。")
                click.echo("瀏覽器將保持開啟 10 分鐘，完成付款後可按 Ctrl+C 結束。")
                await _notify_all(cfg, ev.name, ev.url, f"搶票成功 (session: {label})", platform=ev.platform)
                # 保持瀏覽器開啟 10 分鐘讓使用者完成付款
                await asyncio.sleep(600)
            else:
                click.echo(f"[{label}] 搶票失敗")
            return success
    except Exception:
        logger.exception("[%s] session 發生錯誤", label)
        return False
    finally:
        await bot.close()


def _plan_watch_targets(targets, sessions, parallel: bool):
    """規劃 watch 任務的事件/session 對應。

    原則：
    - 單活動且未開 parallel：沿用第一個 session
    - 多活動且未開 parallel：每個活動分配一個獨立 session
    - 單活動且開 parallel：該活動使用全部 sessions
    - 多活動且開 parallel：將 sessions round-robin 分配到各活動，避免同一 session 同時撞多活動
    """
    if not targets:
        return []
    if not sessions:
        raise click.ClickException("找不到可用 sessions")

    if len(targets) == 1 and not parallel:
        plan = [(targets[0], [sessions[0]])]
    elif not parallel:
        if len(sessions) < len(targets):
            raise click.ClickException(
                "多活動並行監測至少需要與活動數相同的 sessions，且每個 session 必須使用不同的 user_data_dir"
            )
        plan = [(ev, [sessions[idx]]) for idx, ev in enumerate(targets)]
    elif len(targets) == 1:
        plan = [(targets[0], list(sessions))]
    else:
        if len(sessions) < len(targets):
            raise click.ClickException(
                "多活動 + 多帳號並行監測至少需要與活動數相同的 sessions"
            )
        buckets = [[] for _ in targets]
        for idx, sess in enumerate(sessions):
            buckets[idx % len(targets)].append(sess)
        plan = [(ev, buckets[idx]) for idx, ev in enumerate(targets)]

    used_profiles: dict[str, str] = {}
    for ev, assigned in plan:
        for sess in assigned:
            profile = (sess.user_data_dir or "").strip()
            if not profile:
                raise click.ClickException(f"session {sess.name} 缺少 user_data_dir，無法並行監測")
            current = f"{ev.name}/{sess.name}"
            previous = used_profiles.get(profile)
            if previous:
                raise click.ClickException(
                    f"並行監測需要每個 session 使用不同的 user_data_dir：{profile} 同時被 {previous} 與 {current} 使用"
                )
            used_profiles[profile] = current

    return plan


def _watch_session_sequence(targets, watch_plan, sessions, parallel: bool):
    """單活動非 parallel 時，允許用多組 sessions 做順序 failover。"""
    if parallel or not watch_plan:
        return []
    if len(targets) == 1 and len(watch_plan) == 1 and len(sessions) > 1:
        return list(sessions)
    if len(watch_plan) == 1:
        return list(watch_plan[0][1])
    return []


async def _watch_with_session(cfg, ev, session, interval: float) -> bool:
    """單一 event + session 的 watch 任務。"""
    use_api = ev.platform == "tixcraft" and cfg.browser.api_mode != "off"
    bot = _create_platform_bot(cfg, ev, session, use_api=use_api)
    success = False
    try:
        success = await bot.watch(interval=interval)
        if success:
            ticket_info = getattr(bot, "last_success_info", "") or ""
            click.echo(f"[{ev.name}][{session.name}] 搶票成功！瀏覽器保持開啟，請在 15 分鐘內完成付款。")
            if ticket_info:
                click.echo(ticket_info)
            status_msg = (
                f"🎉 釋票搶票成功 (session: {session.name})\n{ticket_info}"
                if ticket_info else
                f"釋票搶票成功 (session: {session.name})"
            )
            await _notify_all(cfg, ev.name, ev.url, status_msg, platform=ev.platform)
            if ev.platform == "kktix":
                click.echo(f"[{ev.name}][{session.name}] KKTIX 已停在 Confirm Form 前，瀏覽器保持開啟 10 分鐘。")
            await asyncio.sleep(600)
        return success
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[%s][%s] watch 發生錯誤", ev.name, session.name)
        return False
    finally:
        if not success:
            await bot.close()


async def _watch_event_parallel(cfg, ev, assigned_sessions, interval: float) -> bool:
    """單一活動，多 session 並行監測；任一成功即可取消其餘。"""
    date_text = ev.date_keyword or "第一個可用"
    if len(assigned_sessions) == 1:
        sess = assigned_sessions[0]
        click.echo(f"監測釋票: {ev.name} (日期: {date_text}, session: {sess.name})")
        success = await _watch_with_session(cfg, ev, sess, interval)
        if not success:
            await _notify_all(cfg, ev.name, ev.url, f"釋票搶票失敗 (session: {sess.name})", platform=ev.platform)
        return success

    click.echo(f"監測釋票: {ev.name} (日期: {date_text}, {len(assigned_sessions)} 個 sessions 並行)")
    click.echo(f"刷新間隔: {interval} 秒，偵測到票後自動搶票")
    tasks = [
        asyncio.create_task(_watch_with_session(cfg, ev, sess, interval))
        for sess in assigned_sessions
    ]
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    continue
                except Exception:
                    logger.exception("[%s] 並行 watch task 發生錯誤", ev.name)
                    result = False
                if result:
                    for other in pending:
                        other.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    return True
        await _notify_all(cfg, ev.name, ev.url, "釋票搶票失敗（全部 sessions）", platform=ev.platform)
        return False
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


@click.group()
@click.option("--config", "config_path", default="config.yaml", help="設定檔路徑")
@click.option("--verbose", "-v", is_flag=True, help="詳細日誌輸出")
@click.pass_context
def cli(ctx, config_path, verbose):
    """Ticket Bot CLI"""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@cli.command(name="list")
@click.pass_context
def list_games(ctx):
    """列出所有可用場次"""
    cfg = load_config(ctx.obj["config_path"])

    async def _list():
        from ticket_bot.browser import create_engine

        engine = create_engine(cfg.browser.engine)
        await engine.launch(
            headless=True,
            user_data_dir=cfg.browser.user_data_dir,
            executable_path=cfg.browser.executable_path,
            lang=cfg.browser.lang,
        )

        for ev in cfg.events:
            if ev.platform != "tixcraft":
                continue
            url = ev.url
            if "/activity/detail/" in url:
                game_name = url.rstrip("/").split("/")[-1]
                url = f"https://tixcraft.com/activity/game/{game_name}"

            page = await engine.new_page(url)
            await page.sleep(2)

            rows = await page.evaluate("""
                (() => {
                    const rows = document.querySelectorAll('#gameList > table > tbody > tr');
                    return Array.from(rows).map(row => {
                        const cells = row.querySelectorAll('td');
                        const btn = row.querySelector('button[data-href]');
                        const status = cells[3] ? cells[3].textContent.trim() : '';
                        return {
                            date: cells[0] ? cells[0].textContent.trim() : '',
                            name: cells[1] ? cells[1].textContent.trim() : '',
                            venue: cells[2] ? cells[2].textContent.trim() : '',
                            status: status,
                            available: btn !== null,
                        };
                    });
                })()
            """)

            click.echo(f"\n{'='*70}")
            click.echo(f"活動: {ev.name}")
            click.echo(f"{'='*70}")
            click.echo(f"{'日期時間':<30} {'對戰':<25} {'狀態'}")
            click.echo(f"{'-'*70}")
            for r in rows:
                marker = "✓" if r["available"] else "✗"
                status = "可購票" if r["available"] else r["status"][:20]
                click.echo(f" {marker} {r['date']:<28} {r['name']:<23} {status}")

        await engine.close()

    asyncio.run(_list())


@cli.command()
@click.option("--platform", type=click.Choice(["auto", "tixcraft", "kktix"]), default="auto", help="登入平台")
@click.pass_context
def login(ctx, platform):
    """開啟瀏覽器登入票務網站（tixcraft 設了 tixcraft_sid 會自動注入 cookie）"""
    cfg = load_config(ctx.obj["config_path"])
    target_platform = platform
    if target_platform == "auto":
        target_platform = cfg.events[0].platform if cfg.events else "tixcraft"
    login_url = "https://kktix.com/users/sign_in" if target_platform == "kktix" else "https://tixcraft.com/login"

    sid = ""
    user_data_dir = cfg.browser.user_data_dir
    if target_platform == "tixcraft" and cfg.sessions:
        sid = (cfg.sessions[0].tixcraft_sid or "").strip()
        user_data_dir = cfg.sessions[0].user_data_dir or user_data_dir

    async def _login():
        from ticket_bot.browser import create_engine
        from ticket_bot.platforms.tixcraft import inject_tixcraft_sid

        engine = create_engine(cfg.browser.engine)
        await engine.launch(
            headless=False,
            user_data_dir=user_data_dir,
            executable_path=cfg.browser.executable_path,
            lang=cfg.browser.lang,
        )

        if sid:
            click.echo(f"偵測到 tixcraft_sid（length={len(sid)}），用 cookie 直接登入...")
            page = await engine.new_page("https://tixcraft.com/")
            ok = await inject_tixcraft_sid(page, sid)
            if not ok:
                click.echo("注入 cookie 失敗，請手動登入。")
                await page.goto(login_url)
                click.echo("請在瀏覽器中完成登入，登入成功後回到這裡按 Enter 關閉瀏覽器...")
                await asyncio.get_event_loop().run_in_executor(None, input)
            else:
                # reload 讓 cookie 生效，驗證登入狀態
                await page.goto("https://tixcraft.com/")
                await page.sleep(1.5)
                url = await page.current_url()
                click.echo(f"目前頁面: {url}")
                click.echo("Cookie 已注入，登入狀態已寫入 chrome_profile。按 Enter 關閉瀏覽器...")
                await asyncio.get_event_loop().run_in_executor(None, input)
        else:
            page = await engine.new_page(login_url)
            click.echo(f"瀏覽器已開啟 {target_platform} 登入頁面。")
            click.echo("請在瀏覽器中完成登入，登入成功後回到這裡按 Enter 關閉瀏覽器...")
            await asyncio.get_event_loop().run_in_executor(None, input)
            url = await page.current_url()
            click.echo(f"目前頁面: {url}")

        await engine.close()
        click.echo("瀏覽器已關閉，登入狀態已儲存到 chrome_profile。")

    asyncio.run(_login())


@cli.command()
@click.option("--event", help="指定活動名稱（部分比對）")
@click.option("--date", "date_kw", default=None, help="指定場次日期關鍵字（覆蓋 config，例如 2026/06/13）")
@click.option("--area", "area_kw", default=None, help="指定區域關鍵字（覆蓋 config）")
@click.option("--count", "ticket_count", default=None, type=int, help="票數（覆蓋 config）")
@click.option("--dry-run", is_flag=True, help="測試模式，僅預熱不購買")
@click.option("--parallel", "-p", is_flag=True, help="多 session 並行搶票")
@click.option("--api", is_flag=True, help="使用獨立 API 高速結帳模式")
@click.pass_context
def run(ctx, event, date_kw, area_kw, ticket_count, dry_run, parallel, api):
    """啟動搶票（支援 tixcraft / kktix）"""
    cfg = load_config(ctx.obj["config_path"])

    targets = [e for e in cfg.events if e.platform in {"tixcraft", "kktix"}]
    if event:
        targets = [e for e in targets if event in e.name]
    if not targets:
        click.echo("找不到符合條件的活動")
        return

    # 指令行參數覆蓋 config
    for ev in targets:
        if date_kw is not None:
            ev.date_keyword = date_kw
        if area_kw is not None:
            ev.area_keyword = area_kw
        if ticket_count is not None:
            ev.ticket_count = ticket_count

    sessions = cfg.sessions

    async def _run():
        for ev in targets:
            if parallel and len(sessions) > 1:
                click.echo(f"並行搶票: {ev.name} ({len(sessions)} 個 sessions)")
                tasks = [
                    asyncio.create_task(_run_single_session(cfg, ev, sess, dry_run, use_api=api))
                    for sess in sessions
                ]
                # 任一 session 成功即可，取消其餘
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                any_success = any(t.result() for t in done if not t.cancelled())

                if any_success:
                    # 有人搶到了，取消剩餘
                    for t in pending:
                        t.cancel()
                    click.echo(f"已有 session 搶票成功，取消剩餘 {len(pending)} 個 sessions")
                else:
                    # 第一個完成但失敗了，等其餘跑完
                    if pending:
                        results = await asyncio.gather(*pending, return_exceptions=True)
                        any_success = any(r is True for r in results)

                if not any_success:
                    await _notify_all(cfg, ev.name, ev.url, "搶票失敗（全部 sessions）", platform=ev.platform)
            else:
                # 單 session 模式
                sess = sessions[0]
                click.echo(f"啟動搶票: {ev.name}")
                success = await _run_single_session(cfg, ev, sess, dry_run, use_api=api)
                if not success and not dry_run:
                    await _notify_all(cfg, ev.name, ev.url, "搶票失敗", platform=ev.platform)

    asyncio.run(_run())


@cli.command()
@click.option("--event", help="指定活動名稱（部分比對）")
@click.option("--date", "date_kw", default=None, help="指定場次日期關鍵字")
@click.option("--area", "area_kw", default=None, help="指定區域關鍵字")
@click.option("--count", "ticket_count", default=None, type=int, help="票數")
@click.option("--interval", default=5.0, type=float, help="刷新間隔秒數（預設 5 秒）")
@click.option("--parallel", "-p", is_flag=True, help="多 session 並行監測（單場用全部 sessions；多場自動分配）")
@click.pass_context
def watch(ctx, event, date_kw, area_kw, ticket_count, interval, parallel):
    """釋票監測模式 — 持續刷新，有票自動搶"""
    cfg = load_config(ctx.obj["config_path"])

    targets = [e for e in cfg.events if e.platform in {"tixcraft", "kktix"}]
    if event:
        targets = [e for e in targets if event in e.name]
    if not targets:
        click.echo("找不到符合條件的活動")
        return

    for ev in targets:
        if date_kw is not None:
            ev.date_keyword = date_kw
        if area_kw is not None:
            ev.area_keyword = area_kw
        if ticket_count is not None:
            ev.ticket_count = ticket_count

    sessions = cfg.sessions

    async def _watch():
        from ticket_bot.platforms.tixcraft_api import SessionFailoverRequiredError

        watch_plan = _plan_watch_targets(targets, sessions, parallel)

        # 保留原本的單活動單 session 行為，避免改壞既有流程。
        if len(watch_plan) == 1 and len(watch_plan[0][1]) == 1 and not parallel:
            ev, _assigned = watch_plan[0]
            watch_sessions = _watch_session_sequence(targets, watch_plan, sessions, parallel)
            round_count = 0
            session_index = 0
            while True:
                sess = watch_sessions[session_index]
                round_count += 1
                success = False
                use_api = ev.platform == "tixcraft" and cfg.browser.api_mode != "off"
                bot = _create_platform_bot(cfg, ev, sess, use_api=use_api)
                if use_api and len(watch_sessions) > 1 and hasattr(bot, "enable_session_failover"):
                    bot.enable_session_failover(True)
                try:
                    if round_count == 1:
                        click.echo(f"監測釋票: {ev.name} (日期: {ev.date_keyword or '第一個可用'})")
                        click.echo(f"刷新間隔: {interval} 秒，偵測到票後自動搶票")
                        if len(watch_sessions) > 1:
                            click.echo(f"session 輪替順序: {', '.join(s.name for s in watch_sessions)}")
                        click.echo("按 Ctrl+C 停止監測\n")

                    success = await bot.watch(interval=interval)
                    if success:
                        ticket_info = getattr(bot, "last_success_info", "") or ""
                        click.echo(f"[第 {round_count} 張] 搶票成功！瀏覽器保持開啟，請在 15 分鐘內完成付款。")
                        if ticket_info:
                            click.echo(ticket_info)
                        status_msg = f"🎉 釋票搶票成功（第 {round_count} 張）\n{ticket_info}" if ticket_info else f"釋票搶票成功（第 {round_count} 張）"
                        await _notify_all(cfg, ev.name, ev.url, status_msg, platform=ev.platform)
                        if ev.platform == "kktix":
                            click.echo("KKTIX 已停在 Confirm Form 前，瀏覽器保持開啟 10 分鐘。\n")
                            await asyncio.sleep(600)
                            return
                        click.echo("付款同時繼續搶下一張...\n")
                        continue
                    else:
                        click.echo("搶票失敗")
                        await _notify_all(cfg, ev.name, ev.url, "釋票搶票失敗", platform=ev.platform)
                        break
                except SessionFailoverRequiredError as exc:
                    if len(watch_sessions) <= 1:
                        click.echo(str(exc))
                        await asyncio.sleep(interval)
                        continue
                    next_index = (session_index + 1) % len(watch_sessions)
                    next_session = watch_sessions[next_index]
                    click.echo(f"[{ev.name}][{sess.name}] {exc}")
                    click.echo(f"[{ev.name}] 切換監測 session: {sess.name} -> {next_session.name}\n")
                    session_index = next_index
                    await asyncio.sleep(1.0)
                    continue
                except KeyboardInterrupt:
                    click.echo("\n已停止監測")
                    break
                finally:
                    if not success:
                        await bot.close()
            return

        click.echo("啟動多活動/多帳號並行監測：")
        for ev, assigned in watch_plan:
            session_names = ", ".join(sess.name for sess in assigned)
            click.echo(f"- {ev.name} | 日期: {ev.date_keyword or '第一個可用'} | sessions: {session_names}")
        click.echo(f"刷新間隔: {interval} 秒，偵測到票後自動搶票")
        click.echo("按 Ctrl+C 停止監測\n")

        tasks = [
            asyncio.create_task(_watch_event_parallel(cfg, ev, assigned, interval))
            for ev, assigned in watch_plan
        ]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for idx, result in enumerate(results):
                if isinstance(result, Exception):
                    ev = watch_plan[idx][0]
                    logger.exception("[%s] 並行監測異常結束", ev.name, exc_info=result)
        except KeyboardInterrupt:
            click.echo("\n已停止監測")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_watch())


@cli.command()
@click.option("--event", help="指定活動名稱（部分比對）")
@click.option("--parallel", "-p", is_flag=True, help="多 session 並行搶票")
@click.pass_context
def countdown(ctx, event, parallel):
    """倒數計時模式，精準開賣時啟動"""
    from datetime import datetime

    cfg = load_config(ctx.obj["config_path"])

    targets = [e for e in cfg.events if e.platform == "tixcraft" and e.sale_time]
    if event:
        targets = [e for e in targets if event in e.name]
    if not targets:
        click.echo("找不到有設定 sale_time 的 tixcraft 活動")
        return

    sessions = cfg.sessions

    async def _countdown():
        from ticket_bot.platforms.tixcraft import TixcraftBot
        from ticket_bot.utils.timer import countdown_activate
        import time as _time

        for ev in targets:
            sale_time = datetime.fromisoformat(ev.sale_time)
            click.echo(f"等待開賣: {ev.name} @ {sale_time.isoformat()}")

            # 開賣前 10 分鐘才啟動瀏覽器，避免 session 過期
            warmup_at = sale_time.timestamp() - 600  # 10 分鐘前
            now = _time.time()
            if now < warmup_at:
                wait_sec = warmup_at - now
                wait_min = wait_sec / 60
                click.echo(f"距開賣還有 {wait_min:.0f} 分鐘，開賣前 10 分鐘自動啟動瀏覽器...")
                click.echo(f"預計 {datetime.fromtimestamp(warmup_at).strftime('%H:%M:%S')} 啟動，現在可以放著不管")
                while _time.time() < warmup_at:
                    remaining = warmup_at - _time.time()
                    if remaining > 60:
                        click.echo(f"  待機中... 還有 {remaining/60:.0f} 分鐘啟動瀏覽器")
                        await asyncio.sleep(min(remaining - 60, 300))  # 每 5 分鐘或最後 1 分鐘印一次
                    else:
                        click.echo(f"  即將啟動瀏覽器... ({remaining:.0f} 秒)")
                        await asyncio.sleep(remaining)
                        break

            click.echo("啟動瀏覽器預熱...")

            # 預熱所有 sessions
            bots = []
            for sess in sessions:
                bot = TixcraftBot(cfg, ev, session=sess)
                await bot.start_browser()
                await bot.pre_warm()
                bots.append((bot, sess))
                click.echo(f"  [{sess.name}] 瀏覽器已預熱")

            click.echo("進入倒數...")

            async def _go():
                # --- 第一輪：高速搶票 ---
                first_success = False
                if parallel and len(bots) > 1:
                    click.echo(f"開搶！並行 {len(bots)} 個 sessions")
                    tasks = []
                    for bot, sess in bots:
                        async def _bot_run(b=bot, s=sess):
                            try:
                                success = await b.run()
                                if success:
                                    await _notify_all(cfg, ev.name, ev.url, f"搶票成功 (session: {s.name})")
                                return success
                            except Exception:
                                logger.exception("[%s] 搶票錯誤", s.name)
                                return False
                        tasks.append(asyncio.create_task(_bot_run()))

                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    first_success = any(t.result() for t in done if not t.cancelled())
                    if first_success:
                        for t in pending:
                            t.cancel()
                    else:
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                else:
                    bot, sess = bots[0]
                    first_success = await bot.run()
                    if first_success:
                        await _notify_all(cfg, ev.name, ev.url, "搶票成功")

                # --- 第一輪結束，自動切換 watch 釋票模式 ---
                if first_success:
                    click.echo("第一輪成功！自動切換 watch 釋票模式繼續搶...")
                else:
                    click.echo("第一輪未成功，自動切換 watch 釋票模式...")
                    await _notify_all(cfg, ev.name, ev.url, "首輪搶票未成功，已切換釋票監測模式")

                # 用第一個 session 切入 watch 模式
                round_count = 1 if first_success else 0
                while True:
                    round_count += 1
                    use_api = cfg.browser.api_mode != "off"
                    watch_bot = _create_platform_bot(cfg, ev, bots[0][1], use_api=use_api)
                    try:
                        success = await watch_bot.watch(interval=5.0)
                        if success:
                            ticket_info = getattr(watch_bot, "last_success_info", "") or ""
                            click.echo(f"[第 {round_count} 張] 釋票搶票成功！")
                            status_msg = f"🎉 釋票搶票成功（第 {round_count} 張）\n{ticket_info}" if ticket_info else f"釋票搶票成功（第 {round_count} 張）"
                            await _notify_all(cfg, ev.name, ev.url, status_msg)
                            click.echo("繼續搶下一張...\n")
                            continue
                        else:
                            click.echo("watch 搶票失敗，重試...")
                            continue
                    except KeyboardInterrupt:
                        click.echo("\n已停止")
                        break
                    finally:
                        if not success:
                            await watch_bot.close()

            await countdown_activate(sale_time, _go)

    asyncio.run(_countdown())


@cli.command(name="bot")
@click.option("--platform", "-p", type=click.Choice(["discord", "telegram", "all"]), default="all", help="啟動哪個 Bot（預設 all）")
@click.pass_context
def bot_cmd(ctx, platform):
    """啟動 Bot — 透過 Discord / Telegram 指令控制搶票"""
    config_path = ctx.obj["config_path"]

    if platform == "discord":
        from ticket_bot.discord_bot import run_bot

        click.echo("啟動 Discord Bot...")
        click.echo("在 Discord 頻道輸入 !help 查看指令")
        click.echo("按 Ctrl+C 停止\n")
        run_bot(config_path=config_path)

    elif platform == "telegram":
        from ticket_bot.telegram_bot import run_telegram_bot

        click.echo("啟動 Telegram Bot...")
        click.echo("在 Telegram 輸入 /help 查看指令")
        click.echo("按 Ctrl+C 停止\n")
        run_telegram_bot(config_path=config_path)

    else:
        # 同時啟動 Discord + Telegram
        click.echo("同時啟動 Discord + Telegram Bot...")
        click.echo("按 Ctrl+C 停止\n")

        import asyncio

        from dotenv import load_dotenv
        load_dotenv()

        import os
        import logging

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        async def _run_all():
            tasks = []

            dc_token = os.getenv("DISCORD_BOT_TOKEN", "")
            tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")

            if dc_token:
                from ticket_bot.discord_bot import create_bot
                dc_bot = create_bot(config_path=config_path)
                tasks.append(dc_bot.start(dc_token, reconnect=True))
                click.echo("  Discord Bot: 啟動中...")
            else:
                click.echo("  Discord Bot: 跳過（無 DISCORD_BOT_TOKEN）")

            if tg_token and tg_chat:
                from ticket_bot.telegram_bot import TelegramBotRunner
                from ticket_bot.gemma_client import GemmaClient
                tg_cfg = load_config(config_path)
                gemma = None
                if tg_cfg.gemma.enabled:
                    gemma = GemmaClient(tg_cfg.gemma)
                    click.echo(f"  Gemma 4: 已啟用 ({tg_cfg.gemma.backend}, {tg_cfg.gemma.model})")
                tg_runner = TelegramBotRunner(
                    token=tg_token, chat_id=tg_chat, config_path=config_path, gemma=gemma
                )
                tasks.append(tg_runner.poll())
                click.echo("  Telegram Bot: 啟動中...")
            else:
                click.echo("  Telegram Bot: 跳過（無 TELEGRAM_BOT_TOKEN 或 CHAT_ID）")

            if not tasks:
                click.echo("\n沒有可啟動的 Bot，請設定 .env")
                return

            await asyncio.gather(*tasks)

        try:
            asyncio.run(_run_all())
        except KeyboardInterrupt:
            click.echo("\nBot 已停止")


@cli.command()
@click.argument("keywords", nargs=-1, required=True)
@click.option("--interval", default=60, type=float, help="監控間隔（秒）")
@click.pass_context
def monitor(ctx, keywords, interval):
    """Ticketmaster 事件監控"""
    cfg = load_config(ctx.obj["config_path"])

    async def _monitor():
        from ticket_bot.platforms.ticketmaster import TicketmasterMonitor

        mon = TicketmasterMonitor(cfg)

        async def on_found(info):
            click.echo(f"[{info['status']}] {info['formatted']}")
            await _notify_all(
                cfg,
                event_name=info["name"],
                url=info.get("url", ""),
                status=f"狀態: {info['status']}",
                platform="ticketmaster",
            )

        click.echo(f"監控關鍵字: {', '.join(keywords)} (間隔 {interval}s)")
        await mon.monitor_keywords(list(keywords), interval=interval, on_found=on_found)

    asyncio.run(_monitor())


@cli.command()
@click.option("--dir", "collect_dir", default="", help="驗證碼收集目錄（預設從 config 讀取）")
@click.pass_context
def label(ctx, collect_dir):
    """標註收集的驗證碼圖片（訓練用）"""
    from ticket_bot.captcha.trainer import label_images

    if not collect_dir:
        cfg = load_config(ctx.obj["config_path"])
        collect_dir = cfg.captcha.collect_dir or "./captcha_samples"

    click.echo(f"開始標註驗證碼: {collect_dir}\n")
    label_images(collect_dir)


@cli.command()
@click.option("--dir", "collect_dir", default="", help="驗證碼收集目錄")
@click.option("--output", "output_dir", default="", help="訓練資料輸出目錄")
@click.pass_context
def prepare(ctx, collect_dir, output_dir):
    """將標註資料整理成訓練格式"""
    from ticket_bot.captcha.trainer import prepare_training_data

    if not collect_dir:
        cfg = load_config(ctx.obj["config_path"])
        collect_dir = cfg.captcha.collect_dir or "./captcha_samples"

    click.echo(f"準備訓練資料: {collect_dir}\n")
    prepare_training_data(collect_dir, output_dir)
