"""Workflow learner — extract reusable execution patterns from completed conversations.

After the normal agent loop finishes, this module analyzes the conversation
history and creates a ``TaskWorkflow`` template that can be reused next time
a similar task comes in.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from daoyi.engine.messages import ConversationMessage, ToolResultBlock
from daoyi.task_workflow.classifier import TaskClassifier
from daoyi.task_workflow.models import TaskPhase, TaskWorkflow
from daoyi.task_workflow.registry import WorkflowRegistry

log = logging.getLogger(__name__)

# Phase prompt templates (will be formatted with {user_input}, {accumulated_context}, {phase_results})
_TEMPLATES: dict[str, str] = {
    "understand": (
        "你正在执行「理解」阶段。\n"
        "用户需求：{user_input}\n\n"
        "你的任务：\n"
        "1. 调用 read_file/glob/grep/bash 工具了解环境和现有代码\n"
        "2. 确认清楚需求的范围\n\n"
        "规则：必须通过调用工具获取信息。"
    ),
    "plan": (
        "你正在执行「规划」阶段。\n"
        "用户需求：{user_input}\n"
        "之前阶段完成：\n{phase_results}\n\n"
        "你的任务：列出清晰的实施步骤计划。调用 write_file 工具保存计划文件。\n\n"
        "规则：必须调用 write_file 工具写出计划，不要只输出文字。"
    ),
    "implement": (
        "你正在执行「实现」阶段。\n"
        "用户需求：{user_input}\n"
        "环境信息：\n{accumulated_context}\n\n"
        "你的任务：\n"
        "1. 调用 write_file 工具创建新文件\n"
        "2. 调用 edit_file 工具修改现有文件\n"
        "3. 调用 bash 运行语法检查\n\n"
        "规则：必须调用 write_file/edit_file 工具实际写入文件，只输出代码文字是不合格的。"
    ),
    "verify": (
        "你正在执行「验证」阶段。\n"
        "用户需求：{user_input}\n"
        "实现结果：\n{phase_results}\n\n"
        "你的任务：\n"
        "1. 调用 read_file 检查文件是否存在\n"
        "2. 调用 bash 运行代码或测试\n"
        "3. 如果文件不存在，调用 write_file 重新创建\n"
        "4. 确认输出符合预期\n\n"
        "规则：必须调用 bash 实际执行验证。"
    ),
    "execute": (
        "你正在执行「执行」阶段。\n"
        "用户需求：{user_input}\n\n"
        "你的任务：\n"
        "1. 调用 bash 工具执行用户要求的操作\n"
        "2. 展示并解读输出结果\n\n"
        "规则：必须调用 bash 工具来执行。"
    ),
    "research": (
        "你正在执行「研究」阶段。\n"
        "用户需求：{user_input}\n"
        "之前阶段完成：\n{phase_results}\n\n"
        "你的任务：\n"
        "1. 调用 web_fetch/web_search 搜索信息\n"
        "2. 调用 grep/read_file 查阅现有代码\n\n"
        "规则：调用工具获取信息后再输出结论。"
    ),
    "generate": (
        "你正在执行「生成」阶段。\n"
        "用户需求：{user_input}\n"
        "已收集信息：\n{accumulated_context}\n\n"
        "你的任务：\n"
        "1. 调用 write_file 工具生成目标文件\n"
        "2. 如果生成多个文件，逐个调用 write_file\n\n"
        "规则：必须调用 write_file 工具写文件。"
    ),
    "report": (
        "你正在执行「报告」阶段。\n"
        "用户需求：{user_input}\n"
        "完整结果：\n{accumulated_context}\n\n"
        "你的任务：\n"
        "1. 用 write_file 工具输出报告文件\n"
        "2. 清晰总结关键发现\n\n"
        "规则：必须调用 write_file 工具。"
    ),
}


def _trace_dir_path() -> Path:
    base = Path(os.environ.get("DAOYI_HOME", "~/.daoyi")).expanduser()
    path = base / "traces"
    path.mkdir(parents=True, exist_ok=True)
    return path


class WorkflowLearner:
    """Analyze completed agent sessions and extract workflow templates."""

    def __init__(
        self,
        registry: WorkflowRegistry,
        classifier: TaskClassifier,
    ) -> None:
        self._registry = registry
        self._classifier = classifier

    def learn_from_session(
        self,
        messages: list[ConversationMessage],
        *,
        user_input: str = "",
        model: str = "",
        duration_seconds: float = 0.0,
        tool_registry: Any = None,
    ) -> TaskWorkflow | None:
        """Analyze a completed conversation and create/update a workflow template.

        Returns the workflow if one was created, or *None* if the session
        was too short / too simple to learn from.
        """
        if len(messages) < 3:
            return None

        # 1. extract tool usage pattern
        tools_used: list[str] = []
        for msg in messages:
            if msg.role == "assistant":
                for block in msg.content:
                    if hasattr(block, "name") and block.name not in ("str",):
                        tools_used.append(block.name)

        tool_counts: dict[str, int] = {}
        for t in tools_used:
            tool_counts[t] = tool_counts.get(t, 0) + 1

        if not tool_counts:
            return None

        # 2. derive workflow id from first tool used + task type
        primary_tool = max(tool_counts, key=tool_counts.get)
        trigger_keywords = self._classifier.suggest_triggers(user_input)
        if not trigger_keywords:
            return None

        task_label = trigger_keywords[0][:20]
        wf_id = f"{task_label}_{primary_tool}"

        # 3. check if a workflow with this id already exists
        existing = self._registry.get(wf_id)
        if existing:
            existing.use_count += 1
            existing.tools_observed = list(
                set(existing.tools_observed + list(tool_counts.keys()))
            )
            existing.avg_duration_seconds = (
                existing.avg_duration_seconds * (existing.use_count - 1)
                + duration_seconds
            ) / existing.use_count
            self._registry.save(existing)
            return existing

        # 4. build phases from the conversation structure
        phases = self._segment_into_phases(messages, tool_counts, user_input)

        if not phases:
            # fallback: create a simple 2-phase workflow
            read_tools = {"read", "glob", "grep", "web_fetch", "web_search", "lsp"}
            write_tools = {"write", "edit", "bash", "notebook_edit"}

            if primary_tool in read_tools:
                phases = [
                    TaskPhase(
                        name="research",
                        prompt_template=_TEMPLATES.get("research", ""),
                        tools=list(read_tools),
                        max_turns=2,
                    ),
                    TaskPhase(
                        name="report",
                        prompt_template=_TEMPLATES.get("report", ""),
                        max_turns=2,
                    ),
                ]
            elif primary_tool in write_tools:
                phases = [
                    TaskPhase(
                        name="understand",
                        prompt_template=_TEMPLATES.get("understand", ""),
                        tools=list(read_tools),
                        max_turns=2,
                    ),
                    TaskPhase(
                        name="generate",
                        prompt_template=_TEMPLATES.get("generate", ""),
                        tools=list(write_tools),
                        max_turns=3,
                    ),
                    TaskPhase(
                        name="verify",
                        prompt_template=_TEMPLATES.get("verify", ""),
                        tools=["bash"],
                        max_turns=2,
                    ),
                ]
            else:
                phases = [
                    TaskPhase(
                        name="execute",
                        prompt_template=_TEMPLATES.get("execute", ""),
                        tools=list(tool_counts.keys()),
                        max_turns=3,
                    ),
                ]

        # 5. build trigger patterns from keywords
        trigger_patterns = [re.escape(kw) for kw in trigger_keywords[:3]]

        workflow = TaskWorkflow(
            id=wf_id,
            trigger_patterns=trigger_patterns,
            description=f"Auto-learned workflow for tasks involving {primary_tool}",
            phases=phases,
            source_model=model,
            tools_observed=list(tool_counts.keys()),
            avg_duration_seconds=duration_seconds,
            use_count=1,
        )

        self._registry.save(workflow)
        log.info(
            "learned workflow %s (%d phases, %d tools) from session",
            wf_id,
            len(phases),
            len(tool_counts),
        )
        return workflow

    # ── internal: segment conversation into phases ──────────────

    # ── trace learning ────────────────────────────────────────

    def learn_from_traces(self, trace_dir: str | None = None) -> int:
        """Analyze recorded trace files and learn workflow improvements.

        Scans all JSONL trace files, extracts tool usage patterns,
        failure signatures, and timing data. Updates existing workflows
        with the aggregated statistics.

        Returns the number of trace entries analyzed.
        """
        from pathlib import Path

        trace_root = Path(trace_dir) if trace_dir else _trace_dir_path()
        if not trace_root.exists():
            return 0

        trace_entries: list[dict] = []
        for fpath in sorted(trace_root.glob("*.jsonl")):
            try:
                with open(fpath) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            trace_entries.append(json.loads(line))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("skipping corrupt trace %s: %s", fpath.name, exc)

        if not trace_entries:
            return 0

        # Aggregate: tool usage frequency, failure rates, average duration
        tool_stats: dict[str, dict[str, float]] = {}
        workflow_insights: dict[str, list[str]] = {}

        for entry in trace_entries:
            req = entry.get("request", {})
            tools = [t.get("name", "") for t in req.get("tools", [])]
            events = entry.get("events", [])
            duration = entry.get("duration_s", 0.0)
            messages = req.get("messages", [])
            user_text = ""
            for m in reversed(messages):
                if isinstance(m, dict) and m.get("role") == "user":
                    content = m.get("content", "")
                    user_text = content if isinstance(content, str) else str(content)
                    break

            for t in tools:
                if t not in tool_stats:
                    tool_stats[t] = {"count": 0, "total_duration": 0.0, "failures": 0}
                tool_stats[t]["count"] += 1
                tool_stats[t]["total_duration"] += duration

            # Detect failure patterns
            failed_tools: set[str] = set()
            for ev in events:
                if isinstance(ev, dict) and ev.get("type") == "tool_result":
                    if ev.get("is_error"):
                        failed_tools.add(ev.get("name", ""))

            for t in failed_tools:
                if t in tool_stats:
                    tool_stats[t]["failures"] += 1

            # Match to existing workflows for insight
            matched = self._registry.find(user_text) if user_text else None
            if matched and failed_tools:
                key = f"{matched.id}:{','.join(sorted(failed_tools))}"
                if key not in workflow_insights:
                    workflow_insights[key] = []
                workflow_insights[key].append(user_text[:100])

        # Update registry with failure insights
        for key, examples in workflow_insights.items():
            wf_id, tools_str = key.split(":", 1)
            wf = self._registry.get(wf_id)
            if wf and len(examples) >= 2:
                log.info(
                    "trace insight: workflow=%s tools=[%s] failed %d times (e.g. %r)",
                    wf_id, tools_str, len(examples), examples[0][:60],
                )

        log.info(
            "trace analysis: %d entries, %d tool types, %d failure patterns",
            len(trace_entries), len(tool_stats), len(workflow_insights),
        )
        return len(trace_entries)

    @staticmethod
    def clean_old_sessions(days: int = 7) -> int:
        """Delete session snapshots and traces older than *days*.

        Returns number of files removed.
        """
        from pathlib import Path
        import time as _time

        deadline = _time.time() - days * 86400
        removed = 0

        # Clean trace files
        trace_root = _trace_dir_path()
        if trace_root.exists():
            for f in trace_root.glob("*.jsonl"):
                if f.stat().st_mtime < deadline:
                    f.unlink()
                    removed += 1

        # Clean replay cache
        cache_root = Path(
            os.environ.get("DAOYI_HOME", "~/.daoyi")
        ).expanduser() / "cache" / "replay"
        if cache_root.exists():
            for f in cache_root.glob("*.json"):
                if f.stat().st_mtime < deadline:
                    f.unlink()
                    removed += 1

        # Clean session snapshots
        sessions_root = Path(
            os.environ.get("DAOYI_HOME", "~/.daoyi")
        ).expanduser() / "sessions"
        if sessions_root.exists():
            for f in sessions_root.glob("**/*.json"):
                if f.stat().st_mtime < deadline:
                    f.unlink()
                    removed += 1

        if removed:
            log.info("cleaned %d old session/trace files (>%d days)", removed, days)
        return removed

    def _segment_into_phases(
        self,
        messages: list[ConversationMessage],
        tool_counts: dict[str, int],
        user_input: str,
    ) -> list[TaskPhase]:
        """Try to split the conversation into semantic phases based on tool usage."""
        if not messages:
            return []

        phases: list[TaskPhase] = []
        current_tools: set[str] = set()
        tool_sequence: list[str] = []
        phase_boundaries: list[int] = [0]

        for i, msg in enumerate(messages):
            if msg.role != "assistant":
                continue
            for block in msg.content:
                tname = getattr(block, "name", None)
                if tname and isinstance(tname, str) and tname != "str":
                    tool_sequence.append(tname)
                    current_tools.add(tname)

            # detect phase boundary: when tool use pattern changes significantly
            if tool_sequence and i > 0:
                prev_tools = set()
                for prev_msg in messages[max(0, i - 3) : i]:
                    if prev_msg.role != "assistant":
                        continue
                    for b in prev_msg.content:
                        tn = getattr(b, "name", None)
                        if tn and isinstance(tn, str) and tn != "str":
                            prev_tools.add(tn)

                # new phase if tools changed substantially
                if current_tools and prev_tools and not current_tools.issubset(prev_tools) and not prev_tools.issubset(current_tools):
                    phase_boundaries.append(i)
                    current_tools = set()

        if phase_boundaries[-1] != len(messages):
            phase_boundaries.append(len(messages))

        # build TaskPhase from each segment
        for seg_idx in range(len(phase_boundaries) - 1):
            start = phase_boundaries[seg_idx]
            end = phase_boundaries[seg_idx + 1]
            seg_messages = messages[start:end]

            seg_tools: set[str] = set()
            for msg in seg_messages:
                if msg.role != "assistant":
                    continue
                for block in msg.content:
                    tn = getattr(block, "name", None)
                    if tn and isinstance(tn, str) and tn != "str":
                        seg_tools.add(tn)

            phase_name = self._name_phase(seg_tools or set(tool_counts.keys()), seg_idx)
            template = _TEMPLATES.get(phase_name, _TEMPLATES.get("execute", ""))
            phases.append(
                TaskPhase(
                    name=phase_name,
                    prompt_template=template,
                    tools=list(seg_tools) if seg_tools else [],
                    max_turns=max(2, end - start),
                )
            )

        return phases

    @staticmethod
    def _name_phase(tools: set[str], index: int) -> str:
        """Infer a phase name from the set of tools used."""
        if {"web_fetch", "web_search"} & tools:
            return "research" if index == 0 else "summarize"
        if {"read_file", "glob", "grep"} & tools:
            if index == 0:
                return "understand"
            return "research"
        if {"write_file", "edit_file"} & tools:
            return "implement"
        if {"bash"} & tools:
            if index == 0:
                return "execute"
            return "verify"
        if index == 0:
            return "understand"
        return f"step_{index + 1}"


# ── ToolDiscoverer ─────────────────────────────────────────────────


class ToolDiscoverer:
    """Extract unknown tool names from user input and research them via web search.

    After the classifier finds no match, this module checks whether the
    user is asking about a tool/technology that isn't yet covered by a
    workflow template. If so, it web-searches for the tool, creates a
    minimal workflow, and returns it so the caller can execute it
    immediately.
    """

    # Patterns that indicate a tool name follows (capture group 1)
    _TOOL_PATTERNS: list[re.Pattern] = [
        # Chinese: 用/使用/试试/试一下/打开/启动/安装/了解一下/查一下/搜一下/查找 <tool>
        re.compile(r'(?:用|使用|试试|试一下|打开|启动|安装|了解一下|查一下|搜一下|查找)\s*([A-Za-z][A-Za-z0-9_.\-]*)'),
        # 搜索/查询 <ascii-tool> (only match ASCII tool names to avoid
        # false positives like "搜索日志文件" → "日志文件")
        re.compile(r'(?:搜索|查询)\s*([A-Za-z][A-Za-z0-9_.\-]*)'),
        # English: use/try/run/launch/open/install <tool>
        re.compile(r'(?:use|try|run|launch|open|install|about)\s+([A-Za-z][A-Za-z0-9_.\-]*)', re.IGNORECASE),
        # English: what is / how to use / look up <tool>
        re.compile(r'(?:what\s+is|whats|how\s+to\s+use|look\s+up)\s+([A-Za-z][A-Za-z0-9_.\-]*)', re.IGNORECASE),
        # Tool at the start of the line: "<tool> 是什么" or "<tool> 怎么用"
        re.compile(r'^([A-Za-z][A-Za-z0-9_.\-]*)\s+(?:是什么|怎么用|怎么安装|干什么的|干啥的)'),
        # Plain: last word after "帮我.*一下" or "帮我.*看看"
        re.compile(r'帮我\w*一下\s*([A-Za-z][A-Za-z0-9_.\-]*)$'),
        re.compile(r'帮我\w*看看\s*([A-Za-z][A-Za-z0-9_.\-]*)$'),
    ]

    # Chinese 2-char compounds that are never tool names
    _CN_STOP_COMPOUNDS: frozenset = frozenset({
        "这个文件", "那个文件", "这个程序", "那个程序", "这个工具", "那个工具",
        "这个命令", "那个命令", "这个代码", "那个代码", "这个软件", "那个软件",
        "当前目录", "工作目录", "该文件", "该目录", "该程序",
        "日志文件", "配置文件", "源文件", "目标文件", "输出文件",
        "输入文件", "临时文件", "备份文件", "缓存文件", "日志信息",
        "运行结果", "测试结果", "输出结果", "查询结果", "搜索结果",
        "网络连接", "数据库", "文件夹", "目录下",
    })

    # Well-known local tools that should pass through to the normal workflow
    # even if no existing workflow template matches them.
    _KNOWN_TOOLS: frozenset = frozenset({
        "cua-driver", "cuadriver",  # macOS app automation
        "brew", "port",              # package managers
        "node", "npm", "npx", "pnpm", "yarn", "bun",  # JS runtime/tools
        "python", "python3", "pip", "pip3", "uv",     # Python tools
        "go", "rust", "cargo",                        # Go/Rust tools
        "docker", "docker-compose", "podman",         # containers
        "git", "gh",                                  # VCS
        "curl", "wget", "httpie",                     # HTTP
        "jq", "yq",                                   # JSON/YAML processors
        "rg", "ripgrep", "ag", "fzf", "bat",          # modern CLI tools
        "tmux", "screen",                             # terminal multiplexers
        "vim", "nvim", "neovim", "emacs", "nano",     # editors
        "mysql", "psql", "sqlite3", "redis-cli",       # DB clients
        "ssh", "scp", "rsync",                         # remote tools
        "chrome", "google-chrome", "safari", "firefox", # browsers
    })

    _STOPWORDS: frozenset = frozenset({
        # Chinese common
        "这个", "那个", "什么", "怎么", "如何", "可以", "一个", "那个",
        "一下", "看看", "知道", "告诉", "请问", "你好", "谢谢", "感谢",
        "电脑", "文件", "系统", "程序", "软件", "工具", "命令", "代码",
        "问题", "结果", "信息", "内容", "数据", "情况", "时候", "地方",
        "里面", "上面", "下面", "这里", "那里", "哪里",
        # English common
        "the", "this", "that", "what", "how", "why", "when", "where",
        "which", "with", "from", "into", "over", "under", "about",
        "would", "could", "should", "will", "shall", "can", "may",
        "file", "code", "tool", "command", "program", "software", "app",
        "folder", "directory", "script", "data", "info", "help", "list",
        "open", "use", "run", "try", "new", "get", "set", "find",
    })

    def __init__(
        self,
        registry: WorkflowRegistry,
        classifier: TaskClassifier,
    ) -> None:
        self._registry = registry
        self._classifier = classifier

    async def discover(self, user_input: str) -> TaskWorkflow | None:
        """Detect unknown tools in *user_input*, web search, create workflow.

        Returns the created workflow, or *None* if no tool was detected
        or the tool was already covered by an existing workflow.
        """
        candidates = self._extract_candidates(user_input)
        if not candidates:
            return None

        for name in candidates:
            # Already have a workflow that matches?
            if self._is_tool_covered(name):
                continue

            # Skip well-known local tools — they should be handled
            # by the normal agent loop, not discovered as new tools.
            if name in self._KNOWN_TOOLS or name.replace('-', '') in self._KNOWN_TOOLS:
                continue

            # Web search for the tool
            info = await self._search_tool(name)
            if info is None:
                continue

            # Build and save workflow
            wf = self._build_workflow(name, info)
            if wf is not None:
                self._registry.save(wf)
                log.info("discovered tool=%s → workflow=%s from web search", name, wf.id)
                return wf

        return None

    # ── candidate extraction ──────────────────────────────────────

    def _extract_candidates(self, text: str) -> list[str]:
        """Extract unique candidate tool names from user input."""
        seen: set[str] = set()
        candidates: list[str] = []

        for pat in self._TOOL_PATTERNS:
            for m in pat.finditer(text):
                name = m.group(1).strip().lower()
                if len(name) < 2 or len(name) > 40:
                    continue
                if not re.match(r'^[a-zA-Z][a-zA-Z0-9_.\-]*$', name) and not re.match(r'^[\u4e00-\u9fff]{2,5}$', name):
                    continue
                if name in self._STOPWORDS:
                    continue
                if name in self._CN_STOP_COMPOUNDS:
                    continue
                if name not in seen:
                    seen.add(name)
                    candidates.append(name)

        return candidates

    def _is_tool_covered(self, name: str) -> bool:
        """Check if any existing workflow already covers this tool."""
        # Exact match
        if self._check_coverage(name):
            return True
        # Try common variations: remove hyphens/underscores/dots
        normalized = re.sub(r'[-_.]', '', name)
        if normalized != name and self._check_coverage(normalized):
            return True
        # Try adding hyphens (e.g. "cuadriver" → "cua-driver")
        # Only for names that look like compound words
        if len(name) > 5:
            for i in range(2, len(name) - 1):
                variant = name[:i] + '-' + name[i:]
                if self._check_coverage(variant):
                    return True
        return False

    def _check_coverage(self, name: str) -> bool:
        tool_id = f"tool_{name}"
        if self._registry.get(tool_id):
            return True
        wf = self._registry.find(name)
        return wf is not None

    # ── web search ────────────────────────────────────────────────

    async def _search_tool(self, name: str) -> dict | None:
        """Search the web for information about a tool.

        Tries multiple sources in order:
          1. Wikipedia REST API (structured, reliable for well-known tools)
          2. GitHub search API (great for open-source dev tools)
          3. DuckDuckGo HTML search (general fallback)

        Returns a dict with keys: ``summary``, ``category``, ``url``,
        or *None* if all sources returned nothing useful.
        """
        # 1. Wikipedia exact match
        info = await self._wikipedia_lookup(name)
        if info:
            return info

        # 2. GitHub search (good for developer tools without Wikipedia pages)
        info = await self._github_search(name)
        if info:
            return info

        # 3. Fallback: DuckDuckGo then Bing
        snippets = await self._duckduckgo_search(f"{name} tool command usage")
        if not snippets:
            snippets = await self._duckduckgo_search(f"{name} usage guide")
        if not snippets:
            snippets = await self._bing_search(f"{name} tool")
        if not snippets:
            return None

        combined = " ".join(snippets)
        summary = combined[:300].rsplit(" ", 1)[0] if len(combined) > 300 else combined
        category = self._infer_category(combined, name)

        return {"summary": summary, "category": category, "url": snippets[0] if snippets else ""}

    async def _wikipedia_lookup(self, name: str) -> dict | None:
        """Look up a tool on Wikipedia and return a summary.

        Skips disambiguation pages and non-tool articles (e.g. common
        English words that happen to be tool names).
        """
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                resp = await client.get(
                    f"https://en.wikipedia.org/api/rest_v1/page/summary/{name}",
                    headers={"User-Agent": "OpenHarness/0.1"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    page_type = data.get("type", "")
                    # Skip disambiguation pages
                    if "disambiguation" in page_type:
                        return None
                    summary = (data.get("extract") or data.get("description") or "")[:300]
                    if not summary:
                        return None
                    # Skip if the summary doesn't sound tool-related (avoids
                    # common-word name collisions like "bat" → winged mammal)
                    if not self._is_tool_article(summary, name):
                        return None
                    return {
                        "summary": re.sub(r'\s+', ' ', summary).strip(),
                        "category": self._infer_category(summary, name),
                        "url": data.get("content_urls", {}).get("desktop", {}).get("page", "")
                               or f"https://en.wikipedia.org/wiki/{name}",
                    }
        except (httpx.HTTPError, Exception) as exc:
            log.debug("Wikipedia lookup failed for %r: %s", name, exc)

        return None

    @staticmethod
    def _is_tool_article(extract: str, name: str) -> bool:
        """Check if a Wikipedia extract describes a tool, not a generic topic."""
        extract_lower = extract.lower()
        tool_signals = [
            "software", "tool", "library", "framework", "command",
            "program", "application", "utility", "package", "module",
            "language", "compiler", "interpreter", "runtime",
            "server", "client", "protocol", "format", "codec",
            "algorithm", "implementation",
            "computing", "programming",
            "open source", "free and open",
        ]
        # If the name appears with a clear software context
        if any(sig in extract_lower for sig in tool_signals):
            return True
        # If the name itself is in the extract (not a redirect to a general topic)
        if name.lower() in extract_lower and len(name) > 3:
            return True
        # Short names (≤3 chars) need stronger signals
        if len(name) <= 4:
            return any(sig in extract_lower for sig in tool_signals)
        return False

    async def _github_search(self, name: str) -> dict | None:
        """Search for the tool on GitHub and return the top repository description."""
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                resp = await client.get(
                    "https://api.github.com/search/repositories",
                    params={"q": name, "per_page": 5, "sort": "stars"},
                    headers={
                        "Accept": "application/vnd.github.v3+json",
                        "User-Agent": "OpenHarness/0.1",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("items", [])
                    if not items:
                        return None
                    # Prefer exact-name repo (owner/repo where repo == name)
                    name_lower = name.lower()
                    best = items[0]
                    for item in items:
                        repo_name = item["name"].lower()
                        full_name_lower = item["full_name"].lower()
                        if repo_name == name_lower:
                            best = item
                            break
                        # Also check "tool-name" vs "toolname" normalization
                        if repo_name.replace("-", "").replace("_", "") == name_lower.replace("-", "").replace("_", ""):
                            best = item
                            break
                    desc = best.get("description") or ""
                    summary = f"{best['full_name']}: {desc}"[:300]
                    if desc:
                        return {
                            "summary": summary,
                            "category": self._infer_category(
                                (desc or "") + " " + (best.get("language") or ""),
                                name,
                            ),
                            "url": best.get("html_url", f"https://github.com/{best['full_name']}"),
                        }
        except (httpx.HTTPError, Exception) as exc:
            log.debug("GitHub search failed for %r: %s", name, exc)

        return None

    async def _duckduckgo_search(self, query: str) -> list[str]:
        """Run a DuckDuckGo HTML search and return result snippets."""
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        headers = {"User-Agent": ua, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"}
        try:
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=15.0, trust_env=False,
            ) as client:
                # Warmup: visit home page to get session cookies (avoids CAPTCHA)
                await client.get("https://html.duckduckgo.com/", headers=headers)
                response = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                    headers=headers,
                )
                if response.status_code != 200:
                    return []
        except (httpx.HTTPError, Exception) as exc:
            log.debug("DuckDuckGo search failed for %r: %s", query, exc)
            return []

        return self._parse_ddg_snippets(response.text, limit=5)

    @staticmethod
    def _parse_ddg_snippets(body: str, limit: int) -> list[str]:
        """Extract result snippets from DuckDuckGo HTML response."""
        snippets: list[str] = []
        for m in re.finditer(
            r'<(?:a|div|span)[^>]+class="[^"]*(?:result__snippet|result-snippet)[^"]*"[^>]*>(.*?)</(?:a|div|span)>',
            body,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            text = re.sub(r"(?s)<[^>]+>", " ", m.group(1))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                snippets.append(text)
            if len(snippets) >= limit:
                break

        # If no snippet, try extracting from result body divs
        if not snippets:
            for m in re.finditer(
                r'class="result__body"[^>]*>.*?<a[^>]+class="[^"]*result__a[^"]*"[^>]*>.*?</a>(.*?)</div>',
                body,
                flags=re.IGNORECASE | re.DOTALL,
            ):
                text = re.sub(r"(?s)<[^>]+>", " ", m.group(1))
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    snippets.append(text)
                if len(snippets) >= limit:
                    break

        return snippets

    async def _bing_search(self, query: str) -> list[str]:
        """Run a Bing search and return result snippets."""
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        headers = {"User-Agent": ua, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"}
        try:
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=15.0, trust_env=False,
            ) as client:
                await client.get("https://www.bing.com/", headers=headers)
                response = await client.get(
                    "https://www.bing.com/search",
                    params={"q": query},
                    headers=headers,
                )
                if response.status_code != 200:
                    return []
        except (httpx.HTTPError, Exception) as exc:
            log.debug("Bing search failed for %r: %s", query, exc)
            return []

        return self._parse_bing_snippets(response.text, limit=5)

    @staticmethod
    def _parse_bing_snippets(body: str, limit: int) -> list[str]:
        """Extract result snippets from Bing HTML search results."""
        snippets: list[str] = []
        for m in re.finditer(
            r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
            body,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            item = m.group(1)
            # Get snippet from <p> inside b_caption
            p_m = re.search(
                r'<div[^>]*class="[^"]*b_caption[^"]*"[^>]*>.*?<p[^>]*>(.*?)</p>',
                item,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if p_m:
                text = re.sub(r"(?s)<[^>]+>", " ", p_m.group(1))
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    snippets.append(text)
                    if len(snippets) >= limit:
                        break
        return snippets

    @staticmethod
    def _infer_category(combined: str, name: str) -> str:
        """Infer the tool's category from search result text."""
        combined_lower = combined.lower()
        # Use word-boundary patterns to avoid false positives ("spec" in "respecting")
        def has_word(word: str) -> bool:
            return bool(re.search(rf'\b{re.escape(word)}\b', combined_lower))

        if any(has_word(w) for w in ("library", "framework", "sdk", "api", "package")):
            return "library"
        if any(has_word(w) for w in ("web", "website", "browser", "http", "api")):
            return "web"
        if any(has_word(w) for w in ("cli", "command", "terminal", "shell", "bash")):
            return "cli"
        if any(has_word(w) for w in ("database", "db", "sql", "nosql", "query")):
            return "database"
        if any(has_word(w) for w in ("test", "testing", "assertion", "spec")):
            return "test"
        if any(has_word(w) for w in ("docker", "container", "kubernetes", "k8s")):
            return "container"
        if any(has_word(w) for w in ("image", "picture", "photo", "video", "audio", "media")):
            return "media"
        return "cli"  # default: most tools are CLI

    # ── workflow generation ───────────────────────────────────────

    def _build_workflow(self, name: str, info: dict) -> TaskWorkflow | None:
        """Create a TaskWorkflow for a tool discovered via web search.

        The workflow has one or two phases depending on the category:
          - CLI tools: single "execute" phase
          - Libraries: "learn" + "implement" phases
          - Web tools: "research" + "execute" phases
        """
        category = info.get("category", "cli")
        summary = info.get("summary", "")
        wf_id = f"tool_{name}"

        trigger_patterns = self._generate_trigger_patterns(name)

        phases = self._phases_for_category(category, name, summary)

        return TaskWorkflow(
            id=wf_id,
            trigger_patterns=trigger_patterns,
            description=f"Web-discovered workflow for {name}: {summary[:120]}",
            phases=phases,
            tools_observed=[name],
            use_count=0,
        )

    @staticmethod
    def _generate_trigger_patterns(name: str) -> list[str]:
        """Generate regex trigger patterns for the tool name."""
        escaped = re.escape(name)
        patterns = [escaped]
        # ^<tool> at start (e.g. "^ffmpeg")
        patterns.append(f"^{escaped}")
        # Chinese: <tool>  command (e.g. "ffmpeg 转换")
        patterns.append(f"{escaped}\\s+[\\w\\u4e00-\\u9fff]")
        # 用/使用 <tool>
        patterns.append(f"(?:用|使用|试试|试一下|安装){escaped}")
        # use/try/run <tool>
        patterns.append(f"(?:use|try|run|install)\\s+{escaped}")
        # what is <tool>
        patterns.append(f"(?:what\\s+is|how\\s+to\\s+use)\\s+{escaped}")
        return patterns

    def _phases_for_category(self, category: str, name: str, summary: str) -> list[TaskPhase]:
        """Create appropriate phase(s) based on tool category."""
        if category == "library":
            return [
                TaskPhase(
                    name="research",
                    prompt_template=(
                        f"你正在执行「了解 {name}」阶段。\n"
                        f"用户需求：{{user_input}}\n\n"
                        f"你的任务：\n"
                        f"1. 调用 web_search 搜索 {name} 的文档和 API\n"
                        f"2. 调用 web_fetch 读取关键页面\n"
                        f"3. 调用 read_file 检查项目中是否已引用此库\n\n"
                        f"工具简介：{summary[:200]}\n\n"
                        f"规则：先了解库的用法再进入实现阶段。"
                    ),
                    tools=["web_search", "web_fetch", "read_file", "grep"],
                    max_turns=2,
                ),
                TaskPhase(
                    name="implement",
                    prompt_template=(
                        f"你正在执行「使用 {name} 实现」阶段。\n"
                        f"用户需求：{{user_input}}\n"
                        f"已了解的信息：{{accumulated_context}}\n\n"
                        f"你的任务：\n"
                        f"1. 调用 write_file 工具编写使用 {name} 的代码\n"
                        f"2. 调用 bash 安装依赖（若需要）\n"
                        f"3. 调用 bash 运行并验证代码\n\n"
                        f"规则：必须调用工具实际编写和执行代码。"
                    ),
                    tools=["write_file", "edit_file", "bash", "read_file"],
                    max_turns=3,
                ),
            ]

        if category == "web":
            return [
                TaskPhase(
                    name="research",
                    prompt_template=(
                        f"你正在执行「研究 {name}」阶段。\n"
                        f"用户需求：{{user_input}}\n\n"
                        f"你的任务：\n"
                        f"1. 调用 web_search 搜索 {name} 的相关信息\n"
                        f"2. 调用 web_fetch 读取关键页面\n"
                        f"3. 整理关键信息供下一阶段使用\n\n"
                        f"工具简介：{summary[:200]}\n\n"
                        f"规则：先搜索信息再进入下一步。"
                    ),
                    tools=["web_search", "web_fetch"],
                    max_turns=2,
                ),
                TaskPhase(
                    name="execute",
                    prompt_template=(
                        f"你正在执行「操作 {name}」阶段。\n"
                        f"用户需求：{{user_input}}\n"
                        f"搜索结果：{{accumulated_context}}\n\n"
                        f"你的任务：\n"
                        f"1. 调用 bash 或 write_file 执行用户要求\n"
                        f"2. 展示结果\n\n"
                        f"规则：必须调用工具来执行。"
                    ),
                    tools=["bash", "write_file", "read_file", "web_fetch"],
                    max_turns=2,
                ),
            ]

        if category == "database":
            return [
                TaskPhase(
                    name="detect",
                    prompt_template=(
                        f"你正在执行「检查 {name} 环境」阶段。\n"
                        f"用户需求：{{user_input}}\n\n"
                        f"你的任务：\n"
                        f"1. 调用 bash 检查 {name} 是否已安装\n"
                        f"2. 调用 web_search 搜索连接配置\n"
                        f"3. 调用 read_file 检查项目已有配置\n\n"
                        f"规则：先确认环境和配置。"
                    ),
                    tools=["bash", "web_search", "read_file", "glob"],
                    max_turns=2,
                ),
                TaskPhase(
                    name="execute",
                    prompt_template=(
                        f"你正在执行「执行 {name} 操作」阶段。\n"
                        f"用户需求：{{user_input}}\n"
                        f"环境信息：{{accumulated_context}}\n\n"
                        f"你的任务：\n"
                        f"1. 调用 bash 执行 {name} 命令\n"
                        f"2. 调用 write_file 若需要保存 SQL/配置脚本\n\n"
                        f"规则：必须调用 bash 操作数据库。"
                    ),
                    tools=["bash", "write_file", "read_file"],
                    max_turns=2,
                ),
            ]

        # default: CLI tool → single execute phase
        return [
            TaskPhase(
                name="execute",
                prompt_template=(
                    f"你正在执行「使用 {name}」阶段。\n"
                    f"用户需求：{{user_input}}\n\n"
                    f"当前目录：{{cwd}}\n\n"
                    f"执行流程：\n"
                    f"  1. 确认 {name} 已安装，若未安装则安装\n"
                    f"  2. 探索当前目录，找到用户需求涉及的目标文件/目录\n"
                    f"  3. **用 {name} 对目标文件执行实际的操作** — 这是核心\n"
                    f"  4. 展示执行结果\n\n"
                    f"注意：\"搜索日志文件\"的意思是\"在日志文件内容中搜索\"，不是\"找到日志文件\"。"
                    f"找到目标文件后必须用 {name} 处理它们。"
                ),
                tools=["bash", "web_search", "web_fetch", "read_file", "glob"],
                max_turns=4,
            ),
        ]
