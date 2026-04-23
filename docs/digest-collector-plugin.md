# Digest Collector 插件开发指南

日报（Digest）功能由一组**采集器（Collector）**驱动，每个采集器负责从某个数据源抓取"过去 N 小时的活动快照"，并以 Markdown 字符串的形式返回。本文档面向两类读者：想添加自定义数据源的开发者，以及想了解内部采集架构的维护者。

---

## 一、整体架构

### 采集器是什么

一个采集器就是一个普通 Python 函数，遵循以下约定：

- **无参数**，返回 `str`（Markdown 格式的活动片段）
- 函数名以 `collect_` 开头
- 出错时返回空字符串 `""`，**不抛出异常**
- 每次调用都是"回顾过去 N 小时"的完整快照，没有增量状态

采集完成后，所有采集器的输出拼接成一份原始素材，交给 LLM 生成最终的日报摘要。

### 两层发现机制

Lumina 在启动时自动发现采集器，分两步进行（见 `collectors/__init__.py`）：

1. **内置采集器**：用 `pkgutil.iter_modules` 扫描 `lumina/services/digest/collectors/` 包下所有子模块（`base` 除外），提取其中名为 `collect_*` 的可调用对象。

2. **外部插件**：扫描 `~/.lumina/plugins/collectors/` 目录，对每个 `.py` 文件用 `importlib.util.spec_from_file_location` 动态加载，同样提取 `collect_*` 函数。

两层扫描的结果合并到模块级常量 `COLLECTORS`，后续所有调用都引用这个列表。外部插件如果加载失败，只记录 error 日志，不影响其他采集器。

### 约定优于配置

只要函数命名以 `collect_` 开头，放在正确目录下，就会被自动发现，**无需修改任何注册文件**。

---

## 二、Collector Protocol

采集器需要满足 `lumina/services/digest/collectors/base.py` 中定义的 `Collector` Protocol：

```python
@runtime_checkable
class Collector(Protocol):
    __name__: str          # 函数天然具有此属性
    def __call__(self) -> str: ...
```

关键点：

- **签名**：`() -> str`，无参数，返回 Markdown 字符串
- **错误处理**：内部捕获所有异常，出错时返回 `""`，不能让异常逃逸到调度器
- **`__name__`**：普通函数天然满足，用于 `enabled_collectors` 配置列表的匹配
- **时间窗口**：通过 `from lumina.services.digest.config import get_cfg` 获取 `cfg.history_hours`，计算 `cutoff = time.time() - cfg.history_hours * 3600`

---

## 三、快速上手：写一个自定义采集器

### 第一步：创建插件文件

在 `~/.lumina/plugins/collectors/` 下新建一个 `.py` 文件（文件名不能以 `_` 开头）：

```bash
mkdir -p ~/.lumina/plugins/collectors
touch ~/.lumina/plugins/collectors/my_app_logs.py
```

### 第二步：实现采集函数

```python
# ~/.lumina/plugins/collectors/my_app_logs.py

import time
from pathlib import Path


def collect_my_app_logs() -> str:
    """采集 MyApp 的最近操作日志。"""
    # 从 DigestConfig 读取时间窗口
    try:
        from lumina.services.digest.config import get_cfg
        cfg = get_cfg()
        cutoff = time.time() - cfg.history_hours * 3600
    except Exception:
        cutoff = time.time() - 24 * 3600  # fallback：24 小时

    log_file = Path.home() / "Library" / "Logs" / "MyApp" / "activity.log"
    if not log_file.exists():
        return ""

    try:
        lines = log_file.read_text(errors="replace").splitlines()
        recent = []
        for line in reversed(lines):
            # 假设日志行格式：`1714000000 some action`
            parts = line.split(" ", 1)
            if len(parts) < 2:
                continue
            try:
                ts = float(parts[0])
            except ValueError:
                continue
            if ts < cutoff:
                break  # 已按时间倒序，可提前退出
            recent.append(parts[1].strip())

        if not recent:
            return ""

        items = "\n".join(f"  {entry}" for entry in recent[:20])
        return f"## MyApp 操作记录（过去 {cfg.history_hours:.0f}h）\n{items}"

    except Exception:
        return ""  # 出错一律返回空字符串
```

### 第三步：测试

重启 Lumina 服务后，手动触发一次采集：

```bash
curl -X POST http://localhost:31821/v1/digest/refresh
```

或在 Web UI 的日报标签页点击"刷新"按钮。可通过调试接口查看各采集器的采集结果：

```bash
curl http://localhost:31821/v1/digest/debug | python3 -m json.tool
```

返回结果中会出现 `collect_my_app_logs` 字段，包含 `chars`（采集到的字符数）和 `preview`（内容预览）。

---

## 四、内置采集器列表

