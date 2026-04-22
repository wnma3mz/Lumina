# Lumina 文档索引

这份索引用来回答两个问题：

1. Lumina 这个项目现在主要做什么
2. 想继续开发或排查时，文档应该先看哪一篇

## 项目做了什么

Lumina 是一个本地优先的 AI 工具箱，目标是把常见 AI 工作流直接放到用户自己的机器上完成，而不是把内容交给远端服务。

当前主要覆盖这些能力：

- 本地模型驱动的聊天、翻译、总结与润色
- PDF 翻译、总结，以及目录级批处理
- 图片 OCR、Caption 与多模态理解
- 语音转文字、PTT 录音转写
- 基于 Shell / Git / 浏览器 / 笔记等数据源的活动回顾与日报

项目同时提供多种本地入口：

- Web UI / PWA
- OpenAI 兼容 HTTP API
- 命令行工具
- macOS 菜单栏与 Quick Action

## 先看哪篇文档

如果你是第一次进入代码库，建议按这个顺序看：

1. `configuration.md`
2. `web-ui-htmx.md`
3. `providers-and-engine.md`

这三篇覆盖了当前最容易误改、也最影响维护效率的几条主链路：配置系统、Web UI 架构、Provider/Engine/MLX 加载。

## 文档目录

### 核心架构

- `configuration.md`
  配置真源、`/v1/config`、热更新边界、`restart_required`、新增配置字段时该改哪些位置。

- `web-ui-htmx.md`
  Jinja2 + HTMX 架构、`/fragments/*` 约定、tab 布局约束、静态资源与 PWA 入口。

- `providers-and-engine.md`
  Provider 选择、Engine 主链路、MLX 加载、continuous batching、offload 的真实生效边界。

### MLX / 性能

- `mlx-memory-offload.md`
  MLX L1/L2 分层、`mlx_memory` / `offload_*` 配置、性能权衡与推荐用法。

- `performance-report.md`
  当前性能测试方法、指标定义与基准结果。

### 平台与产品

- `windows-linux-validation.md`
  Windows / Linux 安装、入口集成与手动验收清单。

- `lumina-buddy-spec.md`
  Lumina Buddy 的交互目标、设定与展示方向。

### 历史资料

- `evolution-plan-v1.md`
  早期演进规划与历史设计记录，适合了解项目为什么长成现在这样。

## 维护建议

- 改配置相关逻辑前，先看 `configuration.md`
- 改首页、设置页或 HTMX 片段前，先看 `web-ui-htmx.md`
- 改本地模型加载、batching、offload 前，先看 `providers-and-engine.md` 和 `mlx-memory-offload.md`
- 如果文档与实现不一致，优先修文档或代码中的一个，不要让两边长期漂移

