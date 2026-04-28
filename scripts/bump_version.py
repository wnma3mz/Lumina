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
            "scripts/build.sh",
            r"(CFBundleShortVersionString': ')([^']+)(')",
            rf"\g<1>{version}\g<3>",
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


def apply_replacements(version: str, *, dry_run: bool) -> list[Path]:
    """返回实际修改（或将修改）的文件列表。"""
    changed_files: dict[Path, str] = {}
    replacements_by_file: dict[Path, list[Replacement]] = {}

    for item in build_replacements(version):
        path = ROOT / item.path
        replacements_by_file.setdefault(path, []).append(item)

    for path, replacements in replacements_by_file.items():
        if not path.exists():
            print(f"跳过（不存在）：{path.relative_to(ROOT)}")
            continue
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
                    f"{item.path} 中模式未按预期替换：{item.pattern!r}，"
                    f"期待 {item.expected_count} 次，实际 {count} 次"
                )
            if updated != original:
                file_changed = True
                original = updated
        if file_changed:
            changed_files[path] = updated

    if not changed_files:
        print(f"版本已是 {version}，无需修改。")
        return []

    for path, content in changed_files.items():
        rel = path.relative_to(ROOT)
        print(f"{'将更新' if dry_run else '已更新'} {rel}")
        if not dry_run:
            path.write_text(content, encoding="utf-8")

    return list(changed_files.keys())


def git_commit(version: str, changed: list[Path]) -> None:
    import subprocess
    tag = f"v{version}"
    files = [str(p) for p in changed]
    subprocess.check_call(["git", "add", "--"] + files, cwd=ROOT)
    subprocess.check_call(
        ["git", "commit", "-m", f"release: bump version to {tag}"],
        cwd=ROOT,
    )
    subprocess.check_call(
        ["git", "tag", "-a", tag, "-m", tag],
        cwd=ROOT,
    )
    print(f"已提交并打 tag {tag}（尚未 push，请手动 git push origin main {tag}）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="统一更新项目中的发版版本号")
    parser.add_argument("version", help="新版本号，格式如 0.9.1")
    parser.add_argument("--dry-run", action="store_true", help="只打印将修改的文件，不写入")
    parser.add_argument("--commit", action="store_true", help="修改后自动 git commit 并打 tag（不 push）")
    args = parser.parse_args(argv)

    try:
        validate_version(args.version)
        changed = apply_replacements(args.version, dry_run=args.dry_run)
        if changed and args.commit and not args.dry_run:
            git_commit(args.version, changed)
        return 0
    except Exception as exc:
        print(f"bump_version 失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
