"""
lumina/cli/server.py — server / stop / restart 子命令

包含：
  cmd_server          — 启动 HTTP 服务（含 digest 定时器、PTT、菜单栏）
  cmd_stop            — 停止服务
  cmd_restart         — 重启服务
  build_provider      — 根据 config 构造 Provider 实例
  _run_with_menubar   — rumps 菜单栏 App（含 LuminaApp 类）
  _start_ptt          — PTT 热键守护（含 hot reload）
  _start_digest_timer — 整点摘要定时器
  _start_daily_notify_timer — 每日定时全量日报 + 通知
  _run_digest_task    — 将 digest 协程投递到 uvicorn loop
  _ensure_quick_action_installed — 后台静默安装 Quick Action
"""
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

logger = logging.getLogger("lumina")

_EDITION = os.environ.get("LUMINA_EDITION")

# Quick Action workflow 名称列表
_QA_WORKFLOW_NAMES = [
    "用 Lumina 翻译 PDF",
    "用 Lumina 总结 PDF",
    "用 Lumina 润色文本",
    "用 Lumina 润色选中文字",
    "用 Lumina 翻译选中文字",
]


# ── Provider 工厂 ─────────────────────────────────────────────────────────────

def build_provider(cfg):
    ptype = cfg.provider.type
    if ptype == "openai":
        from lumina.providers.openai import OpenAIProvider
        oa = cfg.provider.openai
        logger.info("Provider: OpenAI-compatible  base_url=%s  model=%s", oa.base_url, oa.model)
        return OpenAIProvider(base_url=oa.base_url, api_key=oa.api_key, model=oa.model)
    elif ptype == "llama_cpp":
        from lumina.providers.llama_cpp import LlamaCppProvider
        lc = cfg.provider.llama_cpp
        logger.info("Provider: llama-cpp-python  model_path=%s  n_gpu_layers=%d",
                    lc.model_path, lc.n_gpu_layers)
        return LlamaCppProvider(
            model_path=lc.model_path,
            n_gpu_layers=lc.n_gpu_layers,
            n_ctx=lc.n_ctx,
        )
    else:
        from lumina.providers.local import LocalProvider
        logger.info("Provider: Local (mlx)  model_path=%s", cfg.provider.model_path)
        return LocalProvider(model_path=cfg.provider.model_path)


# ── Digest 定时器 ─────────────────────────────────────────────────────────────

def _run_digest_task(llm, uvicorn_loop: list = None):
    """将 digest 协程投递到 uvicorn event loop，避免 asyncio.run() 创建新 loop。"""
    from lumina.cli.utils import is_digest_enabled
    if not is_digest_enabled():
        logger.debug("Digest disabled, skip scheduled task")
        return
    import asyncio
    from lumina.digest import maybe_generate_digest
    coro = maybe_generate_digest(llm)
    loop = uvicorn_loop[0] if uvicorn_loop else None
    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        # 非阻塞：不调用 future.result()，通过 done callback 记录错误
        future.add_done_callback(
            lambda f: logger.error("Digest task failed: %s", f.exception())
            if f.exception() else None
        )
    else:
        asyncio.run(coro)


def _start_digest_timer(llm, interval: int = 3600, uvicorn_loop: list = None):
    """整点（或按 interval 覆盖）在后台线程生成摘要。"""
    import threading
    import time

    def _seconds_to_next_hour():
        now = time.time()
        return 3600 - (now % 3600)

    def _loop():
        _run_digest_task(llm, uvicorn_loop=uvicorn_loop)
        delay = _seconds_to_next_hour() if interval == 3600 else interval
        t = threading.Timer(delay, _loop)
        t.daemon = True
        t.start()

    delay = _seconds_to_next_hour() if interval == 3600 else interval
    t = threading.Timer(delay, _loop)
    t.daemon = True
    t.start()
    logger.info("Digest timer started, next trigger in %.0fs (interval=%ds)", delay, interval)


