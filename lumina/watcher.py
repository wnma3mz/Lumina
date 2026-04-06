"""
文件夹监听自动翻译：监听指定目录，新 PDF 出现时自动调用 translate_pdfs()。

防抖：同一文件 3 秒内只处理一次（避免写入未完成时触发）。
过滤：跳过已有 -mono.pdf / -dual.pdf 后缀的输出文件。
"""
import logging
import os
import threading
import time
from pathlib import Path

from lumina.pdf_translate import translate_pdfs

logger = logging.getLogger("lumina.watch")

_DEBOUNCE_SEC = 3.0


class _PDFHandler:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        lang_in: str,
        lang_out: str,
        threads: int,
    ):
        self._base_url = base_url
        self._model = model
        self._api_key = api_key
        self._lang_in = lang_in
        self._lang_out = lang_out
        self._threads = threads
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_created(self, path: str):
        p = Path(path)
        if p.suffix.lower() != ".pdf":
            return
        # 跳过本工具自己生成的输出文件
        if p.stem.endswith("-mono") or p.stem.endswith("-dual"):
            return

        with self._lock:
            # 取消旧计时器（防抖）
            if path in self._pending:
                self._pending[path].cancel()
            timer = threading.Timer(_DEBOUNCE_SEC, self._translate, args=(path,))
            self._pending[path] = timer
            timer.start()

    def _translate(self, path: str):
        with self._lock:
            self._pending.pop(path, None)

        output_dir = str(Path(path).parent)
        logger.info("Auto-translating: %s", path)
        try:
            results = translate_pdfs(
                paths=[path],
                output_dir=output_dir,
                lang_in=self._lang_in,
                lang_out=self._lang_out,
                threads=self._threads,
                base_url=self._base_url,
                model=self._model,
                api_key=self._api_key,
            )
            for mono, dual in results:
                logger.info("Done: %s / %s", mono, dual)
        except Exception as e:
            logger.error("Translation failed for %s: %s", path, e)


def watch(
    directory: str,
    base_url: str = "http://127.0.0.1:31821/v1",
    model: str = "lumina",
    api_key: str = "lumina",
    lang_in: str = "en",
    lang_out: str = "zh",
    threads: int = 4,
):
    """
    监听 directory，前台阻塞运行直到 KeyboardInterrupt。
    """
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        logger.error("watchdog 未安装，请运行: uv add watchdog")
        import sys; sys.exit(1)

    handler_core = _PDFHandler(base_url, model, api_key, lang_in, lang_out, threads)

    class _WatchdogAdapter(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                handler_core.on_created(event.src_path)

        def on_moved(self, event):
            # 某些应用（如浏览器）先写临时文件再 rename
            if not event.is_directory:
                handler_core.on_created(event.dest_path)

    observer = Observer()
    observer.schedule(_WatchdogAdapter(), directory, recursive=False)
    observer.start()

    abs_dir = os.path.abspath(directory)
    logger.info("Watching: %s  (Ctrl+C to stop)", abs_dir)
    print(f"Lumina Watch — 监听目录：{abs_dir}")
    print("新 PDF 文件出现时自动翻译，结果保存在同目录。按 Ctrl+C 停止。")

    try:
        while observer.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        logger.info("Watcher stopped.")
