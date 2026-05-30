# OpenHarness 架构升级方案 — 借鉴 PilotDeck

## 背景

分析了 OpenBMB/PilotDeck 的完整架构后，发现其 5 个关键设计可以直接应用到 OpenHarness 中：
技能上下文管理、分级分类体系、短消息处理、工作空间隔离、自动编排模式。

下文逐一说明**为什么改**、**怎么改**、**改哪些文件**。

---

## 1. 技能懒加载（最高优先级）

### 现状

`executor.py:294-301` 在每个 phase 的 system prompt 里注入完整技能信息：

```
## cli-anything-gimp [core]
Description: Command-line interface for Gimp...
CLI Command: cli-anything-gimp
Available commands:
  - project new: ...
  - layer add: ...
  ... and 20 more commands
```

即使只注入 top-5 技能，每轮对话也浪费 **300-800 tokens** 在技能详情上。

### PilotDeck 的做法（更优）

`PromptAssembler.ts:227-238` 只列出技能名 + 一行描述，放在 `<available-skills>` 块里；

LLM 需要时自己调 `read_skill` 工具加载完整内容。

### 改动方案

**思路**：把"系统主动注入"改成"按需加载"。

1. `skill_context_injector.py` — 重写 `build_skill_context_message()`
   - 输出 `<available-skills>` 块，每个技能只含名称 + 一行描述 + 层级标签
   - 附带说明：Use `read_skill` tool to load full content

2. 新增 `tools/read_skill_tool.py` — 一个简单工具
   - 参数：`skill_name: str`
   - 返回 SKILL.md 完整内容 + 命令列表

3. `executor.py` — 不再把技能详情塞入 system prompt

4. 收益估算：每轮节省 ~500 tokens

### 改动文件清单

| 文件 | 改动 |
|------|------|
| `task_workflow/skill_context_injector.py` | 重写 `build_skill_context_message()` → `<available-skills>` 格式 |
| `tools/read_skill_tool.py` | **新建** — read_skill 工具 |
| `executor.py` | 删除 `skill_msg["content"]` 注入，改为只注入 `<available-skills>` 块 |

### 效果对比

```
改前 system prompt:
  ## cli-anything-gimp [core]
  Description: Command-line interface for Gimp...
  CLI Command: cli-anything-gimp
  Available commands:
    - project new: Create a new project
    - layer add: Add a new layer to image
    ... (全量注入，~500 tokens)

改后 system prompt:
  <available-skills>
  Use read_skill tool to load full content.
  - cli-anything-gimp [core] — Command-line interface for Gimp
  - cli-anything-blender [core] — Command-line interface for Blender
  </available-skills>
  (~50 tokens, 可列出所有技能)
```

---

## 2. 分级分类体系增强

### 现状

- `small_model.py:86-184` 用 few-shot 把用户输入分成 6 类（tool/chat/code/search/code_review/file_ops）
- `classifier.py:22-79` 有 `SKILL_TIER_MAP`（5 级 skill 分层），但分类器**没有使用** tier 信息
- 分类结果只有"类别"，没有"难度"维度

### PilotDeck 的做法

`schema.ts:99-111` 用 4 个 tier + 4 条硬规则，让 judge 模型精确分类：

```
simple:    "Simple greetings, confirmations, single-step Q&A"
medium:    "Single tool call, short text generation, 1-2 file ops"
complex:   "Needs sub-agent orchestration: parallel workstreams"
reasoning: "Deep single-agent work: multi-file ops, data analysis..."
```

再加上 `isShortContinuation()` 检测短消息延续。

### 改动方案

**思路**：在现有 6 类 intent 之上叠一个**难度维度**（simple/medium/complex/reasoning），优化分类 prompt 并加上延续检测。

1. `classifier.py` — 新增 `TIER_DESCRIPTIONS` + `TIER_RULES`
   - 翻译自 PilotDeck 的 4 级描述
   - 给 `SmallModelClient.classify()` 的 prompt 加上 tier 维度

2. `small_model.py` — 增强 `classify()` prompt
   - 保持 6 类输出不变，但内部 prompt 加入 tier 描述约束
   - 例如："If the user says '继续' or 'go', classify as the same intent as the previous turn"

3. `executor.py` — 加 `is_short_continuation()` 预检查
   - 在 `pre_classify()` 之前走这个规则

### 改动文件清单

| 文件 | 改动 |
|------|------|
| `llm/classifier.py` | 新增 `TIER_DESCRIPTIONS`、`TIER_RULES` 常量；`RuleClassifier` 加延续检测 |
| `llm/small_model.py` | `classify()` 把延续规则编入 prompt；加 `is_short_continuation()` 静态方法 |
| `task_workflow/executor.py` | `pre_classify()` 先检查短消息延续 |

---

## 3. 短消息延续检测

### 现状

