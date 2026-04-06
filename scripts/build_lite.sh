#!/usr/bin/env bash
# Lumina Lite 打包脚本
# 不含本地模型，转发到外部 HTTP 服务。
# 首次启动时引导用户填写外部服务地址。
# 产出：build/dist/Lumina Lite.app
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/build"

echo "=== Lumina Lite .app 打包 ==="

cd "$PROJECT_DIR"

HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
    uv add --dev pyinstaller 2>/dev/null || true

mkdir -p "$BUILD_DIR"

cat > "$BUILD_DIR/lumina_lite.spec" <<'SPEC'
# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

project_dir = Path(SPECPATH).parent

a = Analysis(
    [str(project_dir / 'lumina' / 'main.py')],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[
        # Lite 版不打包 models/，只带配置模板
        (str(project_dir / 'lumina' / 'config.lite.json'), 'lumina'),
        (str(project_dir / 'lumina' / 'config.json'), 'lumina'),
    ],
    hiddenimports=[
        'fastapi', 'uvicorn', 'uvicorn.logging',
        'sounddevice', 'scipy',
        'mlx_whisper',
        'transformers', 'huggingface_hub',
        'aiohttp',
        'pdf2zh', 'pdf2zh.common',
    ],
    hookspath=[],
    runtime_hooks=[],
    # 排除本地推理相关的重依赖（Lite 不需要）
    excludes=['mlx', 'mlx.core', 'mlx.nn', 'mlx_lm'],
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
    env={'LUMINA_EDITION': 'lite'},
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='lumina-lite',
)

app = BUNDLE(
    coll,
    name='Lumina Lite.app',
    icon=None,
    bundle_identifier='com.lumina.lite',
    info_plist={
        'CFBundleShortVersionString': '0.1.0',
        'CFBundleName': 'Lumina Lite',
        'LSUIElement': True,
        'NSMicrophoneUsageDescription': 'Lumina 需要麦克风权限用于语音转文本',
    },
)
SPEC

echo "正在执行 PyInstaller（Lite，无模型）..."
uv run pyinstaller "$BUILD_DIR/lumina_lite.spec" \
    --distpath "$BUILD_DIR/dist" \
    --workpath "$BUILD_DIR/work" \
    --noconfirm

ZIP_PATH="$BUILD_DIR/dist/Lumina-Lite.zip"
echo "正在压缩为 zip..."
cd "$BUILD_DIR/dist"
zip -qr "Lumina-Lite.zip" "Lumina Lite.app"
cd "$PROJECT_DIR"

echo ""
echo "✓ 打包完成"
echo "  App : $BUILD_DIR/dist/Lumina Lite.app"
echo "  zip : $ZIP_PATH"
echo ""
echo "上传 Release："
echo "  gh release create v0.1.0 \"$ZIP_PATH\" --title 'Lumina Lite v0.1.0' --notes '首次发布（不含模型）'"
