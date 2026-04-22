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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lumina.services.audio.transcriber import Transcriber
from lumina.engine.llm import LLMEngine

try:
    from importlib.metadata import version as _pkg_version
    _LUMINA_VERSION = _pkg_version("lumina")
except Exception:
    _LUMINA_VERSION = "0.3.0"

# ── 静态文件路径 ───────────────────────────────────────────────────────────────
_BUNDLE_STATIC = (
    Path(_sys._MEIPASS) / "lumina" / "api" / "static"
    if hasattr(_sys, "_MEIPASS")
    else Path(__file__).parent / "static"
)
_REQUIRED_STATIC_FILES = ("style.css", "logo.svg")


def _is_complete_static_dir(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in _REQUIRED_STATIC_FILES)


def _resolve_static_dir() -> Path:
    """优先使用完整的 ~/.lumina/static/；缺文件时回退到 bundle/源码内路径。"""
    p = Path.home() / ".lumina" / "static"
    return p if _is_complete_static_dir(p) else _BUNDLE_STATIC


def _compute_asset_version(static_root: Path) -> int:
    if not static_root.exists():
        return 0
    try:
        return max(
            (int(path.stat().st_mtime) for path in static_root.rglob("*") if path.is_file()),
            default=0,
        )
    except Exception:
        return 0


@asynccontextmanager
async def _default_lifespan(app: FastAPI):
    """默认 lifespan：精确设置服务启动时间戳。

    create_app() 在初始化时已设置估算值；lifespan 在 uvicorn 真正就绪后
    用更精确的时间戳覆盖，供 /v1/digest 等接口判断服务是否重启。
    """
    app.state.server_start_time = time.time()
    try:
        yield
    finally:
        from lumina.engine import request_history as _request_history

        _request_history.shutdown()


def create_app(llm: LLMEngine, transcriber: Transcriber, lifespan=None) -> FastAPI:
    """创建并配置 FastAPI 应用。

    Args:
        llm:        LLMEngine 实例，挂载到 app.state.llm。
        transcriber: Transcriber 实例，挂载到 app.state.transcriber。
        lifespan:   可选的自定义 lifespan 上下文管理器。
                    传入 None 时使用 _default_lifespan（仅更新启动时间戳）。
                    cli/server.py 传入包含 uvicorn loop 捕获逻辑的 closure。
    """
    from lumina.services.document.pdf import PdfJobManager
    from lumina.batch import BatchJobManager
    from lumina.api.routers import batch as batch_router
    from lumina.api.routers import chat as chat_router
    from lumina.api.routers import config as config_router
    from lumina.api.routers import digest as digest_router
    from lumina.api.routers import document as document_router
    from lumina.api.routers import vision as vision_router
    from lumina.api.routers import audio as audio_router
    from lumina.api.routers import fragments as fragments_router
    from lumina.config import get_config

    _lifespan = lifespan if lifespan is not None else _default_lifespan
    app = FastAPI(title="Lumina", version=_LUMINA_VERSION, lifespan=_lifespan)

    # 挂载依赖到 app.state（替代 init_router 全局变量模式）
    app.state.llm = llm
    app.state.transcriber = transcriber
    app.state.pdf_manager = PdfJobManager()
    app.state.batch_manager = BatchJobManager(llm)
    app.state.digest_scheduler = None
    app.state.server_start_time = time.time()  # lifespan 会用精确值覆盖
    app.state.static_root = _resolve_static_dir()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由（不再调用 init_router）
    app.include_router(batch_router.router)
    app.include_router(chat_router.router)
    app.include_router(config_router.router)
    app.include_router(digest_router.router)
    app.include_router(document_router.router)
    app.include_router(vision_router.router)
    app.include_router(audio_router.router)
    app.include_router(fragments_router.router)

    # ── 静态文件（CSS / SVG 等）─────────────────────────────────────────────────
    app.mount("/static", StaticFiles(directory=str(app.state.static_root)), name="static")

    # ── PWA 前端 ──────────────────────────────────────────────────────────────
    _templates_dir = Path(__file__).parent / "templates"
    _tmpl = Jinja2Templates(directory=str(_templates_dir))

    @app.get("/")
    async def pwa_index(request: Request):
        static_root = app.state.static_root
        asset_ver = _compute_asset_version(static_root)

        cfg = get_config()
        configured_username = ""
        if isinstance(getattr(cfg, "branding", None), dict):
            configured_username = str(cfg.branding.get("username", "") or "").strip()
        if configured_username:
            username = configured_username
        else:
            import getpass

            try:
                username = getpass.getuser().capitalize()
            except Exception:
                username = ""

        slogan_candidates = cfg.branding.get("slogans", [])
        home_ui = {
            "enabled_tabs": cfg.ui.home.enabled_tabs,
            "digest_enabled": cfg.digest.enabled,
            "document_enabled": cfg.document.enabled,
            "image_enabled": cfg.vision.enabled,
            "audio_enabled": cfg.audio.enabled,
            "image_modules": cfg.vision.enabled_modules,
            "allow_local_override": cfg.ui.home.allow_local_override,
        }
        image_prompts = {
            "image_ocr": cfg.system_prompts.get("image_ocr", ""),
            "image_caption": cfg.system_prompts.get("image_caption", ""),
            "live_translate": cfg.system_prompts.get("live_translate", ""),
        }
        from lumina.api.ui_meta import (
            AUDIO_TASK_DEFS,
            HOME_TAB_DEFS,
            IMAGE_TASK_DEFS,
            LEGACY_HOME_TAB_MAP,
        )

        return _tmpl.TemplateResponse(
            request,
            "index.html",
            {
                "asset_ver": asset_ver,
                "username": username,
                "slogan_candidates": slogan_candidates,
                "home_ui": home_ui,
                "image_prompts": image_prompts,
                "home_tab_defs": HOME_TAB_DEFS,
                "image_task_defs": IMAGE_TASK_DEFS,
                "audio_task_defs": AUDIO_TASK_DEFS,
                "legacy_home_tab_map": LEGACY_HOME_TAB_MAP,
            },
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/logo.svg")
    async def pwa_logo():
        return FileResponse(app.state.static_root / "logo.svg", media_type="image/svg+xml")

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
