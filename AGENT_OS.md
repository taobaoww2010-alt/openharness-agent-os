# Agent OS — 设计文档

**版本**：v3.1  
**日期**：2026-05-26  
**状态**：Python 原型完成 → C++ 核心重写阶段

---

## 目录

1. [设计思想](#1-设计思想)
2. [整体架构](#2-整体架构)
3. [核心调度流程](#3-核心调度流程)
4. [子系统设计](#4-子系统设计)
5. [关键策略](5-关键策略)
6. [数据流](#6-数据流)
7. [边界与约束](#7-边界与约束)
8. [Roadmap](#8-roadmap)

---

## 1. 设计思想

### 1.1 核心类比：操作系统

| 计算机 | Agent OS |
|--------|----------|
| CPU | GPU / 任何推理硬件 |
| 指令集 | LLM（对话 + 工具调用） |
| 内核 | Agent Loop（调度、上下文管理、syscall 分发） |
| 进程 | Workflow 执行实例 |
| 系统调用 | 工具调用（bash、read_file、web_search 等） |
| 文件系统 | 对话历史 + 项目文件 |
| 用户态 | Workflow 模板（可编排、可学习、可热加载） |

### 1.2 核心原则

1. **模型无关** — 不绑定任何具体 LLM。只要能理解工具调用（function calling）即可接入
2. **小模型调度** — 分类器 / 标准化层 / 上下文选择 用轻量手段（正则、规则、小模型），不消费大模型推理预算
3. **最小化输入** — 每阶段只传递最相关的上下文给大模型，避免"全量塞入"导致的 prompt 膨胀
4. **可观测** — 所有交互记录 trace，用于调试、回放、学习
5. **分层容错** — 每一层都有 fallback / retry / 降级策略

---

## 2. 整体架构

### 2.1 分层结构

```
┌──────────────────────────────────────────────────────────────┐
│                    用户交互层                                │
│  TUI (React/Ink)    CLI (oh -p)     API (HTTP)               │
└──────────────────────┬───────────────────────────────────────┘
                       │ OHJSON / stdin-stdout
┌──────────────────────▼───────────────────────────────────────┐
│                  输入标准化层                                │
│  意图识别 → 实体提取 → 格式化 → 分发                        │
│  (Classifier / NormalizedInput / ToolDiscoverer)             │
└──────────────────────┬───────────────────────────────────────┘
                       │ 匹配 workflow / 无匹配走 chat
┌──────────────────────▼───────────────────────────────────────┐
│                  Kernel 内核层                               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────────┐   │
│  │ Memory   │ │ Process  │ │ Syscall  │ │ Scheduler     │   │
│  │ Mgr      │ │ Table    │ │ Table    │ │ (Context +    │   │
│  │ (L1/L2/  │ │ (分配 +  │ │ (权限 +  │ │  Replay       │   │
│  │ L3/L4)   │ │  信号)   │ │  审计)   │ │  Cache)       │   │
│  └──────────┘ └──────────┘ └──────────┘ └───────────────┘   │
└──────────────────────┬───────────────────────────────────────┘
                       │ stream_message / tool_execute
┌──────────────────────▼───────────────────────────────────────┐
│                   LLM 接入层                                 │
│  OpenAI 兼容 API (任何推理后端: llama.cpp / vLLM / TGI)       │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 分层职责

| 层 | 职责 | 不负责 |
|----|------|--------|
| **用户交互层** | 输入输出渲染、用户交互 | 任何业务逻辑、LLM 调用 |
| **输入标准化层** | 意图分类、实体提取、workflow 匹配 | 模型推理、工具执行 |
| **Kernel 内核层** | 进程/内存/syscall/调度/缓存/trace | 具体工具的语义 |
| **LLM 接入层** | 统一 API 接口、流式响应、重试 | prompt 设计、工具选择 |

---

## 3. 核心调度流程

### 3.1 请求生命周期

```
用户输入
  │
  ├─→ 输入标准化层
  │      ├─ Classifier.match() ──→ 命中 workflow
  │      └─ ToolDiscoverer.discover() ──→ 发现新工具 → 创建 workflow
  │
  ├─→ 命中 workflow → Kernel execute()
  │      for each phase:
  │        1. MemoryManager: 重置 L1/L2，准备 L3
  │        2. SyscallTable: 裁剪工具列表（scoped registry）
  │        3. Scheduler: 选择相关上下文（L4 → L3 → L2 → L1）
  │        4. InputNormalizer: 注入结构化信息到 prompt
  │        5. Agent Loop:
  │           a. LLM.stream_message() ← ReplayEngine 缓存层
  │           b. 解析 tool_uses → asyncio.gather() 并行执行
  │           c. L1 追加本轮对话
  │           d. 无 tool_uses → 自然终止
  │        6. L3 累计 + Snapshot checkpoint
  │
  └─→ 无匹配 → chat fast path（零工具、最小 prompt）
         └─ 模型输出含 refusal + 用户含动作词 → 回退完整 Agent Loop
```

### 3.2 Phase 状态机

```
IDLE → RUNNING → BLOCKED (LLM 调用) → RUNNING → BLOCKED (工具执行) → RUNNING → ...
  → DONE | KILLED | TIMEOUT | ERROR
```

---

## 4. 子系统设计

### 4.1 MemoryManager — 分层缓存

| 层级 | 类比 | 内容 | 范围 | 清除 |
|------|------|------|------|------|
| **L0** | 寄存器 | 当前 LLM 请求消息列表 | 单次 LLM 调用 | 下一轮 LLM 调用 |
| **L1** | L1 Cache | 当前 Phase 全部对话轮次 | 单 Phase | Phase 结束 |
| **L2** | L2 Cache | 工具调用结果（同名同参数不重复执行） | 单 Phase | Phase 结束 |
| **L3** | L3 Cache | 跨 Phase 累计上下文 + 阶段结果 | 整个 Workflow | Workflow 结束 |
| **L4** | 主存 | 完整历史（按需召回，文件/向量） | 跨 Workflow | 按策略淘汰 |

关键规则：读类工具（read_file、grep、web_fetch）结果缓存到 L2，同 Phase 内相同调用直接命中。

### 4.2 ProcessTable — 进程管理

- `alloc(workflow_id)` → 返回全局唯一 pid
- 状态：`IDLE → RUNNING → BLOCKED → DONE | KILLED`
- 信号：`SIGTERM`（软终止，允许清理）、`SIGKILL`（强制终止）
- CPU 时间统计：LLM 调用时间 + 工具执行时间

### 4.3 SyscallTable — 系统调用与权限

- 将所有工具（包括 MCP 工具）注册到 syscall 类型
- `build_scoped(caps, explicit_tools)`：按 phase 裁剪工具列表
- 优先级：`explicit_tools > caps`（phase 显式声明的工具始终可用）
- `dispatch_with_audit()`：执行 + 审计记录

### 4.4 Scheduler — 调度与缓存

**ContextScheduler**：
- 从 L2/L3/L4 选择最相关的上下文
- 按预算（token）自动截断
- 优先级：Phase results > Tool cache > Accumulated context

**ReplayEngine**：
- 按 `(messages_hash)` 缓存 LLM 响应
- 缓存命中：0.1s 返回；未命中：请求 LLM 后写入缓存
- 同时录制 trace（输入 + 输出 + 耗时）

### 4.5 Workflow 子系统

#### 数据模型

```python
class TaskWorkflow:
    id: str
    trigger_patterns: list[str]   # 正则触发词
    description: str
    phases: list[TaskPhase]

class TaskPhase:
    name: str                     # execute / understand / implement / verify 等
    prompt_template: str          # {user_input} {cwd} {accumulated_context} {phase_results}
    tools: list[str]              # 该 phase 可用工具名列表
    max_turns: int                # 默认 20，安全兜底
```

#### Classifier（输入标准化层）

纯正则匹配 + 优先级打分（零 LLM 开销）：
```
score = len(pattern_text)                    # 基础分：pattern 越长越精确
score += min(matched_prefix_len, 20)         # 锚定奖励：实际匹配的前缀长度
score += 10 if exact_pattern else 0          # 精确命令奖励
```

#### WorkflowExecutor

每 phase 独立执行，相互隔离：
1. 构建 scoped registry（只注册本 phase 需要的工具）
2. 构建 phase prompt（template + 上下文 + 标准化信息）
3. Agent Loop（LLM ⇄ 工具 交替）
4. 工具调用用 `asyncio.gather(return_exceptions=True)` 并行执行
5. L3 累计上下文 + 自动 checkpoint

#### ToolDiscoverer（输入标准化层）

当分类器无匹配时，从用户输入提取工具名候选，搜索 web 创建新 workflow：

```
用户输入 → 正则提取候选
  → 检查是否已有 workflow 覆盖（含模糊匹配：cuadriver ↔ cua-driver）
  → 检查是否为已知本地工具（brew / docker / git 等 40+ 跳过）
  → Wikipedia → GitHub → DDG → Bing 逐级搜索
  → 生成 1-2 phase 的 workflow 模板
```

### 4.6 Error Recovery

| 故障类型 | 策略 | 参数 |
|----------|------|------|
| LLM 调用失败 | 重试 3 次，指数退避 1s/2s/4s | `_MAX_LLM_RETRIES = 3` |
| Phase 超时 | 单 phase 超时后跳至下一 phase | `_PHASE_TIMEOUT = 120s` |
| 连续失败 | Circuit breaker，5 次后终止 | `_MAX_CONSECUTIVE_FAILURES = 5` |
| 工具执行异常 | 并行容错，单工具失败不影响其他 | `return_exceptions=True` |
| 网络不可达 | 自动降级 fallback | DDG → Bing → 提示用户 |
| 模型拒绝执行 | 检测 refusal 关键词 + 用户含动作词 → 回退完整 Loop | chat fast path 内置 |

### 4.7 Trace 与学习

```
LLM 请求/响应 → ReplayEngine 录制 → traces/{session_id}.jsonl
                                        ↓
                                  WorkflowLearner
                                        ↓
                              工具频率 / 失败率 / 耗时统计
                                        ↓
                                  Workflow 改进提示
```

**Session 管理**：
- 每轮执行结束自动保存 snapshot
- `clean_old_sessions(days=7)` 清理过期 trace / 缓存 / 快照

### 4.8 输入标准化

将自然语言转换为结构化格式：

```python
class NormalizedInput:
    task_type: str               # write_code / debug_fix / web_research 等
    target_language: str | None  # python / javascript / rust
    target_framework: str | None # fastapi / react / django
    goal: str                    # 核心目标（截取前 200 字）
    requirements: list[str]      # 功能需求（正则提取 bullet list）
    constraints: list[str]       # 约束条件（"使用 xxx" 模式）
    context_files: list[str]     # 路径模式匹配
```

---

## 5. 关键策略

### 5.1 工具注册裁剪

不全部注册 43 个工具。每 phase 只注册该 phase 需要的子集（2-6 个），通过 SyscallTable 的 `build_scoped` 实现。直接效果：
- prompt 中 tool schema 从 ~3000 tokens 降到 ~300-800 tokens
- 模型更不容易选错工具

### 5.2 上下文隔离

Phase 间 L1/L2 隔离，L3 累计。类比 OS 的进程隔离：
- 错误不会跨 phase 传播
- 前序 phase 的工具结果缓存不会干扰当前 phase
- 累计上下文（L3）保留关键信息

### 5.3 结果截断

- 工具输出截断 4K tokens
- 读类工具结果缓存（L2），同 phase 内不重复执行

---

## 6. 数据流

### 6.1 主要数据路径

```
        用户输入
           │
           ▼
    ┌──────────────┐    匹配     ┌────────────┐
    │  Classifier  │ ────────→   │  Workflow  │
    │  (正则匹配)   │            │  Executor   │
    └──────┬───────┘            └──────┬──────┘
           │ 不匹配                    │
           ▼                          ▼
    ┌──────────────┐           ┌──────────────┐
    │ToolDiscoverer│           │ Agent Loop   │
    │ (web 搜索)   │           │ LLM ⇄ Tools  │
    └──────┬───────┘           └──────┬───────┘
           │ 不匹配                   │
           ▼                          ▼
    ┌──────────────┐           ┌──────────────┐
    │ Chat Fast    │           │ MemoryManager│
    │ Path         │           │ L1/L2/L3     │
    └──────────────┘           └──────────────┘
```

### 6.2 存储路径

| 数据 | 路径 | 格式 |
|------|------|------|
| Workflow 模板 | `~/.daoyi/workflows/*.json` | JSON |
| LLM 响应缓存 | `~/.daoyi/cache/replay/*.json` | JSON |
| Trace 录制 | `~/.daoyi/traces/{session_id}.jsonl` | JSONL |
| 进程快照 | `~/.daoyi/snapshots/*.json` | JSON |

---

## 7. 边界与约束

### 7.1 模型要求

- 必须支持 function calling（tool_use 格式）
- 建议 context window ≥ 32K（Qwen3VL-8B 的 64K 足够）
- 建议支持流式输出（非必须，但影响 UX）

### 7.2 不做的事情

- 不自己做模型训练 / fine-tune
- 不绑定具体推理框架（llama.cpp / vLLM / TGI 均可）
- 不保存用户隐私数据（trace 仅用于调试，可关闭）

### 7.3 已知限制

1. 8B 级模型指令遵循不稳定，step-by-step prompt 不一定严格执行
2. DuckDuckGo 部分网络被拦截，依赖 Bing fallback
3. ToolDiscoverer 对非英文工具名效果有限
4. 触发词冲突需手动调整（如"搜索日志文件"命中 file_search + web_research）

---

## 8. Roadmap

### P0 — 已完成
- [x] 14 个内置 Workflow 模板
- [x] Kernel：Memory / Process / Syscall / Replay / Snapshot
- [x] Workflow Executor（阶段化 + 并行工具 + 隔离）
- [x] Classifier（纯正则，零 LLM 开销）
- [x] ToolDiscoverer（自动工具发现）
- [x] Chat Fast Path（非 workflow 轻量处理）
- [x] 输入标准化层（NormalizedInput）
- [x] 错误恢复策略（retry / timeout / circuit breaker）
- [x] Trace 录制与分析
- [x] WebSearchTool（双源 fallback）

### P1 — 进行中
- [ ] L4 上下文向量检索（文件级 recall）
- [ ] Trace → Workflow 自动改进闭环
- [ ] 小模型调度器（小模型做上下文评分，代替纯正则）

### P2 — 规划中
- [ ] Multi-session 上下文共享
- [ ] Workflow 模板版本管理
- [ ] 可视化 workflow 编辑器（或 /workflow 命令增强）
- [ ] 自动化回归测试套件

---

## 附录 A：C++ 核心重写设计

### A.1 动机

Python 版完成架构验证后，C++ 重写解决三个核心问题：

| 问题 | Python 现状 | C++ 目标 |
|------|------------|----------|
| **Agent Loop 延迟** | LLM 调用 6-22s/轮，其中 1-3s 是 Python 开销（消息序列化、token 计数、asyncio 调度） | LLM 调用 = 纯推理时间，周边开销 <1ms |
| **内存控制** | Python GC 在 context 切换时产生 ~50ms 的 pause | 确定性内存池，零 GC 停顿 |
| **嵌入性** | 被 C/C++ 程序调用需要 subprocess 通信 | C ABI 直接调用，`dlopen` 即用 |

### A.2 组件映射

| Python 模块 | C++ 目标 | 策略 |
|-------------|----------|------|
| `kernel/memory.py` | `libdaoyi/memory/` | 直接重写，pybind11 绑定 |
| `kernel/process.py` | `libdaoyi/process/` | 直接重写 |
| `kernel/syscall.py` | `libdaoyi/syscall/` | 直接重写 |
| `kernel/replay.py` | `libdaoyi/replay/` | 直接重写 |
| `kernel/snapshot.py` | `libdaoyi/snapshot/` | 直接重写 |
| `kernel/context_scheduler.py` | `libdaoyi/scheduler/` | 直接重写 |
| `kernel/input_normalizer.py` | `libdaoyi/normalizer/` | 直接重写 |
| `task_workflow/executor.py` | `libdaoyi/executor/` | 核心 Agent Loop 重写 |
| `task_workflow/classifier.py` | `libdaoyi/classifier/` | 纯正则，C++ `std::regex` / `re2` |
| `task_workflow/models.py` | `libdaoyi/models/` | 数据结构 |
| `task_workflow/learner.py` | 保留 Python | I/O bound 分析，重写收益小 |
| `task_workflow/registry.py` | `libdaoyi/registry/` | JSON 加载 + 缓存 |
| `tools/*.py` | `libdaoyi/tools/` | 核心工具渐进重写 |
| `ui/` | 保留 Python | React/Ink TUI 前端不变 |

### A.3 构建系统

```
src/cpp/
├── CMakeLists.txt              # 顶层 CMake
├── third_party/                 # 第三方依赖
│   └── llama.cpp/               # git submodule
├── libdaoyi/              # 静态库
│   ├── CMakeLists.txt
│   ├── core/                    # 核心类型
│   │   ├── types.h              # Message, ToolCall, ToolResult 等
│   │   ├── types.cpp
│   │   ├── error.h              # Error 类型体系
│   │   └── error.cpp
│   ├── memory/                  # MemoryManager
│   │   ├── memory_manager.h
│   │   └── memory_manager.cpp
│   ├── process/                 # ProcessTable
│   │   ├── process_table.h
│   │   └── process_table.cpp
│   ├── syscall/                 # SyscallTable
│   │   ├── syscall_table.h
│   │   └── syscall_table.cpp
│   ├── executor/                # WorkflowExecutor
│   │   ├── executor.h
│   │   ├── executor.cpp
│   │   ├── agent_loop.h
│   │   └── agent_loop.cpp
│   ├── classifier/              # Classifier
│   │   ├── classifier.h
│   │   └── classifier.cpp
│   ├── scheduler/               # ContextScheduler + ReplayEngine
│   │   ├── context_scheduler.h
│   │   ├── context_scheduler.cpp
│   │   ├── replay_engine.h
│   │   └── replay_engine.cpp
│   ├── normalizer/              # InputNormalizer
│   │   ├── input_normalizer.h
│   │   └── input_normalizer.cpp
│   ├── snapshot/                # SnapshotManager
│   │   ├── snapshot_manager.h
│   │   └── snapshot_manager.cpp
│   ├── tools/                   # 工具实现
│   │   ├── tool_base.h          # 工具基类
│   │   ├── bash_tool.h / .cpp
│   │   ├── read_tool.h / .cpp
│   │   ├── write_tool.h / .cpp
│   │   ├── glob_tool.h / .cpp
│   │   ├── grep_tool.h / .cpp
│   │   └── web_search_tool.h / .cpp
│   └── llm/                     # LLM 推理封装
│       ├── llm_engine.h          # llama.cpp 直接嵌入
│       ├── llm_engine.cpp
│       ├── inference.h           # 推理请求/响应
│       └── inference.cpp
├── bindings/                    # pybind11
│   └── python/
│       ├── CMakeLists.txt
│       ├── module.cpp            # 模块入口
│       ├── bind_memory.cpp
│       ├── bind_executor.cpp
│       └── ...
└── tests/                       # C++ 单元测试
    ├── CMakeLists.txt
    ├── test_memory.cpp
    ├── test_executor.cpp
    └── ...
```

### A.4 llama.cpp 嵌入

**策略**：以 git submodule 引入 `llama.cpp`，`target_link_libraries` 直接链接 `libllama.a`。

```cmake
# third_party/llama.cpp/CMakeLists.txt 已提供 llama 目标
# 根据平台自动选择后端（无 CUDA 依赖）
target_link_libraries(daoyi PRIVATE llama ggml)
```

#### GPU 后端支持（跨平台，无 CUDA）

| 平台 | 后端 | CMake 选项 |
|------|------|-----------|
| macOS | Metal | `-DLLAMA_METAL=ON` |
| Linux | OpenCL / Vulkan | `-DLLAMA_OPENCL=ON` 或 `-DLLAMA_VULKAN=ON` |
| Windows | OpenCL / Vulkan | `-DLLAMA_OPENCL=ON` 或 `-DLLAMA_VULKAN=ON` |
| 通用 | CPU（默认） | 无需额外选项 |

**推荐配置**（根据平台自动检测）：

```cmake
# 自动检测并配置 GPU 后端
option(OH_ENABLE_GPU "Enable GPU acceleration" ON)

if(OH_ENABLE_GPU)
    if(APPLE)
        set(LLAMA_METAL ON CACHE BOOL "" FORCE)
    elseif(UNIX AND NOT APPLE)
        find_package(OpenCL)
        if(OpenCL_FOUND)
            set(LLAMA_OPENCL ON CACHE BOOL "" FORCE)
        else()
            message(STATUS "OpenCL not found, falling back to CPU")
        endif()
    else()
        set(LLAMA_OPENCL ON CACHE BOOL "" FORCE)
    endif()
endif()
```

**推理调用路径**：

```
C++ Executor
  → LLMEngine::infer(context)
    → llama_decode()           # 直接推理，无 HTTP 开销
    → 流式 output 回调         # 每个 token 生成后回调
      → Executor 检查 stop_reason
        → tool_use → 解析 tool calls → 分发执行
        → stop → 结束本轮
```

**关键优化**：
- 共享模型 weight（单次加载，多次推理）
- KV cache 跨轮复用（`llama_kv_cache_seq_rm` 做滑动窗口）
- 批处理（同一 phase 中多个 LLM 调用复用 batch slot）

### A.5 协程模型 (libuv / asio)

工具并行执行用 **C++20 协程** + **libuv**（跨平台事件循环）：

```cpp
// Agent Loop 核心
task<> agent_phase(Executor& exec, Phase& phase) {
    for (int turn = 0; turn < phase.max_turns; turn++) {
        auto response = co_await exec.llm().infer(phase.context());
        
        if (!response.has_tool_calls()) co_return;  // 自然终止
        
        // 并行执行工具
        auto results = co_await parallel_for_each(
            response.tool_calls().begin(),
            response.tool_calls().end(),
            [&](const ToolCall& tc) { return exec.dispatch(tc); }
        );
        
        phase.context().append(results);  // L1 追加
    }
}

// parallel_for_each 实现
template<typename It, typename F>
task<std::vector<result_of_t<F>>> parallel_for_each(It begin, It end, F func) {
    std::vector<task<...>> tasks;
    for (auto it = begin; it != end; ++it)
        tasks.push_back(std::move(func(*it)));
    
    // 协程并发执行，等价 asyncio.gather
    co_return co_await when_all(std::move(tasks));
}
```

**vs Python `asyncio.gather`**：

| 方面 | Python | C++20 + libuv |
|------|--------|---------------|
| 协程创建开销 | ~1µs | ~50ns |
| 上下文切换 | ~100ns | ~10ns |
| 内存分配 | 每协程独立分配 | 可预分配 arena |
| 取消 | `Task.cancel()` 需事件循环协作 | `co_await cancel_at` 显式点 |

### A.6 pybind11 桥接

**原则**：Module-by-module replacement，每个 C++ 模块暴露 Python 接口后，Python 侧逐步切换到 C++ 实现。

```python
# 过渡期代码（Python）
from daoyi.kernel import memory  # 旧 Python 实现

try:
    from _daoyi import MemoryManager as CppMemoryManager
    memory.MemoryManager = CppMemoryManager  # 替换为 C++ 版
    HAS_CPP_MEMORY = True
except ImportError:
    HAS_CPP_MEMORY = False
```

```cpp
// pybind11 绑定
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include "memory/memory_manager.h"

namespace py = pybind11;

PYBIND11_MODULE(_daoyi, m) {
    py::class_<MemoryManager>(m, "MemoryManager")
        .def(py::init<size_t>(), py::arg("context_limit"))
        .def("register_phase", &MemoryManager::registerPhase)
        .def("evict", &MemoryManager::evict)
        .def("get_cached", &MemoryManager::getCached)
        .def("set_cached", &MemoryManager::setCached);
}
```

### A.7 增量迁移顺序

```
Phase 1: 工程骨架 + 数据类型 + pybind11 基础设施
Phase 2: MemoryManager + ContextScheduler
Phase 3: SyscallTable + 核心工具 (bash/read/glob/grep)
Phase 4: Agent Loop + Executor + Classifier
Phase 5: llama.cpp 嵌入 + ReplayEngine
Phase 6: 剩余模块 (snapshot/input_normalizer) + 性能调优
```

每 phase 结束后：
1. C++ 模块通过 pybind11 暴露给 Python
2. Python 测试套件通过桥接层运行
3. `oh -p "..."` 端到端验证

### A.8 性能目标

| 场景 | Python 当前 | C++ 目标 | 关键路径 |
|------|------------|----------|----------|
| bash_ops (ls) | 6-10s | 5-8s | LLM 推理占绝大部分，C++ 节省 ~1s 周边开销 |
| write_code (3 phase) | 77s | 55-65s | 多轮 LLM 调用间上下文操作加速 |
| 工具并行执行 | ~5ms/tool | <0.1ms/tool | asyncio → 协程 |
| 分类器匹配 | ~50µs | <5µs | Python re → re2 |
| 消息序列化 | ~200µs | <10µs | json → protobuf / flatbuffers |
| LLM 响应缓存命中 | ~100ms | <50µs | 文件 I/O → mmap |

### A.9 不重写的部分

| 组件 | 理由 |
|------|------|
| ToolDiscoverer | Web 搜索 I/O bound，Python aiohttp 足够快 |
| WorkflowLearner | 批量文件分析，ms 级，非 hot path |
| Plugin/Skill 加载 | 动态 `importlib`，不适合 C++ |
| React TUI 前端 | 独立进程，通信协议不变 |
| `oh setup` / provider 管理 | 用户交互逻辑，性能不敏感 |
| Workflow 模板（JSON） | 数据格式，与语言无关 |
