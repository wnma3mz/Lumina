#!/usr/bin/env bash
# Lumina Full 打包脚本
# 内含本地模型，启动后直接可用，无需配置。
# 产出：build/dist/Lumina.app
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/build"

echo "=== Lumina Full .app 打包 ==="

cd "$PROJECT_DIR"

HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
    uv add --dev pyinstaller 2>/dev/null || true

mkdir -p "$BUILD_DIR"

cat > "$BUILD_DIR/lumina_full.spec" <<'SPEC'
# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

project_dir = Path(SPECPATH).parent

a = Analysis(
    [str(project_dir / 'lumina' / 'main.py')],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[
        (str(project_dir / 'models'), 'models'),
        (str(project_dir / 'lumina' / 'config.json'), 'lumina'),
        (str(project_dir / 'lumina' / 'config.lite.json'), 'lumina'),
    ],
    hiddenimports=[
        'mlx', 'mlx.core', 'mlx.nn',
        'mlx_lm', 'mlx_whisper',
        'sounddevice', 'scipy',
        'fastapi', 'uvicorn', 'uvicorn.logging',
        'transformers', 'huggingface_hub',
        'aiohttp',
        'pdf2zh', 'pdf2zh.common',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='lumina',
    debug=False,
    strip=False,
    upx=False,
    console=True,
    # 注入版本标记
    env={'LUMINA_EDITION': 'full'},
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='lumina-full',
)

app = BUNDLE(
    coll,
    name='Lumina.app',
    icon=None,
    bundle_identifier='com.lumina.server',
    info_plist={
        'CFBundleShortVersionString': '0.1.0',
        'CFBundleName': 'Lumina',
        'LSUIElement': True,
        'NSMicrophoneUsageDescription': 'Lumina 需要麦克风权限用于语音转文本',
    },
)
SPEC

echo "正在执行 PyInstaller（Full，含模型约 622MB）..."
uv run pyinstaller "$BUILD_DIR/lumina_full.spec" \
    --distpath "$BUILD_DIR/dist" \
    --workpath "$BUILD_DIR/work" \
    --noconfirm

echo ""
echo "✓ 打包完成: $BUILD_DIR/dist/Lumina.app"
echo "将 Lumina.app 拖入 /Applications，双击即可启动，无需任何配置。"
