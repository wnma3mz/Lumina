"""
lumina/cli/text.py — 文本处理子命令

cmd_polish   lumina polish
cmd_popup    lumina popup
"""
import logging
from pathlib import Path

logger = logging.getLogger("lumina")


def cmd_polish(args):
    from lumina.services.document.text_polish import polish_text, polish_file
    from lumina.cli.utils import setup_logging

    setup_logging(args.log_level)

    for path in args.paths:
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


def cmd_popup(args):
    """弹出选中文字的润色/翻译悬浮窗（由 Quick Action Service 调用）。
    直接在本进程运行 NSPanel run loop，不再 spawn 子进程。
    文本优先从 --file 读取（避免 shell 参数传多行文本的转义问题）。
    """
    from lumina.platform_support.popup import _run_popup

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        text = args.text or ""

    text = text.strip()
    if not text:
        return

    lang = args.lang
    if not lang:
        zh_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        lang = "zh" if zh_count / len(text) > 0.2 else "en"

    label = "润色" if args.action == "polish" else "翻译"
    _run_popup({
        "original": text,
        "action": args.action,
        "lang": lang,
        "base_url": f"http://127.0.0.1:{args.port}",
        "label": label,
    })
