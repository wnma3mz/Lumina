__all__ = ["AudioRecorder", "Transcriber"]


def __getattr__(name: str):
    if name == "AudioRecorder":
        from .recorder import AudioRecorder

        return AudioRecorder
    if name == "Transcriber":
        from .transcriber import Transcriber

        return Transcriber
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
