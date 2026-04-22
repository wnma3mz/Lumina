"""
lumina/digest/scheduler.py — Digest 定时调度与启动补齐。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Optional

logger = logging.getLogger("lumina")


async def maybe_backfill_reports(llm, now: Optional[dt.datetime] = None) -> None:
    """服务启动或 digest 配置恢复后补齐缺失的日报/周报/月报。"""
    from lumina.cli.utils import is_digest_enabled
    from lumina.services.digest import generate_report
    from lumina.services.digest.config import get_cfg
    from lumina.services.digest.reports import (
        find_missing_daily_report_keys,
        find_missing_monthly_report_keys,
        find_missing_weekly_report_keys,
    )

    if not is_digest_enabled():
        return

    cfg = get_cfg()
    now = now or dt.datetime.now()
    today = now.date()

    daily_missing = find_missing_daily_report_keys(now=now, notify_time=cfg.notify_time or "20:00")
    if daily_missing:
        logger.info("Backfill: missing daily reports detected: %s", daily_missing)
    for key in daily_missing:
        try:
            await generate_report(llm, "daily", key)
        except Exception as exc:
            logger.warning("Backfill: daily report %s failed: %s", key, exc)

    weekly_missing = find_missing_weekly_report_keys(today=today)
    if weekly_missing:
        logger.info("Backfill: missing weekly reports detected: %s", weekly_missing)
    for key in weekly_missing:
        try:
            await generate_report(llm, "weekly", key)
        except Exception as exc:
            logger.warning("Backfill: weekly report %s failed: %s", key, exc)

    monthly_missing = find_missing_monthly_report_keys(today=today)
    if monthly_missing:
        logger.info("Backfill: missing monthly reports detected: %s", monthly_missing)
    for key in monthly_missing:
        try:
            await generate_report(llm, "monthly", key)
        except Exception as exc:
            logger.warning("Backfill: monthly report %s failed: %s", key, exc)


class DigestScheduler:
    def __init__(
        self,
        *,
        llm,
        get_loop: Callable[[], Optional[asyncio.AbstractEventLoop]],
        digest_interval_override: Optional[int] = None,
    ) -> None:
        self._llm = llm
        self._get_loop = get_loop
        self._digest_interval_override = digest_interval_override
        self._digest_timer: Optional[threading.Timer] = None
        self._notify_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._stopped = False

    def start(self) -> None:
        self.reload(run_startup=True)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._digest_timer is not None:
                self._digest_timer.cancel()
                self._digest_timer = None
            if self._notify_timer is not None:
                self._notify_timer.cancel()
                self._notify_timer = None

    def reload(self, *, run_startup: bool = False) -> None:
        with self._lock:
            if self._stopped:
                return
            if self._digest_timer is not None:
                self._digest_timer.cancel()
                self._digest_timer = None
            if self._notify_timer is not None:
                self._notify_timer.cancel()
                self._notify_timer = None

        self._schedule_digest_timer()
        self._schedule_daily_notify()
        if run_startup:
            self._start_startup_digest_thread()

    def _digest_interval_seconds(self) -> int:
        if self._digest_interval_override:
            return int(self._digest_interval_override)
        from lumina.services.digest.config import get_cfg

        return int(get_cfg().refresh_hours * 3600)

    @staticmethod
    def _seconds_to_next_hour() -> float:
        now = time.time()
        return 3600 - (now % 3600)

    @staticmethod
    def _seconds_to_next_notify(notify_time: str) -> float:
        try:
            hour, minute = map(int, notify_time.split(":"))
        except Exception:
            return -1

        now = time.time()
        today = dt.date.today()
        target = dt.datetime(today.year, today.month, today.day, hour, minute)
        target_ts = target.timestamp()
        if target_ts <= now:
            target_ts += 86400
        return target_ts - now

    def _submit_coro(self, coro, *, label: str, wait: bool = False, timeout: Optional[float] = None) -> None:
        loop = self._get_loop()
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            if wait:
                try:
                    future.result(timeout=timeout)
                except FutureTimeoutError as exc:
                    future.cancel()
                    raise TimeoutError(f"{label} timed out after {timeout}s") from exc
                return

            def _done(done_future):
                try:
                    done_future.result()
                except FutureCancelledError:
                    return
                except Exception as exc:
                    logger.error("%s failed: %s", label, exc)

            future.add_done_callback(_done)
            return

        if wait:
            asyncio.run(coro)
            return

        def _runner():
            try:
                asyncio.run(coro)
            except Exception as exc:
                logger.error("%s failed: %s", label, exc)

        threading.Thread(target=_runner, daemon=True).start()

    def _start_startup_digest_thread(self) -> None:
        def _startup():
            from lumina.cli.utils import is_digest_enabled
            from lumina.services.digest import maybe_generate_digest

            for _ in range(300):
                if self._stopped:
                    return
                loop = self._get_loop()
                if loop and loop.is_running():
                    break
                time.sleep(0.1)
            else:
                logger.warning("Digest startup: uvicorn loop not ready, skipping")
                return

            if not is_digest_enabled():
                logger.info("Digest is disabled: skip startup generation")
                return

            async def _startup_coro():
                await maybe_generate_digest(self._llm)
                await maybe_backfill_reports(self._llm)

            try:
                self._submit_coro(
                    _startup_coro(),
                    label="Digest startup",
                    wait=True,
                    timeout=300,
                )
            except Exception as exc:
                logger.error("Digest startup failed: %s", exc)

        threading.Thread(target=_startup, daemon=True).start()

    def _schedule_digest_timer(self) -> None:
        interval = self._digest_interval_seconds()
        if interval <= 0:
            logger.warning("Digest timer: invalid interval=%s, skipping", interval)
            return

        delay = self._seconds_to_next_hour() if interval == 3600 else interval

        def _fire():
            with self._lock:
                if self._stopped:
                    return
                self._digest_timer = None

            from lumina.cli.utils import is_digest_enabled
            from lumina.services.digest import maybe_generate_digest

            if is_digest_enabled():
                self._submit_coro(
                    maybe_generate_digest(self._llm),
                    label="Digest scheduled task",
                )
            else:
                logger.debug("Digest disabled, skip scheduled task")

            self._schedule_digest_timer()

        timer = threading.Timer(delay, _fire)
        timer.daemon = True
        with self._lock:
            if self._stopped:
                return
            self._digest_timer = timer
        timer.start()
        logger.info("Digest timer started, next trigger in %.0fs (interval=%ds)", delay, interval)

    def _schedule_daily_notify(self) -> None:
        from lumina.services.digest.config import get_cfg

        notify_time = get_cfg().notify_time
        if not notify_time:
            return

        delay = self._seconds_to_next_notify(notify_time)
        if delay < 0:
            logger.warning("Daily notify: invalid notify_time %r, skipping", notify_time)
            return

        def _fire():
            with self._lock:
                if self._stopped:
                    return
                self._notify_timer = None

            from lumina.cli.utils import is_digest_enabled, notify
            from lumina.services.digest import generate_report, maybe_generate_digest
            from lumina.services.digest.config import get_cfg
            from lumina.services.digest.core import load_digest
            from lumina.services.digest.reports import daily_key, monthly_key, weekly_key

            async def _generate_and_notify():
                now = dt.datetime.now()
                today = now.date()

                if is_digest_enabled():
                    try:
                        await maybe_generate_digest(self._llm, force_full=True)
                    except Exception as exc:
                        logger.error("Daily notify: digest generation failed: %s", exc)

                    try:
                        await generate_report(self._llm, "daily", daily_key(today))
                    except Exception as exc:
                        logger.warning("Daily notify: daily report failed: %s", exc)

                    cfg = get_cfg()
                    if today.weekday() == cfg.weekly_report_day:
                        try:
                            await generate_report(self._llm, "weekly", weekly_key(today - dt.timedelta(days=1)))
                        except Exception as exc:
                            logger.warning("Daily notify: weekly report failed: %s", exc)

                    if today.day == cfg.monthly_report_day:
                        try:
                            await generate_report(self._llm, "monthly", monthly_key(today - dt.timedelta(days=1)))
                        except Exception as exc:
                            logger.warning("Daily notify: monthly report failed: %s", exc)

                digest = load_digest() or ""
                lines = [line.strip() for line in digest.splitlines() if line.strip() and not line.startswith("<!--")]
                summary = next(
                    (line.lstrip("#").strip() for line in lines if line.startswith("#")),
                    "今日日报已生成",
                )
                notify("Lumina 日报", summary[:60])

            self._submit_coro(_generate_and_notify(), label="Daily notify")
            self._schedule_daily_notify()

        timer = threading.Timer(delay, _fire)
        timer.daemon = True
        with self._lock:
            if self._stopped:
                return
            self._notify_timer = timer
        timer.start()
        fire_at = (dt.datetime.now() + dt.timedelta(seconds=delay)).strftime("%H:%M")
        logger.info("Daily notify timer started, first trigger at %s", fire_at)