async def _maybe_backfill_reports(llm) -> None:
    """服务启动时补生成缺失的报告。

    对每种报告类型，判断「本周期的报告应该已经存在但缺失」时进行补生成：
    - 日报：今天的日报不存在，且当前时间已经过了 notify_time（说明定时器已错过）
    - 周报：本周（周一起算）已过去至少 1 天，上周的周报不存在
    - 月报：本月已过去至少 1 天，上月的月报不存在
    """
    import datetime as _dt
    from lumina.digest import generate_report
    from lumina.digest.config import get_cfg
    from lumina.digest.reports import (
        daily_key, weekly_key, monthly_key,
        load_report,
    )
    from lumina.cli.utils import is_digest_enabled

    if not is_digest_enabled():
        return

    cfg = get_cfg()
    now = _dt.datetime.now()
    today = now.date()

    # ── 日报：今天是否已过 notify_time 且报告不存在 ────────────────────────────
    try:
        notify_hour, notify_minute = map(int, (cfg.notify_time or "20:00").split(":"))
    except Exception:
        notify_hour, notify_minute = 20, 0
    notify_passed = (now.hour, now.minute) >= (notify_hour, notify_minute)
    if notify_passed and not load_report("daily", daily_key(today)):
        logger.info("Backfill: today's daily report missing, generating...")
        try:
            await generate_report(llm, "daily", daily_key(today))
        except Exception as e:
            logger.warning("Backfill: daily report failed: %s", e)

    # ── 周报：今天不是触发日（即本周已过去 >0 天），且上周周报不存在 ─────────────
    weekly_day = cfg.weekly_report_day  # 0=Mon
    days_since_report_day = (today.weekday() - weekly_day) % 7
    if days_since_report_day > 0:
        last_week = today - _dt.timedelta(days=days_since_report_day + 1)
        wk = weekly_key(last_week)
        if not load_report("weekly", wk):
            logger.info("Backfill: weekly report %s missing, generating...", wk)
            try:
                await generate_report(llm, "weekly", wk)
            except Exception as e:
                logger.warning("Backfill: weekly report failed: %s", e)

    # ── 月报：今天不是触发日（monthly_report_day），且上月月报不存在 ───────────
    monthly_day = cfg.monthly_report_day  # 1-28
    if today.day != monthly_day:
        last_month = today.replace(day=1) - _dt.timedelta(days=1)
        mk = monthly_key(last_month)
        if not load_report("monthly", mk):
            logger.info("Backfill: monthly report %s missing, generating...", mk)
            try:
                await generate_report(llm, "monthly", mk)
            except Exception as e:
                logger.warning("Backfill: monthly report failed: %s", e)


