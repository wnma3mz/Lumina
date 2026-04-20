"""
PDF 翻译核心逻辑，由 lumina pdf 子命令调用。

原理：
    将 pdf2zh 的翻译后端（openailiked）指向本地 Lumina HTTP 服务，
    Lumina 服务需已在 base_url 指定的地址运行。
"""
import glob
import logging
import os
import sys
import threading
from pathlib import Path
from typing import List

from lumina.config import DEFAULT_API_BASE_URL_V1, DEFAULT_API_KEY

logger = logging.getLogger("lumina.pdf")

_DEFAULT_BASE_URL = DEFAULT_API_BASE_URL_V1
_env_lock = threading.Lock()


def _configure_pdf2zh_env(base_url: str, model: str, api_key: str):
    """设置 pdf2zh 的 openailiked 环境变量，指向 Lumina 服务。"""
    os.environ["OPENAILIKED_BASE_URL"] = base_url
    os.environ["OPENAILIKED_MODEL"] = model
    os.environ["OPENAILIKED_API_KEY"] = api_key
    os.environ["OPENAILIKED_STREAM"] = "false"
    # 防止 httpx/openai client 通过系统代理转发本地请求导致 502
    _no_proxy = "127.0.0.1,localhost"
    existing = os.environ.get("NO_PROXY", "") or os.environ.get("no_proxy", "")
    if existing:
        _no_proxy = existing.rstrip(",") + "," + _no_proxy
    os.environ["NO_PROXY"] = _no_proxy
    os.environ["no_proxy"] = _no_proxy


def _normalize_pdf_url(url: str) -> str:
    """将常见论文页面 URL 转换为 PDF 直链。"""
    import re
    # arXiv: /abs/ → /pdf/，去掉末尾 .pdf 后缀（arxiv 会自动加）
    url = re.sub(r'arxiv\.org/abs/(\d+\.\d+)', r'arxiv.org/pdf/\1', url)
    # Semantic Scholar: /paper/Title/hash → 无法直接转，跳过
    return url


def _download_url(url: str) -> str:
    """
    下载远程 PDF，优先命中本地缓存（~/.lumina/cache/pdf/）。
    缓存命中时直接返回缓存路径；未命中时流式下载后写入缓存再返回。
    """
    import os
    import tempfile
    url = _normalize_pdf_url(url)
    from lumina.services.document.pdf_cache import get_cached, put_cache_file

    cached = get_cached(url)
    if cached:
        logger.info("Cache hit: %s", cached)
        return str(cached)

    try:
        import httpx
    except ImportError:
        logger.error("httpx 未安装，无法下载 URL。请运行: uv add httpx")
        sys.exit(1)

    logger.info("Downloading PDF: %s", url)
    tmp_fd, tmp_str = tempfile.mkstemp(suffix=".pdf", prefix="lumina_dl_")
    tmp_path = Path(tmp_str)
    committed = False
    try:
        os.close(tmp_fd)
        # 下载外部 URL 时允许走系统代理（HTTP_PROXY / HTTPS_PROXY）
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(tmp_str, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)
        cached = put_cache_file(url, tmp_path)
        committed = True
        return str(cached)
    finally:
        if not committed:
            tmp_path.unlink(missing_ok=True)


def _collect_files(paths: List[str]) -> List[str]:
    """
    将输入路径（文件/目录/通配符/URL）展开为 PDF 本地文件列表。
    URL 会先下载到临时目录。
    """
    result = []
    for p in paths:
        if p.startswith("http://") or p.startswith("https://"):
            result.append(_download_url(p))
            continue
        path = Path(p)
        if path.is_dir():
            result.extend(str(f) for f in path.glob("**/*.pdf"))
        elif path.is_file():
            if path.suffix.lower() == ".pdf":
                result.append(str(path))
            else:
                logger.warning("Skipping non-PDF file: %s", p)
        else:
            # 尝试 glob 展开
            matched = glob.glob(p, recursive=True)
            for m in matched:
                if m.lower().endswith(".pdf"):
                    result.append(m)
            if not matched:
                logger.warning("Path not found: %s", p)
    return result


def _translate_model_name(lang_out: str) -> str:
    """
    返回带翻译任务标识的 model name，供 Lumina 服务端识别并路由到翻译 task。
    格式：lumina-translate-{lang_out}，如 lumina-translate-zh。
    """
    return f"lumina-translate-{lang_out.lower()}"


def translate_pdfs(
    paths: List[str],
    output_dir: str,
    lang_in: str = "en",
    lang_out: str = "zh",
    threads: int = 0,
    base_url: str = _DEFAULT_BASE_URL,
    model: str = "",
    api_key: str = DEFAULT_API_KEY,
    callback: object = None,
) -> List[tuple]:
    """
    调用 pdf2zh 翻译一批 PDF。
    若 threads 设为 0，则默认使用 config.json 中的 document.pdf_translation_threads 配置。

    Returns:
        list of (mono_pdf_path, dual_pdf_path)
    """
    try:
        from pdf2zh import translate
        from pdf2zh.doclayout import OnnxModel
    except ImportError:
        logger.error("pdf2zh 未正确安装，请重新安装 Lumina。")
        sys.exit(1)

    if threads <= 0:
        from lumina.config import get_config
        threads = get_config().document.pdf_translation_threads

    # 用带翻译标识的 model name，让 Lumina 服务端识别并路由到翻译 task
    translate_model = model or _translate_model_name(lang_out)

    with _env_lock:
        _configure_pdf2zh_env(base_url, translate_model, api_key)

    files = _collect_files(paths)
    if not files:
        logger.error("No PDF files found in: %s", paths)
        sys.exit(1)

    logger.info("Translating %d PDF(s) -> %s", len(files), output_dir)
    logger.info("Backend: %s  model: %s  %s → %s", base_url, translate_model, lang_in, lang_out)

    os.makedirs(output_dir, exist_ok=True)

    layout_model = OnnxModel.load_available()
    results = translate(
        files=files,
        output=output_dir,
        lang_in=lang_in,
        lang_out=lang_out,
        service="openailiked",
        thread=threads,
        model=layout_model,
        callback=callback,
    )
    return results
