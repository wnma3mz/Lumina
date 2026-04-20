"""
unit/test_config_router.py — GET /v1/config 与 PATCH /v1/config 端点测试。

使用临时 config.json，mock LLMEngine，不依赖真实模型。
"""
import json
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from lumina.config import reset_config
from lumina.config_runtime import set_active_config_path


@pytest.fixture(autouse=True)
def reset_singleton():
    reset_config()
    set_active_config_path(None)
    yield
    reset_config()
    set_active_config_path(None)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _base_config() -> dict:
    return {
        "provider": {
            "type": "local",
            "model_path": "/tmp/model",
            "sampling": {
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "repetition_penalty": 1.0,
                "max_tokens": 512,
            },
            "openai": {"base_url": "", "api_key": "", "model": ""},
        },
        "whisper_model": "whisper-tiny",
        "host": "127.0.0.1",
        "port": 31821,
        "log_level": "INFO",
        "system_prompts": {"_readme": "internal", "chat": "You are helpful."},
        "digest": {"enabled": False},
        "ptt": {"enabled": False, "hotkey": "f5", "language": "zh"},
        "desktop": {"menubar_enabled": True},
        "request_history": {"enabled": True},
        "branding": {"username": "", "slogans": ["让 AI 留在本地"]},
        "ui": {
            "home": {
                "enabled_tabs": ["digest", "document", "image", "settings"],
                "image_enabled": True,
                "image_modules": ["image_ocr"],
                "allow_local_override": True,
            }
        },
    }


@pytest.fixture
def config_path(tmp_path) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(_base_config()), encoding="utf-8")
    return p


@pytest.fixture
def client_and_llm(config_path):
    """返回 (AsyncClient factory, llm mock)，已 patch config 路径。"""
    markdown_stub = types.SimpleNamespace(markdown=lambda text, extensions=None: text)
    with patch.dict(
        "sys.modules",
        {
            "sounddevice": MagicMock(),
            "markdown": markdown_stub,
            "nh3": MagicMock(),
            "numpy": MagicMock(),
            "scipy": MagicMock(),
            "mlx_whisper": MagicMock(),
        },
    ):
        from lumina.api.server import create_app
        from lumina.engine.llm import LLMEngine
        from lumina.services.audio.transcriber import Transcriber

        llm = MagicMock(spec=LLMEngine)
        llm.is_loaded = True
        llm._system_prompts = {"chat": "You are helpful."}
        llm.generate = AsyncMock(return_value="ok")
        llm.generate_stream = AsyncMock()

        transcriber = MagicMock(spec=Transcriber)
        transcriber.is_loaded = False

        with patch("lumina.config_runtime.USER_CONFIG_PATH", config_path):
            with patch("lumina.config._CONFIG_PATH", str(config_path)):
                reset_config()
                set_active_config_path(str(config_path))
                app = create_app(llm=llm, transcriber=transcriber)
                app.state.digest_scheduler = MagicMock()
                yield app, llm


# ── GET /v1/config ────────────────────────────────────────────────────────────

