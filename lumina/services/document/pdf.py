"""
lumina/services/pdf.py — PDF 翻译 & 摘要服务层

将原 api/server.py 中的 PDF 业务逻辑提取为独立服务类，避免在路由层堆积状态。

公开接口：
    PdfJobManager     — 管理翻译 job 生命周期（提交、查状态、取文件）
    fetch_pdf_url     — 无状态：URL → 本地缓存 Path
    stream_pdf_summary — 无状态：pdf_path → AsyncIterator[str]（SSE 行）
"""
import asyncio
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx


# ── 无状态辅助函数 ─────────────────────────────────────────────────────────────

async def fetch_pdf_url(url: str) -> Path:
    """
    获取远程 PDF 的本地路径，优先命中缓存（~/.lumina/cache/pdf/）。
    返回缓存文件路径（永久文件，不应被临时目录清理）。
    使用流式下载避免大文件全量加载进内存。
    """
    from lumina.services.document.pdf_cache import get_cached, put_cache_file
    cached = get_cached(url)
    if cached:
        return cached
    tmp_fd, tmp_str = tempfile.mkstemp(suffix=".pdf", prefix="lumina_dl_")
    tmp_path = Path(tmp_str)
    _committed = False
    try:
        try:
            os.close(tmp_fd)
        except OSError:
            pass
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(tmp_str, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
        result = put_cache_file(url, tmp_path)
        _committed = True
        return result
    finally:
        # 无论 Exception 还是 asyncio.CancelledError（BaseException 子类）都清理临时文件
        if not _committed:
            tmp_path.unlink(missing_ok=True)


async def stream_pdf_summary(pdf_path: str, llm) -> AsyncIterator[str]:
    """提取 PDF 文字，流式生成摘要，yield SSE 数据行。"""
    from lumina.services.document.pdf_summarize import _extract_text
    from lumina.api.sse import stream_llm
    from lumina.config import get_config
    import dataclasses

    cfg = get_config()
    kwargs = {}
    if getattr(cfg.document, "sampling", None):
        s_dict = dataclasses.asdict(cfg.document.sampling) if dataclasses.is_dataclass(cfg.document.sampling) else dict(cfg.document.sampling)
        kwargs.update({k: v for k, v in s_dict.items() if v is not None})

    text = await asyncio.to_thread(_extract_text, pdf_path)
    async for chunk in stream_llm(
        llm,
        text,
        task="summarize",
        log_label="stream_summary",
        origin="pdf_summary",
        **kwargs
    ):
        yield chunk


# ── PdfJobManager ─────────────────────────────────────────────────────────────

class PdfJobManager:
    """管理 PDF 翻译 job 的生命周期。

    _jobs: job_id → {"status": str, "mono": path, "dual": path, "dir": tmpdir, "ts": float}
    _bg_tasks: 持有后台 asyncio task 引用，防止 GC 过早回收。
    """

    def __init__(self) -> None:
        import threading
        self._jobs: dict[str, dict] = {}
        self._jobs_lock = threading.Lock()
        self._bg_tasks: set = set()
        self._cleanup_orphans()

    def _cleanup_orphans(self) -> None:
        """清理超过 24 小时的孤儿临时目录（处理服务异常退出残留）。"""
        tmp_base = tempfile.gettempdir()
        now = time.time()
        try:
            for p in Path(tmp_base).glob("lumina_*"):
                if p.is_dir():
                    try:
                        if now - p.stat().st_mtime > 86400:
                            shutil.rmtree(p, ignore_errors=True)
                    except Exception:
                        pass
        except Exception:
            pass

    def submit_translate(self, pdf_path: str, lang_out: str, tmp_dir: str) -> str:
        """注册翻译 job，启动后台 task，返回 job_id。"""
        job_id = uuid.uuid4().hex
        with self._jobs_lock:
            self._jobs[job_id] = {"status": "running", "dir": tmp_dir, "ts": time.time()}
        task = asyncio.create_task(self._run_translate(job_id, pdf_path, lang_out))
        self._track(task)
        return job_id

    def get_status(self, job_id: str) -> Optional[dict]:
        """返回 job dict 或 None（不存在时）。"""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return dict(job)

    def get_file(self, job_id: str, variant: str) -> Optional[Path]:
        """返回翻译完成的 PDF 路径；job 未完成或文件不存在时返回 None。"""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] != "done":
                return None
            p = job.get("mono" if variant == "mono" else "dual")
        return Path(p) if p and Path(p).exists() else None

    def resolve_download(self, job_id: str, variant: str) -> tuple[str, Optional[Path]]:
        """原子化解析下载状态，尽量避免 status 与文件状态不一致。"""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                return "missing", None
            if job["status"] != "done":
                return "not_ready", None
            p = job.get("mono" if variant == "mono" else "dual")

        path = Path(p) if p else None
        if not path or not path.exists():
            # 自愈陈旧元数据，避免继续暴露 status=done 但文件已不存在的状态。
            with self._jobs_lock:
                self._jobs.pop(job_id, None)
            return "missing", None
        return "ready", path

    async def _run_translate(self, job_id: str, pdf_path: str, lang_out: str) -> None:
        """后台翻译任务，结果写入 _jobs。完成后立即结束，清理由独立 task 负责。"""
        loop = asyncio.get_running_loop()
        try:
            if pdf_path.startswith("http://") or pdf_path.startswith("https://"):
                from lumina.services.document.pdf import fetch_pdf_url
                pdf_path = str(await fetch_pdf_url(pdf_path))

            from lumina.services.document.pdf_translate import translate_pdfs
            with self._jobs_lock:
                tmp_dir = self._jobs[job_id]["dir"]

            def _progress_cb(p) -> None:
                try:
                    if hasattr(p, "n") and hasattr(p, "total") and p.total:
                        pct = int((p.n / p.total) * 100)
                        with self._jobs_lock:
                            if job_id in self._jobs:
                                # Avoid regressing if not strictly increasing
                                if pct > self._jobs[job_id].get("progress", 0):
                                    self._jobs[job_id]["progress"] = pct
                except Exception:
                    pass

            results = await loop.run_in_executor(
                None, lambda: translate_pdfs(
                    paths=[pdf_path],
                    output_dir=tmp_dir,
                    lang_out=lang_out,
                    callback=_progress_cb,
                )
            )
            with self._jobs_lock:
                if job_id in self._jobs:
                    if results:
                        mono, dual = results[0]
                        self._jobs[job_id].update({"status": "done", "mono": mono, "dual": dual})
                    else:
                        self._jobs[job_id].update({"status": "error", "error": "no output"})
        except asyncio.CancelledError:
            with self._jobs_lock:
                if job_id in self._jobs:
                    self._jobs[job_id].update({"status": "cancelled", "error": "cancelled"})
            raise
        except Exception as e:
            with self._jobs_lock:
                if job_id in self._jobs:
                    self._jobs[job_id].update({"status": "error", "error": str(e)})
        finally:
            # 3600 秒后按固定顺序过期：先移除 job，再删除目录，避免暴露 done 但文件已删。
            with self._jobs_lock:
                tmp_dir = self._jobs.get(job_id, {}).get("dir") if job_id in self._jobs else None

            async def _expire_job(jid: str, job_tmp_dir: Optional[str]) -> None:
                await asyncio.sleep(3600)
                with self._jobs_lock:
                    self._jobs.pop(jid, None)
                if job_tmp_dir:
                    await _delayed_rmtree(job_tmp_dir, delay=0)

            task = asyncio.create_task(_expire_job(job_id, tmp_dir))
            self._track(task)

    def _track(self, task: asyncio.Task) -> None:
        """持有 task 引用，task 完成后自动释放。"""
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)


# ── 辅助 ──────────────────────────────────────────────────────────────────────

async def _delayed_rmtree(path: str, delay: int = 300) -> None:
    """延迟删除临时目录（在 asyncio 协程内使用）。"""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    await asyncio.to_thread(shutil.rmtree, path, True)

def cleanup_after(tmp_dir: str, delay: int = 30):
    """返回 BackgroundTask：延迟删除临时目录（流式响应结束后挂载）。"""
    from starlette.background import BackgroundTask

    async def _do() -> None:
        await asyncio.sleep(delay)
        await asyncio.to_thread(shutil.rmtree, tmp_dir, True)

    return BackgroundTask(_do)


async def write_upload(upload_file, dest: str) -> None:
    """流式写入上传文件，避免全量加载进内存。"""
    def _sync_copy():
        with open(dest, "wb") as f:
            shutil.copyfileobj(upload_file.file, f)
    await asyncio.to_thread(_sync_copy)