共 9 个内置采集器，元数据定义在 `lumina/api/ui_meta.py` 的 `COLLECTOR_DEFS`：

| 函数名 | Label | 图标 | filter_key | 时间轴颜色 |
|---|---|---|---|---|
| `collect_shell_history` | Shell | 🖥 | `shell` | indigo |
| `collect_git_logs` | Git | 📁 | `git` | emerald |
| `collect_clipboard` | 剪贴板 | 📌 | `clipboard` | amber |
| `collect_browser_history` | 浏览器 | 🌐 | `browser` | blue |
| `collect_notes_app` | 备忘录 | 📝 | `notes` | purple |
| `collect_calendar` | 日历 | 📅 | `calendar` | rose |
| `collect_markdown_notes` | Markdown | 📄 | `markdown` | cyan |
| `collect_ai_queries` | AI | 🤖 | `ai` | fuchsia |
| `collect_recent_file_activities` | 最近文件 | 🗂 | `files` | orange |

### 外部插件的自动元数据

外部插件没有在 `COLLECTOR_DEFS` 中显式定义时，`resolve_collector_meta()` 会自动生成：

- **label**：去掉 `collect_` 前缀，下划线换空格，首字母大写。
  例：`collect_my_app_logs` → "My App Logs"
- **icon**：默认 📦
- **filter_key**：去掉 `collect_` 前缀，下划线换连字符。
  例：`collect_my_app_logs` → `my-app-logs`
- **时间轴颜色**：回落到中性灰（`bg-zinc-400`）

如需自定义显示效果，在 `lumina/api/ui_meta.py` 的 `COLLECTOR_DEFS` 列表中追加一条即可。

---

## 五、在配置中控制启用的采集器

### `enabled_collectors` 配置项

`config.json` 的 `digest` 节点支持 `enabled_collectors` 字段：

```json
{
  "digest": {
    "enabled": true,
    "history_hours": 24,
    "enabled_collectors": [
      "collect_shell_history",
      "collect_git_logs",
      "collect_my_app_logs"
    ]
  }
}
```

- 值为 `null`（或不填）：启用所有已发现的采集器
- 值为列表：只运行列表中的采集器，其余跳过

### 通过 API 热更新

无需重启，直接 PATCH 配置：

```bash
curl -X PATCH http://localhost:31821/v1/config \
  -H "Content-Type: application/json" \
  -d '{
    "digest": {
      "enabled_collectors": [
        "collect_shell_history",
        "collect_git_logs",
        "collect_my_app_logs"
      ]
    }
  }'
```

`enabled_collectors` 属于 `digest.*` 下的字段，支持热更新，无需重启服务。

---

## 六、内部实现参考

### 核心文件

| 文件 | 职责 |
|---|---|
| `lumina/services/digest/collectors/__init__.py` | 两层扫描入口，导出 `COLLECTORS` 列表 |
| `lumina/services/digest/collectors/base.py` | `Collector` Protocol 定义 |
| `lumina/services/digest/collectors/system.py` | Shell 历史、Git 日志、剪贴板 |
| `lumina/services/digest/collectors/apps.py` | 浏览器、备忘录、日历、AI 对话 |
| `lumina/services/digest/collectors/files.py` | Markdown 笔记、最近文件活动 |
| `lumina/services/digest/core.py` | 调度逻辑：`_collect_all()`，含并发执行和 `enabled_collectors` 过滤 |
| `lumina/api/ui_meta.py` | 前端元数据：`COLLECTOR_DEFS`、`resolve_collector_meta()` |
| `lumina/services/digest/config.py` | `DigestConfig`（含 `enabled_collectors`、`history_hours` 等字段） |

### 两层扫描逻辑简述

`_discover()` 函数执行顺序：

1. 获取当前包路径（`collectors/` 目录），用 `pkgutil.iter_modules` 枚举子模块名称
2. 逐个 `importlib.import_module` 导入，调用 `_extract_collectors(mod)` 提取 `collect_*` 函数
3. 检查 `~/.lumina/plugins/collectors/` 是否存在，遍历其中 `.py` 文件
4. 用 `spec_from_file_location` 动态加载，同样调用 `_extract_collectors`
5. 两批结果合并，赋值给模块级 `COLLECTORS`

`_extract_collectors(mod)` 通过 `dir(mod)` 遍历所有属性，筛选 `attr_name.startswith("collect_")` 且 `isinstance(obj, Collector)`（runtime_checkable Protocol 检查）的对象。

### 并发调度

`core.py` 中的 `_collect_all()` 使用 `ThreadPoolExecutor` 并发执行所有活跃采集器，超时上限 30 秒。`executor.shutdown(wait=False)` 确保超时后主协程立即返回，慢采集器在后台自然结束。每次调用前会对活跃列表做 `random.shuffle`，使各来源在 LLM 上下文中均匀分布。