def _start_daily_notify_timer(llm, uvicorn_loop: list = None):
    """每天在 config.digest.notify_time（默认 20:00）强制全量生成日报并发送通知。"""
    import asyncio
    import threading
    import time
    from lumina.digest.config import get_cfg
    from lumina.cli.utils import is_digest_enabled, notify

    def _seconds_to_next_notify(notify_time: str) -> float:
        try:
            hour, minute = map(int, notify_time.split(":"))
        except Exception:
            return -1
        now = time.time()
        import datetime
        today = datetime.date.today()
        target = datetime.datetime(today.year, today.month, today.day, hour, minute)
        target_ts = target.timestamp()
        if target_ts <= now:
            target_ts += 86400
        return target_ts - now

    def _fire():
        if not is_digest_enabled():
            t = threading.Timer(86400, _fire)
            t.daemon = True
            t.start()
            return
        import datetime as _dt
        from lumina.digest import maybe_generate_digest, generate_report
        from lumina.digest.core import load_digest
        from lumina.digest.reports import daily_key, weekly_key, monthly_key

        async def _generate_and_notify():
            now = _dt.datetime.now()
            today = now.date()
            try:
                await maybe_generate_digest(llm, force_full=True)
            except Exception as e:
                logger.error("Daily notify: digest generation failed: %s", e)

            # 日报：每天触发
            try:
                await generate_report(llm, "daily", daily_key(today))
            except Exception as e:
                logger.warning("Daily notify: daily report failed: %s", e)

            # 周报：触发日可配置（默认周一，weekday_report_day=0）
            _cfg = get_cfg()
            if today.weekday() == _cfg.weekly_report_day:
                try:
                    last_week = today - _dt.timedelta(days=1)
                    await generate_report(llm, "weekly", weekly_key(last_week))
                except Exception as e:
                    logger.warning("Daily notify: weekly report failed: %s", e)

            # 月报：触发日可配置（默认每月 1 日，monthly_report_day=1）
            if today.day == _cfg.monthly_report_day:
                try:
                    last_month = today - _dt.timedelta(days=1)
                    await generate_report(llm, "monthly", monthly_key(last_month))
                except Exception as e:
                    logger.warning("Daily notify: monthly report failed: %s", e)

            digest = load_digest() or ""
            lines = [ln.strip() for ln in digest.splitlines()
                     if ln.strip() and not ln.startswith("<!--")]
            summary = next(
                (ln.lstrip("#").strip() for ln in lines if ln.startswith("#")),
                "今日日报已生成",
            )
            notify("Lumina 日报", summary[:60])

        loop = uvicorn_loop[0] if uvicorn_loop else None
        if loop and loop.is_running():
            # 非阻塞：通知逻辑在协程内部完成，_fire 立即返回
            asyncio.run_coroutine_threadsafe(_generate_and_notify(), loop)
        else:
            asyncio.run(_generate_and_notify())
        t = threading.Timer(86400, _fire)
        t.daemon = True
        t.start()

    notify_time = get_cfg().notify_time
    if not notify_time:
        return
    if not is_digest_enabled():
        logger.info("Digest is disabled: daily notify timer stays idle")
    delay = _seconds_to_next_notify(notify_time)
    if delay < 0:
        logger.warning("Daily notify: invalid notify_time %r, skipping", notify_time)
        return
    t = threading.Timer(delay, _fire)
    t.daemon = True
    t.start()
    import datetime
    fire_at = (datetime.datetime.now() + datetime.timedelta(seconds=delay)).strftime("%H:%M")
    logger.info("Daily notify timer started, first trigger at %s", fire_at)


# ── PTT 守护 ──────────────────────────────────────────────────────────────────

