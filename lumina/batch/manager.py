from __future__ import annotations

import asyncio
import base64
import copy
import time
import uuid
from mimetypes import guess_type
from pathlib import Path
from typing import Iterable, Optional

from lumina.services.document.pdf_summarize import _extract_text
from lumina.services.document.pdf_translate import translate_pdfs
from lumina.engine.request_context import request_context

_DOCUMENT_EXTS = {".pdf", ".txt", ".md", ".markdown"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _utc_suffix() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def _truncate_preview(text: str, *, limit: int = 220) -> str:
    compact = " ".join((text or "").strip().split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _read_text_document(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _image_data_url(path: Path) -> str:
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    content_type = guess_type(path.name)[0] or "image/png"
    return f"data:{content_type};base64,{payload}"


def _validate_image_size(path: Path) -> None:
    max_mb = 12
    max_bytes = max_mb * 1024 * 1024
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("图片内容为空")
    if size > max_bytes:
        raise ValueError(f"图片过大，请控制在 {max_mb} MB 以内")


def _ensure_directory(path_text: str) -> Path:
    path = Path(path_text).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"目录不存在：{path}")
    if not path.is_dir():
        raise ValueError(f"不是目录：{path}")
    return path


def _normalize_output_dir(input_dir: Path, output_dir: Optional[str], suffix: str) -> Path:
    if output_dir and output_dir.strip():
        target = Path(output_dir.strip()).expanduser().resolve()
    else:
        target = input_dir.parent / f"{input_dir.name}-{suffix}-{_utc_suffix()}"
    if target == input_dir:
        raise ValueError("输出目录不能与输入目录相同")
    if target.is_relative_to(input_dir):
        raise ValueError("输出目录不能位于输入目录内部，请改为同级或其他目录")
    return target


def _scan_files(root: Path, exts: Iterable[str]) -> list[Path]:
    allowed = {ext.lower() for ext in exts}
    out: list[Path] = []
    for current_root, dirnames, filenames in __import__("os").walk(root, topdown=True, followlinks=False):
        current_path = Path(current_root)
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".") and not (current_path / name).is_symlink()
        ]
        for filename in filenames:
            if filename.startswith("."):
                continue
            path = current_path / filename
            if path.is_symlink() or path.suffix.lower() not in allowed:
                continue
            out.append(path)
    return sorted(out)


def _batch_instruction(task: str) -> str:
    if task == "image_ocr":
        return "请提取这张图片中的所有可识别文字。"
    return "请描述这张图片。"


def _build_image_messages(image_ref: str, *, instruction: str) -> list[dict]:
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": image_ref}},
        ],
    }]


