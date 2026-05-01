from __future__ import annotations

import types

import lumina.providers.mlx.system_prompt_cache as spc_mod

from lumina.providers.local import LocalProvider, _RequestSlot
from lumina.providers.mlx.system_prompt_cache import SystemPromptCacheEntry as _SystemPromptCacheEntry
from tests.providers.local_provider_test_helpers import mx


def test_system_prompt_cache_reuses_prefix_cache(monkeypatch):
    from lumina.providers.mlx import system_prompt_cache as runtime_spc_mod
    from lumina.providers.mlx.system_prompt_cache import SystemPromptCache

    class FakeCacheLayer:
        def __init__(self, state):
            self._state = state

        @property
        def state(self):
            return self._state

        @property
        def meta_state(self):
            return ()

        @classmethod
        def from_state(cls, state, meta_state):
            return cls(state)

    provider = LocalProvider(model_path="synthetic")
    provider._model = object()
    provider._tokenizer = object()
    provider._spc = SystemPromptCache(provider._model, provider._tokenizer)

    prefill_calls = []

    monkeypatch.setattr(provider, "_derive_system_prefix_tokens", lambda system_text: [1, 2, 3])
    monkeypatch.setattr(runtime_spc_mod.mlx_cache, "make_prompt_cache", lambda model: [FakeCacheLayer((mx.array([1, 2, 3]),))])
    monkeypatch.setattr(
        provider._spc,
        "_prefill",
        lambda prompt_tokens, prompt_cache: prefill_calls.append(list(prompt_tokens)),
    )

    first = provider._get_or_create_system_prompt_cache("same system")
    second = provider._get_or_create_system_prompt_cache("same system")

    assert first is second
    assert prefill_calls == [[1, 2, 3]]


def test_prepare_batch_generator_prompt_falls_back_when_prefix_mismatches():
    from lumina.providers.mlx.system_prompt_cache import SystemPromptCache

    class FakeCacheLayer:
        def __init__(self, state):
            self._state = state

        @property
        def state(self):
            return self._state

        @property
        def meta_state(self):
            return ()

        @classmethod
        def from_state(cls, state, meta_state):
            return cls(state)

    provider = LocalProvider(model_path="synthetic")
    provider._model = object()
    provider._tokenizer = object()
    provider._spc = SystemPromptCache(provider._model, provider._tokenizer)
    provider._spc._cache["sys"] = _SystemPromptCacheEntry(
        system_text="sys",
        prefix_tokens=[9, 9],
        prompt_cache=[FakeCacheLayer((mx.array([1]),))],
    )

    slot = _RequestSlot(
        request_id="slot",
        prompt_tokens=mx.array([1, 2, 3, 4]),
        max_tokens=8,
        temperature=0.0,
        system_text="sys",
        user_text="hello",
    )
    prompt_tokens, prompt_cache = provider._prepare_batch_generator_prompt(slot)

    assert prompt_tokens == [1, 2, 3, 4]
    assert prompt_cache is None


def test_prepare_batch_generator_prompt_reconstructs_full_prompt_when_cache_hits():
    from lumina.providers.mlx.system_prompt_cache import SystemPromptCache

    class FakeCacheLayer:
        def __init__(self, state):
            self._state = state

        @property
        def state(self):
            return self._state

        @property
        def meta_state(self):
            return ()

        @classmethod
        def from_state(cls, state, meta_state):
            return cls(state)

    provider = LocalProvider(model_path="synthetic")
    provider._model = object()
    provider._tokenizer = object()
    provider._spc = SystemPromptCache(provider._model, provider._tokenizer)
    provider._spc._cache["sys"] = _SystemPromptCacheEntry(
        system_text="sys",
        prefix_tokens=[1, 2],
        prompt_cache=[FakeCacheLayer((mx.array([7, 8]),))],
    )

    slot = _RequestSlot(
        request_id="slot",
        prompt_tokens=mx.array([1, 2, 3, 4, 5]),
        max_tokens=8,
        temperature=0.0,
        system_text="sys",
        user_text="hello",
    )
    suffix_tokens, prompt_cache = provider._prepare_batch_generator_prompt(slot)

    assert prompt_cache is not None
    assert provider._spc._cache["sys"].prefix_tokens + suffix_tokens == [1, 2, 3, 4, 5]


def test_system_prompt_cache_evicts_lru():
    from lumina.providers.mlx.system_prompt_cache import SystemPromptCache, SystemPromptCacheEntry

    class FakeCacheLayer:
        @property
        def state(self):
            return mx.array([0])

        @property
        def meta_state(self):
            return ()

        @classmethod
        def from_state(cls, state, meta_state):
            return cls()

    spc = SystemPromptCache(model=object(), tokenizer=object())
    for i in range(33):
        key = f"sys_{i}"
        entry = SystemPromptCacheEntry(
            system_text=key,
            prefix_tokens=[i],
            prompt_cache=[FakeCacheLayer()],
        )
        spc._cache[key] = entry
        spc._cache.move_to_end(key)
        while len(spc._cache) > spc.MAX_SIZE:
            spc._cache.popitem(last=False)

    assert len(spc._cache) == 32
    assert "sys_0" not in spc._cache
    assert "sys_32" in spc._cache


def test_system_prompt_cache_respects_cpu_embedding_toggle(monkeypatch):
    class FakeCacheLayer:
        def __init__(self):
            self.finalized = False

        @property
        def state(self):
            return mx.array([0])

        def finalize(self):
            self.finalized = True

    class FakeEmbedLayer:
        def __init__(self):
            self.calls = 0

        def __call__(self, inputs):
            self.calls += 1
            return ("embeddings", tuple(int(x) for x in inputs.reshape(-1).tolist()))

    class FakeModel:
        def __init__(self):
            self.model = types.SimpleNamespace(embed_tokens=FakeEmbedLayer())
            self.calls = []

        def __call__(self, inputs, cache=None):
            self.calls.append(inputs)
            return mx.zeros((1, 1, 4))

    prompt_cache = [FakeCacheLayer()]
    model = FakeModel()
    spc = spc_mod.SystemPromptCache(
        model=model,
        tokenizer=object(),
        use_cpu_embedding=False,
    )
    spc._prefill([1, 2], prompt_cache)
    assert model.model.embed_tokens.calls == 0
    assert len(model.calls) == 1
    assert hasattr(model.calls[0], "shape")

    prompt_cache = [FakeCacheLayer()]
    model = FakeModel()
    spc = spc_mod.SystemPromptCache(
        model=model,
        tokenizer=object(),
        use_cpu_embedding=True,
    )
    spc._prefill([1, 2], prompt_cache)
    assert model.model.embed_tokens.calls == 1
    assert model.calls[0][0] == "embeddings"
