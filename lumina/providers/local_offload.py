from __future__ import annotations

from typing import Any, Optional

try:
    import mlx.core as mx

    _MLX_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised on non-macOS CI
    mx = None
    _MLX_AVAILABLE = False


def maybe_embed_on_cpu(model: Any, input_ids: Any, *, enable_cpu_embedding: bool) -> Optional[Any]:
    if not enable_cpu_embedding or not _MLX_AVAILABLE:
        return None

    model_internal = getattr(model, "model", model)
    embed_layer = getattr(model_internal, "embed_tokens", None)
    if embed_layer is None:
        return None

    with mx.stream(mx.cpu):
        return embed_layer(input_ids)


def forward_with_cache(model: Any, inputs: Any, *, cache: Any, enable_cpu_embedding: bool) -> Any:
    embeddings = maybe_embed_on_cpu(
        model,
        inputs,
        enable_cpu_embedding=enable_cpu_embedding,
    )
    if embeddings is not None:
        return model(embeddings, cache=cache)
    return model(inputs, cache=cache)
