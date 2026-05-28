# OpenHarness 研发计划

## 总体进度评估：~90%

---

## ✅ 已完成

### C++ 协程层
- `Task<T>` / `Task<void>` — final_awaiter 修复 ✓
- `when_all` / `gather` — 自定义 `WhenAllAwaiter`，`std::thread` 并行 ✓
- `CoroutineScheduler` — `schedule` 改为普通函数 ✓
- 竞态修复 — `scheduling_done` spin-wait ✓
- Clang bug 绕过 x3 — move-only 捕获/循环变量别名/引用野指针，全部自由函数 ✓
- **41/41 C++ 测试通过** ✓

### Python 工具执行层（完全可用）
- ReplayCache 修复 — 空响应不缓存 ✓
- 并行工具执行 — `asyncio.wait(FIRST_COMPLETED)` ✓
- 小模型预分类 — `classify()` → 6 类别 ✓
- L3 持久化 — `save_checkpoint()` / `load_checkpoint()` + 6 测试 ✓
- 错误处理增强 — `asyncio.timeout` + 3×重试 + ErrorEvent ✓
- 集成测试 — 5 端到端测试 ✓
- 性能压测 — 10 基准测试 ✓
- `/Applications/` 注入 — Python + 双路径 ✓

### C++ 基础设施（pybind11 绑定可用）
- MemoryManager（L1/L2/L3）✓
- ProcessManager ✓
- SyscallTable + 6 内置 C++ 工具 ✓
- SnapshotManager ✓
- SmallModelScheduler ✓
- ReplayCache ✓
- ContextSelector ✓
- Classifier ✓
- InputNormalizer ✓
- LLMEngine（llama.cpp 包装）✓
- WorkflowMatcher ✓
- coroutine.h（Task<T>/Task<void>/when_all）✓
- C++ WorkflowExecutor（仅 C++ 内部使用，Python 不走此路径）✓

### 其他
- CI: cpp-tests (macOS, cmake+ninja+googletest) ✓
- pybind11 unique_ptr → raw pointer 修复 ✓
- MemoryManager C++ L2 同步（Python→C++ dual-write）✓
- Python 3.14 `asyncio.as_completed` 兼容性修复 ✓
- **Python 全量测试通过：1162 passed, 6 skipped** ✓
- **CLI-Anything 集成：动态 SKILL 执行 + 意图匹配 + 60 个 SKILL** ✓

---

## 🚧 当前方向（2026-05-27 确定）

**架构：Python 主路径 + C++ 子模块增强**

C++ executor 全量替换 Python executor 方案已废弃（GIL + std::async 死锁不可解）。

Python `execute()` 主循环保持不动，C++ 通过子模块调用嵌入：

```
Python execute() 主循环
  ├─ C++ MemoryManager（L1/L2/L3 加速）
  ├─ C++ Classifier（正则匹配加速）
  ├─ C++ SmallModelScheduler（上下文评分）
  └─ C++ LLMEngine（llama.cpp 本地推理，替代 HTTP 调用）
```

### 待完成

| 优先级 | 任务 | 说明 |
|--------|------|------|
| 优先级 | 任务 | 说明 |
|--------|------|------|
| P0 | ~~清理废弃的 `_execute_with_cpp()` 代码~~ | ✅ 已完成 |
| P1 | ~~C++ MemoryManager Python 集成~~ | ✅ 已完成 — 5 个缺失绑定 + L3 主存储 |
| P2 | ~~C++ Classifier 集成~~ | ✅ 已完成 — pre_classify() C++ 降级路径 |
| P2 | ~~C++ LLMEngine 集成~~ | ✅ 已完成 — WorkflowExecutor `local_model_path` → `CppLLMClient`（api/cpp_client.py） |
| P3 | ~~Python CI~~ | ✅ 已完成 — 1162 测试通过，57 UI + 24 task_workflow + 其余全部通过 |
| P3 | ~~Release/packaging~~ | ✅ 已完成 — pyproject.toml (hatchling) + publish.yml (PyPI trusted publishing) |

---

## 技术债务

