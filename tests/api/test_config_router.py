"""
unit/test_config_router.py — GET /v1/config 与 PATCH /v1/config 端点测试。

使用临时 config.json，mock LLMEngine，不依赖真实模型。
"""
import json
import logging
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from lumina.config import reset_config
from lumina.config_runtime import set_active_config_path
from tests.config_helpers import app_config, reset_config_state


@pytest.fixture(autouse=True)
def reset_singleton():
    reset_config_state()
    yield
    reset_config_state()


def _build_app(config_path: Path, *, config_data: dict | None = None):
    markdown_stub = types.SimpleNamespace(markdown=lambda text, extensions=None: text)
    if config_data is not None:
        config_path.write_text(json.dumps(config_data), encoding="utf-8")

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
        transcriber.model = "whisper-tiny"

        with patch("lumina.config_runtime.USER_CONFIG_PATH", config_path):
            with patch("lumina.config._CONFIG_PATH", str(config_path)):
                reset_config()
                set_active_config_path(str(config_path))
                app = create_app(llm=llm, transcriber=transcriber)
                app.state.digest_scheduler = MagicMock()
                return app, llm, transcriber


@pytest.fixture
def config_path(tmp_path) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(app_config()), encoding="utf-8")
    return p


@pytest.fixture
def client_and_llm(config_path):
    """返回 (AsyncClient factory, llm mock)，已 patch config 路径。"""
    app, llm, _ = _build_app(config_path)
    yield app, llm


