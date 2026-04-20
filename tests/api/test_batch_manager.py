from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lumina.batch import BatchJobManager


class _FakeLLM:
    async def generate(self, text: str, *, task: str):
        return f"{task}: {text[:24]}"

    async def generate_messages(self, *, messages, task: str):
        first = messages[0]["content"][0]["text"]
        return f"{task}: {first}"


async def _wait_done(manager: BatchJobManager, job_id: str, *, timeout: float = 2.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        job = manager.get_status(job_id)
        assert job is not None
        if job["status"] in {"done", "error"}:
            return job
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("batch job did not finish in time")
        await asyncio.sleep(0.01)


def test_document_batch_processes_text_files_and_skips_hidden(tmp_path: Path):
    async def _run() -> None:
        source = tmp_path / "docs"
        source.mkdir()
        (source / "a.txt").write_text("hello from txt", encoding="utf-8")
        (source / "nested").mkdir()
        (source / "nested" / "b.md").write_text("hello from markdown", encoding="utf-8")
        (source / ".hidden.md").write_text("should skip", encoding="utf-8")

        manager = BatchJobManager(_FakeLLM())
        job = manager.submit_document_job(
            input_dir=str(source),
            task="translate",
            target_language="zh",
        )
        done = await _wait_done(manager, job["job_id"])

        assert done["status"] == "done"
        assert done["total"] == 2
        assert done["succeeded"] == 2
        outputs = [Path(item["output_paths"][0]) for item in done["items"]]
        assert sorted(path.name for path in outputs) == ["a.translated.txt", "b.translated.md"]
        assert outputs[0].exists()

    asyncio.run(_run())


def test_image_batch_writes_caption_outputs(tmp_path: Path):
    async def _run() -> None:
        source = tmp_path / "images"
        source.mkdir()
        (source / "shot.png").write_bytes(b"fake-image-bytes")

        manager = BatchJobManager(_FakeLLM())
        job = manager.submit_image_job(
            input_dir=str(source),
            task="image_caption",
        )
        done = await _wait_done(manager, job["job_id"])

        assert done["status"] == "done"
        assert done["succeeded"] == 1, done["items"][0]["error"]
        output_path = Path(done["items"][0]["output_paths"][0])
        assert output_path.name == "shot.caption.md"
        assert output_path.exists()
        assert "image_caption" in output_path.read_text(encoding="utf-8")

    asyncio.run(_run())
