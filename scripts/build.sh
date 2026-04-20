#!/usr/bin/env bash
# Lumina 打包脚本
# 目前仅支持 macOS 平台打包。
# 默认打包 Full 版（不含模型，首次运行下载）。加上 --lite 参数打包 Lite 版（不含模型，需外部接口）。
# 产出：build/dist/Lumina.app (或 Lumina Lite.app) + build/dist/Lumina.zip (或 Lumina-Lite.zip)
set -euo pipefail

OS="$(uname -s)"
if [[ "$OS" != "Darwin" ]]; then
    echo "错误：打包脚本目前仅支持在 macOS 下运行。"
    exit 1
fi

EDITION="full"
if [[ "${1:-}" == "--lite" ]]; then
    EDITION="lite"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/build"

echo "=== Lumina ${EDITION} .app 打包 ==="

cd "$PROJECT_DIR"

# 清理 pyc 缓存和 PyInstaller work 缓存
find lumina -name "*.pyc" -delete 2>/dev/null || true
find lumina -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
rm -rf "$BUILD_DIR/work"

HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
    uv add --dev pyinstaller 2>/dev/null || true

mkdir -p "$BUILD_DIR"

if [[ "$EDITION" == "full" ]]; then
    # Full 逻辑
    RTHOOK="$BUILD_DIR/rthook_edition_full.py"
    RTHOOK_CONTENT='import os
os.environ.setdefault("LUMINA_EDITION", "full")'
    if [[ ! -f "$RTHOOK" ]] || [[ "$(cat "$RTHOOK")" != "$RTHOOK_CONTENT" ]]; then
        printf '%s\n' "$RTHOOK_CONTENT" > "$RTHOOK"
    fi

    SPEC_FILE="$SCRIPT_DIR/lumina_full.spec"
    echo "正在执行 PyInstaller（Full，不含模型）..."
    uv run pyinstaller "$SPEC_FILE" \
        --distpath "$BUILD_DIR/dist" \
        --workpath "$BUILD_DIR/work" \
        --noconfirm

    APP="$BUILD_DIR/dist/Lumina.app"

    # 修复图标
    echo "修复图标透明度..."
    cp "$PROJECT_DIR/assets/lumina.icns" "$APP/Contents/Resources/lumina.icns"

    # 拷贝 mlx.metallib
    MLX_LIB_SRC=$(uv run python -c "import mlx.core; from pathlib import Path; print(Path(mlx.core.__file__).parent / 'lib' / 'mlx.metallib')")
    echo "复制 mlx.metallib 到 Contents/Frameworks/..."
    cp "$MLX_LIB_SRC" "$APP/Contents/Frameworks/mlx.metallib"

    # 生成安装脚本
    rm -f "$BUILD_DIR/dist/安装 Lumina.command"
    INSTALLER="$BUILD_DIR/dist/install.command"
    cat > "$INSTALLER" <<'INSTALLER_SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC="$SCRIPT_DIR/Lumina.app"
APP_DEST="/Applications/Lumina.app"
echo "=== 安装 Lumina ==="
if [ ! -d "$APP_SRC" ]; then
    echo "错误：找不到 Lumina.app，请确保本脚本与 Lumina.app 在同一目录。"
    read -p "按 Enter 退出..." _
    exit 1
fi
if [ -d "$APP_DEST" ]; then
    echo "正在移除旧版本..."
    rm -rf "$APP_DEST"
fi
echo "正在复制 Lumina.app 到应用程序文件夹..."
cp -r "$APP_SRC" "$APP_DEST"
echo "正在移除 macOS 安全限制..."
xattr -cr "$APP_DEST"
echo "正在安装 Finder 右键快速操作..."
QA_SCRIPT="$APP_DEST/Contents/Resources/scripts/install_quick_action.sh"
if [[ -f "$QA_SCRIPT" ]]; then
    bash "$QA_SCRIPT" 2>/dev/null && echo "✓ 快速操作已安装" || echo "⚠ 快速操作安装失败"
fi
echo ""
echo "✓ 安装完成！"
read -p "按 Enter 关闭此窗口..." _
INSTALLER_SCRIPT
    chmod +x "$INSTALLER"

    ZIP_PATH="$BUILD_DIR/dist/Lumina.zip"
    echo "正在压缩为 zip..."
    cd "$BUILD_DIR/dist"
    zip -qr "Lumina.zip" "Lumina.app" "install.command"
    cd "$PROJECT_DIR"
    
    echo ""
    echo "✓ 打包完成"
    echo "  App : $APP"
    echo "  zip : $ZIP_PATH"

else
    # Lite 逻辑
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
        (str(project_dir / 'lumina' / 'config.json'), 'lumina'),
        (str(project_dir / 'lumina' / 'api' / 'static'), 'lumina/api/static'),
    ],
    hiddenimports=[
        'fastapi', 'uvicorn', 'uvicorn.logging',
        'sounddevice', 'scipy', 'mlx_whisper',
        'transformers', 'huggingface_hub', 'aiohttp',
        'pdf2zh', 'pdf2zh.common', 'rumps'
    ],
    excludes=['mlx', 'mlx.core', 'mlx.nn', 'mlx_lm'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name='lumina', debug=False, strip=False, upx=False, console=True, env={'LUMINA_EDITION': 'lite'}
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False, name='lumina-lite'
)
app = BUNDLE(
    coll,
    name='Lumina Lite.app',
    icon=None,
    bundle_identifier='com.lumina.lite',
    info_plist={
        'CFBundleShortVersionString': '0.8.4',
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

    APP="$BUILD_DIR/dist/Lumina Lite.app"
    ZIP_PATH="$BUILD_DIR/dist/Lumina-Lite.zip"
    echo "正在压缩为 zip..."
    cd "$BUILD_DIR/dist"
    zip -qr "Lumina-Lite.zip" "Lumina Lite.app"
    cd "$PROJECT_DIR"
    
    echo ""
    echo "✓ 打包完成"
    echo "  App : $APP"
    echo "  zip : $ZIP_PATH"
fi
