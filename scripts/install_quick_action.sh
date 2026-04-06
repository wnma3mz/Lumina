#!/usr/bin/env bash
# 安装 Lumina Finder Quick Action（右键菜单）
#   - 用 Lumina 翻译 PDF
#   - 用 Lumina 总结 PDF
# 安装后在 Finder 中选中 PDF 文件 → 右键 → 快速操作
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICES_DIR="$HOME/Library/Services"
LUMINA_BIN="$HOME/.lumina/lumina"

# ── 查找 lumina 可执行文件 ──────────────────────────────────────────────────
if [[ -x "$LUMINA_BIN" ]]; then
    LUMINA_CMD="$LUMINA_BIN"
elif command -v lumina &>/dev/null; then
    LUMINA_CMD="$(command -v lumina)"
elif [[ -f "$PROJECT_DIR/pyproject.toml" ]] && command -v uv &>/dev/null; then
    WRAPPER="$HOME/.lumina/lumina-dev"
    mkdir -p "$HOME/.lumina"
    printf '#!/usr/bin/env bash\ncd "%s"\nexec uv run lumina "$@"\n' "$PROJECT_DIR" > "$WRAPPER"
    chmod +x "$WRAPPER"
    LUMINA_CMD="$WRAPPER"
    echo "注意：使用开发模式（uv run），包装脚本已写入 $WRAPPER"
else
    echo "错误：未找到 lumina 可执行文件。"
    echo "请先运行 bash scripts/install_full.sh 或 bash scripts/install_lite.sh"
    exit 1
fi

mkdir -p "$SERVICES_DIR"

