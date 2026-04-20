"""
lumina/popup.py — 剪贴板处理结果预览浮窗（AppKit NSPanel 实现）

show_popup() 在子进程中启动原生 NSPanel，主进程立即返回。
子进程入口：python -m lumina.popup <json_args>

特性：
  - 完全透明背景，圆角胶囊贴 Dock 上方居中
  - NSNonactivatingPanelMask：不抢键盘焦点，用户可继续在原应用操作
  - 流式输出，8 秒无操作自动关闭，鼠标悬停暂停倒计时
  - ✕ 取消 / ✓ 复制（⌘C 同效）
"""
import json
import logging
import subprocess
import sys

from lumina.platform_support.runtime import IS_MACOS

logger = logging.getLogger("lumina.popup")

_PILL_W = 600
_PILL_H_SHORT  = 76
_PILL_H_MEDIUM = 108
_PILL_H_LONG   = 148
_DOCK_MARGIN   = 24


def _pill_height(text_len: int) -> int:
    if text_len <= 80:
        return _PILL_H_SHORT
    if text_len <= 300:
        return _PILL_H_MEDIUM
    return _PILL_H_LONG


def _get_screen_geometry() -> tuple[int, int, int]:
    """返回 (screen_w, screen_h, dock_h)，单位 pt。"""
    try:
        from AppKit import NSScreen, NSApplication
        NSApplication.sharedApplication()
        screen = NSScreen.mainScreen()
        frame = screen.frame()
        visible = screen.visibleFrame()
        return int(frame.size.width), int(frame.size.height), int(visible.origin.y)
    except Exception:
        return 1440, 900, 80


def show_popup(original: str, action: str, lang: str, base_url: str, label: str) -> None:
    """在子进程中弹出预览浮窗，主进程立即返回。"""
    args = json.dumps({
        "original": original,
        "action": action,
        "lang": lang,
        "base_url": base_url,
        "label": label,
    })
    subprocess.Popen(
        [sys.executable, "-m", "lumina.popup", args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ── HTML ─────────────────────────────────────────────────────────────────────

_POPUP_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --text: #f0f0f0;
    --text-dim: rgba(255,255,255,0.38);
    --accent: #007aff;
    --accent-hover: #0062cc;
    --btn-cancel: rgba(255,255,255,0.13);
    --btn-cancel-hover: rgba(255,255,255,0.22);
    --font: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  }
  html, body {
    width: 100%; height: 100%;
    background: transparent;
    overflow: hidden;
    margin: 0;
  }
  body {
    font-family: var(--font);
    -webkit-font-smoothing: antialiased;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 6px;
  }
  .pill {
    width: 100%; height: 100%;
    background: rgba(26, 26, 28, 0.93);
    border-radius: 40px;
    display: flex;
    align-items: center;
    padding: 0 10px 0 22px;
    box-shadow: 0 0 0 0.5px rgba(255,255,255,0.07);
    overflow: hidden;
    position: relative;
  }
  .text-area {
    flex: 1;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-width: 0;
    padding-right: 10px;
  }
  .lbl {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-bottom: 3px;
    line-height: 1;
  }
  .result {
    font-size: 14px;
    color: var(--text);
    line-height: 1.5;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .result.dim { color: var(--text-dim); }
  .result.lines-2 {
    white-space: normal;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .result.lines-3 {
    white-space: pre-wrap;
    word-break: break-word;
    overflow-y: auto;
    max-height: 68px;
    display: block;
  }
  .cursor {
    display: inline-block;
    width: 2px; height: 1em;
    background: var(--accent);
    margin-left: 1px;
    vertical-align: text-bottom;
    animation: blink 1s step-end infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  .actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }
  .btn {
    width: 40px; height: 40px;
    border-radius: 50%;
    border: none;
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.12s, transform 0.08s;
    outline: none; flex-shrink: 0;
  }
  .btn:active { transform: scale(0.92); }
  .btn-cancel { background: var(--btn-cancel); }
  .btn-cancel:hover { background: var(--btn-cancel-hover); }
  .btn-cancel svg { opacity: 0.7; }
  .btn-copy { background: var(--accent); }
  .btn-copy:hover { background: var(--accent-hover); }
  .btn-copy:disabled { opacity: 0.32; cursor: not-allowed; }
  .btn-copy:disabled:active { transform: none; }
  .btn-copy.done { background: #30d158; animation: pop 0.18s ease; }
  @keyframes pop { 0%{transform:scale(.85)} 60%{transform:scale(1.1)} 100%{transform:scale(1)} }

  /* 倒计时进度条 */
  .timer-bar {
    position: absolute;
    bottom: 0; left: 0;
    height: 2px;
    width: 100%;
    background: rgba(255,255,255,0.16);
    transform-origin: left center;
    transform: scaleX(1);
    pointer-events: none;
  }
  .timer-bar.hidden { opacity: 0; }
</style>
</head>
<body>
<div class="pill">
  <div class="text-area">
    <div class="lbl" id="lbl">润色</div>
    <div class="result dim" id="result"><span class="cursor"></span></div>
  </div>
  <div class="actions">
    <button class="btn btn-cancel" onclick="handleCancel()">
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
        <path d="M1 1l10 10M11 1L1 11" stroke="white" stroke-width="1.8" stroke-linecap="round"/>
      </svg>
    </button>
    <button class="btn btn-copy" id="copyBtn" disabled onclick="handleCopy()">
      <svg width="14" height="11" viewBox="0 0 14 11" fill="none">
        <path d="M1 5.5l4 4L13 1" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>
  </div>
  <div class="timer-bar hidden" id="timerBar"></div>
</div>
<script>
const AUTO_CLOSE_MS = 8000;
const s = { result:'', done:false, ctrl:null, elapsed:0, timerStart:null, timerHandle:null };

function init(p) {
  document.getElementById('lbl').textContent = p.label;
  fetchStream(p.base_url, p.action, p.lang, p.original);
}

async function fetchStream(base, action, lang, text) {
  s.ctrl = new AbortController();
  const el = document.getElementById('result');
  const url = action === 'polish' ? base+'/v1/polish' : base+'/v1/translate';
  const body = action === 'polish'
    ? { text, language: lang, stream: true }
    : { text, target_language: lang === 'zh' ? 'en' : 'zh', stream: true };
  try {
    const resp = await fetch(url, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body), signal: s.ctrl.signal
    });
    if (!resp.ok) throw new Error('HTTP '+resp.status);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    el.classList.remove('dim');
    el.innerHTML = '<span class="cursor"></span>';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6).trim();
        if (data === '[DONE]') { finish(); return; }
        try {
          const tok = JSON.parse(data).text;
          if (tok) {
            s.result += tok;
            if (s.result.length > 300 && !el.classList.contains('lines-3')) {
              el.classList.remove('lines-2'); el.classList.add('lines-3');
            } else if (s.result.length > 80 && !el.classList.contains('lines-3')) {
              el.classList.add('lines-2');
            }
            el.innerHTML = esc(s.result) + '<span class="cursor"></span>';
            if (el.classList.contains('lines-3')) el.scrollTop = el.scrollHeight;
          }
        } catch(_) {}
      }
    }
    finish();
  } catch(e) {
    el.classList.add('dim');
    el.textContent = e.name === 'AbortError' ? '已取消' : '请求失败';
  }
}

