#!/usr/bin/env python3
"""快速测试脚本 - 验证 SKILL 生态是否正常工作"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from daoyi.task_workflow.skill_discovery import get_skill_matcher


def main():
    print("\n" + "=" * 60)
    print("🚀 OpenHarness SKILL 生态快速测试")
    print("=" * 60)

    matcher = get_skill_matcher()

    print("\n📦 可用 SKILL 统计:")
    all_skills = matcher.list_all_skills()
    print(f"   总数: {len(all_skills)} 个")

    categories = {
        "🎨 创意设计": ["gimp", "blender", "inkscape", "krita"],
        "🎬 视频处理": ["kdenlive", "shotcut", "obs", "ffmpeg"],
        "🎵 音频处理": ["audacity", "musescore"],
        "📄 办公软件": ["libreoffice", "calibre"],
        "🧠 AI/ML": ["comfyui", "ollama", "novita"],
    }

    for cat, keywords in categories.items():
        matching = [s for s in all_skills if any(kw in s["name"].lower() for kw in keywords)]
        if matching:
            print(f"\n   {cat}:")
            for s in matching[:3]:
                print(f"      - {s['name']}")

    print("\n\n🔍 意图匹配测试:")

    test_intents = [
        "edit image with gimp",
        "create 3d model with blender",
        "make video editing",
        "manage ebooks",
    ]

    for intent in test_intents:
        print(f"\n   意图: '{intent}'")
        results = matcher.find_skills(intent, limit=3)
        if results:
            print(f"   匹配: {results[0]['name']}")
            print(f"   命令: {[c['name'] for c in results[0]['commands'][:3]]}")
        else:
            print(f"   ❌ 未匹配到")

    print("\n" + "=" * 60)
    print("✅ SKILL 生态测试完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
