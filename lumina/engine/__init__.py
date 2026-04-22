__all__ = ["LLMEngine"]


def __getattr__(name: str):
    if name == "LLMEngine":
        from .llm import LLMEngine

        return LLMEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