function finish() {
  s.done = true;
  const el = document.getElementById('result');
  el.innerHTML = esc(s.result);
  if (s.result.length > 300) el.classList.add('lines-3');
  else if (s.result.length > 80) el.classList.add('lines-2');
  if (s.result.trim()) {
    const btn = document.getElementById('copyBtn');
    btn.disabled = false;
    btn.focus();
  }
  startTimer();
}

// ── 倒计时 ────────────────────────────────────────────────────────────────
function startTimer() {
  const bar = document.getElementById('timerBar');
  bar.classList.remove('hidden');
  const remaining = AUTO_CLOSE_MS - s.elapsed;
  bar.style.transition = 'none';
  bar.style.transform = 'scaleX(1)';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    bar.style.transition = `transform ${remaining}ms linear`;
    bar.style.transform = 'scaleX(0)';
  }));
  s.timerStart = Date.now();
  s.timerHandle = setTimeout(() => window.webkit.messageHandlers.close.postMessage(''), remaining);
}

function pauseTimer() {
  if (!s.done || s.timerHandle === null) return;
  clearTimeout(s.timerHandle); s.timerHandle = null;
  s.elapsed += Date.now() - s.timerStart;
  const bar = document.getElementById('timerBar');
  const cur = getComputedStyle(bar).transform;
  bar.style.transition = 'none';
  bar.style.transform = cur;
}

function resumeTimer() {
  if (!s.done || s.elapsed >= AUTO_CLOSE_MS || s.timerHandle !== null) return;
  startTimer();
}

document.querySelector('.pill').addEventListener('mouseenter', pauseTimer);
document.querySelector('.pill').addEventListener('mouseleave', resumeTimer);

function handleCopy() {
  pauseTimer();
  document.getElementById('copyBtn').classList.add('done');
  setTimeout(() => window.webkit.messageHandlers.copy.postMessage(s.result), 300);
}

function handleCancel() {
  pauseTimer();
  if (s.ctrl) s.ctrl.abort();
  window.webkit.messageHandlers.close.postMessage('');
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') handleCancel();
  if ((e.metaKey||e.ctrlKey) && e.key==='c' && s.done && s.result) handleCopy();
});

function esc(t) {
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
}
</script>
</body>
</html>
"""


def _build_popup_html(params: dict, *, bridge: str) -> str:
    html = _POPUP_HTML
    if bridge == "pywebview":
        html = html.replace(
            "window.webkit.messageHandlers.close.postMessage('')",
            "pywebview.api.close()",
        )
        html = html.replace(
            "window.webkit.messageHandlers.copy.postMessage(s.result)",
            "pywebview.api.copy(s.result)",
        )
    return html + f"""
