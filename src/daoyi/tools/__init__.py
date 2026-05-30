"""Built-in tool registration."""

from daoyi.tools.ask_user_question_tool import AskUserQuestionTool
from daoyi.tools.agent_tool import AgentTool
from daoyi.tools.bash_tool import BashTool
from daoyi.tools.cli_anything_tool import CliAnythingTool
from daoyi.tools.base import BaseTool, ToolExecutionContext, ToolRegistry, ToolResult
from daoyi.tools.brief_tool import BriefTool
from daoyi.tools.config_tool import ConfigTool
from daoyi.tools.cron_create_tool import CronCreateTool
from daoyi.tools.cron_delete_tool import CronDeleteTool
from daoyi.tools.cron_list_tool import CronListTool
from daoyi.tools.cron_toggle_tool import CronToggleTool
from daoyi.tools.enter_plan_mode_tool import EnterPlanModeTool
from daoyi.tools.enter_worktree_tool import EnterWorktreeTool
from daoyi.tools.exit_plan_mode_tool import ExitPlanModeTool
from daoyi.tools.exit_worktree_tool import ExitWorktreeTool
from daoyi.tools.file_edit_tool import FileEditTool
from daoyi.tools.file_read_tool import FileReadTool
from daoyi.tools.file_write_tool import FileWriteTool
from daoyi.tools.glob_tool import GlobTool
from daoyi.tools.grep_tool import GrepTool
from daoyi.tools.image_generation_tool import ImageGenerationTool
from daoyi.tools.image_to_text_tool import ImageToTextTool
from daoyi.tools.list_mcp_resources_tool import ListMcpResourcesTool
from daoyi.tools.lsp_tool import LspTool
from daoyi.tools.mcp_auth_tool import McpAuthTool
from daoyi.tools.mcp_tool import McpToolAdapter
from daoyi.tools.notebook_edit_tool import NotebookEditTool
from daoyi.tools.read_mcp_resource_tool import ReadMcpResourceTool
from daoyi.tools.remote_trigger_tool import RemoteTriggerTool
from daoyi.tools.send_message_tool import SendMessageTool
from daoyi.tools.skill_executor_tool import SkillExecutorTool
from daoyi.tools.skill_tool import SkillTool
from daoyi.tools.sleep_tool import SleepTool
from daoyi.tools.task_create_tool import TaskCreateTool
from daoyi.tools.task_get_tool import TaskGetTool
from daoyi.tools.task_list_tool import TaskListTool
from daoyi.tools.task_output_tool import TaskOutputTool
from daoyi.tools.task_stop_tool import TaskStopTool
from daoyi.tools.task_update_tool import TaskUpdateTool
from daoyi.tools.team_create_tool import TeamCreateTool
from daoyi.tools.team_delete_tool import TeamDeleteTool
from daoyi.tools.todo_write_tool import TodoWriteTool
from daoyi.tools.tool_search_tool import ToolSearchTool
from daoyi.tools.web_fetch_tool import WebFetchTool
from daoyi.tools.web_search_tool import WebSearchTool
from daoyi.tools.open_browser_tool import OpenBrowserTool


def create_default_tool_registry(mcp_manager=None) -> ToolRegistry:
    """Return the default built-in tool registry."""
    registry = ToolRegistry()
    for tool in (
        BashTool(),
        CliAnythingTool(),
        AskUserQuestionTool(),
        FileReadTool(),
        FileWriteTool(),
        FileEditTool(),
        NotebookEditTool(),
        LspTool(),
        McpAuthTool(),
        GlobTool(),
        GrepTool(),
        ImageToTextTool(),
        ImageGenerationTool(),
        SkillTool(),
        ToolSearchTool(),
        WebFetchTool(),
        WebSearchTool(),
        OpenBrowserTool(),
        ConfigTool(),
        BriefTool(),
        SkillExecutorTool(),
        SleepTool(),
        EnterWorktreeTool(),
        ExitWorktreeTool(),
        TodoWriteTool(),
        EnterPlanModeTool(),
        ExitPlanModeTool(),
        CronCreateTool(),
        CronListTool(),
        CronDeleteTool(),
        CronToggleTool(),
        RemoteTriggerTool(),
        TaskCreateTool(),
        TaskGetTool(),
        TaskListTool(),
        TaskStopTool(),
        TaskOutputTool(),
        TaskUpdateTool(),
        AgentTool(),
        SendMessageTool(),
        TeamCreateTool(),
        TeamDeleteTool(),
    ):
        registry.register(tool)
    if mcp_manager is not None:
        registry.register(ListMcpResourcesTool(mcp_manager))
        registry.register(ReadMcpResourceTool(mcp_manager))
        for tool_info in mcp_manager.list_tools():
            registry.register(McpToolAdapter(mcp_manager, tool_info))
    return registry


__all__ = [
    "BaseTool",
    "ToolExecutionContext",
    "ToolRegistry",
    "ToolResult",
    "create_default_tool_registry",
]