@pytest.mark.anyio
class TestGetConfig:
    async def test_returns_provider_type(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        assert r.status_code == 200
        assert r.json()["provider"]["type"] == "local"

    async def test_returns_provider_backend(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        assert r.status_code == 200
        assert r.json()["provider"]["backend"] in {"mlx", "llama_cpp", "openai"}

    async def test_returns_sampling_params(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        sampling = r.json()["provider"]["sampling"]
        assert sampling["temperature"] == 0.6
        assert sampling["top_p"] == 0.95
        assert sampling["max_tokens"] == 512

    async def test_returns_system_prompts(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        assert "system_prompts" in r.json()
        assert r.json()["system_prompts"]["chat"] == "You are helpful."

    async def test_hides_private_system_prompt_keys(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        assert "_readme" not in r.json()["system_prompts"]

    async def test_returns_ptt_config(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        ptt = r.json()["ptt"]
        assert ptt["enabled"] is False
        assert ptt["hotkey"] == "f5"

    async def test_returns_ui_home_config(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        home = r.json()["ui"]["home"]
        assert "document" in home["enabled_tabs"]
        assert "image" in home["enabled_tabs"]
        assert home["image_modules"] == ["image_ocr"]

    async def test_returns_desktop_config(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        assert r.json()["desktop"]["menubar_enabled"] is True

    async def test_returns_branding_config(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        assert r.json()["branding"]["username"] == ""
        assert r.json()["branding"]["slogans"] == ["让 AI 留在本地"]

    async def test_legacy_ui_tabs_are_normalized(self, config_path):
        payload = _base_config()
        payload["ui"]["home"]["enabled_tabs"] = ["digest", "document", "image", "settings"]
        config_path.write_text(json.dumps(payload), encoding="utf-8")

        markdown_stub = types.SimpleNamespace(markdown=lambda text, extensions=None: text)
        with patch.dict(
            "sys.modules",
            {
                "sounddevice": MagicMock(),
                "markdown": markdown_stub,
                "nh3": MagicMock(),
                "numpy": MagicMock(),
                "scipy": MagicMock(),
                "mlx_whisper": MagicMock(),
            },
        ):
            from lumina.api.server import create_app
            from lumina.engine.llm import LLMEngine
            from lumina.services.audio.transcriber import Transcriber

            llm = MagicMock(spec=LLMEngine)
            llm.is_loaded = True
            llm._system_prompts = {"chat": "You are helpful."}
            llm.generate = AsyncMock(return_value="ok")
            llm.generate_stream = AsyncMock()

            transcriber = MagicMock(spec=Transcriber)
            transcriber.is_loaded = False

            with patch("lumina.config_runtime.USER_CONFIG_PATH", config_path):
                with patch("lumina.config._CONFIG_PATH", str(config_path)):
                    reset_config()
                    set_active_config_path(str(config_path))
                    app = create_app(llm=llm, transcriber=transcriber)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        assert r.json()["ui"]["home"]["enabled_tabs"] == ["digest", "document", "image", "settings"]


# ── PATCH /v1/config ─────────────────────────────────────────────────────────

@pytest.mark.anyio
class TestPatchConfig:
    async def test_patch_system_prompt_returns_ok(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"system_prompts": {"chat": "Be concise."}})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    async def test_patch_system_prompt_not_restart_required(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"system_prompts": {"chat": "Be concise."}})
        assert r.json()["restart_required"] is False

    async def test_patch_system_prompt_hot_reloads_llm_and_config(self, client_and_llm):
        from lumina.config import get_config

        app, llm = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"system_prompts": {"chat": "Be concise."}})
        assert r.status_code == 200
        assert llm._system_prompts["chat"] == "Be concise."
        assert get_config().system_prompts["chat"] == "Be concise."

    async def test_patch_system_prompt_reloads_asr_prompts(self, client_and_llm):
        app, _ = client_and_llm
        with patch("lumina.services.audio.transcriber.set_asr_prompts") as set_asr_prompts:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch("/v1/config", json={"system_prompts": {"asr_zh": "新的中文提示词"}})
        assert r.status_code == 200
        set_asr_prompts.assert_called_once_with(zh="新的中文提示词", en="")

    async def test_patch_provider_type_requires_restart(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"provider": {"type": "openai"}})
        assert r.json()["restart_required"] is True

    async def test_patch_model_path_requires_restart(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"provider": {"model_path": "/new/model"}})
        assert r.json()["restart_required"] is True

    async def test_patch_llama_cpp_requires_restart(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"provider": {"llama_cpp": {"n_ctx": 8192}}})
        assert r.json()["restart_required"] is True

    async def test_patch_sampling_no_restart_required(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"provider": {"sampling": {"temperature": 0.9}}})
        assert r.status_code == 200
        assert r.json()["restart_required"] is False

    async def test_patch_sampling_written_to_file(self, config_path, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.patch("/v1/config", json={"provider": {"sampling": {"temperature": 0.42}}})
        written = json.loads(config_path.read_text())
        assert written["provider"]["sampling"]["temperature"] == 0.42

    async def test_patch_digest_not_restart_required(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"digest": {"enabled": True}})
        assert r.json()["restart_required"] is False

    async def test_patch_digest_updates_get_config_response(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            patch_res = await c.patch("/v1/config", json={"digest": {"enabled": True, "notify_time": "09:30"}})
            get_res = await c.get("/v1/config")
        assert patch_res.status_code == 200
        assert get_res.json()["digest"]["enabled"] is True
        assert get_res.json()["digest"]["notify_time"] == "09:30"

    async def test_patch_digest_reloads_scheduler(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"digest": {"enabled": True}})
        assert r.status_code == 200
        app.state.digest_scheduler.reload.assert_called_once_with(run_startup=True)

    async def test_patch_desktop_requires_restart(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"desktop": {"menubar_enabled": False}})
            get_res = await c.get("/v1/config")
        assert r.status_code == 200
        assert r.json()["restart_required"] is True
        assert get_res.json()["desktop"]["menubar_enabled"] is False

    async def test_patch_ui_home_not_restart_required(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"ui": {"home": {"enabled_tabs": ["digest", "document", "image", "settings"]}}})
        assert r.status_code == 200
        assert r.json()["restart_required"] is False

    async def test_patch_ui_home_written_to_file(self, config_path, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.patch("/v1/config", json={"ui": {"home": {"image_modules": ["image_caption"]}}})
        written = json.loads(config_path.read_text())
        assert written["ui"]["home"]["image_modules"] == ["image_caption"]

    async def test_patch_ui_home_legacy_tabs_written_as_document(self, config_path, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.patch("/v1/config", json={"ui": {"home": {"enabled_tabs": ["digest", "document", "image", "settings"]}}})
        written = json.loads(config_path.read_text())
        assert written["ui"]["home"]["enabled_tabs"] == ["digest", "document", "image", "settings"]

    async def test_patch_branding_username_not_restart_required(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"branding": {"username": "  Lu  "}})
            get_res = await c.get("/v1/config")
        assert r.status_code == 200
        assert r.json()["restart_required"] is False
        assert get_res.json()["branding"]["username"] == "Lu"

    async def test_patch_branding_username_written_to_file(self, config_path, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.patch("/v1/config", json={"branding": {"username": "Lumina"}})
        written = json.loads(config_path.read_text())
        assert written["branding"]["username"] == "Lumina"

    async def test_patch_port_requires_restart(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"port": 9999})
        assert r.json()["restart_required"] is True

    async def test_patch_empty_body_ok(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={})
        assert r.status_code == 200
        assert r.json()["ok"] is True
