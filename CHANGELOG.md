# OpenHarness 更新日志

## [0.1.0] - 2026-05-26

### 新增功能

#### Phase 5: GPU推理引擎集成
- 添加远程GPU推理支持
- 支持连接到远程GPU服务器（如 `192.168.31.164:8080`）
- 实现 OpenAI 兼容的 `/v1/completions` API 调用
- 支持 API Key 认证
- 添加 `GPUConfig` 配置结构体，支持本地/远程模式切换
- 支持 llama.cpp 的 Metal 后端（无 CUDA 依赖）

#### Phase 4: 协程调度器
- 实现 C++20 协程调度器
- 支持优先级调度
- 支持最大并发数控制
- 支持任务队列管理

#### Phase 3: 小模型调度器
- 实现上下文选择器（ContextSelector）
- 实现 Replay 缓存（借鉴 shimmy 项目设计）
- 支持 LRU + TTL 双策略缓存
- 智能减少 LLM 输入 Token 数量

#### Phase 2: 输入标准化层
- 实现意图分类器（Classifier）
- 实现输入标准化器（Normalizer）
- 实现工作流匹配器（WorkflowMatcher）
- 将自然语言转化为结构化格式

#### Phase 1: C++工程骨架
- 实现 MemoryManager（三层缓存 L1/L2/L3）
- 实现 ProcessManager（进程管理、状态管理、信号机制）
- 实现 SyscallTable（系统调用表、工具注册和执行）

### 架构设计

```
┌─────────────────────────────────────────────────────────────────┐
│                      Agent OS 分层架构                          │
├─────────────────────────────────────────────────────────────────┤
│  Layer 5: 应用层 (Python绑定)                                   │
├─────────────────────────────────────────────────────────────────┤
│  Layer 4: 执行层 (Executor + 工作流匹配)                        │
├─────────────────────────────────────────────────────────────────┤
│  Layer 3: 调度层 (协程调度器 + 小模型调度)                       │
├─────────────────────────────────────────────────────────────────┤
│  Layer 2: 标准化层 (分类器 + 输入标准化)                         │
├─────────────────────────────────────────────────────────────────┤
│  Layer 1: 核心层 (内存管理 + 进程管理 + 系统调用)                │
├─────────────────────────────────────────────────────────────────┤
│  Layer 0: 硬件抽象层 (GPU推理引擎 - llama.cpp + Metal)          │
└─────────────────────────────────────────────────────────────────┘
```

### 核心思想
- **GPU = CPU**: 将GPU作为推理计算核心
- **LLM = 指令集**: 将大模型视为高级指令处理器
- **Agent Loop = 内核**: 协程调度器作为任务调度核心
- **减少大模型依赖**: 通过小模型调度 + Replay缓存优化性能
- **输入标准化**: 将自然语言转化为模型友好的结构化格式

### 测试结果
- ✅ 24 个单元测试全部通过
- ✅ 远程GPU推理成功连接（Qwen3VL-8B-Instruct模型）
- ✅ 协程调度器功能验证完成

### 文件结构
```
cpp-core/
├── include/daoyi/
│   ├── core.h          # 核心类型和接口定义
│   └── coroutine.h     # 协程封装
├── src/
│   ├── memory_manager.cpp
│   ├── process_manager.cpp
│   ├── syscall_table.cpp
│   ├── llm_engine.cpp       # LLM引擎（本地+远程）
│   ├── executor.cpp
│   ├── classifier.cpp
│   ├── normalizer.cpp
│   ├── workflow_matcher.cpp
│   ├── context_selector.cpp
│   ├── replay_cache.cpp
│   ├── small_model_scheduler.cpp
│   ├── coroutine_scheduler.cpp
│   └── main.cpp
├── tests/              # 24个单元测试
├── third_party/llama.cpp
└── CMakeLists.txt
```

### 使用方式

**本地GPU模式**:
```bash
./daoyi_cli
```

**远程GPU模式**:
```bash
./daoyi_cli --remote
```

### 依赖
- C++20
- llama.cpp（Metal后端）
- curl（远程推理）
- Google Test（测试）
