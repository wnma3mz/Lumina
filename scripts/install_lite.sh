#!/usr/bin/env bash
# Lumina Lite 安装脚本（方案 B）
# 不含本地模型，转发到外部 HTTP 服务。
# 首次运行 lumina server 时会引导填写外部服务地址。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="$HOME/.lumina"

echo "=== Lumina Lite 安装程序 ==="
echo "项目目录: $PROJECT_DIR"
echo "安装目录: $INSTALL_DIR"

if ! command -v uv &>/dev/null; then
    echo "正在安装 uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

mkdir -p "$INSTALL_DIR"

echo "正在创建虚拟环境..."
uv venv "$INSTALL_DIR/.venv" --python 3.12

echo "正在安装依赖（Lite，无本地模型依赖）..."
HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
uv pip install --python "$INSTALL_DIR/.venv/bin/python" \
    fastapi uvicorn pydantic \
    mlx-whisper \
    sounddevice numpy scipy \
    transformers huggingface_hub aiohttp pdf2zh

# 安装 lumina 包
HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
uv pip install --python "$INSTALL_DIR/.venv/bin/python" -e "$PROJECT_DIR"

# 写全局启动脚本（注入 Lite 标记）
cat > "$INSTALL_DIR/lumina" <<EOF
#!/usr/bin/env bash
export LUMINA_EDITION=lite
source "$INSTALL_DIR/.venv/bin/activate"
exec lumina "\$@"
EOF
chmod +x "$INSTALL_DIR/lumina"

# 写 launchd plist（可选开机自启）
PLIST_PATH="$HOME/Library/LaunchAgents/com.lumina.lite.plist"
LUMINA_BIN="$INSTALL_DIR/lumina"
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lumina.lite</string>
    <key>ProgramArguments</key>
    <array>
        <string>$LUMINA_BIN</string>
        <string>server</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LUMINA_EDITION</key>
        <string>lite</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$HOME/.lumina/lumina-lite.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.lumina/lumina-lite.err</string>
</dict>
</plist>
PLIST

echo ""
echo "✓ Lumina Lite 安装完成！"
echo ""
echo "启动服务（首次启动会引导填写外部服务地址）："
echo "  $INSTALL_DIR/lumina server"
echo ""
echo "加入 PATH 后可直接使用（可选）："
echo "  echo 'export PATH=\"$INSTALL_DIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
echo "  lumina server"
echo ""
echo "开机自启（可选）："
echo "  launchctl load $PLIST_PATH"
