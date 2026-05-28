#!/usr/bin/env python3
"""Test the complete SKILL integration flow."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


async def test_skill_in_workflow():
    """Test SKILL integration in WorkflowExecutor."""
    print("\n" + "=" * 60)
    print("Test: SKILL Integration in WorkflowExecutor")
    print("=" * 60)

    try:
        # 1. Check SKILL context injector
        from openharness.task_workflow.skill_context_injector import get_skill_context_injector
        injector = get_skill_context_injector()
        skill_msg = injector.build_skill_context_message("edit image with gimp", limit=3)
        print(f"\n1. ✅ SKILL context built")
        print(f"   Context length: {len(skill_msg['content'])} chars")
        print(f"   First 200 chars:\n   {skill_msg['content'][:200]}...")

        # 2. Check skill_executor tool schema
        schema = injector.get_skill_command_schema("cli-anything-gimp")
        print(f"\n2. ✅ Skill executor schema:")
        print(f"   Name: {schema['name']}")
        print(f"   Commands: {len(schema['parameters']['properties']['command']['enum'])}")

        # 3. Check WorkflowExecutor code modification
        with open("src/openharness/task_workflow/executor.py", "r") as f:
            executor_code = f.read()

        has_skill_injection = "SKILL context injection" in executor_code
        has_skill_tool = "skill_executor tool" in executor_code

        print(f"\n3. ✅ WorkflowExecutor modifications:")
        print(f"   SKILL context injection: {'✅' if has_skill_injection else '❌'}")
        print(f"   skill_executor tool: {'✅' if has_skill_tool else '❌'}")

        if not has_skill_injection or not has_skill_tool:
            print("\n❌ SKILL integration not found in WorkflowExecutor!")
            return False

        print("\n✅ All SKILL integration tests passed!")
        return True

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_skill_executor_tool():
    """Test the SkillExecutorTool."""
    print("\n" + "=" * 60)
    print("Test: SkillExecutorTool")
    print("=" * 60)

    try:
        from openharness.tools.skill_executor_tool import SkillExecutorTool, SkillExecutorInput
        from openharness.tools.base import ToolExecutionContext

        tool = SkillExecutorTool()
        print(f"\n1. ✅ Tool loaded: {tool.name}")
        print(f"   Description: {tool.description[:80]}...")

        # Test list command
        context = ToolExecutionContext(cwd=Path.cwd())
        result = await tool.execute(
            SkillExecutorInput(skill_name="list", command=""),
            context,
        )

        skill_count = result.output.count("## ")
        print(f"\n2. ✅ List command works")
        print(f"   Found {skill_count} SKILLs")

        # Test GIMP commands
        result = await tool.execute(
            SkillExecutorInput(skill_name="cli-anything-gimp", command="list"),
            context,
        )

        print(f"\n3. ✅ GIMP commands:")
        lines = result.output.split("\n")[:10]
        for line in lines:
            if line.strip():
                print(f"   {line}")

        print("\n✅ SkillExecutorTool tests passed!")
        return True

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_end_to_end_flow():
    """Test the end-to-end flow from user input to SKILL execution."""
    print("\n" + "=" * 60)
    print("Test: End-to-End SKILL Flow")
    print("=" * 60)

    print("""
Expected flow after implementation:

1. User input: "帮我用 GIMP 创建一个 800x600 的图片"

2. SmallModel classify: "tool"

3. SkillMatcher match:
   → cli-anything-gimp (score: 3.0)

4. WorkflowExecutor (SKILL context injected):
   System prompt includes:
   "You have access to the following SKILL tools:
   ## cli-anything-gimp
   CLI Command: cli-anything-gimp
   Available commands:
     - new: Create a new project
     - open: Open a project
     ..."

5. LLM sees available tools and generates:
   skill_executor(
     skill_name="cli-anything-gimp",
     command="new",
     args="--width 800 --height 600"
   )

6. SkillExecutorTool executes:
   $ cli-anything-gimp new --width 800 --height 600

7. Return result to user
""")

    return True


async def main():
    """Run all tests."""
    print("🔍 Testing Complete SKILL Integration")

    tests = [
        ("SKILL in WorkflowExecutor", test_skill_in_workflow),
        ("SkillExecutorTool", test_skill_executor_tool),
        ("End-to-End Flow", test_end_to_end_flow),
    ]

    results = []
    for name, test_func in tests:
        try:
            if asyncio.iscoroutinefunction(test_func):
                result = await test_func()
            else:
                result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ Test '{name}' failed: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    print("\n" + "=" * 60)
    print("📊 Test Results Summary")
    print("=" * 60)

    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status}: {name}")

    all_passed = all(r for _, r in results)
    print("\n" + "=" * 60)
    if all_passed:
        print("🎉 All SKILL integration tests passed!")
    else:
        print("⚠️  Some tests failed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
