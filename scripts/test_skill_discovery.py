#!/usr/bin/env python3
"""Test script for SkillMatcher."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from daoyi.task_workflow.skill_discovery import get_skill_matcher


def test_find_skills():
    """Test finding skills based on user intent."""
    matcher = get_skill_matcher()

    test_intents = [
        ("帮我用 GIMP 编辑图片", "image editing with GIMP"),
        ("用 Blender 做 3D 模型", "3D modeling with Blender"),
        ("创建 Word 文档", "Word document creation"),
        ("编辑视频", "video editing"),
        ("搜索网页内容", "web search"),
    ]

    print("\n" + "=" * 60)
    print("Testing SkillMatcher")
    print("=" * 60)

    for cn_intent, en_intent in test_intents:
        print(f"\n📝 Intent: {cn_intent}")
        print(f"   English: {en_intent}")

        results = matcher.find_skills(en_intent, limit=3)
        if results:
            print(f"   Found {len(results)} matching skills:")
            for i, skill in enumerate(results, 1):
                print(f"   {i}. {skill['name']}")
                print(f"      CLI: {skill['cli_command']}")
                print(f"      Desc: {skill['description'][:80]}...")
        else:
            print("   ❌ No matching skills found")


def test_suggest_workflow():
    """Test workflow suggestion."""
    matcher = get_skill_matcher()

    test_intents = [
        "create an image with gimp",
        "edit 3d model in blender",
        "manage ebooks with calibre",
    ]

    print("\n" + "=" * 60)
    print("Testing Workflow Suggestion")
    print("=" * 60)

    for intent in test_intents:
        print(f"\n📝 Intent: {intent}")
        suggestion = matcher.suggest_workflow(intent)

        if suggestion["type"] == "no_match":
            print(f"   ❌ {suggestion['message']}")
        else:
            skill = suggestion["skill"]
            print(f"   ✅ Matched: {skill['name']}")
            print(f"   📦 CLI: {skill['cli_command']}")
            print(f"   🔧 Suggested commands:")
            for cmd in suggestion["suggested_commands"][:3]:
                print(f"      - {cmd['name']}: {cmd['description']}")


def test_list_all():
    """Test listing all skills."""
    matcher = get_skill_matcher()

    print("\n" + "=" * 60)
    print("All Available Skills")
    print("=" * 60)

    skills = matcher.list_all_skills()
    print(f"\nTotal skills: {len(skills)}")

    categories = {
        "Creative": ["gimp", "blender", "inkscape", "krita", "audacity", "musescore"],
        "Video": ["kdenlive", "shotcut", "obs-studio", "openscreen"],
        "Office": ["libreoffice", "calibre", "drawio"],
        "AI/ML": ["comfyui", "ollama", "exa", "novita"],
        "Development": ["godot", "lldb", "nsight"],
    }

    for category, keywords in categories.items():
        matching = [s for s in skills if any(kw in s["name"].lower() for kw in keywords)]
        if matching:
            print(f"\n{category}:")
            for skill in matching[:5]:
                print(f"  - {skill['name']}")


def main():
    """Run all tests."""
    print("🔍 Testing SKILL Discovery and Matching")

    try:
        test_find_skills()
        test_suggest_workflow()
        test_list_all()

        print("\n" + "=" * 60)
        print("✅ All tests completed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
