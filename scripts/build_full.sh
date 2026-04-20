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

# 清理 pyc 缓存和 PyInstaller work 缓存，确保 PyInstaller 使用最新源码
find lumina -name "*.pyc" -delete 2>/dev/null || true
find lumina -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
rm -rf "$BUILD_DIR/work"

HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
    uv add --dev pyinstaller 2>/dev/null || true

mkdir -p "$BUILD_DIR"

# runtime hook：进程启动时注入 LUMINA_EDITION=full
# 写入固定路径，内容不变时不重写（避免 mtime 变化触发 PyInstaller 缓存失效）
RTHOOK="$BUILD_DIR/rthook_edition_full.py"
RTHOOK_CONTENT='import os
os.environ.setdefault("LUMINA_EDITION", "full")'
if [[ ! -f "$RTHOOK" ]] || [[ "$(cat "$RTHOOK")" != "$RTHOOK_CONTENT" ]]; then
    printf '%s\n' "$RTHOOK_CONTENT" > "$RTHOOK"
fi

# spec 文件固定存放在 scripts/lumina_full.spec，不在每次构建时重新生成
# → PyInstaller 可命中 Analysis 缓存，仅代码变化时才重新分析
SPEC_FILE="$SCRIPT_DIR/lumina_full.spec"

echo "正在执行 PyInstaller（Full，不含模型，首次启动时自动下载）..."
uv run pyinstaller "$SPEC_FILE" \
    --distpath "$BUILD_DIR/dist" \
    --workpath "$BUILD_DIR/work" \
    --noconfirm

# mlx 用 dladdr(&current_binary_dir) 获取 libmlx.dylib 所在目录，
# 再在该目录下查找 mlx.metallib 和 Resources/mlx.metallib。
# PyInstaller 将 libmlx.dylib 放在 Contents/Frameworks/，
# 因此 mlx.metallib 必须放到 Contents/Frameworks/ 目录下。
APP="$BUILD_DIR/dist/Lumina.app"

# PyInstaller 用 sips 处理图标会丢失透明通道，用源文件直接覆盖
echo "修复图标透明度..."
cp "$PROJECT_DIR/assets/lumina.icns" "$APP/Contents/Resources/lumina.icns"

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
echo "  gh release create v0.8.3 \"$ZIP_PATH\" \\"
echo "    --title 'Lumina v0.8.3' \\"
echo "    --notes-file \"$SCRIPT_DIR/release_notes.md\""
