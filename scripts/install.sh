#!/usr/bin/env bash
# Lumina 安装脚本
# 自动识别 macOS / Linux 并执行相应安装逻辑。
# macOS 下支持 --lite 参数安装 Lite 版（不含本地模型）。
set -euo pipefail

OS="$(uname -s)"
EDITION="full"
if [[ "${1:-}" == "--lite" ]]; then
    EDITION="lite"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="$HOME/.lumina"

echo "=== Lumina 安装程序 ($OS) ==="
echo "项目目录: $PROJECT_DIR"
echo "安装目录: $INSTALL_DIR"

if ! command -v uv &>/dev/null; then
    echo "正在安装 uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

mkdir -p "$INSTALL_DIR"

# -------------------------
# macOS 安装逻辑
# -------------------------
if [[ "$OS" == "Darwin" ]]; then
    echo "正在创建虚拟环境..."
    uv venv "$INSTALL_DIR/.venv" --python 3.12

    echo "正在安装依赖..."
    HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
    uv pip install --python "$INSTALL_DIR/.venv/bin/python" \
        fastapi uvicorn pydantic \
        mlx-whisper \
        sounddevice numpy scipy \
        transformers huggingface_hub aiohttp pdf2zh

    if [[ "$EDITION" == "full" ]]; then
        # Full 版需要本地模型库
        HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
        uv pip install --python "$INSTALL_DIR/.venv/bin/python" mlx mlx-lm
    fi

    # 安装 lumina 包
    HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
    uv pip install --python "$INSTALL_DIR/.venv/bin/python" -e "$PROJECT_DIR"

    if [[ "$EDITION" == "full" ]]; then
        # 下载内置模型（若尚未存在）
        MODEL_DIR="$PROJECT_DIR/models/qwen3.5-0.8b-4bit"
        if [ ! -d "$MODEL_DIR" ] || [ -z "$(ls -A "$MODEL_DIR" 2>/dev/null)" ]; then
            echo "正在下载内置模型（约 622MB）..."
            mkdir -p "$MODEL_DIR"
            HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
            "$INSTALL_DIR/.venv/bin/python" -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='mlx-community/Qwen3.5-0.8B-4bit',
    local_dir='$MODEL_DIR',
)
print('模型下载完成')
"
        else
            echo "内置模型已存在，跳过下载。"
        fi
    fi

    # 写全局启动脚本
    cat > "$INSTALL_DIR/lumina" <<EOF
#!/usr/bin/env bash
export LUMINA_EDITION=$EDITION
source "$INSTALL_DIR/.venv/bin/activate"
exec lumina "\$@"
EOF
    chmod +x "$INSTALL_DIR/lumina"

    # 写 launchd plist（可选开机自启）
    PLIST_NAME="com.lumina.${EDITION}"
    PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
    LUMINA_BIN="$INSTALL_DIR/lumina"
    cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>$LUMINA_BIN</string>
        <string>server</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LUMINA_EDITION</key>
        <string>$EDITION</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$HOME/.lumina/lumina-${EDITION}.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.lumina/lumina-${EDITION}.err</string>
</dict>
</plist>
PLIST

    # 安装 Finder 右键快速操作
    echo "正在安装 Finder 右键快速操作..."
    bash "$SCRIPT_DIR/install_quick_action.sh" 2>/dev/null && echo "✓ 快速操作安装完成" || echo "⚠ 快速操作安装失败（可稍后手动运行 bash scripts/install_quick_action.sh）"

    echo ""
    echo "✓ Lumina ${EDITION} 安装完成！"
    echo ""
    echo "启动服务："
    echo "  $INSTALL_DIR/lumina server"
    echo ""
    echo "加入 PATH 后可直接使用（可选）："
    echo "  echo 'export PATH=\"$INSTALL_DIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
    echo "  lumina server"
    echo ""
    echo "开机自启（可选）："
    echo "  launchctl load $PLIST_PATH"

# -------------------------
# Linux 安装逻辑
# -------------------------
elif [[ "$OS" == "Linux" ]]; then
    cd "$PROJECT_DIR"
    uv sync

    echo "正在安装桌面集成 (Desktop Integrations)..."
    BIN_DIR="$HOME/.local/bin"
    APP_DIR="$HOME/.local/share/applications"
    LAUNCHER="$BIN_DIR/lumina-file-action"

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

else
    echo "不支持的操作系统: $OS"
    exit 1
fi