每条消息都跑一遍 `pre_classify()`。用户说"继续"、"go"、"然后呢"时，2B 模型经常错误分类成 "chat"，导致走 chat 快速路径而不是继续之前的 tool 操作。

### PilotDeck 的做法

`classifyAndRoute.ts:157-175`：短消息（≤30 字符）+ 匹配延续模式 → 直接继承上一轮分类，不重新 judge。

```typescript
const CONTINUATION_PATTERNS = [
  /^(go|ok|yes|y|sure|do it|proceed|continue|next|done|start|run|好|好的|继续|开始|可以|行|嗯|对|是的|没问题|来吧|冲|走|执行|开搞|干|上)$/i,
];
```

### 改动方案

1. 在 `executor.py` 的 `pre_classify()` 之前加检查
2. 匹配延续模式 → 返回上一次的 intent
3. 不匹配 → 正常走分类

### 改动文件清单

| 文件 | 改动 |
|------|------|
| `llm/classifier.py` | 新增 `is_short_continuation()` 函数 + `CONTINUATION_PATTERNS` 常量 |
| `task_workflow/executor.py` | `pre_classify()` 中先调用 `is_short_continuation()`，匹配则返回上一次 intent |
| `ui/runtime.py` | `handle_line()` 中保存/传递上一个 intent |

---

## 4. 工作空间隔离

### 现状

`tool_metadata` 里有 `enter_worktree_tool` / `exit_worktree_tool`，但这是**手动工具**——LLM 自己决定要不要创建 worktree。实际场景中 LLM 很少主动调用。

### PilotDeck 的做法

`WorkspaceProvider.ts` 定义抽象接口：

```typescript
interface WorkspaceProvider {
  id: string;
  priority: number;
  isApplicable(projectRoot: string): boolean;
  prepare(): WorkspaceHandle;
  publish(handle): { commit, diff };
  dispose(handle, { keep }): void;
}
```

两个内置实现：
- `GitWorktreeProvider`：git worktree（秒级，共享 repo 对象）
- `SnapshotCopyProvider`：全量目录复制

Always-On 调度器在每次 discovery cycle 前自动创建隔离 worktree，用完丢弃。

### 改动方案

**思路**：把 worktree 创建从"LLM 可选工具"变成"执行器自动层"。

1. 新建 `sandbox/workspace_provider.py`
   - `WorkspaceProvider` 基类
   - `GitWorktreeProvider` 实现
   - `SnapshotCopyProvider` 实现

2. `executor.py` 在 `execute()` 开始时自动创建隔离工作空间
   - 工具执行在 worktree 内进行
   - phase 结束后自动清理或 publish

3. 暂不涉及 Always-On（我们没有这个需求）

### 改动文件清单

| 文件 | 改动 |
|------|------|
| `sandbox/workspace_provider.py` | **新建** — WorkspaceProvider 抽象 + 两个实现 |
| `task_workflow/executor.py` | `execute()` 自动创建 worktree，工具调用在隔离空间内执行 |

---

## 5. 自动编排模式（中长期）

### 现状

所有任务走同一个 agent loop。复杂多步任务（比如 "帮我分析这个项目的性能瓶颈并给出优化方案"）经常在单线程里绕圈。

### PilotDeck 的做法

`schema.ts:123-189`：当分类为 "complex" 时，自动切换到 orchestrator 模式：

- 工具白名单：只有 agent / read_file / grep / glob / read_skill 5 个
- 用 `agent` 工具 fork 子 agent 执行子任务
- 子 agent 继承所有工具权限
- 每个子 agent 的 prompt 必须是自包含的（不依赖对话历史）

### 改动方案（暂不实施，先记下）

将来如果要做多 agent 协作：
1. 在 workflow 体系中增加 `orchestrator` 角色
2. `complex` 层级自动切 orchestrator 模式
3. 子 agent 用隔离 context 执行

---

## 实施顺序

| 优先级 | 任务 | 预计收益 | 复杂度 | 工时 |
|--------|------|----------|--------|------|
| **P0** | 技能懒加载（read_skill + `<available-skills>`） | 每轮省 ~500 tokens | 低 | 1-2h |
| **P1** | 短消息延续检测 | 减少 30%+ 误分类 | 极低 | 0.5h |
| **P2** | 分级分类 prompt 增强 | 分类准确率提升 | 低 | 1h |
| **P3** | 工作空间隔离（自动 worktree） | 保护主仓库 | 中 | 3-4h |
| **P4** | 自动编排（多 agent 协作） | 提升复杂任务质量 | 高 | 待定 |

---

## 预期效果

完成 P0-P2 后：
- system prompt token 消耗降低 50%+
- 短消息响应延迟降低（跳过分类重路由）
- 分类准确率提升（少模型误判 + 延续继承）
- 所有 1166 测试继续通过