<script>
window.addEventListener('load', () => init({json.dumps(params)}));
</script>
"""


# ── AppKit NSPanel 实现 ───────────────────────────────────────────────────────

def _run_popup(params: dict):
    """按平台选择 popup 后端。"""
    if IS_MACOS:
        return _run_popup_macos(params)
    return _run_popup_pywebview(params)


def _run_popup_macos(params: dict):
    """用 NSPanel + WKWebView 实现透明胶囊浮窗，在主线程运行。"""
    from AppKit import (
        NSApplication, NSApp, NSPanel, NSScreen,
        NSColor, NSRect, NSPoint, NSSize,
        NSFloatingWindowLevel, NSEvent,
    )
    from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController
    from Foundation import NSObject

    NSApplication.sharedApplication()
    NSApp.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory：不在 Dock 显示

    # ── 屏幕尺寸 ──
    screen = NSScreen.mainScreen()
    mouse_loc = NSEvent.mouseLocation()
    for s in NSScreen.screens():
        f = s.frame()
        if f.origin.x <= mouse_loc.x <= f.origin.x + f.size.width and f.origin.y <= mouse_loc.y <= f.origin.y + f.size.height:
            screen = s
            break

    sf = screen.frame()
    vf = screen.visibleFrame()
    sw = sf.size.width
    dock_h = vf.origin.y

    pill_h = _pill_height(len(params.get("original", "")))
    pill_w = _PILL_W
    x = sf.origin.x + (sw - pill_w) / 2
    y = dock_h + _DOCK_MARGIN

    rect = NSRect(NSPoint(x, y), NSSize(pill_w, pill_h))

    # ── NSPanel ──
    # NSNonactivatingPanelMask (1<<7=128) + NSBorderlessWindowMask (0)
    style = 128  # NSNonactivatingPanelMask
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, 2, False  # NSBackingStoreBuffered=2
    )
    panel.setLevel_(NSFloatingWindowLevel)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setOpaque_(False)
    panel.setHasShadow_(False)
    panel.setCollectionBehavior_(
        1 << 3  # NSWindowCollectionBehaviorCanJoinAllSpaces
    )
    panel.setMovableByWindowBackground_(False)

    # ── WKWebView ──
    config = WKWebViewConfiguration.alloc().init()
    controller = WKUserContentController.alloc().init()
    config.setUserContentController_(controller)

    webview = WKWebView.alloc().initWithFrame_configuration_(rect, config)
    webview.setFrame_(NSRect(NSPoint(0, 0), NSSize(pill_w, pill_h)))
    webview.setOpaque_(False)
    webview.setBackgroundColor_(NSColor.clearColor())
    webview.setValue_forKey_(False, "drawsBackground")

    panel.setContentView_(webview)

    # ── JS → Python 消息处理 ──
    class MessageHandler(NSObject):
        def userContentController_didReceiveScriptMessage_(self, controller, message):
            name = message.name()
            body = message.body() or ""
            if name == "close":
                panel.close()
                NSApp.stop_(None)
            elif name == "copy":
                from lumina.platform_utils import clipboard_set
                try:
                    clipboard_set(str(body))
                except Exception as e:
                    logger.warning("clipboard_set failed: %s", e)
                panel.close()
                NSApp.stop_(None)

    handler = MessageHandler.alloc().init()
    controller.addScriptMessageHandler_name_(handler, "close")
    controller.addScriptMessageHandler_name_(handler, "copy")

    # ── 加载 HTML ──
    html_with_params = _build_popup_html(params, bridge="webkit")
    webview.loadHTMLString_baseURL_(html_with_params, None)
    panel.makeKeyAndOrderFront_(None)

    # ── Run Loop ──
    NSApp.run()


def _run_popup_pywebview(params: dict):
    """非 macOS 使用 pywebview 提供等价弹窗能力。"""
    import webview

    class _PopupApi:
        def __init__(self):
            self.window = None

        def copy(self, text: str):
            from lumina.platform_utils import clipboard_set

            try:
                clipboard_set(str(text))
            finally:
                if self.window is not None:
                    self.window.destroy()

        def close(self):
            if self.window is not None:
                self.window.destroy()

    api = _PopupApi()
    height = _pill_height(len(params.get("original", ""))) + 32
    window = webview.create_window(
        params.get("label", "Lumina"),
        html=_build_popup_html(params, bridge="pywebview"),
        js_api=api,
        width=_PILL_W,
        height=height,
        resizable=False,
        on_top=True,
    )
    api.window = window
    webview.start()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(1)
    try:
        params = json.loads(sys.argv[1])
    except Exception as e:
        print(f"popup: bad args: {e}", file=sys.stderr)
        sys.exit(1)
    _run_popup(params)
