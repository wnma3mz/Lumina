#!/usr/bin/env bash
# Lumina Full 打包脚本
# 不含模型文件，首次启动时自动下载到 ~/.lumina/models/。
# 产出：build/dist/Lumina.app  +  build/dist/Lumina.zip
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/build"

echo "=== Lumina Full .app 打包 ==="

cd "$PROJECT_DIR"

HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
    uv add --dev pyinstaller 2>/dev/null || true

mkdir -p "$BUILD_DIR"

# runtime hook：进程启动时注入 LUMINA_EDITION=full
cat > "$BUILD_DIR/rthook_edition_full.py" <<'RTHOOK'
import os
os.environ.setdefault("LUMINA_EDITION", "full")
RTHOOK

cat > "$BUILD_DIR/lumina_full.spec" <<'SPEC'
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

project_dir = Path(SPECPATH).parent

# 用 collect_all 确保 mlx / mlx_lm / mlx_whisper 的子模块和数据文件完整打包
mlx_datas, mlx_bins, mlx_hidden       = collect_all('mlx')
mlx_lm_datas, mlx_lm_bins, mlx_lm_hidden         = collect_all('mlx_lm')
mlx_wh_datas, mlx_wh_bins, mlx_wh_hidden         = collect_all('mlx_whisper')

# mlx.core 的 rpath 是 @loader_path/lib，即运行时从 mlx/ 找 lib/libmlx.dylib
# PyInstaller collect_all 会把 libmlx.dylib 提升到 Contents/Frameworks/ 顶层，破坏相对路径
# 解决：把 libmlx.dylib 和 mlx.metallib 显式放到 mlx/lib/ 目标目录
import mlx.core as _mlx_core
_mlx_lib_src = Path(_mlx_core.__file__).parent / 'lib'
_mlx_extra_binaries = [
    # (源路径, 目标目录) —— 二进制文件用 binaries，保留 rpath 相对关系
    (str(_mlx_lib_src / 'libmlx.dylib'), 'mlx/lib'),
]
_mlx_extra_datas = [
    # mlx C++ 代码在 macOS App Bundle 里查找路径为 Resources/mlx/mlx.metallib
    # 同时保留 mlx/lib/ 位置作为备用
    (str(_mlx_lib_src / 'mlx.metallib'), 'mlx'),
    (str(_mlx_lib_src / 'mlx.metallib'), 'mlx/lib'),
]

# 从 collect_all 的 binaries 中移除 libmlx.dylib（避免它被再次放到顶层）
mlx_bins = [(src, dst) for src, dst in mlx_bins if 'libmlx.dylib' not in src]

a = Analysis(
    [str(project_dir / 'lumina' / 'main.py')],
    pathex=[str(project_dir)],
    binaries=mlx_bins + mlx_lm_bins + mlx_wh_bins + _mlx_extra_binaries,
    datas=(
        mlx_datas + mlx_lm_datas + mlx_wh_datas
        + _mlx_extra_datas
        + [
            # 模型不打包进 App，首次启动时按需下载到 ~/.lumina/models/
            (str(project_dir / 'lumina' / 'config.json'), 'lumina'),
            (str(project_dir / 'assets' / 'lumina.icns'), 'assets'),
            (str(project_dir / 'lumina' / 'api' / 'static'), 'lumina/api/static'),
            (str(project_dir / 'scripts' / 'install_quick_action.sh'), 'scripts'),
        ]
    ),
    hiddenimports=(
        mlx_hidden + mlx_lm_hidden + mlx_wh_hidden
        + collect_submodules('mlx')
        + collect_submodules('mlx_lm')
        + collect_submodules('mlx_whisper')
        + [
            'sounddevice', 'scipy',
            'fastapi', 'uvicorn', 'uvicorn.logging',
            'transformers', 'huggingface_hub',
            'aiohttp',
            'pdf2zh',
            'rumps',
        ]
    ),
    hookspath=[],
    runtime_hooks=[str(Path(SPECPATH) / 'rthook_edition_full.py')],
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
    icon=str(project_dir / 'assets' / 'lumina.icns'),
    bundle_identifier='com.lumina.server',
    info_plist={
        'CFBundleShortVersionString': '0.1.0',
        'CFBundleName': 'Lumina',
        'LSUIElement': True,
        'NSMicrophoneUsageDescription': 'Lumina 需要麦克风权限用于语音转文本',
    },
)
SPEC