- **`_execute_with_cpp()` 已废弃** — C++ executor 从 Python 路径不通（GIL 死锁），保留代码供参考
- **C++ `register_tool` pybind11 绑定已写但无用** — Python 不走 C++ syscall table
- **Clang macOS ARM64 协程 bug** — 已绕过，Clang 升级后重新验证
- **`ScheduleMultipleVoidTasks` 测试偶发 SIGSEGV** — 已通过自由函数修复
- **C++ WorkflowExecutor 仅有 6 个内置工具** — 不在 Python 路径中使用
- **`WorkflowExecutor.execute()` 不传 `ask_user_prompt`** — `ask_user_question` 工具在 workflow 执行路径中不可用；只能用 `QueryEngine.submit_message()` 路径
- **`WorkflowExecutor.chat()`/`execute()` 纯文本** — 不支持图片附件；图片消息必须走 `QueryEngine.submit_message()` 路径
- **`ReplayEngine` 磁盘缓存** — 缓存位于 `~/.openharness/cache/replay/`；测试中需设置 `OPENHARNESS_HOME` 到临时目录避免缓存污染

---

## 已修改/创建文件清单

### cpp-core/
- `include/openharness/core.h` — 数据结构 + 接口定义
- `include/openharness/coroutine.h` — Task<T>/Task<void>/WhenAllAwaiter
- `src/executor.cpp` — C++ WorkflowExecutor
- `src/memory_manager.cpp` — MemoryManager
- `src/process_manager.cpp` — ProcessManager
- `src/syscall_table.cpp` — SyscallTable + 6 built-in tools
- `src/snapshot.cpp` — SnapshotManager
- `src/small_model_scheduler.cpp` — SmallModelScheduler
- `src/replay_cache.cpp` — ReplayCache
- `src/context_selector.cpp` — ContextSelector
- `src/classifier.cpp` — Classifier
- `src/normalizer.cpp` — InputNormalizer
- `src/coroutine_scheduler.cpp` — CoroutineScheduler
- `src/llm_engine.cpp` — LLMEngine（llama.cpp）
- `src/workflow_matcher.cpp` — WorkflowMatcher
- `src/helpers.cpp` — Helpers
- `bindings/python/bind_executor.cpp` — Executor pybind11
- `bindings/python/bind_scheduler.cpp` — Scheduler pybind11
- `bindings/python/bind_syscall.cpp` — Syscall + register_tool pybind11
- `bindings/python/bind_llm.cpp` — LLM + FakeLLM pybind11
- `bindings/python/module.cpp` — Module entry
- `bindings/python/CMakeLists.txt` — build
- `tests/test_coroutine.cpp` — 14 协程测试
- `tests/test_executor.cpp` — 4 executor 测试

### src/openharness/
- `task_workflow/executor.py` — Python execute() + `local_model_path` → `CppLLMClient`
- `api/cpp_client.py` — `CppLLMClient` (C++ LLMEngine async adapter, retry, tool flattening)
- `ui/runtime.py` — `OH_LOCAL_MODEL_PATH` env var → `local_model_path`; `handle_line()` 图片检测跳过 WorkflowExecutor 快速路径
- `kernel/memory.py` — L3 persistence, L2 dual-write
- `llm/small_model.py` — 6 分类类别
- `tools/cli_anything_tool.py` — CLI-Anything 集成：一个 Meta-Tool 覆盖 20+ 专业软件 CLI

### tests/test_ui/
- `test_react_backend.py` — monkeypatch `SmallModelClient.get_instance` + `OPENHARNESS_HOME` 隔离 ReplayEngine 缓存
- `test_textual_app.py` — monkeypatch `WorkflowExecutor.chat` + 禁用 `workflow_registry` 确保 `ask_user_question` 走正常 agent loop

### tests/test_task_workflow/
- `test_executor_integration.py` — 5
- `test_executor_cpp_e2e.py` — 已删除（废弃路径）
- `test_perf_benchmark.py` — 10
- `test_pre_classify_and_inject.py` — 7

### CI
- `.github/workflows/ci.yml` — cpp-tests job

---

## 🎯 CLI-Anything 集成计划（✅ 已确定正确方向）

### 核心思路（✅ 已确定）

**不要编译集成！要动态调用 SKILL！**

SKILL.md 就是工具的"接口描述文件"，类似于操作系统的 .exe 文件描述。我们应该：

