"""
lumina/providers/mlx_loader.py — mlx-lm 模型加载

职责：路径解析（本地目录 / HF Hub 缓存快照 / repo id 回退）、
模型加载（mlx_lm.load）、BatchGenerator / ThreadPoolExecutor 初始化。

无 warmup 逻辑（warmup 依赖 Layer 1/2 能力，留在 LocalProvider）。
LocalProvider 组合此类：self._loader = MlxModelLoader(...)；
load() 返回 (model, tokenizer, batch_generator, batch_executor)。
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional, Tuple

try:
    from mlx_lm import load as mlx_load
    from mlx_lm.generate import BatchGenerator
    _MLX_AVAILABLE = True
except ImportError:
    _MLX_AVAILABLE = False

logger = logging.getLogger("lumina")

_DEFAULT_MODEL_REPO_ID = "mlx-community/Qwen3.5-0.8B-4bit"
_DEFAULT_MODEL_DIRNAME = "qwen3.5-0.8b-4bit"


class MlxModelLoader:
    """Layer 0：模型路径解析、mlx_lm 加载、BatchGenerator 初始化。

    构造参数：
        model_path:                    用户配置的模型路径（空字符串 = 使用默认模型）。
        max_new_prefill_per_iter:      每轮最多接入的新 prefill 请求数，传给 BatchGenerator。
        use_builtin_batch_engine_fn:   Callable[[], bool] — 是否使用内置 BatchGenerator。
        use_dedicated_batch_executor_fn: Callable[[], bool] — 是否为 batch 分配专属线程池。
        eos_ids_fn:                    Callable[[], set[int]] — 获取 EOS token ids。
    """

    def __init__(
        self,
        model_path: str,
        max_new_prefill_per_iter: int,
        use_builtin_batch_engine_fn: Callable[[], bool],
        use_dedicated_batch_executor_fn: Callable[[], bool],
        eos_ids_fn: Callable[[], set],
    ) -> None:
        self.model_path = model_path
        self.max_new_prefill_per_iter = max_new_prefill_per_iter
        self._use_builtin_batch_engine = use_builtin_batch_engine_fn
        self._use_dedicated_batch_executor = use_dedicated_batch_executor_fn
        self._eos_ids = eos_ids_fn

    # ── 路径解析 ──────────────────────────────────────────────────────────────

    def _hf_hub_cache_dir(self) -> Path:
        hub_cache = os.environ.get("HUGGINGFACE_HUB_CACHE")
        if hub_cache:
            return Path(hub_cache).expanduser()
        hf_home = os.environ.get("HF_HOME")
        if hf_home:
            return Path(hf_home).expanduser() / "hub"
        return Path.home() / ".cache" / "huggingface" / "hub"

    def _find_cached_repo_snapshot(self, repo_id: str) -> Optional[str]:
        repo_cache_dir = (
            self._hf_hub_cache_dir()
            / f"models--{repo_id.replace('/', '--')}"
            / "snapshots"
        )
        if not repo_cache_dir.exists():
            return None

        candidates = [p for p in repo_cache_dir.iterdir() if p.is_dir()]
        if not candidates:
            return None

        # 优先返回最近访问/修改的快照目录。
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for snapshot_dir in candidates:
            if any(snapshot_dir.glob("*.safetensors")):
                return str(snapshot_dir)
        return None

    def resolve_target(self) -> str:
        """路径解析：本地目录 > HF Hub 缓存快照 > repo id 回退。"""
        raw_target = (self.model_path or "").strip()
        if not raw_target:
            cached = self._find_cached_repo_snapshot(_DEFAULT_MODEL_REPO_ID)
            if cached:
                logger.info("Use cached model snapshot: %s", cached)
                return cached
            return _DEFAULT_MODEL_REPO_ID

        expanded = Path(raw_target).expanduser()
        # 目录已存在：按本地模型目录加载。
        if expanded.exists():
            return str(expanded)

        # 不存在的本地路径不能直接传给 mlx_lm.load（会被当作 repo id 并校验失败）。
        is_path_like = (
            expanded.is_absolute()
            or raw_target.startswith(("~", ".", ".."))
            or os.path.sep in raw_target
            or (os.path.altsep and os.path.altsep in raw_target)
        )
        if is_path_like and expanded.name.lower() == _DEFAULT_MODEL_DIRNAME:
            cached = self._find_cached_repo_snapshot(_DEFAULT_MODEL_REPO_ID)
            if cached:
                logger.info(
                    "Local model path missing, use cached snapshot: %s -> %s",
                    expanded,
                    cached,
                )
                return cached
            logger.info(
                "Local model path not found, fallback to repo id download: %s -> %s",
                expanded,
                _DEFAULT_MODEL_REPO_ID,
            )
            return _DEFAULT_MODEL_REPO_ID

        return raw_target

    # ── 加载 ──────────────────────────────────────────────────────────────────

    def load(self, lazy: bool = False, offload_embedding: bool = False, offload_vision: bool = False, offload_audio: bool = False) -> Tuple:
        """加载模型。
        
        参数:
            lazy: 若为 True，则不执行任何预加载（全量磁盘映射）。
            offload_embedding: 若为 True，则 Embedding 层留在磁盘。
            offload_vision: 若为 True，则 Vision Tower 留在磁盘。
            offload_audio: 若为 True，则 Audio Tower 留在磁盘。
        """
        import mlx.core as mx
        import mlx.utils as mx_utils

        load_target = self.resolve_target()
        model, tokenizer = mlx_load(load_target)
        
        if lazy:
            logger.info("Lazy loading: all weights remain disk-mapped.")
        else:
            # 始终预加载 Transformer Layers (L1) 以保证速度，
            # 根据开关选择性卸载辅助塔 (L2)。
            offload_keywords = []
            if offload_embedding: offload_keywords.append("embed_tokens")
            if offload_vision: offload_keywords.extend(["visual", "vision_tower"])
            if offload_audio: offload_keywords.extend(["audio_tower"])

            if offload_keywords:
                logger.info(f"Hybrid loading: eager-loading backbone, offloading {offload_keywords}...")
                all_params = mx_utils.tree_flatten(model.parameters())
                to_eval = [p for name, p in all_params if not any(k in name for k in offload_keywords)]
                mx.eval(to_eval)
            else:
                logger.info("Eager loading: evaluating all model parameters...")
                mx.eval(model.parameters())

        batch_generator, batch_executor = self._init_batch_engine(model, tokenizer)
        return model, tokenizer, batch_generator, batch_executor

    def _init_batch_engine(self, model, tokenizer) -> Tuple:
        """初始化 BatchGenerator 和可选的专属 ThreadPoolExecutor。"""
        if not self._use_builtin_batch_engine():
            return None, None

        eos_ids = getattr(tokenizer, "eos_token_ids", None) or list(self._eos_ids())
        batch_generator = BatchGenerator(
            model,
            stop_tokens=set(eos_ids),
            prefill_batch_size=self.max_new_prefill_per_iter,
            completion_batch_size=max(8, self.max_new_prefill_per_iter * 4),
        )
        batch_executor: Optional[ThreadPoolExecutor] = None
        if self._use_dedicated_batch_executor():
            batch_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="lumina-batch"
            )
        return batch_generator, batch_executor
