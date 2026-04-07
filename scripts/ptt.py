#!/usr/bin/env python3
"""
Lumina PTT 独立脚本（服务已在运行时使用）

用法：
    uv run python scripts/ptt.py                    # 默认 F5 长按
    uv run python scripts/ptt.py --key f6           # 改用 F6
    uv run python scripts/ptt.py --key alt          # 长按 Option 键
    uv run python scripts/ptt.py --key ctrl+alt+r   # 组合键（toggle 模式）
    uv run python scripts/ptt.py --url http://127.0.0.1:31821

需要辅助功能权限（首次运行时 macOS 会弹出授权弹窗）：
    系统设置 → 隐私与安全性 → 辅助功能 → 允许终端
"""
import argparse
import sys
from pathlib import Path

# 支持从项目根直接运行（不依赖安装）
sys.path.insert(0, str(Path(__file__).parent.parent))

from lumina.ptt import PTTDaemon


def main():
    parser = argparse.ArgumentParser(
        description="Lumina PTT — 长按热键录音转写并粘贴",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  uv run python scripts/ptt.py
  uv run python scripts/ptt.py --key f6
  uv run python scripts/ptt.py --key alt
  uv run python scripts/ptt.py --key ctrl+alt+r
        """,
    )
    parser.add_argument(
        "--key", default="alt_r",
        help="热键，如 alt_r / f5 / caps / ctrl+alt+r（默认 alt_r 即右侧 Option，单键=长按，组合键=toggle）"
    )
    parser.add_argument(
        "--url", default="http://127.0.0.1:31821",
        help="Lumina 服务地址（默认 http://127.0.0.1:31821）"
    )
    args = parser.parse_args()
    print("Ctrl+C 退出", flush=True)
    try:
        PTTDaemon(base_url=args.url, hotkey_str=args.key).run()
    except KeyboardInterrupt:
        print("\n已退出", flush=True)


if __name__ == "__main__":
    main()
