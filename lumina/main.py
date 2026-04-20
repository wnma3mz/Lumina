"""
Lumina CLI 入口

用法：
    lumina server              # 启动 HTTP 服务（默认）
    lumina server --port 8080
    lumina pdf paper.pdf
    lumina pdf ./papers/ -o ./out
"""
import argparse
import os
import sys

from lumina.config import (
    DEFAULT_API_BASE_URL,
    DEFAULT_API_BASE_URL_V1,
    DEFAULT_API_KEY,
    DEFAULT_MODEL,
)


def main():
    # PyInstaller 打包后 multiprocessing 子进程会用 sys.executable 重新调用本进程，
    # freeze_support() 在此时拦截并执行子进程逻辑，然后退出，不会走到下面的 argparse。
    import multiprocessing
    multiprocessing.freeze_support()
    # babeldoc 用 multiprocessing.Process 做字体子集化；macOS 默认 spawn 会重走
    # CLI 入口导致 argparse 报错。fork 模式直接复制父进程内存，不重走入口。
    if sys.platform == "darwin":
        try:
            multiprocessing.set_start_method("fork")
        except RuntimeError:
            pass

    from lumina.cli.server import cmd_server, cmd_stop, cmd_restart, cmd_menubar
    from lumina.cli.pdf import cmd_pdf, cmd_summarize, cmd_watch
    from lumina.cli.text import cmd_polish, cmd_popup

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
    p_server.add_argument("--provider", default=None, choices=["local", "llama_cpp", "openai"])
    p_server.add_argument("--model-path", dest="model_path", default=None)
    p_server.add_argument("--whisper-model", dest="whisper_model", default=None)
    p_server.add_argument("--log-level", dest="log_level", default=None,
                          choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_server.add_argument("--digest-interval", dest="digest_interval", type=int, default=None,
                          help="日报定时间隔（秒），默认读取 config.json refresh_hours（1h），测试时可改小如 30")
    menubar_group = p_server.add_mutually_exclusive_group()
    menubar_group.add_argument("--menubar", dest="menubar", action="store_true",
                               help="启用 macOS 菜单栏图标")
    menubar_group.add_argument("--no-menubar", dest="menubar", action="store_false",
                               help="禁用 macOS 菜单栏图标")
    p_server.set_defaults(menubar=None)
    p_server.set_defaults(func=cmd_server)

    # ── lumina stop ───────────────────────────────────────────────────────────
    p_stop = sub.add_parser("stop", help="Stop the running Lumina service")
    p_stop.set_defaults(func=cmd_stop)

    # ── lumina restart ────────────────────────────────────────────────────────
    p_restart = sub.add_parser("restart", help="Restart the Lumina service")
    restart_menubar_group = p_restart.add_mutually_exclusive_group()
    restart_menubar_group.add_argument("--menubar", dest="menubar", action="store_true",
                                       help="重启后显示 macOS 菜单栏图标")
    restart_menubar_group.add_argument("--no-menubar", dest="menubar", action="store_false",
                                       help="重启后隐藏 macOS 菜单栏图标")
    p_restart.set_defaults(menubar=None)
    p_restart.set_defaults(func=cmd_restart)

    # ── lumina menubar ────────────────────────────────────────────────────────
    p_menubar = sub.add_parser("menubar", help="Toggle macOS menubar visibility")
    p_menubar.add_argument("state", choices=["on", "off"], help="on=显示菜单栏，off=隐藏菜单栏")
    p_menubar.set_defaults(func=cmd_menubar)

    # ── lumina pdf ────────────────────────────────────────────────────────────
    p_pdf = sub.add_parser("pdf", help="Translate PDF file(s) via pdf2zh")
    p_pdf.add_argument("paths", nargs="+", help="PDF file(s) or directory")
    p_pdf.add_argument("-o", "--output", default="./translated")
    p_pdf.add_argument("--lang-in", dest="lang_in", default="en")
    p_pdf.add_argument("--lang-out", dest="lang_out", default="zh")
    p_pdf.add_argument("-t", "--threads", type=int, default=0, help="Threads for PDF translation (default: read from config)")
    p_pdf.add_argument("--base-url", dest="base_url", default=DEFAULT_API_BASE_URL_V1)
    p_pdf.add_argument("--model", default=DEFAULT_MODEL)
    p_pdf.add_argument("--api-key", dest="api_key", default=DEFAULT_API_KEY)
    p_pdf.add_argument("--log-level", dest="log_level", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_pdf.set_defaults(func=cmd_pdf)

    # ── lumina summarize ──────────────────────────────────────────────────────
    p_sum = sub.add_parser("summarize", help="Summarize PDF file(s)")
    p_sum.add_argument("paths", nargs="+", help="PDF file(s)")
    p_sum.add_argument("-o", "--output", default=None, help="Output directory (default: same as PDF)")
    p_sum.add_argument("--stdout", action="store_true", help="Print summary to stdout instead of file")
    p_sum.add_argument("--base-url", dest="base_url", default=DEFAULT_API_BASE_URL)
    p_sum.add_argument("--api-key", dest="api_key", default=DEFAULT_API_KEY)
    p_sum.add_argument("--log-level", dest="log_level", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_sum.set_defaults(func=cmd_summarize)

    # ── lumina watch ──────────────────────────────────────────────────────────
    p_watch = sub.add_parser("watch", help="Watch a directory and auto-translate new PDFs")
    p_watch.add_argument("directory", help="Directory to watch")
    p_watch.add_argument("--lang-in", dest="lang_in", default="en")
    p_watch.add_argument("--lang-out", dest="lang_out", default="zh")
    p_watch.add_argument("-t", "--threads", type=int, default=0, help="Threads for PDF translation (default: read from config)")
    p_watch.add_argument("--base-url", dest="base_url", default=DEFAULT_API_BASE_URL_V1)
    p_watch.add_argument("--model", default=DEFAULT_MODEL)
    p_watch.add_argument("--api-key", dest="api_key", default=DEFAULT_API_KEY)
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
    p_pol.add_argument("--base-url", dest="base_url", default=DEFAULT_API_BASE_URL)
    p_pol.add_argument("--api-key", dest="api_key", default=DEFAULT_API_KEY)
    p_pol.add_argument("--log-level", dest="log_level", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_pol.set_defaults(func=cmd_polish)

    # ── lumina popup ──────────────────────────────────────────────────────────
    p_popup = sub.add_parser("popup", help="Show polish/translate result popup (used by Quick Action)")
    p_popup.add_argument("--action", required=True, choices=["polish", "translate"])
    p_popup.add_argument("--lang", default=None, choices=["zh", "en"],
                         help="Language (default: auto-detect)")
    p_popup.add_argument("--text", default=None, help="Text to process")
    p_popup.add_argument("--file", default=None, help="File containing text to process")
    p_popup.add_argument("--port", type=int, default=31821)
    p_popup.set_defaults(func=cmd_popup)

    # 双击 .app 启动时没有参数，默认当 server 运行
    _edition = os.environ.get("LUMINA_EDITION")
    if len(sys.argv) == 1 and _edition in ("full", "lite"):
        sys.argv.append("server")

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
