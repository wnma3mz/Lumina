"""
unit/test_config.py — Config 加载、SamplingConfig 解析、环境变量覆盖测试。

使用临时 JSON 文件，不依赖真实 ~/.lumina/config.json。
"""
import json
import pytest

from lumina.config import Config, reset_config
from lumina.platform_support import runtime as runtime_mod


@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前后重置 config 单例，防止测试间污染。"""
    reset_config()
    yield
    reset_config()


def _write_config(tmp_path, data: dict) -> str:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def _base_config() -> dict:
    """最小合法配置。"""
    return {
        "provider": {"type": "local", "model_path": "/tmp/model"},
        "whisper_model": "whisper-tiny",
        "host": "127.0.0.1",
        "port": 31821,
        "log_level": "INFO",
        "system_prompts": {},
    }


# ── 基础加载 ─────────────────────────────────────────────────────────────────

class TestBasicLoad:
    def test_loads_host_and_port(self, tmp_path):
        cfg = Config(_write_config(tmp_path, {**_base_config(), "host": "0.0.0.0", "port": 9999}))
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 9999

    def test_loads_provider_type(self, tmp_path):
        cfg = Config(_write_config(tmp_path, _base_config()))
        assert cfg.provider.type == "local"

    def test_loads_model_path(self, tmp_path):
        cfg = Config(_write_config(tmp_path, _base_config()))
        assert cfg.provider.model_path == "/tmp/model"

    def test_provider_backend_is_derived(self, tmp_path):
        cfg = Config(_write_config(tmp_path, _base_config()))
        assert cfg.provider.backend in {"mlx", "llama_cpp", "openai"}

    def test_default_port_when_missing(self, tmp_path):
        data = _base_config()
        del data["port"]
        cfg = Config(_write_config(tmp_path, data))
        assert cfg.port == 31821

    def test_missing_model_path_uses_platform_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runtime_mod, "IS_MACOS", False)
        monkeypatch.setattr(runtime_mod, "IS_WINDOWS", True)
        monkeypatch.setattr(runtime_mod, "IS_LINUX", False)
        data = _base_config()
        data["provider"]["model_path"] = ""
        cfg = Config(_write_config(tmp_path, data))
        assert cfg.provider.backend == "llama_cpp"
        assert cfg.provider.model_path.endswith(".gguf")

    def test_legacy_mlx_whisper_model_maps_on_non_macos(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runtime_mod, "IS_MACOS", False)
        monkeypatch.setattr(runtime_mod, "IS_WINDOWS", True)
        monkeypatch.setattr(runtime_mod, "IS_LINUX", False)
        data = _base_config()
        data["whisper_model"] = runtime_mod.DEFAULT_MLX_WHISPER_MODEL
        cfg = Config(_write_config(tmp_path, data))
        assert cfg.whisper_model == runtime_mod.DEFAULT_FASTER_WHISPER_MODEL

    def test_system_prompts_loaded(self, tmp_path):
        data = {**_base_config(), "system_prompts": {"chat": "You are helpful."}}
        cfg = Config(_write_config(tmp_path, data))
        assert cfg.system_prompts["chat"] == "You are helpful."

    def test_branding_slogans_loaded(self, tmp_path):
        data = {
            **_base_config(),
            "branding": {"slogans": ["让 AI 留在本地", "你的本地 AI 工作台"]},
        }
        cfg = Config(_write_config(tmp_path, data))
        assert cfg.branding["slogans"] == ["让 AI 留在本地", "你的本地 AI 工作台"]

    def test_branding_username_loaded_and_trimmed(self, tmp_path):
        data = {
            **_base_config(),
            "branding": {"username": "  Lumina 用户  "},
        }
        cfg = Config(_write_config(tmp_path, data))
        assert cfg.branding["username"] == "Lumina 用户"

    def test_ui_home_loaded(self, tmp_path):
        data = {
            **_base_config(),
            "ui": {
                "home": {
                    "enabled_tabs": ["digest", "document", "image", "settings"],
                    "image_enabled": True,
                    "image_modules": ["image_ocr"],
                    "allow_local_override": False,
                }
            },
        }
        cfg = Config(_write_config(tmp_path, data))
        assert cfg.ui.home.enabled_tabs == ["digest", "document", "image", "settings"]
        assert cfg.ui.home.image_modules == ["image_ocr"]
        assert cfg.ui.home.allow_local_override is False

    def test_ui_home_legacy_tabs_migrated(self, tmp_path):
        data = {
            **_base_config(),
            "ui": {
                "home": {
                    "enabled_tabs": ["digest", "document", "image", "settings"],
                }
            },
        }
        cfg = Config(_write_config(tmp_path, data))
        assert cfg.ui.home.enabled_tabs == ["digest", "document", "image", "settings"]


# ── SamplingConfig 解析 ───────────────────────────────────────────────────────

class TestSamplingConfigParsing:
    def test_sampling_all_fields(self, tmp_path):
        data = _base_config()
        data["provider"]["sampling"] = {
            "temperature": 0.7,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.01,
            "presence_penalty": 0.5,
            "repetition_penalty": 1.1,
            "max_tokens": 512,
        }
        cfg = Config(_write_config(tmp_path, data))
        s = cfg.provider.sampling
        assert s.temperature == 0.7
        assert s.top_p == 0.95
        assert s.top_k == 20
        assert s.min_p == 0.01
        assert s.presence_penalty == 0.5
        assert s.repetition_penalty == 1.1
        assert s.max_tokens == 512

    def test_sampling_missing_fields_use_defaults(self, tmp_path):
        data = _base_config()
        data["provider"]["sampling"] = {"temperature": 0.5}
        cfg = Config(_write_config(tmp_path, data))
        s = cfg.provider.sampling
        assert s.temperature == 0.5
        # absent fields fall back to config.py defaults (not None)
        assert s.top_p == 0.8
        assert s.top_k == 20
        assert s.max_tokens == 512

    def test_sampling_absent_uses_all_defaults(self, tmp_path):
        cfg = Config(_write_config(tmp_path, _base_config()))
        s = cfg.provider.sampling
        # absent sampling block falls back to config.py defaults
        assert s.temperature == 0.7
        assert s.top_p == 0.8
        assert s.top_k == 20
        assert s.max_tokens == 512

    def test_sampling_readme_field_ignored(self, tmp_path):
        data = _base_config()
        data["provider"]["sampling"] = {
            "_readme": "some note",
            "temperature": 0.6,
        }
        cfg = Config(_write_config(tmp_path, data))
        assert cfg.provider.sampling.temperature == 0.6

    def test_sampling_top_k_parsed_as_int(self, tmp_path):
        data = _base_config()
        data["provider"]["sampling"] = {"top_k": 40}
        cfg = Config(_write_config(tmp_path, data))
        assert isinstance(cfg.provider.sampling.top_k, int)
        assert cfg.provider.sampling.top_k == 40

    def test_sampling_temperature_parsed_as_float(self, tmp_path):
        data = _base_config()
        data["provider"]["sampling"] = {"temperature": 1}   # int in JSON
        cfg = Config(_write_config(tmp_path, data))
        assert isinstance(cfg.provider.sampling.temperature, float)


# ── 环境变量覆盖 ─────────────────────────────────────────────────────────────

class TestEnvOverrides:
    def test_env_overrides_host(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LUMINA_HOST", "192.168.1.1")
        cfg = Config(_write_config(tmp_path, _base_config()))
        assert cfg.host == "192.168.1.1"

    def test_env_overrides_port(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LUMINA_PORT", "9999")
        cfg = Config(_write_config(tmp_path, _base_config()))
        assert cfg.port == 9999

    def test_env_overrides_provider_type(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LUMINA_PROVIDER_TYPE", "openai")
        cfg = Config(_write_config(tmp_path, _base_config()))
        assert cfg.provider.type == "openai"

    def test_env_overrides_model_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LUMINA_MODEL_PATH", "/env/model")
        cfg = Config(_write_config(tmp_path, _base_config()))
        assert cfg.provider.model_path == "/env/model"

    def test_env_overrides_openai_base_url(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LUMINA_OPENAI_BASE_URL", "http://remote:8080/v1")
        cfg = Config(_write_config(tmp_path, _base_config()))
        assert cfg.provider.openai.base_url == "http://remote:8080/v1"


# ── RequestHistoryConfig 解析 ─────────────────────────────────────────────────

class TestRequestHistoryConfig:
    def test_default_values(self, tmp_path):
        cfg = Config(_write_config(tmp_path, _base_config()))
        rh = cfg.request_history
        assert rh.enabled is True
        assert rh.retention_days == 14
        assert rh.max_total_mb == 512

    def test_custom_values(self, tmp_path):
        data = {**_base_config(), "request_history": {
            "enabled": False,
            "retention_days": 7,
            "max_total_mb": 100,
            "compress_after_days": 3,
            "cleanup_on_startup": False,
        }}
        cfg = Config(_write_config(tmp_path, data))
        rh = cfg.request_history
        assert rh.enabled is False
        assert rh.retention_days == 7
        assert rh.max_total_mb == 100
        assert rh.compress_after_days == 3
        assert rh.cleanup_on_startup is False

    def test_retention_days_clamped_to_zero(self, tmp_path):
        data = {**_base_config(), "request_history": {"retention_days": -5}}
        cfg = Config(_write_config(tmp_path, data))
        assert cfg.request_history.retention_days == 0

    def test_max_total_mb_clamped_to_one(self, tmp_path):
        data = {**_base_config(), "request_history": {"max_total_mb": 0}}
        cfg = Config(_write_config(tmp_path, data))
        assert cfg.request_history.max_total_mb == 1


# ── reset_config 单例隔离 ─────────────────────────────────────────────────────

class TestSingleton:
    def test_get_config_returns_same_instance(self, tmp_path):
        from lumina.config import get_config
        path = _write_config(tmp_path, _base_config())
        a = get_config(path)
        b = get_config(path)
        assert a is b

    def test_reset_config_forces_reload(self, tmp_path):
        from lumina.config import get_config
        path = _write_config(tmp_path, _base_config())
        a = get_config(path)
        reset_config()
        b = get_config(path)
        assert a is not b
