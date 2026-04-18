# Lumina Buddy — Web 终端 ASCII 宠物设计规范

本文件定义了 Lumina 项目中集成的交互式 ASCII 宠物 "Lumina Buddy" 的实现细节。该功能借鉴了 `1270011/claude-buddy` 的设计思路，旨在提升本地 AI 工作台的交互趣味性。

## 1. 核心定位
Lumina Buddy 是一个居住在 Web UI 右下角的 ASCII 字符生物，它能够实时感知 Lumina 的任务状态（翻译、总结、OCR、日报生成等）并做出视觉和文字上的反馈。

## 2. 宠物系统设计

### 2.1 全量物种 (18 Species)
继承自经典设计的 18 种物种，分为：
- **经典类**: Duck (鸭子), Goose (鹅), Cat (猫), Rabbit (兔子)
- **智慧类**: Owl (猫头鹰), Penguin (企鹅)
- **悠闲类**: Turtle (乌龟), Snail (蜗牛)
- **幻想类**: Dragon (龙), Ghost (幽灵)
- **水生类**: Octopus (章鱼), Axolotl (六角恐龙)
- **自然类**: Cactus (仙人掌), Mushroom (蘑菇)
- **特殊类**: Robot (机器人), Blob (果冻), Chonk (肥猫), Capybara (水豚)

### 2.2 视觉规范
- **尺寸**: 固定 5 行高 × 12 字符宽的 ASCII 矩阵。
- **渲染**: 使用 `<pre>` 标签，配合 `JetBrains Mono` 等宽字体。
- **配色**: 支持深色模式，根据稀有度（Gray/Emerald/Amber）切换颜色。

## 3. 技术实现方案

### 3.1 状态机与动画
宠物共有四种核心状态：
- `IDLE`: 默认状态，包含呼吸动画和随机眨眼（500ms 刷新）。
- `WORKING`: 任务执行中，触发敲键盘或思考动画（200ms 刷新）。
- `SUCCESS`: 任务成功，展示跳跃或心形符号。
- `ERROR`: 任务失败，展示晕倒或流汗符号。

### 3.2 确定性生成 (Deterministic Forge)
通过用户的本地标识（如机器名或 UUID）计算哈希值：
- `Species = hash % 18`
- `Rarity = 基于概率权重的哈希映射`
- `Personality = 决定随机台词的风格`

### 3.3 对话系统
支持 ASCII 气泡对话框，根据事件类型（Event Type）触发台词池：
- **翻译开始**: "正在解析外星信号..."
- **OCR 识别**: "我看得很清楚，别担心。"
- **日报生成**: "又是充实的一天，对吧？"
- **空闲随机**: "本地运行的感觉真安全。"

## 4. 实施路线图 (Agent 指令)

### 第一阶段：资源注入
1. 在 `lumina/api/static/js/` 下创建 `pet-data.js`，定义全量物种的帧模板。
2. 在 `lumina/api/templates/index.html` 注入 `#lumina-buddy` 容器。

### 第二阶段：逻辑挂载
1. 修改 `workspace-core.js`，在 `startDocumentTask`、`runLabTask` 等核心函数中插入 `buddy.setState()`。
2. 实现 `buddy-core.js` 作为动画引擎。

### 第三阶段：细节打磨
1. 添加鼠标交互：Hover 时宠物做出反应。
2. 移动端适配：小屏幕下自动缩小或进入静默模式。

## 5. 参考资料
- 原型来源: [claude-buddy](https://github.com/1270011/claude-buddy)
- 设计风格: Bento Card / Retro Terminal
