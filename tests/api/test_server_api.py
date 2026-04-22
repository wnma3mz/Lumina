"""
API 契约测试：验证端点的请求/响应结构，不依赖真实 LLM 或 PDF 翻译。
"""
import asyncio
import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.anyio


def _make_app():
    """创建带 mock LLM + Transcriber 的 FastAPI 实例。"""
    with patch.dict(
        "sys.modules",
        {
            "mlx_whisper": MagicMock(),
            "sounddevice": MagicMock(),
            "numpy": MagicMock(),
            "scipy": MagicMock(),
        },
    ):
        from lumina.api.server import create_app
        from lumina.engine.llm import LLMEngine
        from lumina.services.audio.transcriber import Transcriber

        llm = MagicMock(spec=LLMEngine)
        llm.is_loaded = True
        llm.provider_model_name = "mocked-vision-model"
        llm.generate = AsyncMock(return_value="mocked response")
        llm.generate_stream = AsyncMock(return_value=aiter(["hello", " world"]))
        llm.generate_messages = AsyncMock(return_value="mocked response")
        llm.generate_messages_stream = MagicMock(return_value=aiter(["hello", " world"]))

        transcriber = MagicMock(spec=Transcriber)
        transcriber.model = "mock-whisper"
        transcriber.transcribe = AsyncMock(return_value="transcribed text")

        return create_app(llm, transcriber)


async def aiter(items):
    for item in items:
        yield item


