"""
文本润色：读取文本文件，调用 Lumina /v1/polish 接口，写出润色后版本。
"""
import logging
import sys
from pathlib import Path

import httpx

logger = logging.getLogger("lumina.polish")

_DEFAULT_BASE_URL = "http://127.0.0.1:31821"


def polish_text(
    text: str,
    language: str = "zh",
    base_url: str = _DEFAULT_BASE_URL,
    api_key: str = "lumina",
) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # 绕过系统代理，直连本地服务
    transport = httpx.HTTPTransport(proxy=None)
    with httpx.Client(transport=transport, timeout=120) as client:
        resp = client.post(
            f"{base_url.rstrip('/')}/v1/polish",
            json={"text": text, "language": language, "stream": False},
            headers=headers,
        )
        resp.raise_for_status()
    return resp.json().get("text", "")


def polish_file(
    path: str,
    language: str = "zh",
    base_url: str = _DEFAULT_BASE_URL,
    api_key: str = "lumina",
    output: str = None,
) -> str:
    """
    读取文件，润色后写出结果。output 为 None 时写到原文件同目录的 *-polished.* 。
    返回润色后的文本。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        logger.error("文件为空：%s", path)
        sys.exit(1)

    logger.info("Polishing %s (%d chars, lang=%s)...", p.name, len(text), language)
    result = polish_text(text, language=language, base_url=base_url, api_key=api_key)

    if output is None:
        output = str(p.parent / f"{p.stem}-polished{p.suffix}")

    Path(output).write_text(result, encoding="utf-8")
    logger.info("Saved: %s", output)
    return result
