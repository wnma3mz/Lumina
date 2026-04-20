#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
LAUNCHER="$BIN_DIR/lumina-file-action"

echo "=== 安装 Lumina (Linux) ==="
echo "项目目录: $PROJECT_DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo "未检测到 uv，开始安装..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

cd "$PROJECT_DIR"
uv sync

echo "正在安装桌面集成 (Desktop Integrations)..."
mkdir -p "$BIN_DIR" "$APP_DIR"

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$PROJECT_DIR"
exec uv run python "$PROJECT_DIR/scripts/lumina_file_action.py" "\$@"
EOF
chmod +x "$LAUNCHER"

create_entry() {
    local name="$1"
    local filename="$2"
    local mime="$3"
    local action="$4"
    cat > "$APP_DIR/$filename" <<EOF
[Desktop Entry]
Type=Application
Name=$name
Exec=$LAUNCHER $action %F
MimeType=$mime
Terminal=true
NoDisplay=true
StartupNotify=false
EOF
}

create_entry "Lumina Translate PDF" "lumina-translate-pdf.desktop" "application/pdf;" "translate"
create_entry "Lumina Summarize PDF" "lumina-summarize-pdf.desktop" "application/pdf;" "summarize"
create_entry "Lumina Polish Text" "lumina-polish-text.desktop" "text/plain;text/markdown;" "polish"

echo ""
echo "✓ 依赖和桌面集成安装完成"
echo "桌面快捷操作已安装至 $APP_DIR"
echo "你现在可以在文件管理器中使用右键菜单 (Open With) 调用 Lumina"
echo ""
echo "启动服务："
echo "  uv run lumina server"
echo ""
echo "运行 smoke 检查："
echo "  uv run python scripts/smoke_check.py"
