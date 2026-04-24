import re

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from lumina.engine.request_context import request_context

router = APIRouter(tags=["game"])


class OpeningRequest(BaseModel):
    scene: str
    npc_name: str
    npc_personality: str


class OpeningResponse(BaseModel):
    opening: str


class ScoreRequest(BaseModel):
    scene: str
    npc_name: str
    npc_personality: str
    current_score: int = Field(ge=0, le=15)
    player_input: str = Field(max_length=500)


class ScoreResponse(BaseModel):
    score: int
    feedback: str


def _parse_score(raw: str) -> tuple[int, str]:
    """从 LLM 输出中解析分数和反馈，失败时返回默认值。"""
    score_match = re.search(r"分数[：:]\s*([1-5])", raw)
    feedback_match = re.search(r"反馈[：:]\s*(.+)", raw)

    score = int(score_match.group(1)) if score_match else 3
    feedback = feedback_match.group(1).strip() if feedback_match else "对方似乎在考虑你的话。"
    if len(feedback) > 30:
        feedback = feedback[:30]
    return score, feedback


@router.post("/v1/game/opening", response_model=OpeningResponse)
async def game_opening(req: OpeningRequest, raw: Request) -> OpeningResponse:
    llm = raw.app.state.llm

    user_text = (
        f"场景：{req.scene}\n"
        f"你是：{req.npc_name}，性格：{req.npc_personality}\n"
        "请说一句符合你性格的开场白（不超过30字，每次可以有所变化）。\n只输出开场白本身，不加引号。"
    )

    with request_context(origin="game_opening", stream=False):
        raw_text = await llm.generate(user_text, task="game_opening")

    opening = raw_text.strip()[:50] or f"（{req.npc_name}看向你）"
    return OpeningResponse(opening=opening)


@router.post("/v1/game/score", response_model=ScoreResponse)
async def game_score(req: ScoreRequest, raw: Request) -> ScoreResponse:
    llm = raw.app.state.llm

    user_text = (
        f"场景：{req.scene}\n"
        f"你是：{req.npc_name}，性格：{req.npc_personality}\n"
        f"当前累计进度：{req.current_score}/10\n"
        f"对方说：\"{req.player_input}\"\n"
        "请以你的身份和语气回应，严格按格式输出：\n分数: [1-5整数]\n反馈: [你说的一句话，符合你的性格，30字以内]"
    )

    with request_context(origin="game_score", stream=False):
        raw_text = await llm.generate(user_text, task="game_score")

    score, feedback = _parse_score(raw_text)

    if not re.search(r"分数[：:]\s*([1-5])", raw_text):
        with request_context(origin="game_score", stream=False):
            raw_text2 = await llm.generate(user_text, task="game_score")
        score2, feedback2 = _parse_score(raw_text2)
        if re.search(r"分数[：:]\s*([1-5])", raw_text2):
            score, feedback = score2, feedback2

    return ScoreResponse(score=score, feedback=feedback)
