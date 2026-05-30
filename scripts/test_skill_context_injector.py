#!/usr/bin/env python3
"""Test SKILL Context Injector."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from daoyi.task_workflow.skill_context_injector import get_skill_context_injector


def test_build_context_message():
    """Test building SKILL context message."""
    injector = get_skill_context_injector()

    print("\n" + "=" * 60)
    print("Test 1: Build SKILL context message")
    print("=" * 60)

    # Test without intent (list all)
    msg = injector.build_skill_context_message(limit=3)
    print(f"\nRole: {msg['role']}")
    print(f"Content preview:\n{msg['content'][:500]}...")

    return True


def test_get_tools_for_llm():
    """Test getting SKILL tools formatted for LLM."""
    injector = get_skill_context_injector()

    print("\n" + "=" * 60)
    print("Test 2: Get SKILL tools for LLM")
    print("=" * 60)

    # Test with intent
    tools = injector.get_skill_tools_for_llm("edit image with gimp", limit=3)

    print(f"\nFound {len(tools)} tools:")
    for tool in tools:
        func = tool["function"]
        print(f"\n- {func['name']}")
        print(f"  Description: {func['description'][:80]}...")
        print(f"  Parameters: {list(func['parameters']['properties'].keys())}")

    return len(tools) > 0


def test_get_command_schema():
    """Test getting command schema for specific SKILL."""
    injector = get_skill_context_injector()

    print("\n" + "=" * 60)
    print("Test 3: Get command schema for cli-anything-gimp")
    print("=" * 60)

    schema = injector.get_skill_command_schema("cli-anything-gimp")

    if schema:
        print(f"\nTool name: {schema['name']}")
        print(f"Description: {schema['description']}")
        print(f"Parameters:")
        for param_name, param_info in schema["parameters"]["properties"].items():
            print(f"  - {param_name}: {param_info['description']}")
            if "enum" in param_info:
                print(f"    Enum values: {param_info['enum'][:5]}...")
        return True
    else:
        print("❌ Schema not found")
        return False


def test_list_all_skills_brief():
    """Test listing all SKILLs briefly."""
    injector = get_skill_context_injector()

    print("\n" + "=" * 60)
    print("Test 4: List all SKILLs briefly")
    print("=" * 60)

    skills = injector.list_all_skills_brief()

    print(f"\nTotal SKILLs: {len(skills)}")
    print("\nFirst 5 SKILLs:")
    for skill in skills[:5]:
        print(f"\n- {skill['name']}")
        print(f"  Description: {skill['description']}")
        print(f"  Commands: {skill['command_count']}")
        print(f"  CLI: {skill['cli_command']}")

    return len(skills) > 0


def main():
    """Run all tests."""
    print("🔍 Testing SKILL Context Injector")

    tests = [
        ("Build context message", test_build_context_message),
        ("Get tools for LLM", test_get_tools_for_llm),
        ("Get command schema", test_get_command_schema),
        ("List all SKILLs brief", test_list_all_skills_brief),
    ]

    results = []
    for name, test_func in tests:
        try:
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
        print("✅ All tests passed!")
    else:
        print("❌ Some tests failed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
