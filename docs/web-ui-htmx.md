# Lumina Web UI 与 HTMX 架构

Lumina 的 Web UI 不是 SPA。页面由 Jinja2 模板和 HTMX HTML 片段组成，目标是保持实现简单、首屏可直接渲染、局部刷新不依赖前端构建链。

## 1. 总体结构

入口在 `lumina/api/server.py`：

- `GET /` 渲染 `templates/index.html`
- `/static` 提供 CSS / logo 等静态资源
- `/manifest.json` 提供 PWA manifest

局部刷新入口在 `lumina/api/routers/fragments.py`：

- `/fragments/digest`
- `/fragments/digest/sources`
- `/fragments/digest/storage`
- `/fragments/pdf/status/{job_id}`
- `/fragments/config`
- `/fragments/report/{report_type}`

这些端点都返回 **HTML**，不是 JSON。

## 2. 模板目录布局

主要目录：

- `lumina/api/templates/index.html`
- `lumina/api/templates/panels/*.html`
- `lumina/api/templates/config_form.html`
- `lumina/api/templates/digest_content.html`
- `lumina/api/templates/digest_sources.html`
- `lumina/api/templates/pdf_progress.html`
- `lumina/api/templates/pdf_result.html`
- `lumina/api/templates/pdf_error.html`
- `lumina/api/templates/report_content.html`

设计原则：

- 页面骨架在 `index.html`
- 各 tab 的初始内容放在 `panels/`
- HTMX 按需刷新的区域放在独立片段模板中

## 3. 为什么使用 HTMX

当前方案的优点：

- 服务端直接掌握状态与渲染逻辑
- 不需要额外前端状态管理
- FastAPI + Jinja2 已足够支持表单、轮询、局部替换
- 对设置页、digest、PDF 进度这类“HTML 片段更新”场景很合适

适合 HTMX 的功能：

- digest 内容刷新
- PDF 翻译进度轮询
- 设置页内容按需加载
- 报告内容切换

## 4. HTMX 使用约定

### 返回 HTML，不返回 JSON

`/fragments/*` 端点必须返回 HTML。HTMX 不会自动解析 JSON 成 DOM。

### `innerHTML` 与 `outerHTML`

- 需要保留容器 `id` 时，用 `hx-swap="innerHTML"`
- 只有在明确需要替换整个节点时，才用 `hx-swap="outerHTML"`

### 轮询

长任务或状态刷新通常用：

- `hx-trigger="every 2s"`
- `hx-trigger="every 5m"`

例如 PDF 进度片段会在任务完成后返回不再包含轮询触发器的 HTML，从而自动停轮询。

### 请求完成后的脚本

如果某段逻辑需要在 HTMX 响应完成后触发，使用：

- `hx-on::after-request`

不要把这类逻辑写成依赖首次插入事件的 `load` 钩子。

## 5. radio tab 布局约束

首页 tab 切换使用纯 CSS 的 radio + 兄弟选择器，不依赖 JS。

关键约束：

- tab radio
- `.tabs`
- `<main>`
- `#save-bar`

必须在同一父节点下，才能让 `:checked ~ ...` 生效。

如果把 radio 放到外层、目标面板放到内层容器，选择器会失效。

## 6. 设置页与配置 API 的关系

设置页表单最终会走：

- `GET /fragments/config`
- `PATCH /v1/config`

建议把字段行为理解成两层：

- 表单只是采集和提交配置
- 真正的 merge / 写盘 / 热更新由配置系统处理

因此改设置字段时，应同时关注：

- `config_form.html`
- `config.py`
- `config_runtime.py`
- `config_apply.py`

## 7. 静态资源与 PWA

静态资源不是只从源码目录直接读取。

`server.py` 会优先使用：

- `~/.lumina/static/`

若该目录不完整，再回退到 bundle / 源码内的静态目录。

这样可以保证：

- CLI 运行
- 打包后的 `.app`

都尽量使用同一份最新静态资源。

PWA 相关能力包括：

- `/manifest.json`
- 首页中的 manifest link
- `theme-color`
- iOS add-to-home-screen 所需 meta

## 8. 修改 Web UI 时的建议顺序

1. 先确认是改“页面骨架”还是“局部片段”
2. 如果只是局部刷新，优先放到 `/fragments/*`
3. 如果会影响设置保存逻辑，同时检查配置热更新链路
4. 如果要改 tab 或布局结构，先确认 radio sibling 约束是否仍成立
5. 如果要改样式来源，优先改 `input.css`，避免直接在生成产物里堆逻辑

## 9. 容易踩坑的地方

- 把 HTMX 端点写成 JSON 响应
- `outerHTML` 替掉了后续还要操作的容器 `id`
- 只改模板，不检查 `fragments.py` 上下文数据
- 只改源码静态目录，不理解 `~/.lumina/static/` 的优先级
- 把 tab 结构改散，导致 CSS 兄弟选择器失效

