from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("lumina")


@dataclass(frozen=True)
class ConfigApplyRule:
    apply: Callable[[Any, Any, Any, dict[str, Any]], bool] | None = None


def sync_digest_config(cfg: Any) -> None:
    from lumina.services.digest.config import configure as digest_configure

    digest_configure(cfg.digest)


def reload_digest_scheduler(app: Any) -> None:
    scheduler = getattr(app.state, "digest_scheduler", None)
    if scheduler is not None:
        scheduler.reload(run_startup=True)


def sync_request_history(cfg: Any, *, run_startup_cleanup: bool = False) -> None:
    from lumina.engine import request_history as request_history_mod

    request_history_mod.configure(cfg.request_history, run_startup_cleanup=run_startup_cleanup)


def sync_asr_prompts(cfg: Any) -> None:
    from lumina.services.audio.transcriber import set_asr_prompts

    set_asr_prompts(
        zh=cfg.system_prompts.get("asr_zh", ""),
        en=cfg.system_prompts.get("asr_en", ""),
    )


def sync_transcriber_model(app: Any, cfg: Any) -> None:
    transcriber = getattr(app.state, "transcriber", None)
    if transcriber is None:
        return
    transcriber.model = cfg.whisper_model


def sync_llm_prompts(app: Any, cfg: Any) -> None:
    llm = getattr(app.state, "llm", None)
    if llm is None:
        return
    llm._system_prompts.clear()
    llm._system_prompts.update(cfg.system_prompts)


def sync_log_level(cfg: Any) -> None:
    level_name = str(cfg.log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.getLogger().setLevel(level)
    for name in ("lumina", "uvicorn", "uvicorn.error"):
        logging.getLogger(name).setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)


def sync_openai_provider(app: Any, cfg: Any) -> None:
    llm = getattr(app.state, "llm", None)
    if llm is None:
        return
    provider = getattr(llm, "_provider", None)
    if provider is None or type(provider).__name__ != "OpenAIProvider":
        return
    provider.base_url = cfg.provider.openai.base_url.rstrip("/")
    provider.api_key = cfg.provider.openai.api_key
    provider.model = cfg.provider.openai.model


def _apply_digest(app: Any, old_cfg: Any, new_cfg: Any, patch_dict: dict[str, Any]) -> bool:
    sync_digest_config(new_cfg)
    reload_digest_scheduler(app)
    return True


def _apply_audio(app: Any, old_cfg: Any, new_cfg: Any, patch_dict: dict[str, Any]) -> bool:
    sync_asr_prompts(new_cfg)
    sync_transcriber_model(app, new_cfg)
    return True


def _apply_system(app: Any, old_cfg: Any, new_cfg: Any, patch_dict: dict[str, Any]) -> bool:
    applied = False
    system_patch = patch_dict.get("system")
    if isinstance(system_patch, dict) and "request_history" in system_patch:
        sync_request_history(new_cfg)
        applied = True
    if isinstance(system_patch, dict):
        server_patch = system_patch.get("server")
        if isinstance(server_patch, dict) and "log_level" in server_patch:
            sync_log_level(new_cfg)
            applied = True
    return applied


def _apply_provider(app: Any, old_cfg: Any, new_cfg: Any, patch_dict: dict[str, Any]) -> bool:
    provider_patch = patch_dict.get("provider")
    if not isinstance(provider_patch, dict):
        return False
    if "openai" in provider_patch and new_cfg.provider.backend == "openai":
        sync_openai_provider(app, new_cfg)
        return True
    return False


CONFIG_APPLY_RULES: dict[str, ConfigApplyRule] = {
    "digest": ConfigApplyRule(apply=_apply_digest),
    "audio": ConfigApplyRule(apply=_apply_audio),
    "system": ConfigApplyRule(apply=_apply_system),
    "provider": ConfigApplyRule(apply=_apply_provider),
    "document": ConfigApplyRule(),
    "vision": ConfigApplyRule(),
    "ui": ConfigApplyRule(),
}


class ConfigApplier:
    def apply(self, app: Any, *, old_cfg: Any, new_cfg: Any, patch_dict: dict[str, Any]) -> None:
        for section, rule in CONFIG_APPLY_RULES.items():
            if section not in patch_dict:
                continue
            if rule.apply is None:
                continue
            try:
                applied = rule.apply(app, old_cfg, new_cfg, patch_dict)
                if applied:
                    logger.info("Config: %s config hot-reloaded", section)
                else:
                    logger.info("Config: %s config saved; no runtime hot-reload action", section)
            except Exception as exc:
                logger.warning("Config: %s hot-reload failed: %s", section, exc)

        try:
            sync_llm_prompts(app, new_cfg)
        except Exception as exc:
            logger.warning("Config: failed to update LLM system_prompts: %s", exc)
