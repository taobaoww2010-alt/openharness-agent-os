# DaoYi Agent OS 研发计划

## 总体进度评估：~96%

---

## ✅ 已完成

### 核心架构
- **Python 主路径 + C++ 子模块增强** — C++ executor 全量替换 Python executor 废弃（GIL 死锁）
- **小模型预分类** — Qwen3-2B Q4_K_M 本地推理 → 6 分类类别
- **Skill 系统** — `SkillMatcher` 基于关键词意图匹配 + 51 Skills with tier 分类
- **Skill 懒加载 (PilotDeck P0)** — `<available-skills>` 块（name + desc + tier）注入，LLM 按需调用 `read_skill`
- **简短延续检测 (PilotDeck P1)** — "继续"/"go"/"然后呢" 继承上一步意图，减少 ~30% 误分类
- **Tier 分类提示增强 (PilotDeck P2)** — `INTENT_DIFFICULTY_TIERS` + 规则注入 few-shot prompt
- **Workspace 隔离 (PilotDeck P3)** — `GitWorktreeProvider` + `SnapshotCopyProvider`，工具在隔离副本运行
- **项目重命名 OpenHarness → DaoYi Agent OS** — 310+ .py 文件，CLI `oh` → `dy`，配置目录 `~/.openharness/` → `~/.daoyi/`
- **环境变量统一** — `DAOYI_*` 优先 + `OPENHARNESS_*` 回退，涉及 20+ 文件 (settings.py, paths.py, cli.py, openai_client.py, swarm/*, hooks/*, autodream/*)
- **`CliAnythingTool` 恢复** — 重新加入默认 registry（原被移除，现因工具链依赖恢复）
- **天气 API 回退** — `wttr.in` 无 key 查询，含中文城市映射，失败则走搜索引擎链
- **内存控制** — 模型空闲卸载 (5min)、ReplayCache LRU 上限 (5000)、Trace 文件上限 (50)、Session 快照上限 (50)

### 模型层
- **Qwen3-2B-VL-Instruct Q4_K_M** (1GB) 替代 Qwen2.5-0.5B
- **LLM 思考流** — `reasoning_content` → `ThinkingDelta` events → 前端渲染
- **C++ LLMEngine** (llama.cpp) — `CppLLMClient` 本地推理
- **模型空闲卸载** — 本地小模型 300s 无请求自动卸载，释放 ~1GB 显存

### 工具执行
- ReplayCache 修复，并行工具执行 (`asyncio.wait(FIRST_COMPLETED)`)
- `skill_executor_tool` — 动态执行 SKILL.md 命令
- `read_skill_tool` — LLM 按需获取 Skill 完整命令列表
- **cua-driver 浏览器自动化** — real Chrome 单例 (`ChromeSession`) + 智能页面加载等待 + `open -a` 导航回退
- **Baidu 搜索引擎** — 替换 Google（被墙），Baidu JS 提取 + 文本回退解析
- **cua-daemon 自动启动** — UI 启动时检测并拉起 `CuaDriver serve`
- 错误处理增强 (`asyncio.timeout` + 3×重试 + ErrorEvent)
- `/Applications/` 注入 (Python + 双路径)
- `CppLLMClient` API 重试 + tool flattening

### C++ 基础设施 (pybind11 绑定可用)
- MemoryManager (L1/L2/L3), ProcessManager, SyscallTable + 6 内置工具
- SnapshotManager, SmallModelScheduler, ReplayCache, ContextSelector
- Classifier, InputNormalizer, LLMEngine, WorkflowMatcher
- coroutine.h (Task<T>/Task<void>/when_all), C++ WorkflowExecutor (Python 不走此路径)
- **41/41 C++ 测试通过**

### 测试
- **Python 全量测试通过：1161 passed, 11 skipped**
- CI: cpp-tests (macOS, cmake+ninja+googletest)
- 对比测试完成：原版 OpenHarness web search 无限重试循环；DaoYi ~5 min 完成
- **web 工具测试：13 passed, 1 skipped** — web_search (Baidu) + web_fetch (cua-driver Chrome)

---

## 🚧 架构总览

```
Python execute() 主循环
  ├─ SmallModel (Qwen3-2B) — pre_classify (6 categories)
  ├─ SkillMatcher — intent→skill matching (Chinese char aware)
  ├─ SkillContextInjector — <available-skills> block
  ├─ ScopedToolRegistry — read_skill + skill_executor
  ├─ WorkspaceProvider — git worktree / snapshot isolation
  └─ C++ submodules:
       ├─ MemoryManager
       ├─ Classifier (regex fallback)
       ├─ SmallModelScheduler
       └─ LLMEngine
```

### Agent Loop
```
User → pre_classify (continuation? → inherit last intent)
      → SmallModel (2B) classify → 6 categories
      → RuleClassifier (Python regex)
      → C++ Classifier (fallback)
      → SkillMatcher → <available-skills> injection
      → LLM (Qwen3VL-8B remote or C++ local)
      → tool_use? → SkillExecutor / built-in tools → result
```

---

## 技术债务

- **`_execute_with_cpp()` 已废弃** — 保留参考，GIL 死锁不可解
- **C++ `register_tool` pybind11 绑定无用** — Python 不走 C++ syscall table
- **Clang macOS ARM64 协程 bug** — 已绕过，Clang 升级后重新验证
- **`WorkflowExecutor.execute()` 不传 `ask_user_prompt`** — 只能用 `QueryEngine.submit_message()`
- **``WorkflowExecutor.chat()`/`execute()` 纯文本** — 不支持图片，需走 `QueryEngine.submit_message()`
- **`ReplayEngine` 磁盘缓存** — `~/.daoyi/cache/replay/`，已加 LRU 上限 5000 条
- **Trace 文件堆积** — 已加上限 50 .jsonl 文件
- **Session 快照堆积** — 已加上限 50 个 per project
- **cua-driver `launch_app` TCC 问题** — 本机 bundle attribution 错误，已用 `open -a` + 检测已有 Chrome 绕过
- **远程 Qwen3VL-8B 服务器** — 响应 1s~85s 不等，GPU 争用

---

## 已修改/创建文件清单

### cpp-core/
- `include/daoyi/core.h`, `coroutine.h`
- `src/executor.cpp`, `memory_manager.cpp`, `process_manager.cpp`, `syscall_table.cpp`
- `src/snapshot.cpp`, `small_model_scheduler.cpp`, `replay_cache.cpp`, `context_selector.cpp`
- `src/classifier.cpp`, `normalizer.cpp`, `coroutine_scheduler.cpp`, `llm_engine.cpp`, `workflow_matcher.cpp`, `helpers.cpp`
- `bindings/python/*.cpp`, `tests/test_coroutine.cpp`, `tests/test_executor.cpp`

### src/daoyi/
- `task_workflow/executor.py` — execute(), workspace isolation, `<available-skills>`, `read_skill`/`skill_executor` registration
- `task_workflow/skill_discovery.py` — SkillMatcher, Chinese char matching
- `task_workflow/skill_context_injector.py` — `<available-skills>` block, lazy loading (P0)
- `llm/small_model.py` — Qwen3-2B default, tier-enhanced prompt (P2)
- `llm/classifier.py` — SKILL_TIER_MAP, CONTINUATION_PATTERNS (P1), INTENT_DIFFICULTY_TIERS (P2)
- `tools/skill_executor_tool.py` — dynamic SKILL execution
- `tools/read_skill_tool.py` — lazy SKILL loading
- `tools/_chrome_session.py` — Shared Chrome session singleton (cua-driver, `open -a` fallback)
- `tools/web_search_tool.py` — Baidu 搜索 + wttr.in 天气回退
- `tools/web_fetch_tool.py` — 通过共享 Chrome session 抓取页面
- `tools/cli_anything_tool.py` — legacy hardcoded kept for tests
- `tools/__init__.py` — CliAnythingTool restored to registry
- `sandbox/workspace_provider.py` — GitWorktreeProvider + SnapshotCopyProvider (P3)
- `api/cpp_client.py` — CppLLMClient
- `ui/runtime.py` — _last_intent tracking (P1), env vars
- `config/paths.py` — DAOYI_* + OPENHARNESS_* backward compat
- `config/settings.py` — DAOYI_* env vars primary
- `swarm/spawn_utils.py`, `registry.py` — DAOYI_* + OPENHARNESS_* dual vars

---

## 🎯 下一步研发方向

| 优先级 | 任务 | 说明 |
|--------|------|------|
| P4 | **Auto-Orchestration (多 Agent)** | 复杂任务自动拆分子任务 → 多 agent 协同执行 (marked "暂不实施" in upgrade plan) |
| P4 | **Skill 自动组合** | LLM 动态编排多个 Skill 形成 Workflow，而非单一 Skill 执行 |
| P3 | **搜索引擎缓存** | 缓存搜索结果减少重复 Chrome 调用 |
| P3 | **`settings.json` 后向兼容** | `~/.openharness/settings.json` → `~/.daoyi/settings.json` fallback |
| P2 | **预分类正则优化** | classifier.py 添加更多中文分类 pattern，减少 2B 模型误判 |
| P2 | **`CliAnythingTool` 测试保留验证** | 确认现有 4 个测试正常通过，无需额外维护 |
| P1 | **C++ LLMEngine 生产化** | 稳定性增强 + Metal GPU 预热加速 |
| P1 | **Memory 持久化增强** | L3 存储 + session 级 memory 迭代 |
