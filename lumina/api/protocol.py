"""
OpenAI 兼容协议 + 扩展。

标准接口：
    POST /v1/chat/completions
    POST /v1/audio/transcriptions
    GET  /v1/models

扩展接口（便捷入口，内部映射到 chat/completions）：
    POST /v1/translate
    POST /v1/summarize
    POST /v1/polish
"""
import time
import uuid
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field

from lumina.sampling import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MIN_P,
    DEFAULT_PRESENCE_PENALTY,
    DEFAULT_REPETITION_PENALTY,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
)


def random_uuid() -> str:
    return uuid.uuid4().hex


# ── Chat Completions ──────────────────────────────────────────────────────────

class MessageContent(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: Union[str, List[MessageContent]]


class ChatCompletionRequest(BaseModel):
    model: str = "lumina"
    messages: List[ChatMessage]
    stream: bool = False
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    top_p: float = DEFAULT_TOP_P
    top_k: int = DEFAULT_TOP_K
    min_p: float = DEFAULT_MIN_P
    presence_penalty: float = DEFAULT_PRESENCE_PENALTY
    repetition_penalty: float = DEFAULT_REPETITION_PENALTY


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Optional[str] = "stop"


class ChatCompletionStreamDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None


class ChatCompletionStreamChoice(BaseModel):
    index: int = 0
    delta: ChatCompletionStreamDelta
    finish_reason: Optional[str] = None


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{random_uuid()}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "lumina"
    choices: List[ChatCompletionChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


class ChatCompletionStreamResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{random_uuid()}")
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "lumina"
    choices: List[ChatCompletionStreamChoice]


# ── Audio Transcription ───────────────────────────────────────────────────────

class TranscriptionResponse(BaseModel):
    text: str


# ── 便捷接口 ──────────────────────────────────────────────────────────────────

class TranslateRequest(BaseModel):
    text: str
    target_language: Literal["zh", "en"] = "zh"
    stream: bool = False


class SummarizeRequest(BaseModel):
    text: str
    stream: bool = False


class TextResponse(BaseModel):
    text: str


class PolishRequest(BaseModel):
    text: str
    language: Literal["zh", "en"] = "zh"
    stream: bool = False


# ── PDF 接口 ──────────────────────────────────────────────────────────────────

class PdfUrlRequest(BaseModel):
    url: str
    lang_out: str = "zh"


# ── 录音控制 ──────────────────────────────────────────────────────────────────

class RecordStartResponse(BaseModel):
    session_id: str
    status: Literal["recording"] = "recording"


class RecordStopRequest(BaseModel):
    session_id: str
    language: Optional[str] = None


# ── 模型列表 ──────────────────────────────────────────────────────────────────

class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "lumina"


class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelCard]
