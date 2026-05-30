"""Input normalization layer — convert natural language to structured format."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NormalizedInput:
    """Structured representation of a user's natural language request."""

    raw: str
    task_type: str = ""
    target_language: Optional[str] = None
    target_framework: Optional[str] = None
    goal: str = ""
    requirements: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    context_files: list[str] = field(default_factory=list)
    output_format: Optional[str] = None

    def enrich_prompt(self, original_prompt: str) -> str:
        """Append structured metadata to a phase prompt."""
        parts = [original_prompt]

        if self.target_language:
            parts.append(f"\n语言：{self.target_language}")
        if self.target_framework:
            parts.append(f"框架：{self.target_framework}")
        if self.requirements:
            parts.append("\n需求：")
            for i, req in enumerate(self.requirements, 1):
                parts.append(f"  {i}. {req}")
        if self.constraints:
            parts.append("\n约束：")
            for i, c in enumerate(self.constraints, 1):
                parts.append(f"  {i}. {c}")
        if self.context_files:
            parts.append("\n相关文件：")
            for f in self.context_files:
                parts.append(f"  - {f}")

        return "\n".join(parts)


# ── Extraction helpers ────────────────────────────────────────────

_LANG_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bpython\b', re.IGNORECASE), "python"),
    (re.compile(r'\bjavascript\b', re.IGNORECASE), "javascript"),
    (re.compile(r'\btypescript\b', re.IGNORECASE), "typescript"),
    (re.compile(r'\bjava\b', re.IGNORECASE), "java"),
    (re.compile(r'\brust\b', re.IGNORECASE), "rust"),
    (re.compile(r'\bgo\b', re.IGNORECASE), "go"),
    (re.compile(r'\bruby\b', re.IGNORECASE), "ruby"),
    (re.compile(r'\brust\b', re.IGNORECASE), "rust"),
    (re.compile(r'\bcpp\b|\bc\+\+\b', re.IGNORECASE), "cpp"),
    (re.compile(r'\bc#\b|\bcsharp\b', re.IGNORECASE), "csharp"),
    (re.compile(r'\bswift\b', re.IGNORECASE), "swift"),
    (re.compile(r'\bkotlin\b', re.IGNORECASE), "kotlin"),
    (re.compile(r'\bshell\b|\bbash\b', re.IGNORECASE), "bash"),
    (re.compile(r'\bsql\b', re.IGNORECASE), "sql"),
]

_FRAMEWORK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bdjango\b', re.IGNORECASE), "django"),
    (re.compile(r'\bflask\b', re.IGNORECASE), "flask"),
    (re.compile(r'\bfastapi\b', re.IGNORECASE), "fastapi"),
    (re.compile(r'\breact\b', re.IGNORECASE), "react"),
    (re.compile(r'\bvue\b', re.IGNORECASE), "vue"),
    (re.compile(r'\bangular\b', re.IGNORECASE), "angular"),
    (re.compile(r'\bnext\.?js\b', re.IGNORECASE), "nextjs"),
    (re.compile(r'\bexpress\b', re.IGNORECASE), "express"),
    (re.compile(r'\btorch\b|\bpytorch\b', re.IGNORECASE), "pytorch"),
    (re.compile(r'\btensorflow\b|\btf\b', re.IGNORECASE), "tensorflow"),
    (re.compile(r'\bspring\b', re.IGNORECASE), "spring"),
]

_OUTPUT_FORMAT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bmarkdown\b|\bmd\b', re.IGNORECASE), "markdown"),
    (re.compile(r'\bjson\b', re.IGNORECASE), "json"),
    (re.compile(r'\byaml\b', re.IGNORECASE), "yaml"),
    (re.compile(r'\bhtml\b', re.IGNORECASE), "html"),
    (re.compile(r'\bcsv\b', re.IGNORECASE), "csv"),
]


def extract_language(text: str) -> Optional[str]:
    for pat, lang in _LANG_PATTERNS:
        if pat.search(text):
            return lang
    return None


def extract_framework(text: str) -> Optional[str]:
    for pat, fw in _FRAMEWORK_PATTERNS:
        if pat.search(text):
            return fw
    return None


def extract_output_format(text: str) -> Optional[str]:
    for pat, fmt in _OUTPUT_FORMAT_PATTERNS:
        if pat.search(text):
            return fmt
    return None


def extract_requirements(text: str) -> list[str]:
    """Extract bullet/numbered requirements from text."""
    results: list[str] = []
    for m in re.finditer(r'(?:^|\n)\s*[-*\d]+\.?\s+(.+?)(?=\n\s*[-*\d]+\.?\s+|\Z)', text, re.DOTALL):
        req = m.group(1).strip()
        if req and len(req) > 3:
            results.append(req)
    if not results:
        cn_matches = re.findall(r'(?:要求|需要|支持|实现)([^，。\n]{4,40})', text)
        results = [m.strip() for m in cn_matches if m.strip()]
    return results


def extract_constraints(text: str) -> list[str]:
    results: list[str] = []
    for m in re.finditer(r'(?:使用|用|采用|基于)\s+(.{2,30}?)(?:[，。；]|$)', text):
        c = m.group(1).strip()
        if c and len(c) > 2 and c not in results:
            results.append(f"使用 {c}")
    return results


def extract_files(text: str) -> list[str]:
    files: list[str] = []
    for m in re.finditer(r'(?:/[\w.\-]+)+', text):
        files.append(m.group(0))
    return files[:10]


def normalize(text: str) -> NormalizedInput:
    """Convert natural language input into a structured NormalizedInput."""
    return NormalizedInput(
        raw=text,
        task_type=_infer_task_type(text),
        target_language=extract_language(text),
        target_framework=extract_framework(text),
        goal=text[:200],
        requirements=extract_requirements(text),
        constraints=extract_constraints(text),
        context_files=extract_files(text),
        output_format=extract_output_format(text),
    )


def _infer_task_type(text: str) -> str:
    text_lower = text.lower()
    if re.search(r'(bug|fix|修复|报错|错误|debug|issue)', text_lower):
        return "debug_fix"
    if re.search(r'(write|create|新建|创建|实现|implement|写)', text_lower):
        return "write_code"
    if re.search(r'(refactor|重构|modify|修改|重写)', text_lower):
        return "refactor_code"
    if re.search(r'(search|find|搜索|查找|查|找|grep|ripgrep)', text_lower):
        return "file_search"
    if re.search(r'(review|审查|审阅|code review)', text_lower):
        return "code_review"
    if re.search(r'(test|测试|pytest|unittest)', text_lower):
        return "run_tests"
    if re.search(r'(install|安装|pip|npm)', text_lower):
        return "install_deps"
    if re.search(r'(docker|container|镜像)', text_lower):
        return "docker_ops"
    if re.search(r'(数据库|mysql|postgres|sqlite|redis)', text_lower):
        return "db_ops"
    if re.search(r'(research|调研|调查|report|报告|搜索|查一下)', text_lower):
        return "web_research"
    return ""
