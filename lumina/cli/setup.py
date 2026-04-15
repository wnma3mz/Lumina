"""
lumina/cli/setup.py — 首次启动初始化

Full 版：模型检测与下载
Lite 版：配置向导
"""
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("lumina")

_EDITION = os.environ.get("LUMINA_EDITION")
_USER_CONFIG_PATH = Path.home() / ".lumina" / "config.json"
_MODEL_REPO_ID = "mlx-community/Qwen3.5-0.8B-4bit"
_MODEL_CACHE_DIR = Path.home() / ".lumina" / "models" / "qwen3.5-0.8b-4bit"


def _provider_type_from_config() -> str:
    """从 config.json 读取 provider.type，读取失败时返回 'local'。"""
    try:
        with open(_USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("provider", {}).get("type", "local")
    except Exception:
        return "local"


def ensure_model():
    """
    Full 版启动时检测模型是否已下载；若无则从 HuggingFace 下载。
    下载完成后更新 LUMINA_MODEL_PATH 环境变量。
    """
    if _EDITION != "full":
        return

    # 若用户配置了非本地 provider，无需本地模型
    if _provider_type_from_config() != "local":
        return

    from lumina.cli.utils import notify

    model_dir = _MODEL_CACHE_DIR
    if model_dir.exists() and any(model_dir.glob("*.safetensors")):
        logger.info("Model found at %s", model_dir)
        os.environ.setdefault("LUMINA_MODEL_PATH", str(model_dir))
        return

    print()
    print("首次启动需要下载内置模型（约 622MB），请稍候…")
    print(f"  来源：{_MODEL_REPO_ID}")
    print(f"  目标：{model_dir}")
    if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY"):
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        print(f"  代理：{proxy}")
    print()

    notify("Lumina", "正在下载模型，请稍候（约 622MB）…")

    try:
        from huggingface_hub import snapshot_download
        model_dir.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=_MODEL_REPO_ID,
            local_dir=str(model_dir),
        )
    except Exception as e:
        print(f"\n模型下载失败：{e}")
        print("请检查网络连接，或设置代理后重试：")
        print("  export HTTPS_PROXY=http://127.0.0.1:7890")
        notify("Lumina 下载失败", "模型下载失败，请检查网络后重新启动")
        sys.exit(1)

    print(f"✓ 模型下载完成：{model_dir}")
    os.environ["LUMINA_MODEL_PATH"] = str(model_dir)


def needs_lite_setup() -> bool:
    """Lite 版且尚未完成过配置向导。"""
    if _EDITION != "lite":
        return False
    if _USER_CONFIG_PATH.exists():
        try:
            with open(_USER_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return not data.get("provider", {}).get("openai", {}).get("base_url", "")
        except Exception:
            return True
    return True


def lite_setup_wizard():
    """Lite 版首次启动向导：引导用户填写外部服务地址，写入 ~/.lumina/config.json。"""
    print("=" * 55)
    print("  Lumina Lite — 首次启动配置")
    print("=" * 55)
    print()
    print("Lite 版需要连接一个外部 LLM HTTP 服务。")
    print("请填写该服务的地址（OpenAI 兼容接口）。")
    print()

    while True:
        base_url = input("服务地址（如 http://192.168.1.10:8080/v1）: ").strip()
        if base_url:
            break
        print("地址不能为空，请重新输入。")

    api_key = input("API Key（留空则使用默认值 'lumina'）: ").strip() or "lumina"
    model   = input("模型名称（留空则使用默认值 'lumina'）: ").strip() or "lumina"

    port_str = input("本机监听端口（留空则使用默认值 31821）: ").strip()
    port = int(port_str) if port_str.isdigit() else 31821

    _pkg_dir = Path(__file__).parent.parent
    with open(_pkg_dir / "config.json", "r", encoding="utf-8") as f:
        cfg_data = json.load(f)

    cfg_data["provider"]["openai"]["base_url"] = base_url
    cfg_data["provider"]["openai"]["api_key"]  = api_key
    cfg_data["provider"]["openai"]["model"]    = model
    cfg_data["port"] = port

    _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_USER_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg_data, f, indent=2, ensure_ascii=False)

    print()
    print(f"✓ 配置已保存至 {_USER_CONFIG_PATH}")
    print()
