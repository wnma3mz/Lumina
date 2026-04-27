"""
lumina/providers/system_prompt_cache.py — System Prompt KV-Cache

对高频 system prompt 做 KV-cache 预填充并缓存（LRU 上限 32 条），
避免每次推理都重新计算 system prompt 部分的注意力。

──────────────────────────────────────────────────────────────────────────────
工作原理
──────────────────────────────────────────────────────────────────────────────
1. 用 sentinel 字符串占位，把 system prompt 渲染成完整 prompt 文本，
   再通过 tokenizer offset mapping 定位 sentinel 对应的 token 索引。
2. sentinel 索引之前的 tokens 即为 system prefix tokens，做一次完整 prefill
   写入 mlx_cache，得到 KV-cache 状态。
3. 命中时 clone_cache 拷贝缓存状态，将 suffix tokens（用户输入部分）继续送给模型，
   跳过对 prefix 的重复计算。

注意：SystemPromptCache 不拥有 model/tokenizer，通过构造参数外部传入。
render_fn 是 Callable[[str], Optional[List[int]]]，由 LocalProvider 通过
lambda 注入，将 system_text → prefix token ids，避免循环依赖。
"""
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from .local_offload import forward_with_cache

logger = logging.getLogger("lumina")

_SYSTEM_PROMPT_CACHE_SIZE = 32
_SYSTEM_PROMPT_SENTINEL = "<lumina_system_cache_user_probe_7a93d1e4>"

try:
    import mlx.core as mx
    from mlx_lm.generate import cache as mlx_cache
    _MLX_AVAILABLE = True
except ImportError:
    _MLX_AVAILABLE = False


@dataclass
class SystemPromptCacheEntry:
    """单个 system prompt 的缓存条目。"""
    system_text: str
    prefix_tokens: List[int]
    prompt_cache: List[Any]


class SystemPromptCache:
    """System Prompt KV-Cache，LRU 淘汰，最多保留 MAX_SIZE 条。

    使用方式：
        spc = SystemPromptCache(model, tokenizer)
        entry = spc.get_or_create(system_text, render_fn)
        if entry:
            cloned = spc.clone_cache(entry)
            suffix_tokens = prompt_tokens[len(entry.prefix_tokens):]
    """

    MAX_SIZE: int = _SYSTEM_PROMPT_CACHE_SIZE

    def __init__(
        self,
        model: Any,
        tokenizer: Any,
        loaded_as_vlm: bool = False,
        use_cpu_embedding: bool = True,
    ) -> None:
        self._model = model
        self._tokenizer = tokenizer
        self.loaded_as_vlm = loaded_as_vlm
        self.use_cpu_embedding = use_cpu_embedding
        self._cache: OrderedDict[str, SystemPromptCacheEntry] = OrderedDict()

    def get_or_create(
        self,
        system_text: str,
        render_fn: Callable[[str], Optional[List[int]]],
    ) -> Optional[SystemPromptCacheEntry]:
        """返回缓存条目（命中时移动到最新位置）；未命中时构建并写入缓存。

        Args:
            system_text: 原始 system prompt 字符串，作为 LRU key。
            render_fn:   由 LocalProvider 注入，将 system_text 转换为
                         prefix token ids（即 sentinel 之前的 token 列表）。
                         返回 None 表示 system prompt 不适合缓存（例如
                         sentinel 无法唯一定位，或 tokenizer 不支持 offset）。

        Returns:
            SystemPromptCacheEntry 或 None（不适合缓存时）。
        """
        cached = self._cache.get(system_text)
        if cached is not None:
            self._cache.move_to_end(system_text)
            logger.debug(
                "system_prompt_cache HIT  key_len=%d prefix_tokens=%d cache_size=%d",
                len(system_text), len(cached.prefix_tokens), len(self._cache),
            )
            return cached

        prefix_tokens = render_fn(system_text)
        if not prefix_tokens:
            logger.debug(
                "system_prompt_cache SKIP  key_len=%d (prefix derivation failed)",
                len(system_text),
            )
            return None

        logger.debug(
            "system_prompt_cache MISS  key_len=%d → building prefix cache (%d tokens)",
            len(system_text), len(prefix_tokens),
        )
        if getattr(self, "loaded_as_vlm", False):
            inner = getattr(self._model, "language_model", None) or self._model
            prompt_cache = mlx_cache.make_prompt_cache(inner)
        else:
            prompt_cache = mlx_cache.make_prompt_cache(self._model)
        self._prefill(prefix_tokens, prompt_cache)
        entry = SystemPromptCacheEntry(
            system_text=system_text,
            prefix_tokens=prefix_tokens,
            prompt_cache=self.clone_cache_raw(prompt_cache),
        )
        self._cache[system_text] = entry
        self._cache.move_to_end(system_text)
        while len(self._cache) > self.MAX_SIZE:
            evicted = next(iter(self._cache))
            self._cache.popitem(last=False)
            logger.debug("system_prompt_cache EVICT  key_len=%d (LRU)", len(evicted))
        return entry

    def clone_cache(self, entry: SystemPromptCacheEntry) -> List[Any]:
        """深拷贝条目的 prompt_cache（供单次推理独占使用）。"""
        return self.clone_cache_raw(entry.prompt_cache)

    @staticmethod
    def clone_cache_raw(prompt_cache: List[Any]) -> List[Any]:
        """深拷贝 mlx KV-cache 层列表。"""
        return [
            type(cache_layer).from_state(
                cache_layer.state,
                cache_layer.meta_state,
            )
            for cache_layer in prompt_cache
        ]

    def _prefill(self, prefix_tokens: List[int], prompt_cache: List[Any]) -> None:
        """将 prefix_tokens 批量送入模型，更新 prompt_cache。"""
        prompt = mx.array(prefix_tokens)
        while len(prompt) > 0:
            n_to_process = min(2048, len(prompt))
            inputs = prompt[:n_to_process][None]

            forward_with_cache(
                self._model,
                inputs,
                cache=prompt_cache,
                enable_cpu_embedding=self.use_cpu_embedding,
            )

            # 安全 eval state（兼容 ArraysCache 含 None 的情况）
            flat = []
            for c in prompt_cache:
                s = c.state
                if isinstance(s, list):
                    flat.extend(x for x in s if x is not None)
                elif s is not None:
                    flat.append(s)
            if flat:
                mx.eval(flat)

            prompt = prompt[n_to_process:]
            mx.clear_cache()
        for cache_layer in prompt_cache:
            if hasattr(cache_layer, "finalize"):
                cache_layer.finalize()
