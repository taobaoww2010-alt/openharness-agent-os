"""Syscall table — 系统调用风格的工具有权限护。

工具调用标准化为 syscall 编号，按 capability 分组。
Phase 白名单从 tool name 列表改为 capability 集合。
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Any

from daoyi.engine.messages import ToolResultBlock
from daoyi.tools.base import ToolRegistry

log = logging.getLogger(__name__)

# ── C++ 核心桥接 ──

try:
    import _daoyi as _CPP
    _HAS_CPP = True
except ImportError:
    _CPP = None
    _HAS_CPP = False

# C++ 原生实现的工具（可绕过 Python 直接执行）
_CPP_NATIVE_TOOLS = {"read_file", "write_file", "edit_file", "bash", "glob", "grep"}


# ── Syscall Numbers ───────────────────────────────────────────────


class Syscall(enum.IntEnum):
    """系统调用编号。

    类别分区:
      00-09: 文件系统
      10-19: 进程 / Agent
      20-29: 网络
      30-39: GPU / 推理
      40-49: 元操作
      50+: 外部 / MCP
    """

    # ── 文件系统 (00-09) ──
    SYS_READ = 0
    SYS_WRITE = 1
    SYS_EDIT = 2
    SYS_GLOB = 3
    SYS_GREP = 4
    SYS_BASH = 5

    # ── 进程 / Agent (10-19) ──
    SYS_SEND_MESSAGE = 10
    SYS_ASK_USER = 11
    SYS_TASK_CREATE = 12
    SYS_TASK_GET = 13
    SYS_TASK_LIST = 14
    SYS_TASK_UPDATE = 15
    SYS_TASK_STOP = 16
    SYS_TASK_OUTPUT = 17
    SYS_AGENT = 18
    SYS_SLEEP = 19

    # ── 网络 (20-29) ──
    SYS_WEB_FETCH = 20
    SYS_WEB_SEARCH = 21
    SYS_TOOL_SEARCH = 22

    # ── 元操作 (40-49) ──
    SYS_SKILL = 40
    SYS_CONFIG = 41
    SYS_BRIEF = 42
    SYS_TODO_WRITE = 43
    SYS_EXIT_PLAN = 44
    SYS_ENTER_PLAN = 45
    SYS_EXIT_WORKTREE = 46
    SYS_ENTER_WORKTREE = 47

    # ── 外部 / MCP (50+) ──
    SYS_MCP = 50
    SYS_MCP_AUTH = 51
    SYS_READ_MCP_RESOURCE = 52
    SYS_LIST_MCP_RESOURCES = 53

    # ── Cron (60+) ──
    SYS_CRON_CREATE = 60
    SYS_CRON_LIST = 61
    SYS_CRON_TOGGLE = 62
    SYS_CRON_DELETE = 63

    # ── 图片 / 多媒体 (70+) ──
    SYS_IMAGE_GEN = 70
    SYS_IMAGE_TO_TEXT = 71
    SYS_NOTEBOOK_EDIT = 72
    SYS_REMOTE_TRIGGER = 73

    # ── LSP (80+) ──
    SYS_LSP = 80

    # ── 团队 (90+) ──
    SYS_TEAM_CREATE = 90
    SYS_TEAM_DELETE = 91


# ── Capability Groups ─────────────────────────────────────────────

# 与 workflow phase 一一对应的 capability

CAP_FILE_READ = {Syscall.SYS_READ, Syscall.SYS_GLOB, Syscall.SYS_GREP}
CAP_FILE_WRITE = {Syscall.SYS_WRITE, Syscall.SYS_EDIT}
CAP_FILE_ALL = CAP_FILE_READ | CAP_FILE_WRITE
CAP_SHELL = {Syscall.SYS_BASH}
CAP_NET = {Syscall.SYS_WEB_FETCH, Syscall.SYS_WEB_SEARCH, Syscall.SYS_TOOL_SEARCH}
CAP_AGENT = {
    Syscall.SYS_SEND_MESSAGE, Syscall.SYS_ASK_USER,
    Syscall.SYS_TASK_CREATE, Syscall.SYS_TASK_GET,
    Syscall.SYS_TASK_LIST, Syscall.SYS_TASK_UPDATE,
    Syscall.SYS_TASK_STOP, Syscall.SYS_TASK_OUTPUT,
    Syscall.SYS_AGENT, Syscall.SYS_SLEEP,
}
CAP_META = {
    Syscall.SYS_SKILL, Syscall.SYS_CONFIG, Syscall.SYS_BRIEF,
    Syscall.SYS_TODO_WRITE,
    Syscall.SYS_EXIT_PLAN, Syscall.SYS_ENTER_PLAN,
    Syscall.SYS_EXIT_WORKTREE, Syscall.SYS_ENTER_WORKTREE,
}
CAP_MCP = {Syscall.SYS_MCP, Syscall.SYS_MCP_AUTH,
           Syscall.SYS_READ_MCP_RESOURCE, Syscall.SYS_LIST_MCP_RESOURCES}
CAP_CRON = {Syscall.SYS_CRON_CREATE, Syscall.SYS_CRON_LIST,
            Syscall.SYS_CRON_TOGGLE, Syscall.SYS_CRON_DELETE}
CAP_IMAGE = {Syscall.SYS_IMAGE_GEN, Syscall.SYS_IMAGE_TO_TEXT,
             Syscall.SYS_NOTEBOOK_EDIT, Syscall.SYS_REMOTE_TRIGGER}
CAP_LSP = {Syscall.SYS_LSP}
CAP_TEAM = {Syscall.SYS_TEAM_CREATE, Syscall.SYS_TEAM_DELETE}

# 全部权限
CAP_ALL = (
    CAP_FILE_ALL | CAP_SHELL | CAP_NET | CAP_AGENT | CAP_META
    | CAP_MCP | CAP_CRON | CAP_IMAGE | CAP_LSP | CAP_TEAM
)

# Phase 名称 → 默认 capability 映射
PHASE_CAP_MAP: dict[str, set[Syscall]] = {
    # 理解 / 分析阶段 — 只读
    "understand": CAP_FILE_READ | CAP_SHELL,
    "analyze": CAP_FILE_READ | CAP_SHELL,
    "investigate": CAP_FILE_READ | CAP_SHELL,
    "search": CAP_FILE_READ | CAP_SHELL,
    "research": CAP_FILE_READ | CAP_NET,
    "discover": CAP_FILE_READ | CAP_SHELL,
    "detect": CAP_FILE_READ | CAP_SHELL,
    # 执行阶段 — 读写 + shell
    "implement": CAP_FILE_ALL | CAP_SHELL,
    "modify": CAP_FILE_ALL | CAP_SHELL,
    "fix": CAP_FILE_ALL | CAP_SHELL,
    "execute": CAP_SHELL | CAP_FILE_READ,
    "run": CAP_SHELL | CAP_FILE_READ,
    "process": CAP_FILE_ALL | CAP_SHELL,
    "install": CAP_SHELL | CAP_FILE_READ,
    "operate": CAP_SHELL | CAP_FILE_ALL,
    # 验证 / 报告
    "verify": CAP_FILE_READ | CAP_SHELL,
    "report": CAP_FILE_READ,
    "check": CAP_FILE_READ | CAP_SHELL,
    "confirm": CAP_FILE_READ | CAP_SHELL,
    "summarize": CAP_FILE_READ,
    # 其它
    "generate": CAP_FILE_ALL | CAP_SHELL | CAP_IMAGE,
    "check_status": CAP_FILE_READ | CAP_SHELL,
}


# ── Tool → Syscall 映射 ─────────────────────────────────────────

_TOOL_SYSCALL_MAP: dict[str, Syscall] = {
    # 文件
    "read_file": Syscall.SYS_READ,
    "write_file": Syscall.SYS_WRITE,
    "edit_file": Syscall.SYS_EDIT,
    "glob": Syscall.SYS_GLOB,
    "grep": Syscall.SYS_GREP,
    # shell
    "bash": Syscall.SYS_BASH,
    # 网络
    "web_fetch": Syscall.SYS_WEB_FETCH,
    "web_search": Syscall.SYS_WEB_SEARCH,
    "tool_search": Syscall.SYS_TOOL_SEARCH,
    # 进程 / Agent
    "send_message": Syscall.SYS_SEND_MESSAGE,
    "ask_user_question": Syscall.SYS_ASK_USER,
    "task_create": Syscall.SYS_TASK_CREATE,
    "task_get": Syscall.SYS_TASK_GET,
    "task_list": Syscall.SYS_TASK_LIST,
    "task_update": Syscall.SYS_TASK_UPDATE,
    "task_stop": Syscall.SYS_TASK_STOP,
    "task_output": Syscall.SYS_TASK_OUTPUT,
    "agent": Syscall.SYS_AGENT,
    "sleep": Syscall.SYS_SLEEP,
    # 元
    "skill": Syscall.SYS_SKILL,
    "config": Syscall.SYS_CONFIG,
    "brief": Syscall.SYS_BRIEF,
    "todo_write": Syscall.SYS_TODO_WRITE,
    "exit_plan_mode": Syscall.SYS_EXIT_PLAN,
    "enter_plan_mode": Syscall.SYS_ENTER_PLAN,
    "exit_worktree": Syscall.SYS_EXIT_WORKTREE,
    "enter_worktree": Syscall.SYS_ENTER_WORKTREE,
    # MCP
    "mcp": Syscall.SYS_MCP,
    "mcp_auth": Syscall.SYS_MCP_AUTH,
    "read_mcp_resource": Syscall.SYS_READ_MCP_RESOURCE,
    "list_mcp_resources": Syscall.SYS_LIST_MCP_RESOURCES,
    # Cron
    "cron_create": Syscall.SYS_CRON_CREATE,
    "cron_list": Syscall.SYS_CRON_LIST,
    "cron_toggle": Syscall.SYS_CRON_TOGGLE,
    "cron_delete": Syscall.SYS_CRON_DELETE,
    # 图片 / 多媒体
    "image_generation": Syscall.SYS_IMAGE_GEN,
    "image_to_text": Syscall.SYS_IMAGE_TO_TEXT,
    "notebook_edit": Syscall.SYS_NOTEBOOK_EDIT,
    "remote_trigger": Syscall.SYS_REMOTE_TRIGGER,
    # LSP
    "lsp": Syscall.SYS_LSP,
    # 团队
    "team_create": Syscall.SYS_TEAM_CREATE,
    "team_delete": Syscall.SYS_TEAM_DELETE,
}

# ── SyscallTable ──────────────────────────────────────────────────


class SyscallTable:
    """系统调用表 — 在 ToolRegistry 之上加 capability 过滤和审计。

    用法:
      table = SyscallTable(full_registry)
      scoped = table.build_scoped(phase_caps={SYS_READ, SYS_WRITE})
      scoped.to_api_schema()  # 只包含允许的工具
    """

    def __init__(self, full_registry: ToolRegistry) -> None:
        self._full = full_registry
        self._audit_log: list[dict] = []
        self._cpp = _CPP.create_syscall_table() if _HAS_CPP else None

    # ── C++ 原生执行 ──

    def execute_direct(self, name: str, args: dict) -> ToolResultBlock | None:
        """通过 C++ syscall 原生执行工具（绕过 Python ToolRegistry）。

        仅在 _HAS_CPP 且工具为 C++ 原生实现时生效。
        返回 ToolResultBlock 或 None（非原生工具）。
        """
        if not self._cpp or name not in _CPP_NATIVE_TOOLS:
            return None
        cpp_args = {k: str(v) for k, v in args.items()}
        result = self._cpp.execute(name, cpp_args)
        return ToolResultBlock(
            tool_use_id=args.get("__id", "cpp_direct"),
            content=result.content,
            is_error=result.is_error,
        )

    # ── 工具名 ↔ syscall 转换 ──

    @staticmethod
    def tool_to_syscall(name: str) -> Syscall | None:
        return _TOOL_SYSCALL_MAP.get(name)

    @staticmethod
    def syscall_number(name: str) -> int | None:
        sc = _TOOL_SYSCALL_MAP.get(name)
        return sc.value if sc is not None else None

    # ── 自动注册新工具 ──────────────────────────────────────────

    # 工具名前缀 → 推断的 capability
    _PREFIX_CAP_MAP: dict[str, Syscall] = {
        # 文件系统
        "read_": Syscall.SYS_READ,
        "write_": Syscall.SYS_WRITE,
        "edit_": Syscall.SYS_EDIT,
        "create_": Syscall.SYS_WRITE,
        "delete_": Syscall.SYS_WRITE,
        "remove_": Syscall.SYS_WRITE,
        "copy_": Syscall.SYS_WRITE,
        "move_": Syscall.SYS_WRITE,
        "rename_": Syscall.SYS_WRITE,
        "upload_": Syscall.SYS_WRITE,
        "download_": Syscall.SYS_READ,
        "list_": Syscall.SYS_READ,
        "get_": Syscall.SYS_READ,
        "find_": Syscall.SYS_GLOB,
        "search_": Syscall.SYS_GREP,
        # shell / 进程
        "exec_": Syscall.SYS_BASH,
        "run_": Syscall.SYS_BASH,
        "spawn_": Syscall.SYS_BASH,
        "shell_": Syscall.SYS_BASH,
        # 网络
        "fetch_": Syscall.SYS_WEB_FETCH,
        "web_": Syscall.SYS_WEB_FETCH,
        "http_": Syscall.SYS_WEB_FETCH,
        # docker
        "docker_": Syscall.SYS_BASH,
        "container_": Syscall.SYS_BASH,
        # git
        "git_": Syscall.SYS_BASH,
        # 包管理
        "npm_": Syscall.SYS_BASH,
        "pip_": Syscall.SYS_BASH,
        "cargo_": Syscall.SYS_BASH,
        "yarn_": Syscall.SYS_BASH,
        "brew_": Syscall.SYS_BASH,
        # 数据库
        "db_": Syscall.SYS_BASH,
        "sql_": Syscall.SYS_BASH,
        "query_": Syscall.SYS_READ,
        # team
        "team_": Syscall.SYS_TEAM_CREATE,
    }

    # 描述中的关键词 → 回退推断
    _DESC_KEYWORD_CAP_MAP: dict[str, Syscall] = {
        "browser": Syscall.SYS_WEB_FETCH,
        "web": Syscall.SYS_WEB_FETCH,
        "search": Syscall.SYS_TOOL_SEARCH,
        "file": Syscall.SYS_READ,
        "directory": Syscall.SYS_READ,
        "folder": Syscall.SYS_READ,
        "shell": Syscall.SYS_BASH,
        "command": Syscall.SYS_BASH,
        "terminal": Syscall.SYS_BASH,
        "database": Syscall.SYS_BASH,
        "sql": Syscall.SYS_BASH,
        "git": Syscall.SYS_BASH,
        "docker": Syscall.SYS_BASH,
        "container": Syscall.SYS_BASH,
        "npm": Syscall.SYS_BASH,
        "pip": Syscall.SYS_BASH,
        "image": Syscall.SYS_IMAGE_GEN,
        "picture": Syscall.SYS_IMAGE_GEN,
        "photo": Syscall.SYS_IMAGE_GEN,
    }

    @classmethod
    def auto_register(cls, tool_name: str, tool_description: str = "") -> Syscall:
        """自动推断并注册新工具的 syscall 编号。

        按优先级:
          1. 工具名前缀匹配 (_PREFIX_CAP_MAP)
          2. 工具名包含的关键词
          3. 描述关键词匹配 (_DESC_KEYWORD_CAP_MAP)
          4. 默认 SYS_MCP

        返回分配的 Syscall。
        """
        # 已注册 — 直接返回
        existing = _TOOL_SYSCALL_MAP.get(tool_name)
        if existing is not None:
            return existing

        sc = cls._infer_syscall(tool_name, tool_description)
        _TOOL_SYSCALL_MAP[tool_name] = sc
        log.info("auto_registered syscall=%s for tool=%s desc=%r", sc.name, tool_name, tool_description[:60])
        return sc

    @classmethod
    def _infer_syscall(cls, name: str, description: str) -> Syscall:
        """推断工具的 syscall，不修改 _TOOL_SYSCALL_MAP。"""
        name_lower = name.lower()

        # 1. 名前缀匹配
        for prefix, sc in cls._PREFIX_CAP_MAP.items():
            if name_lower.startswith(prefix):
                return sc

        # 2. 工具名包含关键词（任意位置）
        name_keywords = {
            "search": Syscall.SYS_TOOL_SEARCH,
            "find": Syscall.SYS_GLOB,
            "lookup": Syscall.SYS_READ,
            "query": Syscall.SYS_READ,
            "fetch": Syscall.SYS_WEB_FETCH,
            "create": Syscall.SYS_WRITE,
            "update": Syscall.SYS_WRITE,
            "delete": Syscall.SYS_WRITE,
            "remove": Syscall.SYS_WRITE,
            "write": Syscall.SYS_WRITE,
            "edit": Syscall.SYS_EDIT,
            "read": Syscall.SYS_READ,
            "open": Syscall.SYS_READ,
            "list": Syscall.SYS_READ,
            "exec": Syscall.SYS_BASH,
            "run": Syscall.SYS_BASH,
            "bash": Syscall.SYS_BASH,
            "shell": Syscall.SYS_BASH,
            "ssh": Syscall.SYS_BASH,
            "web": Syscall.SYS_WEB_FETCH,
            "browser": Syscall.SYS_WEB_FETCH,
            "http": Syscall.SYS_WEB_FETCH,
            "git": Syscall.SYS_BASH,
            "docker": Syscall.SYS_BASH,
            "image": Syscall.SYS_IMAGE_GEN,
            "generate": Syscall.SYS_IMAGE_GEN,
            "draw": Syscall.SYS_IMAGE_GEN,
            "paint": Syscall.SYS_IMAGE_GEN,
            "team": Syscall.SYS_TEAM_CREATE,
            "agent": Syscall.SYS_AGENT,
            "task": Syscall.SYS_TASK_CREATE,
        }
        for keyword, sc in name_keywords.items():
            if keyword in name_lower:
                return sc

        # 3. 描述关键词
        desc_lower = description.lower()
        for keyword, sc in cls._DESC_KEYWORD_CAP_MAP.items():
            if keyword in desc_lower:
                return sc

        # 4. 默认
        return Syscall.SYS_MCP

    @classmethod
    def infer_capability(cls, tool_name: str, tool_description: str = "") -> str:
        """返回工具所属 capability 组名（用于 phase scoping / workflow 建议）。"""
        sc = cls._infer_syscall(tool_name, tool_description)
        for cap_name, cap_set in {
            "file_read": CAP_FILE_READ,
            "file_write": CAP_FILE_WRITE,
            "shell": CAP_SHELL,
            "network": CAP_NET,
            "mcp": CAP_MCP,
            "image": CAP_IMAGE,
        }.items():
            if sc in cap_set:
                return cap_name
        return "mcp"

    # ── 构建作用域注册表（capability 过滤） ──

    def build_scoped(
        self,
        caps: set[Syscall] | None,
        explicit_tools: list[str] | None = None,
    ) -> ToolRegistry:
        """根据 capability 或显式工具名列表构建作用域注册表。

        Args:
            caps: 允许的 syscall 集合。None = 允许全部。
                  仅当 explicit_tools 为 None 时生效。
            explicit_tools: 显式工具名白名单。不为 None 时优先于 caps。
        """
        scoped = ToolRegistry()

        for tool in self._full.list_tools():
            # 显式白名单模式：只包含列出的工具，忽略 caps
            if explicit_tools is not None:
                if tool.name not in explicit_tools:
                    continue
                scoped.register(tool)
                continue

            # 能力过滤模式：没有显式白名单时按 caps 过滤
            if caps is not None:
                sc = _TOOL_SYSCALL_MAP.get(tool.name)
                if sc is None:
                    sc = self.auto_register(tool.name, getattr(tool, 'description', ''))
                if sc not in caps:
                    continue

            scoped.register(tool)

        return scoped

    @staticmethod
    def caps_for_phase(phase_name: str) -> set[Syscall] | None:
        """获取 phase 名称对应的默认 capability。未知名称返回 None（=全部允许）。"""
        return PHASE_CAP_MAP.get(phase_name)

    # ── 审计 ──

    async def dispatch_with_audit(
        self,
        tool_name: str,
        tool_input: dict,
        tool_id: str,
        scoped_registry: ToolRegistry,
        executor,
    ) -> tuple:
        """审计包装的 tool dispatch（供 executor 使用）。

        scoped_registry 已由 build_scoped 根据 phase caps / explicit_tools
        完成过滤，不再重复校验。工具只要在 scoped_registry 中即允许。

        返回 (ToolResultBlock, tool_obj_or_None)。
        """
        t0 = time.monotonic()
        sc = _TOOL_SYSCALL_MAP.get(tool_name)
        sc_num = sc.value if sc is not None else -1

        tool = scoped_registry.get(tool_name)
        if tool is None:
            block = ToolResultBlock(
                tool_use_id=tool_id,
                content=(
                    f"Error: tool '{tool_name}' not available in this phase "
                    f"(syscall={sc_num})"
                ),
                is_error=True,
            )
            self._audit_log.append({
                "tool": tool_name, "syscall": sc_num, "pid": getattr(getattr(executor, 'process', None), 'pid', None),
                "allowed": False, "duration_s": time.monotonic() - t0, "error": "not in scope",
            })
            return block, None

        self._audit_log.append({
            "tool": tool_name, "syscall": sc_num, "pid": getattr(getattr(executor, 'process', None), 'pid', None),
            "allowed": True, "duration_s": 0.0,  # filled after execution
        })
        return None, tool  # caller proceeds with tool execution

    def get_audit_log(self) -> list[dict]:
        return list(self._audit_log)

    def clear_audit_log(self) -> None:
        self._audit_log.clear()

    # ── 工具 ──

    def format_caps(self, caps: set[Syscall] | None) -> str:
        """Human-readable capability list."""
        if caps is None:
            return "ALL"
        return ", ".join(sorted(sc.name for sc in caps))
