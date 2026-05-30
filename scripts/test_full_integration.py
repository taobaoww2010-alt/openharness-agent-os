#!/usr/bin/env python3
"""完整的 SKILL + LLM 集成测试"""

import requests
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from daoyi.task_workflow.skill_discovery import get_skill_matcher
from daoyi.task_workflow.skill_context_injector import get_skill_context_injector


def test_llm():
    """测试 LLM 调用"""
    print("\n" + "=" * 60)
    print("🤖 测试 1: LLM 调用")
    print("=" * 60)

    url = "http://192.168.31.164:8080/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer tomle2026"
    }

    prompt = "用一句话介绍自己"

    data = {
        "model": "qwen3vl-8b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 100
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        result = response.json()

        if "choices" in result:
            content = result["choices"][0]["message"]["content"]
            print(f"\n✅ LLM 调用成功!")
            print(f"回复: {content}")
            return True
    except Exception as e:
        print(f"❌ LLM 调用失败: {e}")
        return False


def test_skill_discovery():
    """测试 SKILL 发现"""
    print("\n" + "=" * 60)
    print("🔍 测试 2: SKILL 发现")
    print("=" * 60)

    matcher = get_skill_matcher()

    test_intents = [
        "edit image with gimp",
        "3d blender model",
        "video editing",
    ]

    for intent in test_intents:
        results = matcher.find_skills(intent, limit=2)
        if results:
            print(f"\n✅ 意图: '{intent}'")
            print(f"   匹配: {results[0]['name']}")

    return True


def test_skill_context_injection():
    """测试 SKILL 上下文注入"""
    print("\n" + "=" * 60)
    print("💉 测试 3: SKILL 上下文注入")
    print("=" * 60)

    injector = get_skill_context_injector()

    # 测试生成上下文消息
    msg = injector.build_skill_context_message("edit image with gimp", limit=2)

    print(f"\n✅ 上下文消息生成成功!")
    print(f"角色: {msg['role']}")
    print(f"内容预览:\n{msg['content'][:300]}...")

    # 测试生成工具 schema
    schema = injector.get_skill_command_schema("cli-anything-gimp")

    if schema:
        print(f"\n✅ 工具 Schema 生成成功!")
        print(f"工具名: {schema['name']}")
        print(f"命令数: {len(schema['parameters']['properties']['command']['enum'])}")

    return True


def test_full_flow():
    """测试完整流程：用户意图 → SKILL 发现 → LLM 调用"""
    print("\n" + "=" * 60)
    print("🌊 测试 4: 完整流程")
    print("=" * 60)

    matcher = get_skill_matcher()
    injector = get_skill_context_injector()

    user_intent = "帮我用 GIMP 创建一个 800x600 的图片"

    print(f"\n用户输入: {user_intent}")

    # 1. 发现 SKILL
    skills = matcher.find_skills(user_intent, limit=1)
    if skills:
        skill = skills[0]
        print(f"\n1. SKILL 匹配: {skill['name']}")

        # 2. 获取 SKILL 上下文
        context_msg = injector.build_skill_context_message(user_intent, limit=1)

        # 3. 构造 LLM 请求
        url = "http://192.168.31.164:8080/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer tomle2026"
        }

        messages = [
            {"role": "system", "content": context_msg["content"] + "\n\n请根据用户需求，选择合适的工具完成任务。"},
            {"role": "user", "content": user_intent}
        ]

        data = {
            "model": "qwen3vl-8b",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 200
        }

        print(f"\n2. 发送带 SKILL 上下文的请求到 LLM...")

        try:
            response = requests.post(url, headers=headers, json=data, timeout=30)
            result = response.json()

            if "choices" in result:
                content = result["choices"][0]["message"]["content"]
                print(f"\n3. ✅ LLM 回复:")
                print(f"   {content}")
                return True
        except Exception as e:
            print(f"❌ 请求失败: {e}")
            return False

    return False


def main():
    print("\n" + "=" * 60)
    print("🚀 OpenHarness 完整集成测试")
    print("=" * 60)

    tests = [
        ("LLM 调用", test_llm),
        ("SKILL 发现", test_skill_discovery),
        ("SKILL 上下文注入", test_skill_context_injection),
        ("完整流程", test_full_flow),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ 测试 '{name}' 出错: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

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
    main()