# ── 安装单个 Quick Action ──────────────────────────────────────────────────
# 参数：$1=workflow名称  $2=shell脚本内容(heredoc占位)
# install_workflow <名称> <shell脚本内容> [文件类型UTI，默认 com.adobe.pdf，"none"=不限文件]
install_workflow() {
    local name="$1"
    local shell_script="$2"
    local file_type="${3:-com.adobe.pdf}"
    local workflow_path="$SERVICES_DIR/${name}.workflow"

    rm -rf "$workflow_path"
    mkdir -p "$workflow_path/Contents"

    # 生成 NSSendFileTypes 段
    if [[ "$file_type" == "none" ]]; then
        local filetypes_xml=""
    else
        local filetypes_xml="            <key>NSSendFileTypes</key>
            <array>
                <string>${file_type}</string>
            </array>"
    fi

    # Info.plist
    cat > "$workflow_path/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>NSServices</key>
    <array>
        <dict>
            <key>NSMenuItem</key>
            <dict>
                <key>default</key>
                <string>${name}</string>
            </dict>
            <key>NSMessage</key>
            <string>runWorkflowAsService</string>
            <key>NSRequiredContext</key>
            <dict>
                <key>NSApplicationIdentifier</key>
                <string>com.apple.finder</string>
            </dict>
${filetypes_xml}
        </dict>
    </array>
</dict>
</plist>
PLIST

    # 临时写入 shell 脚本
    local tmp_sh="$workflow_path/Contents/_action.sh"
    printf '%s' "$shell_script" > "$tmp_sh"

    # 用 Python 安全生成 document.wflow（XML 转义 shell 内容）
    python3 - "$tmp_sh" "$workflow_path/Contents/document.wflow" <<'PYEOF'
import sys
import xml.sax.saxutils as saxutils

shell_script_path = sys.argv[1]
output_path = sys.argv[2]

with open(shell_script_path) as f:
    shell_content = f.read()

escaped = saxutils.escape(shell_content)

wflow = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>AMApplicationBuild</key><string>521</string>
    <key>AMApplicationVersion</key><string>2.10</string>
    <key>AMDocumentVersion</key><string>2</string>
    <key>actions</key>
    <array>
        <dict>
            <key>action</key>
            <dict>
                <key>ActionBundlePath</key>
                <string>/System/Library/Automator/Run Shell Script.action</string>
                <key>ActionName</key><string>Run Shell Script</string>
                <key>ActionParameters</key>
                <dict>
                    <key>COMMAND_STRING</key>
                    <string>""" + escaped + """</string>
                    <key>CheckedForUserDefaultShell</key><true/>
                    <key>inputMethod</key><integer>1</integer>
                    <key>shell</key><string>/bin/bash</string>
                    <key>source</key><string></string>
                </dict>
                <key>BundleIdentifier</key>
                <string>com.apple.RunShellScript</string>
                <key>CFBundleVersion</key><string>2.0.3</string>
                <key>CanShowSelectedItemsWhenRun</key><false/>
                <key>CanShowWhenRun</key><true/>
                <key>Category</key>
                <array><string>AMCategoryUtilities</string></array>
                <key>Class Name</key><string>RunShellScriptAction</string>
                <key>InputUUID</key><string>D2F90C67-C3B3-4FE1-92C4-9EDAE80C6E97</string>
                <key>OutputUUID</key><string>3B714E91-27F7-4C9E-935E-A60B6C30D01B</string>
                <key>UUID</key><string>1A2B3C4D-5E6F-7890-ABCD-EF1234567890</string>
                <key>UnlockTimeout</key><integer>0</integer>
                <key>arguments</key><dict/>
                <key>isViewVisible</key><integer>1</integer>
                <key>location</key><string>309.000000:253.000000</string>
            </dict>
            <key>isViewVisible</key><integer>1</integer>
        </dict>
    </array>
    <key>connectors</key><dict/>
    <key>workflowMetaData</key>
    <dict>
        <key>workflowTypeIdentifier</key>
        <string>com.apple.Automator.servicesMenu</string>
    </dict>
</dict>
</plist>"""

with open(output_path, "w", encoding="utf-8") as f:
    f.write(wflow)
PYEOF

    rm -f "$tmp_sh"
    echo "✓ 已安装：${workflow_path}"
}

# ── Quick Action 1：翻译 PDF ───────────────────────────────────────────────
TRANSLATE_SCRIPT='#!/usr/bin/env bash
if ! curl -s --noproxy '"'"'*'"'"' --max-time 2 http://127.0.0.1:31821/health &>/dev/null; then
    osascript -e '"'"'display notification "请先启动 lumina server" with title "Lumina" subtitle "服务未运行"'"'"'
    exit 1
fi
for f in "$@"; do
    name="$(basename "$f")"
    ext="${name##*.}"
    lower_ext="$(echo "$ext" | tr '"'"'[:upper:]'"'"' '"'"'[:lower:]'"'"')"
    if [[ "$lower_ext" != "pdf" ]]; then continue; fi
    dir="$(dirname "$f")"
    base="$(basename "$f" .pdf)"
    osascript -e "display notification \"正在翻译：${base}.pdf\" with title \"Lumina\""
    LUMINA_LOG_LEVEL=WARNING LUMINA_CMD pdf "$f" -o "$dir" 2>/tmp/lumina_qa.log
    if [[ $? -eq 0 ]]; then
        osascript -e "display notification \"翻译完成：${base}-mono.pdf\" with title \"Lumina\" subtitle \"双语版：${base}-dual.pdf\""
        open -R "$dir/${base}-mono.pdf" 2>/dev/null || open "$dir"
    else
        osascript -e "display notification \"翻译失败，详情见 /tmp/lumina_qa.log\" with title \"Lumina\""
    fi
done'
TRANSLATE_SCRIPT="${TRANSLATE_SCRIPT//LUMINA_CMD/$LUMINA_CMD}"
install_workflow "用 Lumina 翻译 PDF" "$TRANSLATE_SCRIPT"

# ── Quick Action 2：总结 PDF ───────────────────────────────────────────────
SUMMARIZE_SCRIPT='#!/usr/bin/env bash
if ! curl -s --noproxy '"'"'*'"'"' --max-time 2 http://127.0.0.1:31821/health &>/dev/null; then
    osascript -e '"'"'display notification "请先启动 lumina server" with title "Lumina" subtitle "服务未运行"'"'"'
    exit 1
fi
for f in "$@"; do
    name="$(basename "$f")"
    ext="${name##*.}"
    lower_ext="$(echo "$ext" | tr '"'"'[:upper:]'"'"' '"'"'[:lower:]'"'"')"
    if [[ "$lower_ext" != "pdf" ]]; then continue; fi
    dir="$(dirname "$f")"
    base="$(basename "$f" .pdf)"
    out_file="$dir/${base}-summary.txt"
    osascript -e "display notification \"正在生成摘要：${base}.pdf\" with title \"Lumina\""
    LUMINA_LOG_LEVEL=WARNING LUMINA_CMD summarize "$f" 2>/tmp/lumina_qa.log
    if [[ $? -eq 0 ]]; then
        osascript -e "display notification \"摘要已保存：${base}-summary.txt\" with title \"Lumina\""
        open -R "$out_file" 2>/dev/null || open "$dir"
    else
        osascript -e "display notification \"生成摘要失败，详情见 /tmp/lumina_qa.log\" with title \"Lumina\""
    fi
done'
SUMMARIZE_SCRIPT="${SUMMARIZE_SCRIPT//LUMINA_CMD/$LUMINA_CMD}"
install_workflow "用 Lumina 总结 PDF" "$SUMMARIZE_SCRIPT"

# ── Quick Action 3：录音并总结（不需要选中文件，从 Finder 服务菜单触发）──────
# 录音脚本用 heredoc 写入临时文件，避免引号嵌套问题
_RECORD_TMP=$(mktemp /tmp/lumina_record_XXXXXX.sh)
cat > "$_RECORD_TMP" <<'RECORD_EOF'
#!/usr/bin/env bash
if ! curl -s --noproxy '*' --max-time 2 http://127.0.0.1:31821/health &>/dev/null; then
    osascript -e 'display notification "请先启动 lumina server" with title "Lumina" subtitle "服务未运行"'
    exit 1
fi

RESP=$(curl -s --noproxy '*' -X POST http://127.0.0.1:31821/v1/audio/record/start)
SESSION_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])" 2>/dev/null)
if [[ -z "$SESSION_ID" ]]; then
    osascript -e 'display notification "无法开始录音" with title "Lumina"'
    exit 1
fi

osascript -e 'display dialog "正在录音…\n\n说完后点击「停止」" buttons {"停止"} default button "停止" with title "Lumina 录音"' &>/dev/null

STOP_RESP=$(curl -s --noproxy '*' -X POST http://127.0.0.1:31821/v1/audio/record/stop \
    -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$SESSION_ID\"}")
TRANSCRIPT=$(echo "$STOP_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('text',''))" 2>/dev/null)

if [[ -z "$TRANSCRIPT" ]]; then
    osascript -e 'display notification "未识别到语音内容" with title "Lumina"'
    exit 0
fi

TEXT_JSON=$(python3 -c "import sys,json; print(json.dumps(sys.argv[1]))" "$TRANSCRIPT")
SUM_RESP=$(curl -s --noproxy '*' -X POST http://127.0.0.1:31821/v1/summarize \
    -H 'Content-Type: application/json' \
    -d "{\"text\":$TEXT_JSON}")
SUMMARY=$(echo "$SUM_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('text',''))" 2>/dev/null)

TS=$(date +"%Y%m%d_%H%M%S")
OUT="$HOME/Desktop/lumina-record-${TS}.txt"
printf "=== 转写 ===\n%s\n\n=== 摘要 ===\n%s\n" "$TRANSCRIPT" "$SUMMARY" > "$OUT"
osascript -e "display dialog \"摘要：\n\n${SUMMARY}\n\n已保存到桌面\" buttons {\"好\"} default button \"好\" with title \"Lumina 录音结果\""
open -R "$OUT" 2>/dev/null || true
RECORD_EOF
RECORD_SCRIPT=$(cat "$_RECORD_TMP")
rm -f "$_RECORD_TMP"
install_workflow "用 Lumina 录音并总结" "$RECORD_SCRIPT" "none"

# ── Quick Action 4：润色文本（处理 TXT / MD 文件）────────────────────────────
POLISH_SCRIPT='#!/usr/bin/env bash
if ! curl -s --noproxy '"'"'*'"'"' --max-time 2 http://127.0.0.1:31821/health &>/dev/null; then
    osascript -e '"'"'display notification "请先启动 lumina server" with title "Lumina" subtitle "服务未运行"'"'"'
    exit 1
fi
for f in "$@"; do
    ext="$(echo "${f##*.}" | tr '"'"'[:upper:]'"'"' '"'"'[:lower:]'"'"')"
    if [[ "$ext" != "txt" && "$ext" != "md" ]]; then continue; fi
    dir="$(dirname "$f")"
    base="$(basename "$f" ".$ext")"
    out_file="$dir/${base}-polished.$ext"

    # 语言检测：README 或含 -en 的文件名用英文，否则中文
    fname_lower="$(echo "$base" | tr '"'"'[:upper:]'"'"' '"'"'[:lower:]'"'"')"
    if [[ "$fname_lower" == *readme* || "$fname_lower" == *-en* ]]; then
        LANG_ARG="en"
    else
        LANG_ARG="zh"
    fi

    osascript -e "display notification \"正在润色：$(basename "$f")\" with title \"Lumina\""
    LUMINA_LOG_LEVEL=WARNING LUMINA_CMD polish "$f" --lang "$LANG_ARG" 2>/tmp/lumina_qa.log
    if [[ $? -eq 0 ]]; then
        osascript -e "display notification \"润色完成：${base}-polished.$ext\" with title \"Lumina\""
        open -R "$out_file" 2>/dev/null || open "$dir"
    else
        osascript -e "display notification \"润色失败，详情见 /tmp/lumina_qa.log\" with title \"Lumina\""
    fi
done'
POLISH_SCRIPT="${POLISH_SCRIPT//LUMINA_CMD/$LUMINA_CMD}"
install_workflow "用 Lumina 润色文本" "$POLISH_SCRIPT" "public.plain-text"

# 刷新 macOS Services 缓存
/System/Library/CoreServices/pbs -flush 2>/dev/null || true

echo ""
echo "使用方法："
echo "  PDF 文件 → 右键 → 快速操作："
echo "     「用 Lumina 翻译 PDF」  输出 *-mono.pdf 和 *-dual.pdf"
echo "     「用 Lumina 总结 PDF」  输出 *-summary.txt"
echo "  TXT/MD 文件 → 右键 → 快速操作："
echo "     「用 Lumina 润色文本」  输出 *-polished.txt / *-polished.md"
echo "  Finder 菜单栏 → 服务："
echo "     「用 Lumina 录音并总结」  录音 → 转写 + 摘要 → 保存到桌面"
echo ""
echo "前提：lumina server 需已在运行。"
echo "  启动：$LUMINA_CMD server"
echo "  开机自启：launchctl load ~/Library/LaunchAgents/com.lumina.server.plist"