```
OpenHarness Agent OS
    │
    ├─ SKILL 注册表（动态发现）
    │     ├─ ~/.openharness/skills/cli-anything-gimp/SKILL.md
    │     ├─ ~/.openharness/skills/cli-anything-blender/SKILL.md
    │     └─ ... 50+ 个 SKILL
    │
    └─ SKILL 执行器（动态调用）
          ├─ 解析 SKILL.md 提取命令定义
          ├─ 执行对应的 CLI 命令
          └─ 解析 JSON 输出返回结果
```

### 现有代码解读

#### 1. SKILL 加载器（✅ 已存在）
**文件**：`src/openharness/skills/loader.py`

功能：
- 扫描多个目录发现 SKILL.md 文件
- 支持用户目录、项目目录、插件目录
- 解析 YAML frontmatter 提取元数据

扫描目录：
```python
get_user_skills_dir()        # ~/.openharness/skills/
~/.claude/skills/            # Claude 兼容
~/.agents/skills/            # Agent 兼容
./.openharness/skills/      # 项目级 SKILL
```

#### 2. SKILL 类型定义（✅ 已存在）
**文件**：`src/openharness/skills/types.py`

```python
@dataclass
class SkillDefinition:
    name: str                    # SKILL 名称
    description: str             # SKILL 描述
    content: str                 # SKILL.md 内容
    source: str                 # 来源（user/project/bundled）
    path: str                   # SKILL.md 路径
    base_dir: str               # SKILL 目录
    command_name: str           # 命令名
```

#### 3. CLI-Anything 工具（⚠️ 需要增强）
**文件**：`src/openharness/tools/cli_anything_tool.py`

当前实现：
- 硬编码支持 20 个软件列表
- 用户需要手动输入软件名和命令
- 不支持动态发现 SKILL.md 中的命令

**问题**：用户必须知道精确的命令格式，无法动态发现

#### 4. SKILL.md 格式（CLI-Anything）
**示例**：`CLI-Anything/skills/cli-anything-gimp/SKILL.md`

```yaml
---
name: "cli-anything-gimp"
description: >-
  Command-line interface for Gimp...
---

# cli-anything-gimp

## Command Groups

| Command | Description |
|---------|-------------|
| `project new` | Create a new project |
| `layer add` | Add a layer |
| `export render` | Render to image |
```

### 实现方案

#### 方案：增强 SKILL 执行器

1. **新增文件**：`src/openharness/tools/skill_executor_tool.py`

```python
class SkillExecutorTool(BaseTool):
    """动态执行 SKILL.md 中定义的 CLI 命令"""
    
    name = "skill_executor"
    
    async def execute(self, arguments, context):
        # 1. 读取 SKILL.md
        # 2. 解析命令表格
        # 3. 执行 CLI 命令
        # 4. 返回 JSON 结果
```

2. **集成 CLI-Anything SKILL**

复制 50+ SKILL.md 到 `~/.openharness/skills/`

3. **效果**

用户输入：`"帮我用 GIMP 创建一个 800x600 的图片"`
↓
小模型分类 → "tool"
↓
WorkflowExecutor 发现 `cli-anything-gimp` SKILL
↓
动态执行 `cli-anything-gimp project new --width 800 --height 600`

### 正确架构优势

| 特性 | 硬编码方案 | SKILL 动态方案 |
|------|-----------|---------------|
| 新工具添加 | 需要改代码、编译 | 直接放 SKILL.md |
| 热更新 | 需要重启 | 实时生效 |
| 维护成本 | 高 | 极低 |
| 灵活性 | 低 | 高 |
| 命令发现 | 手动查看文档 | 自动解析 SKILL.md |

### 实现步骤

#### 第 1 步：实现 SKILL 命令解析器 ✅
- 解析 SKILL.md 中的命令表格
- 提取命令名称、参数、描述
- 生成结构化的命令定义

#### 第 2 步：实现 SKILL 执行器 ✅
- 读取 SKILL.md 中的命令定义
- 调用对应的 CLI 工具
- 解析 JSON 输出返回结果

#### 第 3 步：集成 CLI-Anything SKILL ✅
- 创建 `~/.openharness/skills/` 目录
- 复制 CLI-Anything 的 60 个 SKILL.md 文件
- 支持从项目目录 `./.openharness/skills/` 发现 SKILL

#### 第 4 步：增强 Workflow 匹配 ✅
- 创建 `SkillMatcher` 类（`task_workflow/skill_discovery.py`）
- 根据用户意图自动选择合适的 SKILL
- 提供工作流建议（suggest_workflow）