echo "正在执行 PyInstaller（Full，不含模型，首次启动时自动下载）..."
uv run pyinstaller "$BUILD_DIR/lumina_full.spec" \
    --distpath "$BUILD_DIR/dist" \
    --workpath "$BUILD_DIR/work" \
    --noconfirm

# mlx 用 dladdr(&current_binary_dir) 获取 libmlx.dylib 所在目录，
# 再在该目录下查找 mlx.metallib 和 Resources/mlx.metallib。
# PyInstaller 将 libmlx.dylib 放在 Contents/Frameworks/，
# 因此 mlx.metallib 必须放到 Contents/Frameworks/ 目录下。
APP="$BUILD_DIR/dist/Lumina.app"
MLX_LIB_SRC=$(uv run python -c "import mlx.core; from pathlib import Path; print(Path(mlx.core.__file__).parent / 'lib' / 'mlx.metallib')")
echo "复制 mlx.metallib 到 Contents/Frameworks/..."
cp "$MLX_LIB_SRC" "$APP/Contents/Frameworks/mlx.metallib"

# 生成安装脚本：用户双击即可完成 xattr + 移动到应用程序
# 清理可能残留的旧文件
rm -f "$BUILD_DIR/dist/安装 Lumina.command"
INSTALLER="$BUILD_DIR/dist/install.command"
cat > "$INSTALLER" <<'INSTALLER_SCRIPT'
#!/usr/bin/env bash
# 双击此文件即可安装 Lumina 到「应用程序」文件夹
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC="$SCRIPT_DIR/Lumina.app"
APP_DEST="/Applications/Lumina.app"

echo "=== 安装 Lumina ==="
echo ""

if [ ! -d "$APP_SRC" ]; then
    echo "错误：找不到 Lumina.app，请确保本脚本与 Lumina.app 在同一目录。"
    read -p "按 Enter 退出..." _
    exit 1
fi

# 移除旧版本
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
    bash "$QA_SCRIPT" 2>/dev/null && echo "✓ 快速操作已安装" || echo "⚠ 快速操作安装失败（可稍后手动运行）"
else
    echo "⚠ 未找到快速操作安装脚本，跳过"
fi

echo ""
echo "✓ 安装完成！"
echo "  Lumina.app 已安装到「应用程序」文件夹"
echo "  双击 Lumina.app 即可启动"
echo ""
read -p "按 Enter 关闭此窗口..." _
INSTALLER_SCRIPT
chmod +x "$INSTALLER"

ZIP_PATH="$BUILD_DIR/dist/Lumina.zip"
echo "正在压缩为 zip..."
cd "$BUILD_DIR/dist"
zip -qr "Lumina.zip" "Lumina.app" "install.command"
# 验证安装脚本确实进了 zip（用精确匹配，避免匹配到同名子串）
unzip -l "Lumina.zip" | grep -qE "^\s+[0-9].*install\.command$" && echo "安装脚本已打入 zip" || echo "警告：安装脚本未打入 zip"
cd "$PROJECT_DIR"

echo ""
echo "✓ 打包完成"
echo "  App : $BUILD_DIR/dist/Lumina.app"
echo "  zip : $ZIP_PATH"
echo ""
echo "上传 Release："
echo "  gh release create v0.1.0 \"$ZIP_PATH\" \\"
echo "    --title 'Lumina v0.1.0' \\"
echo "    --notes-file \"$SCRIPT_DIR/release_notes.md\""
