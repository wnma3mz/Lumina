"""
unit/test_sampling.py — resolve_sampling() 三层优先级合并测试。

不依赖任何外部库，纯函数测试。
"""
from lumina.config import SamplingConfig
from lumina.engine.sampling import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MIN_P,
    DEFAULT_PRESENCE_PENALTY,
    DEFAULT_REPETITION_PENALTY,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
    resolve_sampling,
)


def _cfg(**kwargs) -> SamplingConfig:
    """快速构造 SamplingConfig，未指定字段为 None。"""
    return SamplingConfig(**kwargs)


def _resolve(cfg: SamplingConfig, **api_kwargs) -> dict:
    """调用 resolve_sampling，未传的 API 参数默认为 None。"""
    defaults = dict(
        temperature=None,
        top_p=None,
        top_k=None,
        min_p=None,
        presence_penalty=None,
        repetition_penalty=None,
        max_tokens=None,
    )
    defaults.update(api_kwargs)
    return resolve_sampling(cfg, **defaults)


# ── 全部为 None 时使用 DEFAULT 常量 ───────────────────────────────────────────

class TestFallbackToDefault:
    def test_all_none_uses_defaults(self):
        result = _resolve(_cfg())
        assert result["temperature"] == DEFAULT_TEMPERATURE
        assert result["top_p"] == DEFAULT_TOP_P
        assert result["top_k"] == DEFAULT_TOP_K
        assert result["min_p"] == DEFAULT_MIN_P
        assert result["presence_penalty"] == DEFAULT_PRESENCE_PENALTY
        assert result["repetition_penalty"] == DEFAULT_REPETITION_PENALTY
        assert result["max_tokens"] == DEFAULT_MAX_TOKENS

    def test_result_has_no_none_values(self):
        result = _resolve(_cfg())
        for key, val in result.items():
            assert val is not None, f"{key} should not be None"

    def test_returns_all_seven_fields(self):
        result = _resolve(_cfg())
        expected_keys = {
            "temperature", "top_p", "top_k", "min_p",
            "presence_penalty", "repetition_penalty", "max_tokens",
        }
        assert set(result.keys()) == expected_keys


# ── config 层覆盖 DEFAULT ────────────────────────────────────────────────────

class TestConfigOverridesDefault:
    def test_temperature_from_config(self):
        result = _resolve(_cfg(temperature=0.9))
        assert result["temperature"] == 0.9

    def test_top_p_from_config(self):
        result = _resolve(_cfg(top_p=0.85))
        assert result["top_p"] == 0.85

    def test_top_k_from_config(self):
        result = _resolve(_cfg(top_k=50))
        assert result["top_k"] == 50

    def test_max_tokens_from_config(self):
        result = _resolve(_cfg(max_tokens=1024))
        assert result["max_tokens"] == 1024

    def test_partial_config_uses_default_for_missing(self):
        # 只配置 temperature，其余应 fallback 到 DEFAULT
        result = _resolve(_cfg(temperature=0.7))
        assert result["temperature"] == 0.7
        assert result["top_p"] == DEFAULT_TOP_P
        assert result["top_k"] == DEFAULT_TOP_K

    def test_all_config_fields(self):
        cfg = _cfg(
            temperature=0.5,
            top_p=0.9,
            top_k=10,
            min_p=0.05,
            presence_penalty=0.3,
            repetition_penalty=1.1,
            max_tokens=256,
        )
        result = _resolve(cfg)
        assert result["temperature"] == 0.5
        assert result["top_p"] == 0.9
        assert result["top_k"] == 10
        assert result["min_p"] == 0.05
        assert result["presence_penalty"] == 0.3
        assert result["repetition_penalty"] == 1.1
        assert result["max_tokens"] == 256


# ── API 层覆盖 config ────────────────────────────────────────────────────────

class TestApiOverridesConfig:
    def test_api_temperature_overrides_config(self):
        result = _resolve(_cfg(temperature=0.9), temperature=0.1)
        assert result["temperature"] == 0.1

    def test_api_temperature_zero_overrides_config(self):
        # 0.0 是合法的显式值，不应被忽略
        result = _resolve(_cfg(temperature=0.9), temperature=0.0)
        assert result["temperature"] == 0.0

    def test_api_max_tokens_overrides_config(self):
        result = _resolve(_cfg(max_tokens=256), max_tokens=2048)
        assert result["max_tokens"] == 2048

    def test_api_overrides_all_fields(self):
        cfg = _cfg(
            temperature=0.9, top_p=0.99, top_k=100,
            min_p=0.1, presence_penalty=1.0, repetition_penalty=1.5, max_tokens=128,
        )
        result = _resolve(
            cfg,
            temperature=0.3, top_p=0.8, top_k=5,
            min_p=0.02, presence_penalty=0.2, repetition_penalty=1.05, max_tokens=512,
        )
        assert result["temperature"] == 0.3
        assert result["top_p"] == 0.8
        assert result["top_k"] == 5
        assert result["min_p"] == 0.02
        assert result["presence_penalty"] == 0.2
        assert result["repetition_penalty"] == 1.05
        assert result["max_tokens"] == 512

    def test_api_partial_override_leaves_config_for_rest(self):
        cfg = _cfg(temperature=0.9, top_p=0.8)
        result = _resolve(cfg, temperature=0.1)
        assert result["temperature"] == 0.1   # API 覆盖
        assert result["top_p"] == 0.8          # config 值
        assert result["top_k"] == DEFAULT_TOP_K  # DEFAULT fallback


# ── 优先级完整链验证 ─────────────────────────────────────────────────────────

class TestPriorityChain:
    def test_priority_api_gt_config_gt_default(self):
        # temperature: API=0.1, config=0.7, default=DEFAULT_TEMPERATURE
        # top_p:       API=None, config=0.88, default=DEFAULT_TOP_P
        # top_k:       API=None, config=None, default=DEFAULT_TOP_K
        cfg = _cfg(temperature=0.7, top_p=0.88)
        result = _resolve(cfg, temperature=0.1)
        assert result["temperature"] == 0.1       # API wins
        assert result["top_p"] == 0.88            # config wins
        assert result["top_k"] == DEFAULT_TOP_K   # default wins

    def test_api_none_does_not_override_config(self):
        # 明确传 None 不应覆盖 config
        result = _resolve(_cfg(temperature=0.6), temperature=None)
        assert result["temperature"] == 0.6

    def test_config_none_does_not_override_default(self):
        # SamplingConfig 字段为 None 不应覆盖 DEFAULT
        result = _resolve(_cfg(temperature=None))
        assert result["temperature"] == DEFAULT_TEMPERATURE
