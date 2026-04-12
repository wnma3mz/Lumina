"""
Lumina HTTP 服务

提供 OpenAI 兼容接口 + 语音录制转写接口 + PWA 前端。
路由逻辑拆分到 lumina/api/routers/，业务服务在 lumina/services/。

依赖注入：各路由从 request.app.state 获取 llm / transcriber / pdf_manager，
不再使用模块级全局变量和 init_router() 模式。
"""
import asyncio
import sys as _sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from lumina.asr.transcriber import Transcriber
from lumina.engine.llm import LLMEngine

try:
    from importlib.metadata import version as _pkg_version
    _LUMINA_VERSION = _pkg_version("lumina")
except Exception:
    _LUMINA_VERSION = "0.3.0"

# ── 静态文件路径 ───────────────────────────────────────────────────────────────
_STATIC_DIR = (
    Path(_sys._MEIPASS) / "lumina" / "api" / "static"
    if hasattr(_sys, "_MEIPASS")
    else Path(__file__).parent / "static"
)


@asynccontextmanager
async def _default_lifespan(app: FastAPI):
    """默认 lifespan：精确设置服务启动时间戳。

    create_app() 在初始化时已设置估算值；lifespan 在 uvicorn 真正就绪后
    用更精确的时间戳覆盖，供 /v1/digest 等接口判断服务是否重启。
    """
    app.state.server_start_time = time.time()
    yield


def create_app(llm: LLMEngine, transcriber: Transcriber, lifespan=None) -> FastAPI:
    """创建并配置 FastAPI 应用。

    Args:
        llm:        LLMEngine 实例，挂载到 app.state.llm。
        transcriber: Transcriber 实例，挂载到 app.state.transcriber。
        lifespan:   可选的自定义 lifespan 上下文管理器。
                    传入 None 时使用 _default_lifespan（仅更新启动时间戳）。
                    cli/server.py 传入包含 uvicorn loop 捕获逻辑的 closure。
    """
    from lumina.services.pdf import PdfJobManager
    from lumina.api.routers import pdf as pdf_router
    from lumina.api.routers import chat as chat_router
    from lumina.api.routers import config as config_router
    from lumina.api.routers import digest as digest_router
    from lumina.api.routers import audio as audio_router
    from lumina.api.routers import text as text_router

    _lifespan = lifespan if lifespan is not None else _default_lifespan
    app = FastAPI(title="Lumina", version=_LUMINA_VERSION, lifespan=_lifespan)

    # 挂载依赖到 app.state（替代 init_router 全局变量模式）
    app.state.llm = llm
    app.state.transcriber = transcriber
    app.state.pdf_manager = PdfJobManager()
    app.state.server_start_time = time.time()  # lifespan 会用精确值覆盖

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由（不再调用 init_router）
    app.include_router(pdf_router.router)
    app.include_router(chat_router.router)
    app.include_router(config_router.router)
    app.include_router(digest_router.router)
    app.include_router(audio_router.router)
    app.include_router(text_router.router)

    # ── PWA 前端 ──────────────────────────────────────────────────────────────

    @app.get("/")
    async def pwa_index():
        return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/logo.svg")
    async def pwa_logo():
        return FileResponse(_STATIC_DIR / "logo.svg", media_type="image/svg+xml")

    @app.get("/manifest.json")
    async def pwa_manifest():
        return JSONResponse({
            "name": "Lumina",
            "short_name": "Lumina",
            "description": "本地 AI 翻译与摘要",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": "#007aff",
            "icons": [
                {"src": "/logo.svg", "sizes": "256x256", "type": "image/svg+xml", "purpose": "any"}
            ]
        })

    # ── 健康检查 ─────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        return {"status": "ok", "llm_loaded": llm.is_loaded}

    # ── 模型列表 ─────────────────────────────────────────────────────────────

    @app.get("/v1/models")
    async def list_models():
        from lumina.api.protocol import ModelCard, ModelList
        return ModelList(
            data=[
                ModelCard(id="lumina"),
                ModelCard(id="lumina-whisper"),
            ]
        )

    return app


async def raw_request_disconnected(request) -> bool:
    """辅助函数，检查客户端是否断开（流式场景）。"""
    try:
        return await asyncio.wait_for(request.is_disconnected(), timeout=0.001)
    except asyncio.TimeoutError:
        return False
    except Exception:
        return False
