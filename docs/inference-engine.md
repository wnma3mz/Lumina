# Lumina 本地推理引擎：加速机制详解

Lumina 运行于 Apple Silicon，推理核心基于 mlx-lm。本文档解释四个关键加速机制的设计动机和实际效果，面向想理解"为什么快"的技术用户和维护者。

---

## 整体架构

请求的完整路径：

```
HTTP 请求
  → asyncio Queue（prefill_queue）
  → 调度器（批处理循环，跑在 executor 线程）
    ├─ Phase 1: prefill 新请求
    └─ Phase 2: decode 已有 slot
  → 每个 slot 的 token_queue
  → generate_stream（异步协程，SSE 输出）
```

调度器是唯一接触 GPU 的地方，所有请求在这里排队共享算力。

---

## 一、Continuous Batching

### 问题

朴素实现是串行：一个请求跑完，下一个才开始。用户 B 的首个 token（TTFT）要等用户 A 把所有 token 全部生成完毕——A 输出越长，B 等得越久。

### 解法

调度器把每次循环拆成两个阶段：

```
每轮调度循环：
  1. 快照：existing_decode = 当前所有处于 decode 状态的 slot
  2. Phase 1（prefill）：对队列里所有新请求做 prefill，转为 decode 状态
  3. Phase 2（decode）：只推进 existing_decode 里的 slot，新 prefill 的请求不参与本轮
```

**快照是关键。** Phase 2 的 decode batch 在 Phase 1 开始前就已锁定。刚完成 prefill 的新请求不会混入本轮 decode，避免它们的 KV-cache 与已有请求竞争。下一轮循环开始时，新请求才加入 decode 队列。

### 效果

后到的请求最多等一轮调度就能看到首 token，TTFT 与前面请求的输出长度解耦。并发场景下延迟分布更平坦。

---

## 二、System Prompt Cache（LRU）

### 问题

绝大多数请求的 system prompt 是固定的（"你是一个翻译助手……"），但每次请求都重新做 prefill 意味着每次都在 GPU 上重复计算相同的 KV-cache，纯粹浪费。

### 解法

对 system prompt 的 KV-cache 做 LRU 缓存，最多保留 32 条前缀。每次请求进来：

```
if system_prompt in cache:
    prompt_cache = cache[system_prompt]   # 直接复用
    prefill 只处理 suffix（用户输入部分）
else:
    prefill 整个 prompt（含 system）
    cache[system_prompt] = prompt_cache   # 写入缓存
```

命中时，prefill 只需处理用户新输入的 token，system prefix 的 KV 状态直接接续。

### 效果

高频 system prompt（翻译、润色、总结等预设角色）的 prefill 时间显著缩短，在 system prompt 较长时效果最明显。

---

## 三、全量 Decode（处理多字节字符边界）

### 问题

BPE tokenizer 对上下文敏感，中文、emoji 等多字节字符往往跨越 token 边界。如果每生成一个 token 就单独 decode 并输出，遇到跨边界字符会产生乱码（`U+FFFD`）。

例如：一个汉字被拆成两个 token，第一个 token 单独 decode 时无法构成合法 UTF-8 序列。

### 解法

每次生成新 token 后，全量 decode 所有已生成的 token，再取增量：

```
# 每轮 token 生成后：
full_text = tokenizer.decode(all_tokens_so_far)
delta = full_text[len(already_sent):]
stream_out(delta)
already_sent = full_text
```

### 代价与合理性

全量 decode 是 O(n) CPU 操作，随输出长度线性增长。但 GPU 推理才是实际瓶颈，CPU decode 的耗时相对可忽略，换来字符输出的正确性是合算的。

---

## 四、两套调度器与自动切换

推理核心有两条路径，根据模型类型自动选择，无需配置：

### BatchGenerator 路径（默认，纯文本模型）

条件：`type(self) is LocalProvider and not loaded_as_vlm`

使用 mlx-lm 内置的 `BatchGenerator`，将多个请求的 decode 步骤合并成真正的 GPU 批量计算，吞吐效率更高。这是大多数文本模型的默认路径。

### Legacy 路径（VLM 或子类扩展时）

条件：加载了视觉语言模型，或子类覆盖了推理行为

手动管理 prefill/decode 状态机，兼容 mlx-vlm 的接口差异（`LanguageModelOutput` 解包、`language_model.make_cache()` 等），也为未来扩展留出空间。

两条路径共享同一套 `_RequestSlot` 和 `token_queue` 机制，上层 SSE 流式输出完全不感知切换。

---

## 五、asyncio.Queue 作为隔离层

每个请求在进入调度器前，会创建一个独立的 `_RequestSlot`，其中包含一个专属的 `token_queue`：

```
_RequestSlot:
  ├─ prompt tokens
  ├─ token_queue: asyncio.Queue   ← 调度器写，HTTP 响应读
  └─ done event
```

调度器运行在 `ThreadPoolExecutor` 线程里，通过 `loop.call_soon_threadsafe` 把 token 安全地投递到事件循环；`generate_stream` 协程在异步侧 `await queue.get()` 消费。

这个设计的核心价值：**调度器和 HTTP 响应流完全解耦**。调度器遇到异常只影响对应请求的 slot，异常会通过 token_queue 传递给对应的流，不会扩散到其他请求，也不会阻塞主事件循环。

---

## 相关源文件

| 文件 | 内容 |
|---|---|
| `lumina/providers/local.py` | 调度主逻辑、`_RequestSlot`、EOS 检测（token id 248046） |
| `lumina/providers/system_prompt_cache.py` | LRU 缓存实现 |
| `lumina/providers/scheduler.py` | `MlxBatchScheduler`（BatchGenerator 路径） |
| `lumina/engine/scheduler.py` | `EngineScheduler`（Legacy 路径） |
| `lumina/providers/mlx_loader.py` | `BatchGenerator` 初始化、模型加载策略 |