def _start_ptt(cfg, menubar_app=None):
    """启动 PTT 热键守护，并监听 config.json 变化实现热键 hot reload。"""
    if not getattr(cfg.ptt, "enabled", True):
        logger.info("PTT is disabled by config (ptt.enabled=false), skipping")
        return None

    import json
    import threading
    from lumina.ptt import PTTDaemon

    base_url = f"http://127.0.0.1:{cfg.port}"
    _current_ptt: list = []

    def _make_ptt(hotkey: str, language):
        ptt = PTTDaemon(
            base_url=base_url,
            hotkey_str=hotkey,
            language=language,
            menubar_app=menubar_app,
        )
        t = threading.Thread(target=ptt.run, daemon=True)
        t.start()
        return ptt

    _current_ptt.append(_make_ptt(cfg.ptt.hotkey, cfg.ptt.language))

    _user_cfg = Path.home() / ".lumina" / "config.json"
    _pkg_cfg  = Path(__file__).parent.parent / "config.json"
    _watch_path = _user_cfg if _user_cfg.exists() else _pkg_cfg

    def _watcher():
        import time
        last_mtime = _watch_path.stat().st_mtime if _watch_path.exists() else 0
        last_hotkey   = cfg.ptt.hotkey
        last_language = cfg.ptt.language

        while True:
            time.sleep(1)
            try:
                mtime = _watch_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime == last_mtime:
                continue
            last_mtime = mtime

            try:
                with open(_watch_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                ptt_cfg = data.get("ptt", {})
                new_hotkey   = ptt_cfg.get("hotkey", last_hotkey) or last_hotkey
                new_language = ptt_cfg.get("language", last_language) or None
            except Exception as e:
                print(f"⚠ PTT hot reload：读取配置失败，保留当前热键（{e}）", flush=True)
                continue

            if new_hotkey == last_hotkey and new_language == last_language:
                continue

            print(f"PTT 热键更新：{last_hotkey.upper()} → {new_hotkey.upper()}", flush=True)
            paused = _current_ptt[0].paused if _current_ptt else False
            if _current_ptt:
                _current_ptt[0].stop()
                _current_ptt.clear()
            new_ptt = _make_ptt(new_hotkey, new_language)
            if paused:
                new_ptt.pause()
            _current_ptt.append(new_ptt)
            last_hotkey   = new_hotkey
            last_language = new_language

    threading.Thread(target=_watcher, daemon=True).start()
    return _current_ptt


# ── Quick Action 安装检测 ──────────────────────────────────────────────────────

def _ensure_quick_action_installed() -> None:
    """检测所有 Quick Action workflow 是否已安装，缺任意一个就运行安装脚本。"""
    import subprocess
    services_dir = Path.home() / "Library" / "Services"
    all_installed = all(
        (services_dir / f"{name}.workflow").exists()
        for name in _QA_WORKFLOW_NAMES
    )
    if all_installed:
        return

    _candidates = [
        Path(sys._MEIPASS) / "scripts" / "install_quick_action.sh" if hasattr(sys, "_MEIPASS") else None,
        Path(__file__).parent.parent.parent / "scripts" / "install_quick_action.sh",
    ]
    script_path = next((p for p in _candidates if p and p.exists()), None)
    if script_path is None:
        logger.debug("Quick Action install script not found, skipping")
        return

    logger.info("Quick Action not installed, running %s", script_path)
    try:
        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Quick Action installed successfully")
        else:
            logger.warning("Quick Action install failed (rc=%d): %s",
                           result.returncode, result.stderr.strip()[:200])
    except Exception as e:
        logger.debug("Quick Action install error: %s", e)


# ── 菜单栏 App ────────────────────────────────────────────────────────────────

def _run_with_menubar(fastapi_app, cfg, llm, config_path: str | None = None):
    """启动 rumps 菜单栏 App，uvicorn 在后台线程运行。"""
    import threading
    import uvicorn
    import rumps
    from lumina.cli.utils import (
        is_digest_enabled, persist_digest_enabled, persist_ptt_enabled, persist_host,
        remove_pid, uvicorn_log_config,
    )

    edition_label = {"full": "Full", "lite": "Lite"}.get(_EDITION, "")
    title = f"Lumina {edition_label}".strip()
    server = uvicorn.Server(uvicorn.Config(
        fastapi_app,
        host=cfg.host,
        port=cfg.port,
        log_level=cfg.log_level.lower(),
        log_config=uvicorn_log_config(cfg.log_level),
    ))

    def _serve():
        import asyncio
        asyncio.run(server.serve())

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    import sys as _sys
    _icon_candidates = [
        Path(_sys._MEIPASS) / "assets" / "lumina.icns" if hasattr(_sys, "_MEIPASS") else None,
        Path(__file__).parent.parent.parent / "assets" / "lumina.icns",
    ]
    _icon_path = next((str(p) for p in _icon_candidates if p and p.exists()), None)

    class LuminaApp(rumps.App):
        def __init__(self):
            super().__init__(title, icon=_icon_path, quit_button=None, template=False)
            self._ptt_ref: list = []
            self._digest_toggle_item = rumps.MenuItem("", callback=self._toggle_digest)
            self._refresh_digest_menu_label()
            self._ptt_toggle_item = rumps.MenuItem("", callback=self._toggle_ptt)
            self._refresh_ptt_menu_label()
            self._lan_toggle_item = rumps.MenuItem("", callback=self._toggle_lan)
            self._refresh_lan_menu_label()
            self._ip_item = rumps.MenuItem("", callback=self._copy_ip)
            self._refresh_ip_label()
            self.menu = [
                rumps.MenuItem("打开界面", callback=self._open_ui),
                rumps.MenuItem("打开设置", callback=self._open_settings),
                self._ip_item,
                self._digest_toggle_item,
                self._ptt_toggle_item,
                self._lan_toggle_item,
                None,
                rumps.MenuItem("重启服务", callback=self._restart),
                rumps.MenuItem("退出 Lumina", callback=self._quit),
            ]

        def _refresh_digest_menu_label(self):
            self._digest_toggle_item.title = (
                "停止日报定时采集" if is_digest_enabled() else "开启日报定时采集"
            )

        def _toggle_digest(self, _):
            from lumina.digest.config import set_enabled
            enabled = not is_digest_enabled()
            set_enabled(enabled)
            try:
                persist_digest_enabled(enabled, config_path=config_path)
            except Exception as e:
                logger.error("Failed to persist digest toggle: %s", e)
            self._refresh_digest_menu_label()
            logger.info("Digest toggled via menubar: enabled=%s", enabled)

        def _refresh_ptt_menu_label(self):
            if not self._ptt_ref:
                self._ptt_toggle_item.title = "启用语音识别"
            else:
                ptt = self._ptt_ref[0]
                self._ptt_toggle_item.title = "恢复语音识别" if ptt.paused else "暂停语音识别"

        def _toggle_ptt(self, _):
            if not self._ptt_ref:
                try:
                    cfg.ptt.enabled = True
                    persist_ptt_enabled(True, config_path=config_path)
                except Exception as e:
                    logger.error("Failed to persist ptt toggle: %s", e)
                ptt_ref = _start_ptt(cfg, menubar_app=self)
                if ptt_ref is not None:
                    self._ptt_ref = ptt_ref
                logger.info("PTT enabled via menubar")
            else:
                ptt = self._ptt_ref[0]
                if ptt.paused:
                    ptt.resume()
                else:
                    ptt.pause()
            self._refresh_ptt_menu_label()

        def _refresh_lan_menu_label(self):
            lan_open = cfg.host == "0.0.0.0"
            self._lan_toggle_item.title = "关闭局域网访问" if lan_open else "开启局域网访问"

        def _toggle_lan(self, _):
            lan_open = cfg.host == "0.0.0.0"
            new_host = "127.0.0.1" if lan_open else "0.0.0.0"
            try:
                persist_host(new_host, config_path=config_path)
            except Exception as e:
                logger.error("Failed to persist host toggle: %s", e)
                return
            logger.info("Host toggled to %s via menubar, restarting...", new_host)
            server.should_exit = True
            t.join(timeout=5)
            remove_pid()
            import subprocess
            subprocess.Popen([sys.executable] + sys.argv)
            rumps.quit_application()

        def _get_local_ip(self) -> str:
            import socket
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("8.8.8.8", 80))
                    return s.getsockname()[0]
            except Exception:
                return "127.0.0.1"

        def _refresh_ip_label(self):
            ip = self._get_local_ip()
            self._ip_item.title = f"复制地址  {ip}:{cfg.port}"

        def _copy_ip(self, _):
            from lumina.platform_utils import clipboard_set
            ip = self._get_local_ip()
            clipboard_set(f"http://{ip}:{cfg.port}")
            self._ip_item.title = f"已复制 ✓  {ip}:{cfg.port}"
            import threading
            threading.Timer(2.0, self._refresh_ip_label).start()

        def _open_ui(self, _):
            import subprocess
            subprocess.Popen(["open", f"http://127.0.0.1:{cfg.port}"])

        def _open_settings(self, _):
            import subprocess
            subprocess.Popen(["open", f"http://127.0.0.1:{cfg.port}/#settings"])

        def _restart(self, _):
            server.should_exit = True
            t.join(timeout=5)
            remove_pid()
            import subprocess
            subprocess.Popen([sys.executable] + sys.argv)
            rumps.quit_application()

        def _quit(self, _):
            server.should_exit = True
            t.join(timeout=5)
            remove_pid()
            rumps.quit_application()

    try:
        app = LuminaApp()
        ptt_ref = _start_ptt(cfg, menubar_app=app)
        if ptt_ref is not None:
            app._ptt_ref = ptt_ref
            app._refresh_ptt_menu_label()
        logger.info("Menubar app starting event loop")
        app.run()
        logger.info("Menubar app event loop returned (user quit or crashed)")
    except Exception:
        logger.exception("Menubar app crashed")
    finally:
        server.should_exit = True
        remove_pid()


