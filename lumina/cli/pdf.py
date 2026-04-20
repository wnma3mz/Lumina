"""
lumina/cli/pdf.py — PDF 相关子命令

cmd_pdf      lumina pdf
cmd_summarize lumina summarize
cmd_watch    lumina watch
"""
import logging
from pathlib import Path

logger = logging.getLogger("lumina")


def _resolve_pdf_path(path: str) -> tuple[str, bool]:
    """
    解析 PDF 路径或 URL。
    返回 (本地路径, is_tmp)，is_tmp=True 表示需要清理的临时文件。
    URL 存入持久缓存，is_tmp=False。
    """
    if path.startswith("http://") or path.startswith("https://"):
        from lumina.services.document.pdf_translate import _download_url
        return _download_url(path), False
    return path, False


def cmd_pdf(args):
    from lumina.services.document.pdf_translate import translate_pdfs
    from lumina.cli.utils import setup_logging

    setup_logging(args.log_level)
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


def cmd_summarize(args):
    import shutil
    from lumina.services.document.pdf_summarize import summarize_pdf
    from lumina.cli.utils import setup_logging

    setup_logging(args.log_level)

    for path in args.paths:
        local_path, is_tmp = _resolve_pdf_path(path)
        p = Path(local_path)
        if not p.is_file() or p.suffix.lower() != ".pdf":
            logger.warning("Skipping non-PDF or missing file: %s", path)
            continue

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


def cmd_watch(args):
    from lumina.services.document.watcher import watch
    from lumina.cli.utils import setup_logging

    setup_logging(args.log_level)
    watch(
        directory=args.directory,
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        lang_in=args.lang_in,
        lang_out=args.lang_out,
        threads=args.threads,
    )
