import re

from fastapi import APIRouter, Request
from pydantic import BaseModel

from lumina.engine.request_context import request_context

router = APIRouter(tags=["game"])


class ScoreRequest(BaseModel):
    scene: str
    npc_personality: str
    current_score: int
    player_input: str


class ScoreResponse(BaseModel):
    score: int
    feedback: str


def _parse_score(raw: str) -> tuple[int, str]:
    """从 LLM 输出中解析分数和反馈，失败时返回默认值。"""
    score_match = re.search(r"分数[：:]\s*([1-5])", raw)
    feedback_match = re.search(r"反馈[：:]\s*(.+)", raw)

    score = int(score_match.group(1)) if score_match else 3
    feedback = feedback_match.group(1).strip() if feedback_match else "对方似乎在考虑你的话。"
    # 截断过长反馈
    if len(feedback) > 20:
        feedback = feedback[:20]
    return score, feedback


@router.post("/v1/game/score", response_model=ScoreResponse)
async def game_score(req: ScoreRequest, raw: Request) -> ScoreResponse:
    llm = raw.app.state.llm

    user_text = (
        f"场景：{req.scene}\n"
        f"对手性格：{req.npc_personality}\n"
        f"当前累计进度：{req.current_score}/10\n"
        f"玩家说：\"{req.player_input}\"\n"
        "请输出：\n分数: [整数]\n反馈: [一句话]"
    )

    with request_context(origin="game_score", stream=False):
        raw_text = await llm.generate(user_text, task="game_score")

    score, feedback = _parse_score(raw_text)

    # 若解析失败（默认值），尝试重试一次
    if not re.search(r"分数[：:]\s*([1-5])", raw_text):
        with request_context(origin="game_score", stream=False):
            raw_text2 = await llm.generate(user_text, task="game_score")
        score2, feedback2 = _parse_score(raw_text2)
        if re.search(r"分数[：:]\s*([1-5])", raw_text2):
            score, feedback = score2, feedback2

    return ScoreResponse(score=score, feedback=feedback)