class BatchJobManager:
    def __init__(self, llm) -> None:
        self._llm = llm
        self._jobs: dict[str, dict] = {}
        self._bg_tasks: set[asyncio.Task] = set()

    def submit_document_job(
        self,
        *,
        input_dir: str,
        task: str,
        output_dir: Optional[str] = None,
        target_language: str = "zh",
    ) -> dict:
        if task not in {"translate", "summarize"}:
            raise ValueError("不支持的文档批处理任务")
        source_dir = _ensure_directory(input_dir)
        target_dir = _normalize_output_dir(
            source_dir,
            output_dir,
            f"lumina-{task}{('-' + target_language) if task == 'translate' else ''}",
        )
        files = _scan_files(source_dir, _DOCUMENT_EXTS)
        if not files:
            raise ValueError("目录下未找到 PDF / TXT / MD 文件")
        return self._submit_job(
            kind="document",
            task=task,
            source_dir=source_dir,
            target_dir=target_dir,
            target_language=target_language if task == "translate" else None,
            files=files,
        )

    def submit_image_job(
        self,
        *,
        input_dir: str,
        task: str,
        output_dir: Optional[str] = None,
    ) -> dict:
        if task not in {"image_ocr", "image_caption"}:
            raise ValueError("不支持的图像批处理任务")
        source_dir = _ensure_directory(input_dir)
        target_dir = _normalize_output_dir(source_dir, output_dir, f"lumina-{task.replace('_', '-')}")
        files = _scan_files(source_dir, _IMAGE_EXTS)
        if not files:
            raise ValueError("目录下未找到可处理的图片文件")
        return self._submit_job(
            kind="image",
            task=task,
            source_dir=source_dir,
            target_dir=target_dir,
            target_language=None,
            files=files,
        )

    def get_status(self, job_id: str) -> Optional[dict]:
        job = self._jobs.get(job_id)
        return copy.deepcopy(job) if job else None

    def _submit_job(
        self,
        *,
        kind: str,
        task: str,
        source_dir: Path,
        target_dir: Path,
        target_language: Optional[str],
        files: list[Path],
    ) -> dict:
        job_id = uuid.uuid4().hex
        items = [
            {
                "path": str(path),
                "rel_path": str(path.relative_to(source_dir)),
                "status": "pending",
                "output_paths": [],
                "preview": None,
                "error": None,
                "started_at": None,
                "finished_at": None,
            }
            for path in files
        ]
        self._jobs[job_id] = {
            "job_id": job_id,
            "kind": kind,
            "task": task,
            "status": "queued",
            "input_dir": str(source_dir),
            "output_dir": str(target_dir),
            "total": len(items),
            "completed": 0,
            "succeeded": 0,
            "failed": 0,
            "current_item": None,
            "items": items,
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "target_language": target_language,
            "error": None,
        }
        task_ref = asyncio.create_task(self._run_job(job_id))
        self._track(task_ref)
        return self.get_status(job_id)

    async def _run_job(self, job_id: str) -> None:
        job = self._jobs[job_id]
        try:
            job["status"] = "running"
            job["started_at"] = time.time()
            Path(job["output_dir"]).mkdir(parents=True, exist_ok=True)
            for item in job["items"]:
                job["current_item"] = item["rel_path"]
                item["status"] = "running"
                item["started_at"] = time.time()
                try:
                    outputs, preview = await self._process_item(job, item)
                    item["status"] = "done"
                    item["output_paths"] = outputs
                    item["preview"] = preview
                    job["succeeded"] += 1
                except Exception as exc:
                    item["status"] = "error"
                    item["error"] = str(exc)
                    job["failed"] += 1
                finally:
                    item["finished_at"] = time.time()
                    job["completed"] += 1
                    job["current_item"] = None
            job["status"] = "done"
            job["finished_at"] = time.time()
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            job["finished_at"] = time.time()
        finally:
            cleanup = asyncio.create_task(self._cleanup_job_later(job_id))
            self._track(cleanup)

    async def _process_item(self, job: dict, item: dict) -> tuple[list[str], Optional[str]]:
        path = Path(item["path"])
        rel_path = Path(item["rel_path"])
        rel_parent = rel_path.parent
        output_root = Path(job["output_dir"])
        if job["kind"] == "document":
            return await self._process_document_item(
                path=path,
                rel_parent=rel_parent,
                output_root=output_root,
                task=job["task"],
                target_language=job.get("target_language") or "zh",
            )
        return await self._process_image_item(
            path=path,
            rel_parent=rel_parent,
            output_root=output_root,
            task=job["task"],
        )

    async def _process_document_item(
        self,
        *,
        path: Path,
        rel_parent: Path,
        output_root: Path,
        task: str,
        target_language: str,
    ) -> tuple[list[str], Optional[str]]:
        suffix = path.suffix.lower()
        if suffix == ".pdf" and task == "translate":
            per_file_output = output_root / rel_parent
            per_file_output.mkdir(parents=True, exist_ok=True)
            results = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: translate_pdfs(
                    paths=[str(path)],
                    output_dir=str(per_file_output),
                    lang_out=target_language,
                ),
            )
            if not results:
                raise RuntimeError("PDF 翻译没有生成输出")
            mono, dual = results[0]
            return [str(Path(mono)), str(Path(dual))], "已生成单语和双语 PDF"

        if suffix == ".pdf":
            source_text = await asyncio.to_thread(_extract_text, str(path))
            if not source_text.strip():
                raise RuntimeError("PDF 无可提取文字")
            with request_context(origin="batch_document", stream=False):
                summary = await self._llm.generate(source_text, task="summarize")
            output_path = output_root / rel_parent / f"{path.stem}.summary.md"
            await asyncio.to_thread(_write_text, output_path, summary.strip() + "\n")
            return [str(output_path)], _truncate_preview(summary)

        source_text = await asyncio.to_thread(_read_text_document, path)
        if not source_text:
            raise RuntimeError("文件内容为空")
        llm_task = "translate_to_zh" if task == "translate" and target_language == "zh" else (
            "translate_to_en" if task == "translate" else "summarize"
        )
        with request_context(origin="batch_document", stream=False):
            result = await self._llm.generate(source_text, task=llm_task)
        if task == "translate":
            output_path = output_root / rel_parent / f"{path.stem}.translated{path.suffix}"
        else:
            output_path = output_root / rel_parent / f"{path.stem}.summary.md"
        await asyncio.to_thread(_write_text, output_path, result.strip() + "\n")
        return [str(output_path)], _truncate_preview(result)

    async def _process_image_item(
        self,
        *,
        path: Path,
        rel_parent: Path,
        output_root: Path,
        task: str,
    ) -> tuple[list[str], Optional[str]]:
        await asyncio.to_thread(_validate_image_size, path)
        image_ref = await asyncio.to_thread(_image_data_url, path)
        messages = _build_image_messages(image_ref, instruction=_batch_instruction(task))
        with request_context(origin="batch_image", stream=False):
            result = await self._llm.generate_messages(messages=messages, task=task)
        suffix = ".ocr.md" if task == "image_ocr" else ".caption.md"
        output_path = output_root / rel_parent / f"{path.stem}{suffix}"
        await asyncio.to_thread(_write_text, output_path, result.strip() + "\n")
        return [str(output_path)], _truncate_preview(result)

    async def _cleanup_job_later(self, job_id: str, *, delay: int = 86400) -> None:
        await asyncio.sleep(delay)
        self._jobs.pop(job_id, None)

    def _track(self, task: asyncio.Task) -> None:
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
