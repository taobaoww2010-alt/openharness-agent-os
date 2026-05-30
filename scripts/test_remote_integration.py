#!/usr/bin/env python3
"""综合测试脚本：测试远程 LLM + SKILL 执行"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from daoyi.api.cpp_client import CppLLMClient
from daoyi.task_workflow.skill_discovery import get_skill_matcher


async def test_remote_llm():
    """测试远程 LLM 调用"""
    print("\n" + "=" * 60)
    print("测试 1: 远程 LLM 调用")
    print("=" * 60)

    client = CppLLMClient(
        host="192.168.31.164",
        port=8080,
        api_key="tomle2026"
    )

    print(f"连接模式: {client._mode}")
    print(f"引擎: {client._engine}")

    messages = [
        {"role": "user", "content": "你好，请用一句话介绍自己"}
    ]

    print("\n发送请求到远程 GPU 服务器...")
    response_text = ""

    try:
        from daoyi.api.client import ApiMessageCompleteEvent

        # 使用实际的消息列表
        request_messages = [
            {"role": "user", "content": "你好，请用一句话介绍自己"}
        ]

        class MockRequest:
            messages = request_messages
            model = "qwen3vl-8b"
            temperature = 0.7
            max_tokens = 500

        async for event in client.stream_message(MockRequest()):
            if hasattr(event, 'delta'):
                response_text += event.delta
                print(f"收到: {event.delta}", end="", flush=True)
            elif hasattr(event, 'content'):
                response_text += event.content
                print(f"收到: {event.content}", end="", flush=True)

        print("\n\n✅ 远程 LLM 调用成功!")
        return True

    except Exception as e:
        print(f"\n\n❌ 远程 LLM 调用失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_skill_discovery():
    """测试 SKILL 发现"""
    print("\n" + "=" * 60)
    print("测试 2: SKILL 发现和匹配")
    print("=" * 60)

    matcher = get_skill_matcher()

    # 测试意图匹配
    test_cases = [
        "edit image with gimp",
        "create 3d model with blender",
        "make video editing",
        "manage ebooks",
    ]

    all_passed = True

    for intent in test_cases:
        print(f"\n📝 意图: {intent}")
        results = matcher.find_skills(intent, limit=3)

        if results:
            print(f"   ✅ 找到 {len(results)} 个匹配:")
            for i, skill in enumerate(results, 1):
                print(f"      {i}. {skill['name']}")
            if results[0]['name'].lower().replace('-', '').replace('_', '') in intent.lower().replace('-', '').replace('_', ''):
                print(f"      ⭐ 最佳匹配!")
        else:
            print(f"   ❌ 未找到匹配")
            all_passed = False

    if all_passed:
        print("\n✅ SKILL 发现测试通过!")
    else:
        print("\n⚠️ 部分 SKILL 发现测试未通过")

    return all_passed


async def test_skill_executor_list():
    """测试 SKILL 执行器列表功能"""
    print("\n" + "=" * 60)
    print("测试 3: SKILL 执行器列表")
    print("=" * 60)

    from daoyi.tools.skill_executor_tool import SkillExecutorTool, SkillExecutorInput
    from daoyi.tools.base import ToolExecutionContext

    tool = SkillExecutorTool()
    context = ToolExecutionContext(cwd=Path.cwd())

    try:
        result = await tool.execute(
            SkillExecutorInput(skill_name="list", command=""),
            context,
        )

        # 统计找到的 SKILL 数量
        skill_count = result.output.count("## ")
        print(f"\n📦 找到 {skill_count} 个 SKILL")

        # 列出前 5 个
        lines = result.output.split("\n")
        in_section = False
        count = 0
        for line in lines:
            if line.startswith("## "):
                if not in_section:
                    in_section = True
                if count < 5:
                    print(f"   - {line[3:]}")
                    count += 1
            elif line.startswith("CLI Command:"):
                if count < 5:
                    print(f"     {line}")

        if count > 5:
            print(f"   ... 还有 {skill_count - 5} 个")

        print(f"\n✅ SKILL 执行器列表测试通过!")
        return True

    except Exception as e:
        print(f"\n❌ SKILL 执行器列表测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("🚀 OpenHarness 综合测试")
    print("=" * 60)
    print("\n远程服务器: 192.168.31.164:8080")
    print("API Key: tomle2026")

    results = []

    # 测试 1: 远程 LLM
    result1 = await test_remote_llm()
    results.append(("远程 LLM 调用", result1))

    # 测试 2: SKILL 发现
    result2 = test_skill_discovery()
    results.append(("SKILL 发现", result2))

    # 测试 3: SKILL 执行器
    result3 = await test_skill_executor_list()
    results.append(("SKILL 执行器", result3))

    # 总结
    print("\n" + "=" * 60)
    print("📊 测试结果汇总")
    print("=" * 60)

    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status}: {name}")

    passed = sum(1 for _, r in results if r)
    total = len(results)

    print("\n" + "=" * 60)
    if passed == total:
        print(f"🎉 全部测试通过! ({passed}/{total})")
    else:
        print(f"⚠️  {passed}/{total} 测试通过")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
