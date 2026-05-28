#!/usr/bin/env python3
"""Test script for SkillExecutorTool."""

import asyncio
from pathlib import Path

# Add src to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from openharness.tools.skill_executor_tool import (
    SkillExecutorTool,
    SkillExecutorInput,
    get_skill_commands,
    list_available_skills,
    ToolExecutionContext,
)


async def test_list_skills():
    """Test listing all available skills."""
    print("\n" + "=" * 60)
    print("Test 1: List all available skills")
    print("=" * 60)

    tool = SkillExecutorTool()
    context = ToolExecutionContext(cwd=Path.cwd())

    result = await tool.execute(
        SkillExecutorInput(skill_name="list", command=""),
        context,
    )

    print(result.output)
    print(f"\nIs Error: {result.is_error}")
    return not result.is_error


async def test_list_gimp_commands():
    """Test listing commands for a specific skill."""
    print("\n" + "=" * 60)
    print("Test 2: List commands for cli-anything-gimp")
    print("=" * 60)

    skill = get_skill_commands("cli-anything-gimp")
    if skill:
        print(f"Skill: {skill.name}")
        print(f"Description: {skill.description}")
        print(f"CLI Command: {skill.cli_command}")
        print(f"\nCommands ({len(skill.commands)}):")
        for cmd in skill.commands[:10]:
            print(f"  - {cmd.name}: {cmd.description}")
        if len(skill.commands) > 10:
            print(f"  ... and {len(skill.commands) - 10} more")
        return True
    else:
        print("❌ Skill not found")
        return False


async def test_tool_list():
    """Test the tool's list command."""
    print("\n" + "=" * 60)
    print("Test 3: Tool list command")
    print("=" * 60)

    tool = SkillExecutorTool()
    context = ToolExecutionContext(cwd=Path.cwd())

    result = await tool.execute(
        SkillExecutorInput(skill_name="list", command=""),
        context,
    )

    print(f"Found {result.output.count('## cli-')} skills")
    return not result.is_error


async def main():
    """Run all tests."""
    print("\n🔍 Testing SkillExecutorTool")
    print("=" * 60)

    tests = [
        ("List Skills", test_list_skills),
        ("Get GIMP Commands", test_list_gimp_commands),
        ("Tool List Command", test_tool_list),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = await test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ Test '{name}' failed with exception:")
            print(f"   {e}")
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
    asyncio.run(main())
