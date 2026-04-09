"""
Lumina CLI 入口

用法：
    lumina server              # 启动 HTTP 服务（默认）
    lumina server --port 8080
    lumina pdf paper.pdf
    lumina pdf ./papers/ -o ./out
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("lumina")

# 打包时注入的版本标记：LUMINA_EDITION = "lite" | "full" | None（开发模式）
_EDITION = os.environ.get("LUMINA_EDITION")

# 用户级配置文件路径（Lite 版首次启动后写入，持久化用户填写的地址）
_USER_CONFIG_PATH = Path.home() / ".lumina" / "config.json"

# PID 文件，用于 lumina stop 定位进程
_PID_FILE = Path.home() / ".lumina" / "lumina.pid"

# Full 版内置模型：下载到用户目录，与 App 本体解耦
_MODEL_REPO_ID = "mlx-community/Qwen3.5-0.8B-4bit"
_MODEL_CACHE_DIR = Path.home() / ".lumina" / "models" / "qwen3.5-0.8b-4bit"


def _setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _resolve_config_path() -> str | None:
    """
    确定加载哪个 config.json：
      1. 用户级配置（~/.lumina/config.json）优先——Lite 向导写入后续都用这个
      2. 打包内置 config（同目录下 config.json）
      3. 开发模式：返回 None，由 get_config() 用默认路径
    """
    if _USER_CONFIG_PATH.exists():
        return str(_USER_CONFIG_PATH)
    return None


def _lite_setup_wizard():
    """
    Lite 版首次启动向导：引导用户填写外部服务地址，写入 ~/.lumina/config.json。
    """
    print("=" * 55)
    print("  Lumina Lite — 首次启动配置")
    print("=" * 55)
    print()
    print("Lite 版需要连接一个外部 LLM HTTP 服务。")
    print("请填写该服务的地址（OpenAI 兼容接口）。")
    print()

    while True:
        base_url = input("服务地址（如 http://192.168.1.10:8080/v1）: ").strip()
        if base_url:
            break
        print("地址不能为空，请重新输入。")

    api_key = input("API Key（留空则使用默认值 'lumina'）: ").strip() or "lumina"
    model   = input("模型名称（留空则使用默认值 'lumina'）: ").strip() or "lumina"

    port_str = input("本机监听端口（留空则使用默认值 31821）: ").strip()
    port = int(port_str) if port_str.isdigit() else 31821

    # 读取内置 config.json 作模板，填入用户值
    _pkg_dir = Path(__file__).parent
    with open(_pkg_dir / "config.json", "r", encoding="utf-8") as f:
        cfg_data = json.load(f)

    cfg_data["provider"]["openai"]["base_url"] = base_url
    cfg_data["provider"]["openai"]["api_key"]  = api_key
    cfg_data["provider"]["openai"]["model"]    = model
    cfg_data["port"] = port

    _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_USER_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg_data, f, indent=2, ensure_ascii=False)

    print()
    print(f"✓ 配置已保存至 {_USER_CONFIG_PATH}")
    print()


def _needs_lite_setup() -> bool:
    """Lite 版且尚未完成过配置向导。"""
    if _EDITION != "lite":
        return False
    if _USER_CONFIG_PATH.exists():
        # 已有配置，检查 base_url 是否已填写
        try:
            with open(_USER_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return not data.get("provider", {}).get("openai", {}).get("base_url", "")
        except Exception:
            return True
    return True


def _write_pid():
    """写入当前进程 PID 到文件。"""
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


def _read_pid() -> int | None:
    """读取 PID 文件，返回 PID；文件不存在或内容无效则返回 None。"""
    try:
        return int(_PID_FILE.read_text().strip())
    except Exception:
        return None


def _remove_pid():
    _PID_FILE.unlink(missing_ok=True)


def _ensure_model():
    """
    Full 版启动时检测模型是否已下载；若无则从 HuggingFace 下载。
    下载期间发系统通知提示进度，支持系统代理（HTTP_PROXY / HTTPS_PROXY）。
    下载完成后更新 LUMINA_MODEL_PATH 环境变量，让 config 读到正确路径。
    """
    if _EDITION != "full":
        return

    model_dir = _MODEL_CACHE_DIR
    # 判断是否已下载：目录存在且含模型权重文件
    if model_dir.exists() and any(model_dir.glob("*.safetensors")):
        logger.info("Model found at %s", model_dir)
        os.environ.setdefault("LUMINA_MODEL_PATH", str(model_dir))
        return

    # 模型不存在，开始下载
    print()
    print("首次启动需要下载内置模型（约 622MB），请稍候…")
    print(f"  来源：{_MODEL_REPO_ID}")
    print(f"  目标：{model_dir}")
    if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY"):
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        print(f"  代理：{proxy}")
    print()

    _notify("Lumina", f"正在下载模型，请稍候（约 622MB）…")

    try:
        from huggingface_hub import snapshot_download
        model_dir.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=_MODEL_REPO_ID,
            local_dir=str(model_dir),
            # huggingface_hub 自动读取 HTTP_PROXY / HTTPS_PROXY 环境变量
        )
    except Exception as e:
        print(f"\n模型下载失败：{e}")
        print("请检查网络连接，或设置代理后重试：")
        print("  export HTTPS_PROXY=http://127.0.0.1:7890")
        _notify("Lumina 下载失败", "模型下载失败，请检查网络后重新启动")
        sys.exit(1)

    print(f"✓ 模型下载完成：{model_dir}")
    os.environ["LUMINA_MODEL_PATH"] = str(model_dir)


def build_provider(cfg):
    if cfg.provider.type == "openai":
        from lumina.providers.openai import OpenAIProvider
        oa = cfg.provider.openai
        logger.info("Provider: OpenAI-compatible  base_url=%s  model=%s", oa.base_url, oa.model)
        return OpenAIProvider(base_url=oa.base_url, api_key=oa.api_key, model=oa.model)
    else:
        from lumina.providers.local import LocalProvider
        logger.info("Provider: Local  model_path=%s", cfg.provider.model_path)
        return LocalProvider(model_path=cfg.provider.model_path)


def cmd_server(args):
    import uvicorn
    from lumina.config import get_config
    from lumina.asr.transcriber import Transcriber
    from lumina.engine.llm import LLMEngine
    from lumina.api.server import create_app

    # Full 版：首次启动时下载模型（若尚未存在）
    _ensure_model()

    # Lite 版：首次启动时运行配置向导
    if _needs_lite_setup():
        _lite_setup_wizard()

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

    config_path = getattr(args, "config", None) or _resolve_config_path()
    cfg = get_config(config_path)
    _setup_logging(cfg.log_level)

    provider = build_provider(cfg)
    llm = LLMEngine(provider=provider, system_prompts=cfg.system_prompts)

    logger.info("Loading provider...")
    llm.load()
    logger.info("Provider ready.")

    transcriber = Transcriber(model=cfg.whisper_model or None)
    logger.info("Whisper model: %s", transcriber.model)

    # 检查端口是否已被占用
    if _is_port_in_use(cfg.host, cfg.port):
        msg = f"端口 {cfg.port} 已被占用，Lumina 可能已在运行。\n请查看菜单栏图标，或运行 lumina stop 后重试。"
        print(f"\nERROR: {msg}\n")
        _notify("Lumina 已在运行", f"端口 {cfg.port} 已被占用，请查看菜单栏图标")
        sys.exit(1)

    # 初始化 digest 配置
    from lumina import digest as _digest_mod
    _digest_mod.configure({"digest": cfg.digest} if hasattr(cfg, "digest") else {})

    fastapi_app = create_app(llm, transcriber)

    # 启动完成提示
    _print_ready_banner(cfg.host, cfg.port)

    # 写 PID 文件（供 lumina stop / lumina restart 使用）
    _write_pid()

    # 启动后台摘要任务（不阻塞服务）
    # 先跑全量（若已过期），再立即跑一次增量（捕捉启动前的新活动）
    import threading
    def _startup_digest():
        _run_digest_task(llm, changelog=False)   # 全量（超过 history_hours 才真正执行）
        _run_digest_task(llm, changelog=True)    # 增量：有新活动则立即追加，无则跳过
    threading.Thread(target=_startup_digest, daemon=True).start()

    # LUMINA_DIGEST_INTERVAL 可在环境变量里覆盖（测试用），命令行参数优先
    _env_interval = int(os.environ.get("LUMINA_DIGEST_INTERVAL", 3600))
    digest_interval = getattr(args, "digest_interval", _env_interval)
    _start_digest_timer(llm, interval=digest_interval)
    _start_daily_notify_timer()
    _start_ptt(cfg)

    if _EDITION in ("full", "lite") or getattr(args, "menubar", False):
        _run_with_menubar(fastapi_app, cfg, llm)
    else:
        try:
            uvicorn.run(fastapi_app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())
        finally:
            _remove_pid()


def _run_digest_task(llm, changelog: bool = False):
    """在独立线程里运行摘要生成（全量或增量）。"""
    import asyncio
    from lumina.digest import maybe_generate_digest, maybe_generate_changelog
    if changelog:
        asyncio.run(maybe_generate_changelog(llm))
    else:
        asyncio.run(maybe_generate_digest(llm))


def _start_digest_timer(llm, interval: int = 3600):
    """整点（或按 interval 覆盖）在后台线程生成 changelog。"""
    import threading
    import time

    def _seconds_to_next_hour():
        now = time.time()
        return 3600 - (now % 3600)

    def _loop():
        _run_digest_task(llm, changelog=True)
        # 下一次仍对齐整点
        delay = _seconds_to_next_hour() if interval == 3600 else interval
        t = threading.Timer(delay, _loop)
        t.daemon = True
        t.start()

    delay = _seconds_to_next_hour() if interval == 3600 else interval
    t = threading.Timer(delay, _loop)
    t.daemon = True
    t.start()
    logger.info("Digest timer started, next trigger in %.0fs (interval=%ds)", delay, interval)


def _start_daily_notify_timer():
    """每天在 config.digest.notify_time（默认 20:00）发送今日日报通知。"""
    import threading
    import time
    from lumina.digest.config import get_cfg

    def _seconds_to_next_notify(notify_time: str) -> float:
        try:
            hour, minute = map(int, notify_time.split(":"))
        except Exception:
            return -1  # 格式错误，禁用
        now = time.time()
        import datetime
        today = datetime.date.today()
        target = datetime.datetime(today.year, today.month, today.day, hour, minute)
        target_ts = target.timestamp()
        if target_ts <= now:
            # 今天已过，等到明天
            target_ts += 86400
        return target_ts - now

    def _fire():
        from lumina.digest.core import load_digest
        digest = load_digest() or ""
        # 取第一条日报的标题行作为通知摘要
        lines = [l.strip() for l in digest.splitlines() if l.strip() and not l.startswith("<!--")]
        summary = next((l.lstrip("#").strip() for l in lines if l.startswith("#")), "今日日报已生成")
        _notify("Lumina 日报", summary[:60])
        # 24 小时后再次触发
        t = threading.Timer(86400, _fire)
        t.daemon = True
        t.start()

    notify_time = get_cfg().notify_time
    if not notify_time:
        return  # 空字符串表示禁用
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


def _start_ptt(cfg):
    """启动 PTT 热键守护（后台 daemon 线程，随主进程退出）。"""
    from lumina.ptt import PTTDaemon
    ptt = PTTDaemon(
        base_url=f"http://127.0.0.1:{cfg.port}",
        hotkey_str=cfg.ptt.hotkey,
        language=cfg.ptt.language,
    )
    t = threading.Thread(target=ptt.run, daemon=True)
    t.start()


def _run_with_menubar(fastapi_app, cfg, llm):
    """启动 rumps 菜单栏 App，uvicorn 在后台线程运行。"""
    import threading
    import uvicorn
    import rumps

    edition_label = {"full": "Full", "lite": "Lite"}.get(_EDITION, "")
    title = f"Lumina {edition_label}".strip()

    server = uvicorn.Server(uvicorn.Config(
        fastapi_app,
        host=cfg.host,
        port=cfg.port,
        log_level=cfg.log_level.lower(),
    ))

    def _serve():
        import asyncio
        asyncio.run(server.serve())

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    # 菜单栏图标：打包后从 Resources/assets/lumina.icns 读取，开发模式从 assets/ 读
    import sys as _sys
    _icon_candidates = [
        Path(_sys._MEIPASS) / "assets" / "lumina.icns" if hasattr(_sys, "_MEIPASS") else None,
        Path(__file__).parent.parent / "assets" / "lumina.icns",
    ]
    _icon_path = next((str(p) for p in _icon_candidates if p and p.exists()), None)

    class LuminaApp(rumps.App):
        def __init__(self):
            super().__init__(title, icon=_icon_path, quit_button=None, template=False)
            self.menu = [
                rumps.MenuItem(f"打开界面", callback=self._open_ui),
                None,  # 分隔线
                rumps.MenuItem("重启服务", callback=self._restart),
                rumps.MenuItem("退出 Lumina", callback=self._quit),
            ]

        def _open_ui(self, _):
            import subprocess
            subprocess.Popen(["open", f"http://127.0.0.1:{cfg.port}"])

        def _restart(self, _):
            server.should_exit = True
            t.join(timeout=5)
            _remove_pid()
            import subprocess, sys
            subprocess.Popen([sys.executable] + sys.argv)
            rumps.quit_application()

        def _quit(self, _):
            server.should_exit = True
            t.join(timeout=5)
            _remove_pid()
            rumps.quit_application()

    try:
        app = LuminaApp()
        app.run()
    finally:
        server.should_exit = True
        _remove_pid()


def _get_lan_ip() -> str | None:
    """获取本机局域网 IP（非回环地址）。"""
    import socket
    try:
        # 连接一个外部地址（不会真正发包），获取本机出口 IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return None


def _notify(title: str, message: str):
    """发送 macOS 系统通知（仅在 App 打包模式下）。"""
    if _EDITION not in ("full", "lite"):
        return
    import subprocess
    script = (
        f'display notification "{message}" '
        f'with title "{title}" '
        f'sound name "default"'
    )
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _is_port_in_use(host: str, port: int) -> bool:
    """检查指定端口是否已被占用。"""
    import socket
    # 0.0.0.0 监听时，检查 127.0.0.1 即可判断本机是否已有服务
    check_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((check_host, port)) == 0


def _print_ready_banner(host: str, port: int):
    edition_label = {"full": "Full", "lite": "Lite"}.get(_EDITION, "Dev")
    print()
    print("=" * 55)
    print(f"  Lumina {edition_label} 已就绪")
    print(f"  本机访问：http://127.0.0.1:{port}")

    # 监听 0.0.0.0 时显示局域网地址（供手机 PWA 使用）
    if host in ("0.0.0.0", ""):
        lan_ip = _get_lan_ip()
        if lan_ip:
            print(f"  局域网访问：http://{lan_ip}:{port}")
            print(f"  手机扫码或在 Safari 打开上方地址")
            print(f"  添加到主屏幕即可像 App 一样使用")

    print("=" * 55)
    print()

    _notify("Lumina 已就绪", f"服务运行于 http://127.0.0.1:{port}")


def cmd_stop(args):
    """杀死正在运行的 Lumina 服务进程。"""
    import signal
    pid = _read_pid()
    if pid is None:
        print("Lumina 未在运行（未找到 PID 文件）。")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        _remove_pid()
        print(f"已停止 Lumina（PID {pid}）。")
    except ProcessLookupError:
        print(f"进程 {pid} 不存在，清理 PID 文件。")
        _remove_pid()
    except PermissionError:
        print(f"无权限停止进程 {pid}，请用 sudo。")


def cmd_restart(args):
    """停止当前 Lumina 进程，然后重新启动服务。"""
    import signal
    pid = _read_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"已停止 Lumina（PID {pid}）。")
        except ProcessLookupError:
            pass
        _remove_pid()

    # 用相同参数重新启动
    import subprocess
    cmd = [sys.argv[0], "server"]
    print("正在重启 Lumina…")
    subprocess.Popen(cmd)


def cmd_pdf(args):
    from lumina.pdf_translate import translate_pdfs

    _setup_logging(args.log_level)
    results = translate_pdfs(
        paths=args.paths,
        output_dir=args.output,
        lang_in=args.lang_in,
        lang_out=args.lang_out,
        threads=args.threads,
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
    )
    print(f"\nTranslation complete. {len(results)} file(s) translated.")
    for mono, dual in results:
        print(f"  mono: {mono}")
        print(f"  dual: {dual}")


def cmd_polish(args):
    from lumina.text_polish import polish_text, polish_file

    _setup_logging(args.log_level)

    for path in args.paths:
        # stdin 模式
        if path == "-":
            import sys as _sys
            text = _sys.stdin.read()
            lang = args.lang or "zh"
            result = polish_text(text, language=lang, base_url=args.base_url, api_key=args.api_key)
            print(result)
            continue

        p = Path(path)
        if not p.is_file():
            logger.warning("File not found: %s", path)
            continue

        # 语言自动检测：.md 文件名含 en / README 默认英文，否则中文
        if args.lang:
            lang = args.lang
        elif p.name.lower().startswith("readme") or "-en" in p.stem.lower():
            lang = "en"
        else:
            lang = "zh"

        out_path = None
        if args.output:
            out_path = str(Path(args.output) / f"{p.stem}-polished{p.suffix}")

        result = polish_file(path=path, language=lang, base_url=args.base_url,
                              api_key=args.api_key, output=out_path)

        if args.stdout:
            print(f"\n=== {p.name} (polished) ===\n")
            print(result)
        else:
            # polish_file 已打印保存路径
            pass


def cmd_watch(args):
    from lumina.watcher import watch

    _setup_logging(args.log_level)
    watch(
        directory=args.directory,
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        lang_in=args.lang_in,
        lang_out=args.lang_out,
        threads=args.threads,
    )


def _resolve_pdf_path(path: str) -> tuple[str, bool]:
    """
    解析 PDF 路径或 URL。
    返回 (本地路径, is_tmp)，is_tmp=True 表示是需要清理的临时文件。
    URL 下载后存入持久缓存（~/.lumina/cache/pdf/），不需要清理，is_tmp=False。
    """
    if path.startswith("http://") or path.startswith("https://"):
        from lumina.pdf_translate import _download_url
        return _download_url(path), False
    return path, False


def cmd_summarize(args):
    import shutil
    from lumina.pdf_summarize import summarize_pdf

    _setup_logging(args.log_level)

    for path in args.paths:
        local_path, is_tmp = _resolve_pdf_path(path)
        p = Path(local_path)
        if not p.is_file() or p.suffix.lower() != ".pdf":
            logger.warning("Skipping non-PDF or missing file: %s", path)
            continue

        # 输出文件名基于原始输入（URL 时取 URL 中的文件名）
        stem = Path(path.split("?")[0].split("/")[-1]).stem if path.startswith("http") else p.stem

        out_path = None
        if args.output:
            out_path = str(Path(args.output) / (stem + "-summary.txt"))
        elif not args.stdout:
            out_path = str(p.parent / (stem + "-summary.txt"))

        try:
            summary = summarize_pdf(
                path=local_path,
                base_url=args.base_url,
                api_key=args.api_key,
                output=out_path,
            )
        finally:
            if is_tmp:
                shutil.rmtree(str(p.parent), ignore_errors=True)

        if args.stdout or not out_path:
            print(f"\n=== {stem}.pdf ===\n")
            print(summary)
        else:
            print(f"Summary saved: {out_path}")


def main():
    # PyInstaller 打包后 multiprocessing 子进程（resource_tracker / pool worker）
    # 会用 sys.executable 重新调用本进程，freeze_support() 在此时拦截并执行
    # 子进程逻辑，然后退出，不会走到下面的 argparse。
    import multiprocessing
    multiprocessing.freeze_support()
    # babeldoc 用 multiprocessing.Process 做字体子集化；macOS 默认 spawn 会重走
    # CLI 入口导致 argparse 报错。fork 模式直接复制父进程内存，不重走入口。
    # 放在 freeze_support() 之后、任何业务代码之前；打包版和开发版均生效。
    try:
        multiprocessing.set_start_method("fork")
    except RuntimeError:
        pass  # 已在子进程中，无法再设置（理论上不会到这里）

    parser = argparse.ArgumentParser(
        prog="lumina",
        description="Lumina — local LLM service",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # ── lumina server ─────────────────────────────────────────────────────────
    p_server = sub.add_parser("server", help="Start the HTTP service")
    p_server.add_argument("--config", default=None, help="Path to config.json")
    p_server.add_argument("--host", default=None)
    p_server.add_argument("--port", type=int, default=None)
    p_server.add_argument("--provider", default=None, choices=["local", "openai"])
    p_server.add_argument("--model-path", dest="model_path", default=None)
    p_server.add_argument("--whisper-model", dest="whisper_model", default=None)
    p_server.add_argument("--log-level", dest="log_level", default=None,
                          choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_server.add_argument("--digest-interval", dest="digest_interval", type=int, default=3600,
                          help="日报定时间隔（秒），默认 3600，测试时可改小如 30")
    p_server.add_argument("--no-menubar", dest="menubar", action="store_false",
                          help="禁用 macOS 菜单栏图标")
    p_server.set_defaults(menubar=True)
    p_server.set_defaults(func=cmd_server)

    # ── lumina stop ───────────────────────────────────────────────────────────
    p_stop = sub.add_parser("stop", help="Stop the running Lumina service")
    p_stop.set_defaults(func=cmd_stop)

    # ── lumina restart ────────────────────────────────────────────────────────
    p_restart = sub.add_parser("restart", help="Restart the Lumina service")
    p_restart.set_defaults(func=cmd_restart)

    # ── lumina pdf ────────────────────────────────────────────────────────────
    p_pdf = sub.add_parser("pdf", help="Translate PDF file(s) via pdf2zh")
    p_pdf.add_argument("paths", nargs="+", help="PDF file(s) or directory")
    p_pdf.add_argument("-o", "--output", default="./translated")
    p_pdf.add_argument("--lang-in", dest="lang_in", default="en")
    p_pdf.add_argument("--lang-out", dest="lang_out", default="zh")
    p_pdf.add_argument("-t", "--threads", type=int, default=4)
    p_pdf.add_argument("--base-url", dest="base_url", default="http://127.0.0.1:31821/v1")
    p_pdf.add_argument("--model", default="lumina")
    p_pdf.add_argument("--api-key", dest="api_key", default="lumina")
    p_pdf.add_argument("--log-level", dest="log_level", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_pdf.set_defaults(func=cmd_pdf)

    # ── lumina summarize ──────────────────────────────────────────────────────
    p_sum = sub.add_parser("summarize", help="Summarize PDF file(s)")
    p_sum.add_argument("paths", nargs="+", help="PDF file(s)")
    p_sum.add_argument("-o", "--output", default=None, help="Output directory (default: same as PDF)")
    p_sum.add_argument("--stdout", action="store_true", help="Print summary to stdout instead of file")
    p_sum.add_argument("--base-url", dest="base_url", default="http://127.0.0.1:31821")
    p_sum.add_argument("--api-key", dest="api_key", default="lumina")
    p_sum.add_argument("--log-level", dest="log_level", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_sum.set_defaults(func=cmd_summarize)

    # ── lumina watch ──────────────────────────────────────────────────────────
    p_watch = sub.add_parser("watch", help="Watch a directory and auto-translate new PDFs")
    p_watch.add_argument("directory", help="Directory to watch")
    p_watch.add_argument("--lang-in", dest="lang_in", default="en")
    p_watch.add_argument("--lang-out", dest="lang_out", default="zh")
    p_watch.add_argument("-t", "--threads", type=int, default=4)
    p_watch.add_argument("--base-url", dest="base_url", default="http://127.0.0.1:31821/v1")
    p_watch.add_argument("--model", default="lumina")
    p_watch.add_argument("--api-key", dest="api_key", default="lumina")
    p_watch.add_argument("--log-level", dest="log_level", default="INFO",
                         choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_watch.set_defaults(func=cmd_watch)

    # ── lumina polish ─────────────────────────────────────────────────────────
    p_pol = sub.add_parser("polish", help="Polish text file(s) using LLM")
    p_pol.add_argument("paths", nargs="+", help="Text/Markdown file(s), or '-' for stdin")
    p_pol.add_argument("-o", "--output", default=None, help="Output directory (default: same as input)")
    p_pol.add_argument("--lang", default=None, choices=["zh", "en"],
                       help="Language to polish (default: auto-detect by filename)")
    p_pol.add_argument("--stdout", action="store_true", help="Print result to stdout")
    p_pol.add_argument("--base-url", dest="base_url", default="http://127.0.0.1:31821")
    p_pol.add_argument("--api-key", dest="api_key", default="lumina")
    p_pol.add_argument("--log-level", dest="log_level", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_pol.set_defaults(func=cmd_polish)

    # 双击 .app 启动时没有参数，默认当 server 运行
    if len(sys.argv) == 1 and _EDITION in ("full", "lite"):
        sys.argv.append("server")

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
