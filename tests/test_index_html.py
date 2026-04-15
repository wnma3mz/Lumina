"""
tests/test_index_html.py — index.html 结构完整性测试

每次修改 templates/index.html 或 templates/panels/*.html 后运行
pytest tests/test_index_html.py 即可。

读取方式：用 Jinja2 渲染 templates/index.html（展开所有 {% include %}），
与运行时服务端渲染结果一致。
"""
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

_TEMPLATES_DIR = Path(__file__).parent.parent / "lumina" / "api" / "templates"
_STATIC_DIR = Path(__file__).parent.parent / "lumina" / "api" / "static"
PANEL_KEYS = ["digest", "translate", "summarize", "settings"]

# 非默认面板（CSS 默认 display:none，由 :checked 选择器控制显示）
NON_DEFAULT_PANELS = ["translate", "summarize", "settings"]


@pytest.fixture(scope="module")
def css() -> str:
    return (_STATIC_DIR / "style.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)))
    tmpl = env.get_template("index.html")
    # request 对象只用于 Jinja2 上下文传递，测试中传 None 即可
    return tmpl.render(request=None)


@pytest.fixture(scope="module")
def lines(html) -> list[str]:
    return html.splitlines()


# ── 1. 全局 <div> / <template> 平衡 ──────────────────────────────────────────

def test_global_div_balance(html):
    opens  = html.count("<div")
    closes = html.count("</div>")
    assert opens == closes, f"<div> imbalanced: opens={opens} closes={closes} diff={opens-closes}"


def test_global_template_balance(html):
    # Exclude inline <script> blocks which may contain "<template" string literals
    html_only = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    opens  = html_only.count("<template")
    closes = html_only.count("</template>")
    assert opens == closes, f"<template> imbalanced: opens={opens} closes={closes} diff={opens-closes}"


# ── 2. 每个 panel 的 <div> 平衡 ─────────────────────────────────────────────

def _panel_bounds(lines: list[str], key: str) -> tuple[int, int]:
    """返回 panel div 行的 (start, end) 行索引（end 为下一 panel 起始或 depth 归零处）。"""
    panel_id = f'id="panel-{key}"'
    start = next(
        i for i, l in enumerate(lines)
        if panel_id in l and "<div" in l
    )
    # 尝试从下一个 panel 推断结束位置
    next_keys = PANEL_KEYS[PANEL_KEYS.index(key) + 1:]
    end = None
    for nk in next_keys:
        nkw = f'id="panel-{nk}"'
        try:
            end = next(i for i, l in enumerate(lines) if i > start and nkw in l)
            break
        except StopIteration:
            continue
    if end is None:
        # 最后一个 panel：深度追踪找到闭合 </div>
        depth = 0
        for i in range(start, len(lines)):
            depth += lines[i].count("<div") - lines[i].count("</div>")
            if depth <= 0 and i > start:
                end = i + 1
                break
        if end is None:
            end = len(lines)
    return start, end


@pytest.mark.parametrize("key", PANEL_KEYS)
def test_panel_div_balance(lines, key):
    start, end = _panel_bounds(lines, key)
    section = lines[start:end]
    opens  = sum(l.count("<div")   for l in section)
    closes = sum(l.count("</div>") for l in section)

    # 找到第一个 depth 为负的行，方便 debug
    depth = 0
    first_negative = None
    for i, line in enumerate(section):
        depth += line.count("<div") - line.count("</div>")
        if depth < 0 and first_negative is None:
            first_negative = (start + i + 1, line.rstrip())

    msg = f"Panel '{key}': opens={opens} closes={closes} diff={opens-closes}"
    if first_negative:
        msg += f"\n  First negative depth at line {first_negative[0]}: {first_negative[1][:120]}"
    assert opens == closes, msg


# ── 3. HTMX 架构：radio tab 切换 ────────────────────────────────────────────

def test_radio_tab_inputs_exist(html):
    """四个 radio input 必须存在（CSS tab 切换的控制器）。"""
    for key in PANEL_KEYS:
        assert f'id="tab-{key}"' in html, f"Missing radio input: id=\"tab-{key}\""


def test_radio_tab_inputs_are_hidden(html):
    """radio input 必须设为 hidden，不显示在界面上。"""
    for key in PANEL_KEYS:
        # 找到该 input 行，检查包含 hidden
        lines = html.splitlines()
        line = next((l for l in lines if f'id="tab-{key}"' in l and "radio" in l), None)
        assert line is not None, f"Radio input for tab-{key} not found"
        assert "hidden" in line, f"Radio input tab-{key} missing 'hidden' attribute: {line.strip()[:120]}"


def test_radio_tab_default_digest(html):
    """digest tab 的 radio 必须有 checked 属性（默认选中）。"""
    lines = html.splitlines()
    line = next((l for l in lines if 'id="tab-digest"' in l and "radio" in l), None)
    assert line is not None, "Radio input for tab-digest not found"
    assert "checked" in line, f"Default tab 'digest' missing 'checked': {line.strip()[:120]}"


def test_css_panel_visibility_rules(css):
    """style.css 必须包含 panel 的 display 控制规则（:checked 兄弟选择器方案）。"""
    for key in NON_DEFAULT_PANELS:
        assert f"#tab-{key}:checked" in css, f"Missing CSS rule: #tab-{key}:checked"


    def test_non_default_panels_hidden_by_css(css):
        """非默认 panel 在 style.css 中必须默认 display:none（防 FOUC，纯 CSS 控制）。"""
        for key in NON_DEFAULT_PANELS:
            assert f"#panel-{key}" in css, f"Missing CSS selector for #panel-{key}"
            pattern = rf"#panel-{key}[^{{]*\{{[^}}]*display:\s*none"
            assert re.search(pattern, css), (
                f"CSS missing 'display: none' for #panel-{key} — FOUC risk"
            )


# ── 4. HTMX 属性 ─────────────────────────────────────────────────────────────

def test_htmx_is_present(html):
    """页面必须包含 HTMX（内联脚本或 CDN 链接）。"""
    # 内联版本包含 htmx 函数定义，或有 HTMX_PLACEHOLDER 注释
    has_inline = "htmx" in html.lower()
    assert has_inline, "HTMX not found in page"


def test_htmx_digest_panel(html):
    """digest panel 必须有 hx-get 属性指向 /fragments/digest。"""
    assert 'hx-get="/fragments/digest"' in html, \
        "Digest panel missing hx-get='/fragments/digest'"


def test_settings_config_loader(html):
    """settings panel 必须有 loadSettings() 或 hx-get 指向配置接口。"""
    has_htmx = 'hx-get="/fragments/config"' in html
    has_js = "loadSettings" in html and "/v1/config" in html
    assert has_htmx or has_js, \
        "Settings panel missing config loader (neither hx-get='/fragments/config' nor loadSettings() + /v1/config found)"


def test_no_alpine_xdata(html):
    """HTMX 版本不应含 Alpine.js x-data 绑定。"""
    assert 'x-data=' not in html, "Found Alpine.js x-data — should be removed in HTMX version"


def test_no_cdn_links(html):
    """不应有 CDN 外链（脚本应内联）。"""
    assert "cdn.jsdelivr.net" not in html, "CDN link found — scripts should be inlined"
    assert "unpkg.com" not in html, "CDN link found — scripts should be inlined"


# ── 5. JS 语法（需要 node） ──────────────────────────────────────────────────

def test_js_syntax(html):
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    inline = [s for s in scripts if s.strip() and "HTMX_PLACEHOLDER" not in s]
    assert inline, "No inline scripts found (excluding HTMX placeholder)"

    combined = "\n".join(inline)
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
            f.write(combined)
            fname = f.name
        result = subprocess.run(
            ["node", "--check", fname],
            capture_output=True, text=True, timeout=15,
        )
        Path(fname).unlink(missing_ok=True)
    except FileNotFoundError:
        pytest.skip("node not installed")

    assert result.returncode == 0, f"JS syntax error:\n{result.stderr.strip()}"


# ── 6. 关键 HTML 元素存在 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("selector,desc", [
    ('class="modal-overlay"',      "Compare modal overlay"),
    ('id="summarize-result"',      "Summarize result container"),
    ('id="panel-digest"',          "Digest panel"),
    ('id="panel-translate"',       "Translate panel"),
    ('id="panel-summarize"',       "Summarize panel"),
    ('id="panel-settings"',        "Settings panel"),
])
def test_key_elements_exist(html, selector, desc):
    assert selector in html, f"Missing element: {desc} ({selector!r})"


@pytest.mark.parametrize("selector,desc", [
    ('.bento-card',  "Bento card CSS class"),
    ('#save-bar',    "Save bar CSS ID selector"),
])
def test_key_css_classes_exist(css, selector, desc):
    assert selector in css, f"Missing CSS class: {desc} ({selector!r})"
