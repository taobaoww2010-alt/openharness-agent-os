from __future__ import annotations

import logging
import re
from typing import ClassVar

logger = logging.getLogger(__name__)

INTENT_TOOL = "tool"
INTENT_CHAT = "chat"
INTENT_CODE = "code"
INTENT_SEARCH = "search"
INTENT_CODE_REVIEW = "code_review"
INTENT_FILE_OPS = "file_ops"

INTENT_DIFFICULTY_TIERS: dict[str, str] = {
    "simple": "Simple greetings, confirmations, single-step Q&A",
    "medium": "Single tool call, short text generation, 1-2 file operations",
    "complex": "Needs sub-agent orchestration: parallel workstreams, multi-step",
    "reasoning": "Deep single-agent work: multi-file ops, data analysis, research",
}

INTENT_DIFFICULTY_RULES: list[str] = [
    "If the input is ≤5 characters and matches a continuation pattern ('go', '继续', '然后呢'): inherit previous intent",
    "If the input asks about weather, news, or current info: classify as 'search' with medium difficulty",
    "If the input requests file manipulation (read/write/edit/delete): classify as 'file_ops'",
    "If the input asks to write code or implement something: classify as 'code'",
    "If the input mentions running commands, opening apps, or executing: classify as 'tool'",
]

TIER_CORE = "core"
TIER_DEV = "dev"
TIER_AI = "ai"
TIER_PRO = "pro"
TIER_LITE = "lite"

SKILL_TIER_MAP: dict[str, str] = {
    # T1 核心创作
    "shotcut": TIER_CORE,
    "audacity": TIER_CORE,
    "gimp": TIER_CORE,
    "inkscape": TIER_CORE,
    "krita": TIER_CORE,
    "blender": TIER_CORE,
    "freecad": TIER_CORE,
    "libreoffice": TIER_CORE,
    "musescore": TIER_CORE,
    "obs-studio": TIER_CORE,
    # T2 自动化与开发
    "browser": TIER_DEV,
    "safari": TIER_DEV,
    "iterm2-ctl": TIER_DEV,
    "lldb": TIER_DEV,
    "pm2": TIER_DEV,
    "n8n": TIER_DEV,
    "dify-workflow": TIER_DEV,
    "comfyui": TIER_DEV,
    "wiremock": TIER_DEV,
    "macrocli": TIER_DEV,
    # T3 AI 与数据
    "ollama": TIER_AI,
    "novita": TIER_AI,
    "minimax": TIER_AI,
    "chromadb": TIER_AI,
    "exa": TIER_AI,
    "anygen": TIER_AI,
    "notebooklm": TIER_AI,
    # T4 专业工具
    "qgis": TIER_PRO,
    "cloudcompare": TIER_PRO,
    "threemf": TIER_PRO,
    "nsight-graphics": TIER_PRO,
    "renderdoc": TIER_PRO,
    "unrealinsights": TIER_PRO,
    "rekordbox": TIER_PRO,
    "calibre": TIER_PRO,
    "zotero": TIER_PRO,
    "firefly-iii": TIER_PRO,
    "intelwatch": TIER_PRO,
    "rms": TIER_PRO,
    "adguardhome": TIER_PRO,
    # T5 轻量工具
    "mermaid": TIER_LITE,
    "mubu": TIER_LITE,
    "obsidian": TIER_LITE,
    "videocaptioner": TIER_LITE,
    "quietshrink": TIER_LITE,
    "nslogger": TIER_LITE,
    "seaclip": TIER_LITE,
    "cloudanalyzer": TIER_LITE,
    "unimol-tools": TIER_LITE,
    "mailchimp": TIER_LITE,
    "zoom": TIER_LITE,
}


def tier_for_skill(skill_name: str) -> str:
    """Get the tier for a skill name (handle cli-anything- prefix)."""
    key = skill_name.removeprefix("cli-anything-")
    return SKILL_TIER_MAP.get(key, TIER_LITE)


class RuleClassifier:
    _PREFIX_MAP: ClassVar[list[tuple[tuple[str, ...], str]]] = [
        (("run ", "bash ", "execute ", "cmd ", "terminal "), INTENT_TOOL),
    ]

    _CONTAINS_MAP: ClassVar[list[tuple[tuple[str, ...], str]]] = [
        # tool — command execution
        (
            (
                "in the shell", "in the terminal",
                "run the command", "执行命令", "运行命令",
                "echo ", "pwd", "uname", "whoami",
                "打开", "open ", "启动", "关闭",
                "运行", "执行",
                "run '", "run \"", "bash '", "bash \"",
            ),
            INTENT_TOOL,
        ),
        # search
        (
            (
                "search ", "find ", "grep ",
                "查找", "搜索", "查询", "搜一下", "查一下", "找一下",
            ),
            INTENT_SEARCH,
        ),
        # file ops
        (
            (
                "write ", "create ", "save ",
                "生成文件", "创建文件",
                "read ", "view ", "cat ", "查看", "读取",
                "重命名", "删除", "复制", "移动",
            ),
            INTENT_FILE_OPS,
        ),
        # code
        (
            (
                "implement ", "function ", "method ", "class ",
                "编写代码", "实现", "代码",
                "write a ", "write an ", "编写一个", "写一个",
                "def ", "import ", "print(",
            ),
            INTENT_CODE,
        ),
        # workflow
        (
            ("workflow ", "流程 ", "自动化 ", "pipeline "),
            INTENT_TOOL,
        ),
        # code review
        (
            ("review ", "code review", "审核", "code_review"),
            INTENT_CODE_REVIEW,
        ),
    ]

    def classify(self, text: str) -> str | None:
        lower = text.strip().lower()

        # prefix check
        for prefixes, intent in self._PREFIX_MAP:
            for p in prefixes:
                if lower.startswith(p):
                    return intent

        # contains check
        for patterns, intent in self._CONTAINS_MAP:
            for p in patterns:
                if p in lower:
                    return intent

        return None


CONTINUATION_PATTERNS: list[re.Pattern] = [
    re.compile(p) for p in [
        r"^(go|ok|yes|y|sure|do it|proceed|continue|next|done|start|run)$",
        r"^(好|好的|继续|开始|可以|行|嗯|对|是的|没问题|来吧|冲|走|执行|开搞|干|上)$",
        r"^然后呢|然后|还有呢|还有|接着说|继续说说|继续吧|继续搞$",
        r"^接着|下一步|下个|下一个|下一|往下|然后呢\?*$",
        r"^again|once more|one more|more|another$",
        r"^再来|再一个|再一次|再试试|再搞一下|再整一个$",
    ]
]


def is_short_continuation(text: str) -> bool:
    """Check if text is a short continuation message (≤30 chars matching patterns)."""
    stripped = text.strip()
    if len(stripped) > 30:
        return False
    return any(p.search(stripped.lower()) for p in CONTINUATION_PATTERNS)