### 新增文件

1. **`src/openharness/tools/skill_executor_tool.py`**
   - 动态 SKILL 执行器
   - 支持 `skill_executor list` 查看所有 SKILL
   - 支持 `skill_executor list <name>` 查看特定 SKILL 命令
   - 支持直接执行 SKILL 中的命令

2. **`src/openharness/task_workflow/skill_discovery.py`**
   - SKILL 发现器（SkillMatcher）
   - 基于关键词的意图匹配
   - 工作流建议生成

3. **`scripts/test_skill_executor.py`**
   - SKILL 执行器测试

4. **`scripts/test_skill_discovery.py`**
   - SKILL 发现器测试

5. **`.openharness/skills/`**
   - 60 个 CLI-Anything SKILL 文件

### 测试结果

```
SKILL 执行器测试:
- List Skills: ✅ PASS
- Get GIMP Commands: ✅ PASS
- Tool List Command: ✅ PASS

SKILL 发现器测试:
- 根据意图匹配 SKILL: ✅ 正常工作
- 工作流建议: ✅ 正常工作
- SKILL 总数: 60 个
```

### 使用示例

```python
# 1. 列出所有 SKILL
skill_executor skill_name="list" command=""

# 2. 查看特定 SKILL 的命令
skill_executor skill_name="cli-anything-gimp" command="list"

# 3. 执行 SKILL 命令
skill_executor skill_name="cli-anything-gimp" command="project new" args="--width 800 --height 600"

# 4. 代码中使用 SkillMatcher
from openharness.task_workflow.skill_discovery import get_skill_matcher

matcher = get_skill_matcher()
matched_skills = matcher.find_skills("帮我用 GIMP 编辑图片", limit=3)
workflow = matcher.suggest_workflow("帮我用 Blender 做 3D 模型")
```

### 预期效果
- ✅ 不需要编译
- ✅ 不需要安装包
- ✅ 工具像"安装 App"一样简单（放 SKILL.md 就行）
- ✅ 热更新：添加新工具无需重启
- ✅ 自动发现：LLM 可以读取 SKILL.md 了解工具能力

### 实现的问题和解决方案

#### 问题 1：SKILL 信息没有传递给 LLM
**问题描述**：
- `SkillMatcher.find_skills()` 只返回匹配的 SKILL
- LLM 看不到这些 SKILL 的命令定义
- LLM 不知道有哪些命令可用

**解决方案**：
创建 `SkillContextInjector` 类，将 SKILL 信息注入到 LLM 上下文：
```python
# src/openharness/task_workflow/skill_context_injector.py
class SkillContextInjector:
    def build_skill_context_message(self, user_intent, limit=5):
        # 构建包含 SKILL 命令的系统消息
        # LLM 可以看到有哪些工具可用
```

#### 问题 2：WorkflowExecutor 没有集成 SKILL 工具
**问题描述**：
- SKILL 发现和执行是分离的
- LLM 无法调用 skill_executor 工具

**解决方案**：
修改 `WorkflowExecutor.execute()` 方法：
1. 在 system prompt 中注入 SKILL 上下文
2. 将 skill_executor 工具添加到工具注册表

```python
# src/openharness/task_workflow/executor.py

# SKILL context injection
injector = get_skill_context_injector()
skill_msg = injector.build_skill_context_message(user_input, limit=5)
phase_system_prompt += "\n\n" + skill_msg["content"]

# Add skill_executor tool
schema = injector.get_skill_command_schema("cli-anything-gimp")
scoped_registry._tools.append(ToolDefinition.from_dict(schema))
```

#### 完整的 SKILL 执行流程

```
1. User input: "帮我用 GIMP 创建一个 800x600 的图片"
    │
    ├─ SmallModel classify: "tool"
    │
    ├─ SkillMatcher match:
    │     → cli-anything-gimp (score: 3.0)
    │
    ├─ WorkflowExecutor:
    │     ├─ 注入 SKILL 上下文到 system prompt
    │     ├─ 添加 skill_executor 工具
    │     └─ 调用 LLM
    │
    ├─ LLM 生成调用:
    │     skill_executor(
    │       skill_name="cli-anything-gimp",
    │       command="new",
    │       args="--width 800 --height 600"
    │     )
    │
    └─ SkillExecutorTool 执行:
          $ cli-anything-gimp new --width 800 --height 600
```
