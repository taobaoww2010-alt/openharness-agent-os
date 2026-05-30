# OpenHarness Agent OS 使用指南

## 快速测试

在 `/Users/amyyu12/Documents/daoyi/OpenHarness` 目录下运行：

### 1. 测试 SKILL 生态（不需要网络）
```bash
python3 scripts/quick_test.py
```

### 2. 测试远程 GPU 连接
```bash
python3 scripts/test_remote.py
```

### 3. 完整集成测试（需要网络）
```bash
python3 scripts/test_full_integration.py
```

### 4. 测试 SKILL 发现
```bash
python3 scripts/test_skill_discovery.py
```

### 5. 测试 SKILL 执行器
```bash
python3 scripts/test_skill_executor.py
```

## 预期结果

### quick_test.py
- 显示 60 个可用的 SKILL
- 显示意图匹配结果

### test_remote.py
- 显示 "✅ 远程 GPU 服务器连接成功!"
- 显示 AI 回复

### test_full_integration.py
- 显示 "🎉 全部测试通过! (4/4)"

## 如果测试失败

请告诉我：
1. 运行的命令是什么
2. 错误信息是什么
3. 是哪一步失败的
