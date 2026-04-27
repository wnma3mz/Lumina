"""
lumina/digest/collectors — 各数据源采集函数包

约定：在此包下的子模块（base 除外）中，所有名为 collect_* 的函数
自动被发现并注册到 COLLECTORS 列表。新增数据源只需建文件、写函数，
无需修改本文件。
"""
import importlib
import importlib.util
import pkgutil
import logging
from pathlib import Path

from lumina.services.digest.collectors.base import Collector

logger = logging.getLogger("lumina.services.digest")
_SKIP_MODULES = {"base"}


def _discover() -> list[Collector]:
    """扫描包内子模块及 ~/.lumina/plugins/collectors/，收集满足 Collector 协议的函数。"""
    discovered: list[Collector] = []

    # 1. 扫描内置采集器
    pkg_path = str(Path(__file__).parent)
    for mod_info in pkgutil.iter_modules([pkg_path]):
        if mod_info.name in _SKIP_MODULES:
            continue
        try:
            mod = importlib.import_module(f"{__package__}.{mod_info.name}")
            discovered.extend(_extract_collectors(mod))
        except Exception as e:
            logger.error("Failed to load internal collector %s: %s", mod_info.name, e)

    # 2. 扫描外部插件采集器 (~/.lumina/plugins/collectors/)
    external_dir = Path.home() / ".lumina" / "plugins" / "collectors"
    if external_dir.is_dir():
        for item in external_dir.iterdir():
            if item.suffix == ".py" and not item.name.startswith("_"):
                try:
                    spec = importlib.util.spec_from_file_location(f"lumina_plugin.{item.stem}", item)
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        discovered.extend(_extract_collectors(mod))
                        logger.info("Loaded external collector plugin: %s", item.name)
                except Exception as e:
                    logger.error("Failed to load external plugin %s: %s", item.name, e)

    return discovered


def _extract_collectors(mod) -> list[Collector]:
    """从模块中提取所有符合 collect_* 命名且符合 Protocol 的对象。"""
    items = []
    for attr_name in dir(mod):
        if not attr_name.startswith("collect_"):
            continue
        obj = getattr(mod, attr_name)
        if callable(obj) and isinstance(obj, Collector):
            items.append(obj)
    return items


COLLECTORS = _discover()
