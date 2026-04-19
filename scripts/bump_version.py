#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Replacement:
    path: str
    pattern: str
    replacement: str
    expected_count: int = 1


def build_replacements(version: str) -> list[Replacement]:
    tag = f"v{version}"
    return [
        Replacement(
            "pyproject.toml",
            r'(^version = ")([^"]+)(")',
            rf"\g<1>{version}\g<3>",
        ),
        Replacement(
            "lumina/__init__.py",
            r'(^__version__ = ")([^"]+)(")',
            rf"\g<1>{version}\g<3>",
        ),
        Replacement(
            "scripts/lumina_full.spec",
            r"(CFBundleShortVersionString': ')([^']+)(')",
            rf"\g<1>{version}\g<3>",
        ),
        Replacement(
            "scripts/build_lite.sh",
            r"(CFBundleShortVersionString': ')([^']+)(')",
            rf"\g<1>{version}\g<3>",
        ),
        Replacement(
            "scripts/build_full.sh",
            r'(gh release create )v[0-9]+\.[0-9]+\.[0-9]+',
            rf"\g<1>{tag}",
        ),
        Replacement(
            "scripts/build_full.sh",
            r"(--title 'Lumina )v[0-9]+\.[0-9]+\.[0-9]+(')",
            rf"\g<1>{tag}\g<2>",
        ),
        Replacement(
            "scripts/build_lite.sh",
            r'(gh release create )v[0-9]+\.[0-9]+\.[0-9]+',
            rf"\g<1>{tag}",
        ),
        Replacement(
            "scripts/build_lite.sh",
            r"(--title 'Lumina Lite )v[0-9]+\.[0-9]+\.[0-9]+(')",
            rf"\g<1>{tag}\g<2>",
        ),
        Replacement(
            "lumina/api/templates/index.html",
            r"(>)(v[0-9]+\.[0-9]+\.[0-9]+)(<)",
            rf"\g<1>{tag}\g<3>",
        ),
        Replacement(
            "docs/readme-showcase.html",
            r"(p>)(v[0-9]+\.[0-9]+\.[0-9]+)( <)",
            rf"\g<1>{tag}\g<3>",
        ),
        Replacement(
            "CLAUDE.md",
            r"(当前：`)(v[0-9]+\.[0-9]+\.[0-9]+)(`)",
            rf"\g<1>{tag}\g<3>",
        ),
    ]


def validate_version(version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"版本号必须是 x.y.z 形式，收到：{version}")


def apply_replacements(version: str, *, dry_run: bool) -> int:
    changed_files: dict[Path, str] = {}
    replacements_by_file: dict[Path, list[Replacement]] = {}

    for item in build_replacements(version):
        path = ROOT / item.path
        replacements_by_file.setdefault(path, []).append(item)

    for path, replacements in replacements_by_file.items():
        original = path.read_text(encoding="utf-8")
        updated = original
        file_changed = False
        for item in replacements:
            updated, count = re.subn(
                item.pattern,
                item.replacement,
                updated,
                count=item.expected_count,
                flags=re.MULTILINE,
            )
            if count != item.expected_count:
                raise RuntimeError(
                    f"{item.path} 中模式未按预期替换：{item.pattern!r}，期待 {item.expected_count} 次，实际 {count} 次"
                )
            if updated != original:
                file_changed = True
                original = updated
        if file_changed:
            changed_files[path] = updated

    if not changed_files:
        print(f"版本已是 {version}，无需修改。")
        return 0

    for path, content in changed_files.items():
        rel = path.relative_to(ROOT)
        print(f"{'将更新' if dry_run else '已更新'} {rel}")
        if not dry_run:
            path.write_text(content, encoding="utf-8")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="统一更新项目中的发版版本号")
    parser.add_argument("version", help="新版本号，格式如 0.8.1")
    parser.add_argument("--dry-run", action="store_true", help="只打印将修改的文件，不写入")
    args = parser.parse_args(argv)

    try:
        validate_version(args.version)
        return apply_replacements(args.version, dry_run=args.dry_run)
    except Exception as exc:
        print(f"bump_version 失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
