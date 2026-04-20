#!/usr/bin/env python3
"""
Cross-platform file action helper for desktop integration.

This script is meant to be called by Windows SendTo entries, Linux desktop
entries, or manual shortcuts. It routes file actions to Lumina's existing
PDF/summarize/polish logic and writes outputs next to the source file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


def detect_polish_language(path: Path) -> str:
    name = path.stem.lower()
    if path.name.lower().startswith("readme") or "-en" in name:
        return "en"
    return "zh"


def _ensure_pdf(path: Path) -> None:
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise SystemExit(f"Expected a PDF file: {path}")


def _ensure_text(path: Path) -> None:
    if not path.is_file() or path.suffix.lower() not in {".txt", ".md"}:
        raise SystemExit(f"Expected a .txt or .md file: {path}")


def handle_translate(paths: list[Path], *, base_url: str, api_key: str, model: str) -> None:
    from lumina.services.document.pdf_translate import translate_pdfs

    for path in paths:
        _ensure_pdf(path)
        results = translate_pdfs(
            paths=[str(path)],
            output_dir=str(path.parent),
            base_url=base_url,
            api_key=api_key,
            model=model,
        )
        for mono, dual in results:
            print(f"Translated: {path.name}")
            print(f"  mono: {mono}")
            print(f"  dual: {dual}")


def handle_summarize(paths: list[Path], *, base_url: str, api_key: str) -> None:
    from lumina.services.document.pdf_summarize import summarize_pdf

    for path in paths:
        _ensure_pdf(path)
        out_path = path.with_name(f"{path.stem}-summary.txt")
        summarize_pdf(
            path=str(path),
            base_url=base_url,
            api_key=api_key,
            output=str(out_path),
        )
        print(f"Summary saved: {out_path}")


def handle_polish(paths: list[Path], *, base_url: str, api_key: str) -> None:
    from lumina.services.document.text_polish import polish_file

    for path in paths:
        _ensure_text(path)
        language = detect_polish_language(path)
        out_path = path.with_name(f"{path.stem}-polished{path.suffix}")
        polish_file(
            path=str(path),
            language=language,
            base_url=base_url,
            api_key=api_key,
            output=str(out_path),
        )
        print(f"Polished: {out_path}")


def build_parser() -> argparse.ArgumentParser:
    from lumina.config import DEFAULT_API_KEY, DEFAULT_MODEL

    parser = argparse.ArgumentParser(prog="lumina_file_action")
    parser.add_argument("action", choices=["translate", "summarize", "polish"])
    parser.add_argument("paths", nargs="+", help="Input file paths")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    return parser


def main(argv: list[str] | None = None) -> int:
    from lumina.config import DEFAULT_API_BASE_URL, DEFAULT_API_BASE_URL_V1

    args = build_parser().parse_args(argv)
    paths = [Path(p).expanduser().resolve() for p in args.paths]

    if args.action == "translate":
        handle_translate(
            paths,
            base_url=args.base_url or DEFAULT_API_BASE_URL_V1,
            api_key=args.api_key,
            model=args.model,
        )
    elif args.action == "summarize":
        handle_summarize(
            paths,
            base_url=args.base_url or DEFAULT_API_BASE_URL,
            api_key=args.api_key,
        )
    else:
        handle_polish(
            paths,
            base_url=args.base_url or DEFAULT_API_BASE_URL,
            api_key=args.api_key,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