@pytest.fixture
def openai_client_and_llm(config_path):
    from lumina.providers.openai import OpenAIProvider

    config_data = app_config()
    config_data["provider"]["type"] = "openai"
    config_data["provider"]["openai"] = {
        "base_url": "http://old.example/v1",
        "api_key": "old-key",
        "model": "old-model",
    }
    app, llm, _ = _build_app(config_path, config_data=config_data)
    llm._provider = OpenAIProvider(
        base_url="http://old.example/v1",
        api_key="old-key",
        model="old-model",
    )
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
        assert "prompts" in r.json()["provider"]
        assert r.json()["provider"]["prompts"]["chat"] == "You are helpful."

    async def test_hides_private_system_prompt_keys(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        assert "_readme" not in r.json()["provider"]["prompts"]

    async def test_returns_ptt_config(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        ptt = r.json()["audio"]["ptt"]
        assert ptt["enabled"] is False
        assert ptt["hotkey"] == "f5"

    async def test_returns_ui_home_config(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        home = r.json()["system"]["ui"]["home"]
        assert "document" in home["enabled_tabs"]
        assert "image" in home["enabled_tabs"]
        assert r.json()["vision"]["enabled_modules"] == ["image_ocr"]

    async def test_returns_desktop_config(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        assert r.json()["system"]["desktop"]["menubar_enabled"] is True

    async def test_returns_branding_config(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/config")
        assert r.json()["system"]["branding"]["username"] == ""
        assert r.json()["system"]["branding"]["slogans"] == ["让 AI 留在本地"]

    async def test_legacy_ui_tabs_are_normalized(self, config_path):
        payload = app_config()
        payload.setdefault("system", {})["ui"] = {"home": {"enabled_tabs": ["digest", "document", "image", "settings"]}}
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
        assert r.json()["system"]["ui"]["home"]["enabled_tabs"] == ["digest", "document", "image", "settings"]


# ── PATCH /v1/config ─────────────────────────────────────────────────────────

@pytest.mark.anyio
class TestPatchConfig:
    async def test_patch_system_prompt_returns_ok(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"provider": {"prompts": {"chat": "Be concise."}}})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    async def test_patch_system_prompt_not_restart_required(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"provider": {"prompts": {"chat": "Be concise."}}})
        assert r.json()["restart_required"] is False

    async def test_patch_system_prompt_hot_reloads_llm_and_config(self, client_and_llm):
        from lumina.config import get_config

        app, llm = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"provider": {"prompts": {"chat": "Be concise."}}})
        assert r.status_code == 200
        assert llm._system_prompts["chat"] == "Be concise."
        assert get_config().system_prompts["chat"] == "Be concise."

    async def test_patch_system_prompt_reloads_asr_prompts(self, client_and_llm):
        app, _ = client_and_llm
        with patch("lumina.services.audio.transcriber.set_asr_prompts") as set_asr_prompts:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch("/v1/config", json={"audio": {"prompts": {"asr_zh": "新的中文提示词"}}})
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

    async def test_patch_digest_updates_digest_singleton(self, client_and_llm):
        from lumina.services.digest.config import get_cfg

        app, _ = client_and_llm
        payload = {
            "digest": {
                "enabled": True,
                "active_watch_dirs": ["/tmp/watch-a", "/tmp/watch-b"],
                "notify_time": "09:30",
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json=payload)
        assert r.status_code == 200
        assert get_cfg().enabled is True
        assert get_cfg().notify_time == "09:30"
        assert get_cfg().active_watch_dirs == ["/tmp/watch-a", "/tmp/watch-b"]

    async def test_patch_request_history_updates_runtime_module(self, client_and_llm):
        from lumina.engine import request_history

        app, _ = client_and_llm
        payload = {
            "system": {
                "request_history": {
                    "enabled": False,
                    "retention_days": 3,
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json=payload)
        assert r.status_code == 200
        assert request_history.get_cfg().enabled is False
        assert request_history.get_cfg().retention_days == 3
        request_history.shutdown()

    async def test_patch_whisper_model_hot_reloads_transcriber(self, config_path):
        app, _, transcriber = _build_app(config_path)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"audio": {"whisper_model": "whisper-base"}})
        assert r.status_code == 200
        assert r.json()["restart_required"] is False
        assert transcriber.model == "whisper-base"

    async def test_patch_log_level_hot_reloads_loggers(self, client_and_llm):
        app, _ = client_and_llm
        logger_names = ("", "lumina", "uvicorn", "uvicorn.error", "uvicorn.access")
        previous_levels = {name: logging.getLogger(name).level for name in logger_names}
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch("/v1/config", json={"system": {"server": {"log_level": "DEBUG"}}})
            assert r.status_code == 200
            assert r.json()["restart_required"] is False
            assert logging.getLogger().level == logging.DEBUG
            assert logging.getLogger("lumina").level == logging.DEBUG
            assert logging.getLogger("uvicorn").level == logging.DEBUG
            assert logging.getLogger("uvicorn.error").level == logging.DEBUG
            assert logging.getLogger("uvicorn.access").level == logging.INFO
        finally:
            for name, level in previous_levels.items():
                logging.getLogger(name).setLevel(level)

    async def test_patch_openai_provider_settings_hot_reload(self, openai_client_and_llm):
        app, llm = openai_client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                "/v1/config",
                json={
                    "provider": {
                        "openai": {
                            "base_url": "http://new.example/v1",
                            "api_key": "new-key",
                            "model": "new-model",
                        }
                    }
                },
            )
        assert r.status_code == 200
        assert r.json()["restart_required"] is False
        assert llm._provider.base_url == "http://new.example/v1"
        assert llm._provider.api_key == "new-key"
        assert llm._provider.model == "new-model"

    async def test_patch_openai_settings_on_local_backend_keeps_runtime_provider(self, client_and_llm):
        app, llm = client_and_llm
        sentinel_provider = object()
        llm._provider = sentinel_provider
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                "/v1/config",
                json={"provider": {"openai": {"base_url": "http://new.example/v1"}}},
            )
        assert r.status_code == 200
        assert r.json()["restart_required"] is False
        assert llm._provider is sentinel_provider

    async def test_patch_mlx_memory_written_as_nested_block_without_backend(self, config_path, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"provider": {"mlx_memory": {"offload_audio": False}}})
            get_res = await c.get("/v1/config")
        assert r.status_code == 200
        assert r.json()["restart_required"] is True
        assert get_res.json()["provider"]["offload_audio"] is False

        written = json.loads(config_path.read_text())
        assert written["provider"]["mlx_memory"]["offload_audio"] is False
        assert "offload_audio" not in written["provider"]
        assert "backend" not in written["provider"]

    async def test_patch_offload_logs_saved_but_not_hot_reloaded(self, client_and_llm):
        app, _ = client_and_llm
        with patch("lumina.config_apply.logger.info") as info:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.patch("/v1/config", json={"provider": {"offload_embedding": False}})
        assert r.status_code == 200
        assert any(
            call.args == ("Config: %s config saved; no runtime hot-reload action", "provider")
            for call in info.call_args_list
        )
        assert not any(
            call.args == ("Config: %s config hot-reloaded", "provider")
            for call in info.call_args_list
        )

    async def test_patch_desktop_requires_restart(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"system": {"desktop": {"menubar_enabled": False}}})
            get_res = await c.get("/v1/config")
        assert r.status_code == 200
        assert r.json()["restart_required"] is True
        assert get_res.json()["system"]["desktop"]["menubar_enabled"] is False

    async def test_patch_ui_home_not_restart_required(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"ui": {"home": {"enabled_tabs": ["digest", "document", "image", "settings"]}}})
        assert r.status_code == 200
        assert r.json()["restart_required"] is False

    async def test_patch_ui_home_written_to_file(self, config_path, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.patch("/v1/config", json={"vision": {"enabled_modules": ["image_caption"]}})
        written = json.loads(config_path.read_text())
        assert written["vision"]["enabled_modules"] == ["image_caption"]

    async def test_patch_ui_home_legacy_tabs_written_as_document(self, config_path, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.patch("/v1/config", json={"ui": {"home": {"enabled_tabs": ["digest", "document", "image", "settings"]}}})
        written = json.loads(config_path.read_text())
        assert written["ui"]["home"]["enabled_tabs"] == ["digest", "document", "image", "settings"]

    async def test_patch_branding_username_not_restart_required(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"system": {"branding": {"username": "  Lu  "}}})
            get_res = await c.get("/v1/config")
        assert r.status_code == 200
        assert r.json()["restart_required"] is False
        assert get_res.json()["system"]["branding"]["username"] == "Lu"

    async def test_patch_branding_username_written_to_file(self, config_path, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.patch("/v1/config", json={"system": {"branding": {"username": "Lumina"}}})
        written = json.loads(config_path.read_text())
        assert written["system"]["branding"]["username"] == "Lumina"

    async def test_patch_port_requires_restart(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"system": {"server": {"port": 9999}}})
        assert r.json()["restart_required"] is True

    async def test_patch_host_requires_restart(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"system": {"server": {"host": "0.0.0.0"}}})
        assert r.json()["restart_required"] is True

    async def test_patch_offload_embedding_requires_restart(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={"provider": {"offload_embedding": False}})
        assert r.status_code == 200
        assert r.json()["restart_required"] is True

    async def test_patch_empty_body_ok(self, client_and_llm):
        app, _ = client_and_llm
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/v1/config", json={})
        assert r.status_code == 200
        assert r.json()["ok"] is True