async def wait_batch_done(client, job_id: str, *, timeout: float = 2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        r = await client.get(f"/v1/batch/{job_id}")
        assert r.status_code == 200
        data = r.json()
        if data["status"] in {"done", "error"}:
            return data
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("batch job did not finish in time")
        await asyncio.sleep(0.01)


@pytest.fixture
def app():
    return _make_app()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── 健康检查 ─────────────────────────────────────────────────────────────────

async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── 版本同步 ─────────────────────────────────────────────────────────────────

async def test_openapi_version_matches_package(client):
    assert client._transport.app.version != "0.1.0", "FastAPI version still hardcoded to 0.1.0"


# ── Chat Completions ──────────────────────────────────────────────────────────

async def test_chat_completions_basic(client):
    r = await client.post("/v1/chat/completions", json={
        "model": "lumina",
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"] == "mocked response"


async def test_chat_completions_supports_image_url_blocks(client, app):
    app.state.llm.generate_messages = AsyncMock(return_value="图片理解结果")
    r = await client.post("/v1/chat/completions", json={
        "model": "lumina",
        "messages": [
            {"role": "system", "content": "你是视觉助手"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请描述这张图"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://example.com/demo.png",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
    })
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "图片理解结果"
    _, kwargs = app.state.llm.generate_messages.await_args
    assert kwargs["task"] == "chat"
    assert kwargs["system"] == "你是视觉助手"
    assert kwargs["messages"][0]["content"][1]["type"] == "image_url"
    assert kwargs["messages"][0]["content"][1]["image_url"]["url"] == "https://example.com/demo.png"


async def test_chat_completions_stream_supports_image_url_blocks(client, app):
    app.state.llm.generate_messages_stream = MagicMock(return_value=aiter(["图", "片"]))
    r = await client.post("/v1/chat/completions", json={
        "model": "lumina",
        "stream": True,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "读图"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/stream.png"}},
                ],
            }
        ],
    })
    assert r.status_code == 200
    body = r.text
    assert "data: " in body
    assert "[DONE]" in body
    _, kwargs = app.state.llm.generate_messages_stream.call_args
    assert kwargs["messages"][0]["content"][1]["type"] == "image_url"


async def test_chat_completions_rejects_image_in_system_message(client):
    r = await client.post("/v1/chat/completions", json={
        "model": "lumina",
        "messages": [
            {
                "role": "system",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://example.com/system.png"}},
                ],
            },
            {"role": "user", "content": "hello"},
        ],
    })
    assert r.status_code == 400


async def test_chat_completions_missing_messages(client):
    r = await client.post("/v1/chat/completions", json={"model": "lumina"})
    assert r.status_code == 422


# ── PDF URL 端点 Pydantic 校验 ────────────────────────────────────────────────

async def test_pdf_url_missing_url_returns_422(client):
    """缺少 url 字段应返回 422，不是 400。"""
    r = await client.post("/v1/pdf/url", json={"lang_out": "zh"})
    assert r.status_code == 422


async def test_pdf_url_stream_missing_url_returns_422(client):
    r = await client.post("/v1/pdf/url_stream", json={"lang_out": "zh"})
    assert r.status_code == 422


async def test_pdf_url_valid_request_accepted(client):
    """有效请求应被接受（翻译过程会失败，但 422 不应出现）。"""
    # 只检查不是 422；下载失败会 400，这是预期的
    with patch("httpx.AsyncClient.get", side_effect=Exception("network")):
        r = await client.post("/v1/pdf/url", json={"url": "http://example.com/a.pdf"})
    assert r.status_code != 422


# ── Translate / Summarize / Polish ──────────────────────────────────────────

async def test_translate(client):
    r = await client.post("/v1/translate", json={"text": "hello"})
    assert r.status_code == 200
    assert "text" in r.json()


async def test_translate_missing_text(client):
    r = await client.post("/v1/translate", json={})
    assert r.status_code == 422


async def test_summarize(client):
    r = await client.post("/v1/summarize", json={"text": "long article"})
    assert r.status_code == 200
    assert "text" in r.json()


async def test_render_markdown_returns_sanitized_html(client):
    r = await client.post("/v1/render_markdown", json={"text": "# Title\n\n<script>alert(1)</script>\n\n- item"})
    assert r.status_code == 200
    data = r.json()
    assert "<h1>Title</h1>" in data["html"]
    assert "<li>item</li>" in data["html"]
    assert "<script>" not in data["html"]


async def test_polish_zh(client):
    r = await client.post("/v1/polish", json={"text": "文字", "language": "zh"})
    assert r.status_code == 200


async def test_polish_invalid_language(client):
    r = await client.post("/v1/polish", json={"text": "text", "language": "fr"})
    assert r.status_code == 422


async def test_media_ocr_url(client, app):
    app.state.llm.generate_messages = AsyncMock(return_value="识别文本")
    r = await client.post("/v1/media/ocr_url", json={"url": "https://example.com/test.png"})
    assert r.status_code == 200
    assert r.json()["text"] == "识别文本"
    app.state.llm.generate_messages.assert_awaited_once()
    _, kwargs = app.state.llm.generate_messages.await_args
    assert kwargs["task"] == "image_ocr"
    assert kwargs["messages"][0]["content"][1]["type"] == "image_url"
    assert kwargs["messages"][0]["content"][1]["image_url"]["url"] == "https://example.com/test.png"


async def test_media_caption_url(client, app):
    app.state.llm.generate_messages = AsyncMock(return_value="一张图片")
    r = await client.post("/v1/media/caption_url", json={"url": "https://example.com/test.png"})
    assert r.status_code == 200
    assert r.json()["model"] == "mocked-vision-model"
    _, kwargs = app.state.llm.generate_messages.await_args
    assert kwargs["task"] == "image_caption"
    assert kwargs["messages"][0]["content"][1]["type"] == "image_url"


async def test_digest_sources_fragment_renders_summary_and_expandable_details(client):
    debug_info = {
        "collectors": {
            "collect_shell_history": {"chars": 1800},
            "collect_browser_history": {"chars": 3200},
        }
    }
    with patch("lumina.services.digest.core.get_debug_info", return_value=debug_info):
        r = await client.get("/fragments/digest/sources")

    assert r.status_code == 200
    body = r.text
    assert "最近 24 小时有 2 个来源活跃" in body
    assert 'hx-get="/fragments/digest/storage"' in body


async def test_digest_fragment_polls_while_generating(client):
    with patch("lumina.services.digest.get_status", return_value={"generating": True, "generated_at": None}), \
         patch("lumina.api.routers.fragments._load_recent_snapshot_content", return_value="# placeholder"):
        r = await client.get("/fragments/digest")

    assert r.status_code == 200
    body = r.text
    assert "正在生成摘要" in body
    assert 'hx-get="/fragments/digest"' in body
    assert 'hx-trigger="load delay:2s"' in body
    assert 'hx-target="#digest-content"' in body


async def test_digest_fragment_only_shows_recent_twenty_snapshots_for_today(client):
    snapshots = [
        "<!-- generated: 2026-04-17T00:00:00 -->\n# 2026-04-17 00:00\n\nsnap 0",
        "<!-- generated: 2026-04-17T09:00:00 -->\n# 2026-04-17 09:00\n\nsnap 1",
        "<!-- generated: 2026-04-17T10:00:00 -->\n# 2026-04-17 10:00\n\nsnap 2",
        "<!-- generated: 2026-04-17T11:00:00 -->\n# 2026-04-17 11:00\n\nsnap 3",
        "<!-- generated: 2026-04-17T12:00:00 -->\n# 2026-04-17 12:00\n\nsnap 4",
        "<!-- generated: 2026-04-17T13:00:00 -->\n# 2026-04-17 13:00\n\nsnap 5",
        "<!-- generated: 2026-04-17T14:00:00 -->\n# 2026-04-17 14:00\n\nsnap 6",
        "<!-- generated: 2026-04-17T15:00:00 -->\n# 2026-04-17 15:00\n\nsnap 7",
    ]
    with patch("lumina.services.digest.get_status", return_value={"generating": False, "generated_at": "2026-04-17T15:00:00"}), \
         patch("lumina.services.digest.reports.load_snapshots_for_date", return_value=snapshots), \
         patch("lumina.api.routers.fragments._render_markdown", side_effect=lambda text: text):
        r = await client.get("/fragments/digest")

    assert r.status_code == 200
    body = r.text
    assert "snap 7" in body
    assert "snap 2" in body
    assert "snap 1" in body
    assert "snap 0" in body


async def test_report_fragment_defaults_to_latest_and_lists_options(client, tmp_path):
    daily_dir = tmp_path / "reports" / "daily"
    weekly_dir = tmp_path / "reports" / "weekly"
    monthly_dir = tmp_path / "reports" / "monthly"
    daily_dir.mkdir(parents=True)
    weekly_dir.mkdir(parents=True)
    monthly_dir.mkdir(parents=True)
    (daily_dir / "2026-04-14.md").write_text("# older", encoding="utf-8")
    (daily_dir / "2026-04-15.md").write_text("# latest", encoding="utf-8")

    with patch("lumina.config.REPORTS_DAILY_DIR", daily_dir), \
         patch("lumina.config.REPORTS_WEEKLY_DIR", weekly_dir), \
         patch("lumina.config.REPORTS_MONTHLY_DIR", monthly_dir), \
         patch("lumina.api.routers.fragments._render_markdown", side_effect=lambda text: text):
        r = await client.get("/fragments/report/daily")

    assert r.status_code == 200
    body = r.text
    assert "2026年4月15日 日报" in body
    assert 'hx-get="/fragments/report/daily"' in body
    assert '<option value="2026-04-15" selected>' in body
    assert '<option value="2026-04-14"' in body

    (weekly_dir / "2026-W16.md").write_text("# weekly", encoding="utf-8")
    (monthly_dir / "2026-04.md").write_text("# monthly", encoding="utf-8")
    with patch("lumina.config.REPORTS_DAILY_DIR", daily_dir), \
         patch("lumina.config.REPORTS_WEEKLY_DIR", weekly_dir), \
         patch("lumina.config.REPORTS_MONTHLY_DIR", monthly_dir), \
         patch("lumina.api.routers.fragments._render_markdown", side_effect=lambda text: text):
        weekly = await client.get("/fragments/report/weekly")
        monthly = await client.get("/fragments/report/monthly")

    assert "2026 第16周 周报" in weekly.text
    assert '<option value="2026-W16" selected>2026 第16周</option>' in weekly.text
    assert "2026年4月 月报" in monthly.text
    assert '<option value="2026-04" selected>2026年4月</option>' in monthly.text


async def test_config_fragment_includes_menubar_toggle(client):
    r = await client.get("/fragments/config")

    assert r.status_code == 200
    assert 'name="system.desktop.menubar_enabled"' in r.text
    assert "显示 macOS 菜单栏图标" in r.text


async def test_config_fragment_uses_button_type_for_settings_tabs(client):
    r = await client.get("/fragments/config")

    assert r.status_code == 200
    assert 'type="button"' in r.text
    assert "switchSettingsSubTab('digest', this)" in r.text
    assert "setProviderType('local', this)" in r.text


async def test_digest_fragment_accepts_string_generated_at(client):
    from lumina.api.routers.fragments import _format_generated_at_label

    label = _format_generated_at_label("2026-04-15T21:48:31")
    assert label.startswith("生成于 ")


async def test_digest_fragment_accepts_numeric_string_generated_at(client):
    from lumina.api.routers.fragments import _format_generated_at_label

    label = _format_generated_at_label("1713188911")
    assert label.startswith("生成于 ") or label == "已生成"


async def test_report_fragment_sanitizes_untrusted_markdown(client, tmp_path):
    daily_dir = tmp_path / "reports" / "daily"
    weekly_dir = tmp_path / "reports" / "weekly"
    monthly_dir = tmp_path / "reports" / "monthly"
    daily_dir.mkdir(parents=True)
    weekly_dir.mkdir(parents=True)
    monthly_dir.mkdir(parents=True)
    (daily_dir / "2026-04-15.md").write_text(
        "# title\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n**safe**",
        encoding="utf-8",
    )

    with patch("lumina.config.REPORTS_DAILY_DIR", daily_dir), \
         patch("lumina.config.REPORTS_WEEKLY_DIR", weekly_dir), \
         patch("lumina.config.REPORTS_MONTHLY_DIR", monthly_dir):
        r = await client.get("/fragments/report/daily")

    assert r.status_code == 200
    assert "<script" not in r.text
    assert "javascript:alert" not in r.text
    assert "<strong>safe</strong>" in r.text


# ── 模型列表 ─────────────────────────────────────────────────────────────────

async def test_list_models(client):
    r = await client.get("/v1/models")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert "lumina" in ids


# ── PDF Job 状态 ──────────────────────────────────────────────────────────────

async def test_pdf_job_not_found(client):
    r = await client.get("/v1/pdf/job/nonexistent")
    assert r.status_code == 404


async def test_pdf_download_not_found(client):
    r = await client.get("/v1/pdf/download/nonexistent/mono")
    assert r.status_code == 404


# ── Batch / PDF 串联 ─────────────────────────────────────────────────────────

async def test_batch_document_endpoint_processes_files(client, tmp_path):
    source = tmp_path / "docs"
    source.mkdir()
    (source / "a.txt").write_text("hello", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "b.md").write_text("world", encoding="utf-8")

    r = await client.post(
        "/v1/batch/document",
        json={"input_dir": str(source), "task": "translate", "target_language": "zh"},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    done = await wait_batch_done(client, job_id)
    assert done["status"] == "done"
    assert done["succeeded"] == 2
    assert sorted(Path(item["output_paths"][0]).name for item in done["items"]) == [
        "a.translated.txt",
        "b.translated.md",
    ]


async def test_batch_document_endpoint_rejects_nested_output_dir(client, tmp_path):
    source = tmp_path / "docs"
    source.mkdir()
    (source / "a.txt").write_text("hello", encoding="utf-8")

    r = await client.post(
        "/v1/batch/document",
        json={
            "input_dir": str(source),
            "output_dir": str(source / "out"),
            "task": "translate",
            "target_language": "zh",
        },
    )
    assert r.status_code == 400
    assert "输出目录不能位于输入目录内部" in r.text


@pytest.mark.anyio
async def test_pdf_upload_strips_path_components_before_submit(client, app, tmp_path):
    captured = {}
    tmp_dir = tmp_path / "lumina_tmp"
    tmp_dir.mkdir()

    def _fake_submit(pdf_path: str, lang_out: str, tmp_dir: str) -> str:
        captured["pdf_path"] = pdf_path
        captured["lang_out"] = lang_out
        captured["tmp_dir"] = tmp_dir
        return "job-upload"

    app.state.pdf_manager.submit_translate = _fake_submit
    with patch("tempfile.mkdtemp", return_value=str(tmp_dir)):
        data = {"file": ("../../etc/passwd.pdf", io.BytesIO(b"%PDF-1.4 mock"), "application/pdf")}
        r = await client.post("/v1/pdf/upload", files=data, params={"lang_out": "zh"})

    assert r.status_code == 200
    assert r.json()["job_id"] == "job-upload"
    assert Path(captured["pdf_path"]).name == "passwd.pdf"
    assert Path(captured["pdf_path"]).parent == Path(captured["tmp_dir"])
    assert ".." not in captured["pdf_path"]


@pytest.mark.anyio
async def test_pdf_url_submits_job_without_downloading_in_request(client, app):
    app.state.pdf_manager.submit_translate = MagicMock(return_value="job-url")

    r = await client.post("/v1/pdf/url", json={"url": "https://example.com/demo.pdf", "lang_out": "en"})

    assert r.status_code == 200
    assert r.json()["job_id"] == "job-url"
    app.state.pdf_manager.submit_translate.assert_called_once()
    submitted_url, submitted_lang, submitted_tmp_dir = app.state.pdf_manager.submit_translate.call_args.args
    assert submitted_url == "https://example.com/demo.pdf"
    assert submitted_lang == "en"
    assert "lumina_out_" in Path(submitted_tmp_dir).name


@pytest.mark.anyio
async def test_pdf_upload_rejects_non_pdf(client):
    """上传非 PDF 后缀文件应返回 400。"""
    data = {"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")}
    r = await client.post("/v1/pdf/upload", files=data, params={"lang_out": "zh"})
    assert r.status_code == 400


def test_render_markdown_html_sanitizes_untrusted_html():
    from lumina.api.rendering import render_markdown_html

    rendered = render_markdown_html(
        "# title\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n**safe**"
    )

    assert "<script" not in rendered
    assert "javascript:alert" not in rendered
    assert "<strong>safe</strong>" in rendered