# ── 子命令处理器 ──────────────────────────────────────────────────────────────

def cmd_server(args):
    import threading
    import uvicorn
    from lumina.config import get_config
    from lumina.asr.transcriber import Transcriber
    from lumina.engine.llm import LLMEngine
    from lumina.api.server import create_app
    from lumina.cli.utils import (
        setup_logging, sync_user_config, resolve_config_path,
        is_digest_enabled, is_port_in_use, print_ready_banner,
        write_pid, remove_pid, notify, uvicorn_log_config,
    )
    from lumina.cli.setup import ensure_model, needs_lite_setup, lite_setup_wizard

    ensure_model()

    if needs_lite_setup():
        lite_setup_wizard()

    if args.provider:
        os.environ["LUMINA_PROVIDER_TYPE"] = args.provider
    if args.model_path:
        os.environ["LUMINA_MODEL_PATH"] = args.model_path
    if args.whisper_model:
        os.environ["LUMINA_WHISPER_MODEL"] = args.whisper_model
    if args.host:
        os.environ["LUMINA_HOST"] = args.host
    if args.port:
        os.environ["LUMINA_PORT"] = str(args.port)
    if args.log_level:
        os.environ["LUMINA_LOG_LEVEL"] = args.log_level

    sync_user_config()
    from lumina.cli.utils import sync_static
    sync_static()
    config_path = getattr(args, "config", None) or resolve_config_path()
    cfg = get_config(config_path)

    # 用 bundle 内置 config.json 的 system_prompts 作为默认值，
    # 用户 ~/.lumina/config.json 中有的 key 优先（已由 get_config 读入 cfg.system_prompts）
    import json as _json
    _bundle_cfg_path = Path(__file__).parent.parent / "config.json"
    try:
        _bundle_prompts: dict = _json.loads(_bundle_cfg_path.read_text(encoding="utf-8")).get("system_prompts", {})
        _bundle_prompts = {k: v for k, v in _bundle_prompts.items() if not k.startswith("_")}
    except Exception:
        _bundle_prompts = {}
    _merged = {**_bundle_prompts, **cfg.system_prompts}
    cfg.system_prompts = _merged

    from lumina import request_history as _request_history
    _request_history.configure(
        {"request_history": cfg.request_history.__dict__},
        run_startup_cleanup=True,
    )
    setup_logging(cfg.log_level)

    provider = build_provider(cfg)
    llm = LLMEngine(provider=provider, system_prompts=cfg.system_prompts)

    logger.info("Loading provider...")
    llm.load()
    logger.info("Provider ready.")

    transcriber = Transcriber(model=cfg.whisper_model or None)
    logger.info("Whisper model: %s", transcriber.model)

    # 将配置中的 ASR prompts 注入 transcriber 模块
    from lumina.asr.transcriber import set_asr_prompts as _set_asr_prompts
    _set_asr_prompts(
        zh=cfg.system_prompts.get("asr_zh", ""),
        en=cfg.system_prompts.get("asr_en", ""),
    )

    if is_port_in_use(cfg.host, cfg.port):
        msg = f"端口 {cfg.port} 已被占用，Lumina 可能已在运行。\n请查看菜单栏图标，或运行 lumina stop 后重试。"
        print(f"\nERROR: {msg}\n")
        notify("Lumina 已在运行", f"端口 {cfg.port} 已被占用，请查看菜单栏图标")
        sys.exit(1)

    from lumina import digest as _digest_mod
    _digest_mod.configure({"digest": cfg.digest} if hasattr(cfg, "digest") else {})

    _uvicorn_loop: list = []

    @asynccontextmanager
    async def _cmd_lifespan(app: FastAPI):
        """CLI 专用 lifespan：捕获 uvicorn event loop 供 threadsafe 调用。"""
        import asyncio as _asyncio
        import time as _time
        from lumina.services.pdf import PdfJobManager
        _uvicorn_loop.append(_asyncio.get_running_loop())
        app.state.server_start_time = _time.time()
        app.state.pdf_manager = PdfJobManager()
        try:
            yield
        finally:
            _request_history.shutdown()

    fastapi_app = create_app(llm, transcriber, lifespan=_cmd_lifespan)

    print_ready_banner(cfg.host, cfg.port)
    write_pid()

    async def _startup_digest_coro():
        from lumina.digest import maybe_generate_digest
        await maybe_generate_digest(llm)
        await _maybe_backfill_reports(llm)

    def _startup_digest():
        import asyncio
        import time
        for _ in range(50):
            if _uvicorn_loop:
                break
            time.sleep(0.1)
        if not _uvicorn_loop:
            logger.warning("Digest startup: uvicorn loop not ready, skipping")
            return
        loop = _uvicorn_loop[0]
        future = asyncio.run_coroutine_threadsafe(_startup_digest_coro(), loop)
        # 启动时首次生成摘要可以等待完成（阻塞 daemon 线程，不影响主线程）
        try:
            future.result(timeout=300)
        except Exception as e:
            logger.error("Digest startup failed: %s", e)

    if is_digest_enabled():
        threading.Thread(target=_startup_digest, daemon=True).start()
    else:
        logger.info("Digest is disabled: skip startup generation")

    _env_interval = int(os.environ.get("LUMINA_DIGEST_INTERVAL", 0))
    _cfg_interval = int(cfg.digest.get("refresh_hours", 1.0) * 3600)
    digest_interval = getattr(args, "digest_interval", None) or _env_interval or _cfg_interval
    _start_digest_timer(llm, interval=digest_interval, uvicorn_loop=_uvicorn_loop)
    _start_daily_notify_timer(llm, uvicorn_loop=_uvicorn_loop)

    if sys.platform == "darwin":
        threading.Thread(target=_ensure_quick_action_installed, daemon=True).start()

    if sys.platform == "darwin" and (_EDITION in ("full", "lite") or getattr(args, "menubar", False)):
        _run_with_menubar(fastapi_app, cfg, llm, config_path=config_path)
    else:
        if sys.platform == "darwin":
            _start_ptt(cfg, menubar_app=None)
        try:
            uvicorn.run(fastapi_app, host=cfg.host, port=cfg.port,
                        log_level=cfg.log_level.lower(), log_config=uvicorn_log_config(cfg.log_level))
        finally:
            remove_pid()


def cmd_stop(args):
    import signal
    from lumina.cli.utils import read_pid, remove_pid

    pid = read_pid()
    if pid is None:
        print("Lumina 未在运行（未找到 PID 文件）。")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        remove_pid()
        print(f"已停止 Lumina（PID {pid}）。")
    except ProcessLookupError:
        print(f"进程 {pid} 不存在，清理 PID 文件。")
        remove_pid()
    except PermissionError:
        print(f"无权限停止进程 {pid}，请用 sudo。")


def cmd_restart(args):
    import signal
    import subprocess
    from lumina.cli.utils import read_pid, remove_pid

    pid = read_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"已停止 Lumina（PID {pid}）。")
        except ProcessLookupError:
            pass
        remove_pid()

    cmd = [sys.argv[0], "server"]
    print("正在重启 Lumina…")
    subprocess.Popen(cmd)
