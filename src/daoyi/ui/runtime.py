"""Shared runtime assembly for headless and Textual UIs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

from daoyi.api.client import AnthropicApiClient, SupportsStreamingMessages
from daoyi.api.codex_client import CodexApiClient
from daoyi.api.copilot_client import CopilotClient
from daoyi.api.openai_client import OpenAICompatibleClient
from daoyi.api.provider import auth_status, detect_provider
from daoyi.bridge import get_bridge_manager
from daoyi.commands import (
    CommandContext,
    CommandResult,
    MemoryCommandBackend,
    create_default_command_registry,
    lookup_skill_slash_command,
)
from daoyi.config import get_config_file_path, load_settings
from daoyi.engine import QueryEngine
from daoyi.engine.messages import (
    ConversationMessage,
    ImageBlock,
    ToolResultBlock,
    ToolUseBlock,
    sanitize_conversation_messages,
)
from daoyi.engine.query import MaxTurnsExceeded
from daoyi.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    ErrorEvent,
    StatusEvent,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from daoyi.hooks import HookEvent, HookExecutionContext, HookExecutor, load_hook_registry
from daoyi.hooks.hot_reload import HookReloader
from daoyi.mcp.client import McpClientManager
from daoyi.mcp.config import load_mcp_server_configs
from daoyi.permissions import PermissionChecker
from daoyi.plugins import load_plugins
from daoyi.prompts import build_runtime_system_prompt
from daoyi.state import AppState, AppStateStore
from daoyi.services.session_backend import DEFAULT_SESSION_BACKEND, SessionBackend
from daoyi.tools import ToolRegistry, create_default_tool_registry
from daoyi.keybindings import load_keybindings
from daoyi.task_workflow import (
    ToolDiscoverer,
    WorkflowExecutor,
    WorkflowLearner,
    WorkflowRegistry,
    TaskClassifier,
    get_workflow_registry,
)
from daoyi.task_workflow.templates import BUILTIN_WORKFLOWS

PermissionPrompt = Callable[[str, str], Awaitable[bool]]
AskUserPrompt = Callable[[str], Awaitable[str]]
EditApprovalPrompt = Callable[[str, str, int, int], Awaitable[str]]
SystemPrinter = Callable[[str], Awaitable[None]]
StreamRenderer = Callable[[StreamEvent], Awaitable[None]]
ClearHandler = Callable[[], Awaitable[None]]


def _resolve_image_generation_config(settings) -> dict[str, str]:
    """Resolve image generation configuration from settings, environment, and Codex auth."""
    from daoyi.config.settings import ImageGenerationConfig, ProviderProfile

    cfg = settings.image_generation
    env_cfg = ImageGenerationConfig.from_env()
    resolved = {
        "provider": cfg.provider or env_cfg.provider,
        "model": cfg.model or env_cfg.model,
        "api_key": cfg.api_key or env_cfg.api_key,
        "base_url": cfg.base_url or env_cfg.base_url,
        "codex_model": cfg.codex_model or env_cfg.codex_model,
        "codex_base_url": cfg.codex_base_url or env_cfg.codex_base_url,
    }

    try:
        codex_profile = settings.merged_profiles().get("codex") or ProviderProfile(
            label="Codex Subscription",
            provider="openai_codex",
            api_format="openai",
            auth_source="codex_subscription",
            default_model="gpt-5.4",
        )
        codex_settings = settings.model_copy(
            update={
                "active_profile": "codex",
                "profiles": {**settings.profiles, "codex": codex_profile},
            }
        ).materialize_active_profile()
        codex_auth = codex_settings.resolve_auth()
        resolved["codex_auth_token"] = codex_auth.value
        resolved["codex_base_url"] = resolved["codex_base_url"] or (codex_settings.base_url or "")
        resolved["codex_model"] = resolved["codex_model"] or codex_settings.model
    except Exception:
        pass

    return resolved


def _resolve_vision_config(settings) -> dict[str, str]:
    """Resolve the vision model configuration from settings or environment.

    Priority: settings.vision fields > environment variables > empty.
    """
    from daoyi.config.settings import VisionModelConfig

    cfg = settings.vision
    if cfg.is_configured:
        return {
            "model": cfg.model,
            "api_key": cfg.api_key,
            "base_url": cfg.base_url,
        }

    # Fall back to environment variables
    env_cfg = VisionModelConfig.from_env()
    if env_cfg.is_configured:
        return {
            "model": env_cfg.model,
            "api_key": env_cfg.api_key,
            "base_url": env_cfg.base_url,
        }

    return {}


@dataclass
class RuntimeBundle:
    """Shared runtime objects for one interactive session."""

    api_client: SupportsStreamingMessages
    cwd: str
    mcp_manager: McpClientManager
    tool_registry: ToolRegistry
    app_state: AppStateStore
    hook_executor: HookExecutor
    engine: QueryEngine
    commands: object
    external_api_client: bool
    enforce_max_turns: bool = True
    session_id: str = ""
    settings_overrides: dict[str, Any] = field(default_factory=dict)
    session_backend: SessionBackend = DEFAULT_SESSION_BACKEND
    extra_skill_dirs: tuple[str, ...] = ()
    extra_plugin_roots: tuple[str, ...] = ()
    memory_backend: MemoryCommandBackend | None = None
    include_project_memory: bool = True
    autodream_context: dict[str, object] | None = None
    workflow_registry: WorkflowRegistry | None = None
    workflow_executor: WorkflowExecutor | None = None
    workflow_learner: WorkflowLearner | None = None

    # Channel infrastructure (optional)
    channel_manager: Any | None = None
    channel_bridge: Any | None = None
    message_bus: Any | None = None

    def current_settings(self):
        """Return the effective settings for this session.

        We persist most settings to disk (``~/.daoyi/settings.json``), but
        CLI options like ``--model``/``--api-format`` should remain in effect for
        the lifetime of the running process. Without this overlay, issuing any
        slash command (e.g. ``/fast``) would refresh UI state from disk and
        "snap back" the model/provider to whatever is stored in the config file.
        """
        return load_settings().merge_cli_overrides(**self.settings_overrides)

    def current_plugins(self):
        """Return currently visible plugins for the working tree."""
        return load_plugins(
            self.current_settings(),
            self.cwd,
            extra_roots=self.extra_plugin_roots,
        )

    def hook_summary(self) -> str:
        """Return the current hook summary."""
        return load_hook_registry(self.current_settings(), self.current_plugins()).summary()

    def plugin_summary(self) -> str:
        """Return the current plugin summary."""
        plugins = self.current_plugins()
        if not plugins:
            return "No plugins discovered."
        lines = ["Plugins:"]
        for plugin in plugins:
            state = "enabled" if plugin.enabled else "disabled"
            lines.append(f"- {plugin.manifest.name} [{state}] {plugin.manifest.description}")
        return "\n".join(lines)

    def mcp_summary(self) -> str:
        """Return the current MCP summary."""
        statuses = self.mcp_manager.list_statuses()
        if not statuses:
            return "No MCP servers configured."
        lines = ["MCP servers:"]
        for status in statuses:
            suffix = f" - {status.detail}" if status.detail else ""
            lines.append(f"- {status.name}: {status.state}{suffix}")
            if status.tools:
                lines.append(f"  tools: {', '.join(tool.name for tool in status.tools)}")
            if status.resources:
                lines.append(f"  resources: {', '.join(resource.uri for resource in status.resources)}")
        return "\n".join(lines)


def _resolve_api_client_from_settings(settings) -> SupportsStreamingMessages:
    """Build the appropriate API client for the resolved settings."""
    # Save original base_url before profile materialization clears it
    _orig_base_url = settings.base_url
    # Ensure profile fields (base_url, model, api_format) are projected to settings
    settings = settings.materialize_active_profile()

    def _safe_resolve_auth():
        try:
            return settings.resolve_auth()
        except Exception as exc:
            _print_auth_resolution_error(settings, exc)
            raise SystemExit(1)

    if settings.provider == "cpp":
        from daoyi.api.cpp_client import CppLLMClient

        orig_base_url = _orig_base_url
        host = orig_base_url or os.environ.get("DY_LLM_HOST", "")
        if host:
            parsed = urlparse(host)
            host = parsed.hostname or host
        port = int(os.environ.get("DY_LLM_PORT", "8080"))
        api_key = os.environ.get("DY_LLM_API_KEY") or settings.api_key or ""
        model_path = os.environ.get("DY_LOCAL_MODEL_PATH", "")
        use_local = not host or host in ("localhost", "127.0.0.1", "::1")
        return CppLLMClient(
            use_local=use_local,
            host=host or "localhost",
            port=port,
            api_key=api_key,
            model_path=model_path,
        )
    if settings.api_format == "copilot":
        from daoyi.api.copilot_client import COPILOT_DEFAULT_MODEL

        copilot_model = (
            COPILOT_DEFAULT_MODEL
            if settings.model in {"claude-sonnet-4-20250514", "claude-sonnet-4-6", "sonnet", "default"}
            else settings.model
        )
        return CopilotClient(model=copilot_model)
    if settings.provider == "openai_codex":
        auth = _safe_resolve_auth()
        return CodexApiClient(
            auth_token=auth.value,
            base_url=settings.base_url,
        )
    if settings.provider == "anthropic_claude":
        return AnthropicApiClient(
            auth_token=_safe_resolve_auth().value,
            base_url=settings.base_url,
            claude_oauth=True,
            auth_token_resolver=lambda: settings.resolve_auth().value,
        )
    if settings.api_format in ("openai", "openai_compat"):
        auth = _safe_resolve_auth()
        return OpenAICompatibleClient(
            api_key=auth.value,
            base_url=settings.base_url,
            timeout=settings.timeout,
        )
    auth = _safe_resolve_auth()
    return AnthropicApiClient(
        api_key=auth.value,
        base_url=settings.base_url,
    )


def _print_auth_resolution_error(settings, exc: Exception) -> None:
    """Render auth failures without collapsing subscription errors into API-key advice."""
    try:
        profile_name, profile = settings.resolve_profile()
        auth_source = (getattr(profile, "auth_source", "") or "").strip()
    except Exception:
        profile_name = ""
        auth_source = ""

    message = str(exc).strip() or exc.__class__.__name__
    if auth_source in {"claude_subscription", "codex_subscription"}:
        login_command = "claude-login" if auth_source == "claude_subscription" else "codex-login"
        provider_name = profile_name or (
            "claude-subscription" if auth_source == "claude_subscription" else "codex"
        )
        print(
            f"Error: {message}\n"
            f"  This profile uses subscription auth, not an API key.\n"
            f"  Run `oh auth {login_command}` to bind the local CLI session, then\n"
            f"  run `oh provider use {provider_name}` to activate it.",
            file=sys.stderr,
        )
        return

    print(
        "Error: No API key configured.\n"
        f"  {message}\n"
        "  Run `oh auth login` to set up authentication, or set the\n"
        "  ANTHROPIC_API_KEY (or OPENAI_API_KEY) environment variable.",
        file=sys.stderr,
    )


async def build_runtime(
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    active_profile: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    permission_prompt: PermissionPrompt | None = None,
    ask_user_prompt: AskUserPrompt | None = None,
    edit_approval_prompt: EditApprovalPrompt | None = None,
    restore_messages: list[dict] | None = None,
    restore_tool_metadata: dict[str, object] | None = None,
    enforce_max_turns: bool = True,
    session_backend: SessionBackend | None = None,
    permission_mode: str | None = None,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    extra_plugin_roots: Iterable[str | Path] | None = None,
    memory_backend: MemoryCommandBackend | None = None,
    include_project_memory: bool = True,
    autodream_context: dict[str, object] | None = None,
) -> RuntimeBundle:
    """Build the shared runtime for an OpenHarness session."""
    settings_overrides: dict[str, Any] = {
        "model": model,
        "max_turns": max_turns,
        "effort": effort,
        "base_url": base_url,
        "system_prompt": system_prompt,
        "api_key": api_key,
        "api_format": api_format,
        "active_profile": active_profile,
        "permission_mode": permission_mode,
    }
    settings = load_settings().merge_cli_overrides(**settings_overrides)
    cwd = str(Path(cwd).expanduser().resolve()) if cwd else str(Path.cwd())
    normalized_skill_dirs = tuple(str(Path(path).expanduser().resolve()) for path in (extra_skill_dirs or ()))
    normalized_plugin_roots = tuple(str(Path(path).expanduser().resolve()) for path in (extra_plugin_roots or ()))
    plugins = load_plugins(settings, cwd, extra_roots=normalized_plugin_roots)
    if api_client:
        resolved_api_client = api_client
    else:
        resolved_api_client = _resolve_api_client_from_settings(settings)
    mcp_manager = McpClientManager(load_mcp_server_configs(settings, plugins))
    await mcp_manager.connect_all()
    tool_registry = create_default_tool_registry(mcp_manager)
    # Register plugin-provided tools
    for plugin in plugins:
        if plugin.enabled and plugin.tools:
            for tool in plugin.tools:
                tool_registry.register(tool)
    provider = detect_provider(settings)
    bridge_manager = get_bridge_manager()
    app_state = AppStateStore(
        AppState(
            # Show the effective runtime model (after CLI/env/profile merges),
            # not profile.last_model which may be stale.
            model=settings.model,
            permission_mode=settings.permission.mode.value,
            theme=settings.theme,
            cwd=cwd,
            provider=provider.name,
            auth_status=auth_status(settings),
            base_url=settings.base_url or "",
            vim_enabled=settings.vim_mode,
            voice_enabled=settings.voice_mode,
            voice_available=provider.voice_supported,
            voice_reason=provider.voice_reason,
            fast_mode=settings.fast_mode,
            effort=settings.effort,
            passes=settings.passes,
            mcp_connected=sum(1 for status in mcp_manager.list_statuses() if status.state == "connected"),
            mcp_failed=sum(1 for status in mcp_manager.list_statuses() if status.state == "failed"),
            bridge_sessions=len(bridge_manager.list_sessions()),
            output_style=settings.output_style,
            keybindings=load_keybindings(),
        )
    )
    hook_reloader = HookReloader(get_config_file_path())
    hook_executor = HookExecutor(
        hook_reloader.current_registry() if api_client is None else load_hook_registry(settings, plugins),
        HookExecutionContext(
            cwd=Path(cwd).resolve(),
            api_client=resolved_api_client,
            default_model=settings.model,
        ),
    )
    engine_max_turns = settings.max_turns if (enforce_max_turns or max_turns is not None) else None
    system_prompt_text = build_runtime_system_prompt(
        settings,
        cwd=cwd,
        latest_user_prompt=prompt,
        extra_skill_dirs=normalized_skill_dirs,
        extra_plugin_roots=normalized_plugin_roots,
        include_project_memory=include_project_memory,
    )
    from uuid import uuid4

    session_id = uuid4().hex[:12]

    restored_metadata = {
        "permission_mode": settings.permission.mode.value,
        "read_file_state": [],
        "invoked_skills": [],
        "async_agent_state": [],
        "async_agent_tasks": [],
        "recent_work_log": [],
        "recent_verified_work": [],
        "task_focus_state": {
            "goal": "",
            "recent_goals": [],
            "active_artifacts": [],
            "verified_state": [],
            "next_step": "",
        },
        "compact_checkpoints": [],
    }
    if isinstance(restore_tool_metadata, dict):
        for key, value in restore_tool_metadata.items():
            restored_metadata[key] = value

    engine = QueryEngine(
        api_client=resolved_api_client,
        tool_registry=tool_registry,
        permission_checker=PermissionChecker(settings.permission),
        cwd=cwd,
        model=settings.model,
        system_prompt=system_prompt_text,
        max_tokens=settings.max_tokens,
        context_window_tokens=settings.context_window_tokens or settings.memory.context_window_tokens,
        auto_compact_threshold_tokens=(
            settings.auto_compact_threshold_tokens
            or settings.memory.auto_compact_threshold_tokens
        ),
        max_turns=engine_max_turns,
        permission_prompt=permission_prompt,
        ask_user_prompt=ask_user_prompt,
        hook_executor=hook_executor,
        settings=settings,
        tool_metadata={
            "mcp_manager": mcp_manager,
            "bridge_manager": bridge_manager,
            "extra_skill_dirs": normalized_skill_dirs,
            "extra_plugin_roots": normalized_plugin_roots,
            "session_id": session_id,
            "edit_approval_prompt": edit_approval_prompt,
            "vision_model_config": _resolve_vision_config(settings),
            "image_generation_config": _resolve_image_generation_config(settings),
            **restored_metadata,
        },
    )
    if autodream_context is not None:
        engine.tool_metadata["autodream_context"] = autodream_context
    # Restore messages from a saved session if provided
    if restore_messages:
        restored = sanitize_conversation_messages(
            [ConversationMessage.model_validate(m) for m in restore_messages]
        )
        engine.load_messages(restored)

    # Start Docker sandbox if configured
    if settings.sandbox.enabled and settings.sandbox.backend == "docker":
        from daoyi.sandbox.session import start_docker_sandbox

        await start_docker_sandbox(settings, session_id, Path(cwd))

    # --- Initialize task workflow engine ---
    workflow_registry = get_workflow_registry()
    classifier = TaskClassifier(workflow_registry)
    # Seed built-in templates (they won't overwrite user-saved ones)
    for bw in BUILTIN_WORKFLOWS:
        existing = workflow_registry.get(bw.id)
        if existing is None:
            workflow_registry.save(bw)
        elif existing.trigger_patterns != bw.trigger_patterns:
            # Update when trigger patterns change
            workflow_registry.save(bw)
        elif existing.phases != bw.phases:
            # Update when phase prompt/tools/max_turns change
            workflow_registry.save(bw)

    # Wrap API client with replay engine (deterministic cache + trace)
    from daoyi.kernel import HAS_CPP_CORE
    from daoyi.kernel.replay import ReplayEngine
    replay_engine = ReplayEngine(
        resolved_api_client,
        enable_cache=True,
        record_trace=True,
        session_id=session_id,
    )

    local_model_path = os.environ.get("DY_LOCAL_MODEL_PATH", "")
    if not local_model_path and settings.provider == "local":
        local_model_path = os.environ.get("DY_LOCAL_MODEL_PATH", settings.model)

    workflow_executor = WorkflowExecutor(
        api_client=replay_engine,
        full_tool_registry=tool_registry,
        permission_checker=PermissionChecker(settings.permission),
        cwd=Path(cwd),
        model=settings.model,
        max_tokens=settings.max_tokens,
        effort=settings.effort,
        local_model_path=local_model_path or None,
    )
    workflow_learner = WorkflowLearner(registry=workflow_registry, classifier=classifier)
    # ---------------------------------------

    # --- Initialize chat channels ---
    message_bus: Any = None
    channel_manager: Any = None
    channel_bridge: Any = None
    _channel_names = ("feishu", "telegram", "discord", "slack", "dingtalk", "qq", "whatsapp", "email", "matrix", "mochat")
    enabled_channels = [
        name for name in _channel_names
        if getattr(settings.channels, name, None) is not None
        and getattr(settings.channels, name).enabled
    ]
    if enabled_channels:
        from daoyi.channels.bus.queue import MessageBus as _MessageBus
        from daoyi.channels.impl.manager import ChannelManager as _ChannelManager
        from daoyi.channels.adapter import ChannelBridge as _ChannelBridge
        from daoyi.config.schema import Config as _ChannelConfig

        message_bus = _MessageBus()
        channel_config = _ChannelConfig(channels=settings.channels)
        channel_manager = _ChannelManager(config=channel_config, bus=message_bus)
        channel_bridge = _ChannelBridge(
            api_client=resolved_api_client,
            model=settings.model,
            bus=message_bus,
            tool_registry=tool_registry,
            cwd=cwd,
        )
        logger.info("Chat channels enabled: %s", ", ".join(enabled_channels))
    # ---------------------------------------

    return RuntimeBundle(
        api_client=resolved_api_client,
        cwd=cwd,
        mcp_manager=mcp_manager,
        tool_registry=tool_registry,
        app_state=app_state,
        hook_executor=hook_executor,
        engine=engine,
        commands=create_default_command_registry(
            plugin_commands=[
                command
                for plugin in plugins
                if plugin.enabled
                for command in plugin.commands
            ]
        ),
        external_api_client=api_client is not None,
        enforce_max_turns=enforce_max_turns or max_turns is not None,
        session_id=session_id,
        settings_overrides=settings_overrides,
        session_backend=session_backend or DEFAULT_SESSION_BACKEND,
        extra_skill_dirs=normalized_skill_dirs,
        extra_plugin_roots=normalized_plugin_roots,
        memory_backend=memory_backend,
        include_project_memory=include_project_memory,
        autodream_context=autodream_context,
        workflow_registry=workflow_registry,
        workflow_executor=workflow_executor,
        workflow_learner=workflow_learner,
        channel_manager=channel_manager,
        channel_bridge=channel_bridge,
        message_bus=message_bus,
    )


async def start_runtime(bundle: RuntimeBundle) -> None:
    """Run session start hooks and start chat channels."""
    # Preload small model in background thread (Metal shader compilation ~30s)
    _preload_small_model()

    # Auto-start cua-driver daemon if not running (used by web_search / web_fetch)
    await _ensure_cua_daemon()

    await bundle.hook_executor.execute(
        HookEvent.SESSION_START,
        {"cwd": bundle.cwd, "event": HookEvent.SESSION_START.value},
    )
    if bundle.channel_manager and bundle.channel_bridge:
        await bundle.channel_bridge.start()
        asyncio.create_task(bundle.channel_manager.start_all())


def _preload_small_model() -> None:
    """Kick off small model loading in a background daemon thread."""
    from daoyi.llm.small_model import SmallModelClient
    try:
        SmallModelClient.preload()
    except Exception:
        logger.debug("Small model preload not available", exc_info=True)


async def _ensure_cua_daemon() -> None:
    """Start cua-driver daemon if it's not already running."""
    import shutil

    binary = shutil.which("cua-driver")
    if not binary:
        logger.info("cua-driver not found — web_search/web_fetch will fall back to HTTP")
        return

    # Check if daemon is already running
    proc = await asyncio.create_subprocess_exec(
        binary, "status",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        stdout = b""
    status_text = stdout.decode().strip()

    if "running" in status_text.lower():
        logger.info("cua-driver daemon already running (pid=%s)", status_text.split()[-1] if status_text.split() else "?")
        return

    # Start daemon in background
    logger.info("Starting cua-driver daemon...")
    daemon_proc = await asyncio.create_subprocess_exec(
        binary, "serve",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    # Give it a moment to start
    await asyncio.sleep(2)

    # Verify it started
    proc2 = await asyncio.create_subprocess_exec(
        binary, "status",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5.0)
    except asyncio.TimeoutError:
        proc2.kill()
        await proc2.wait()
        stdout2 = b""

    if "running" in stdout2.decode().lower():
        logger.info("cua-driver daemon started successfully")
    else:
        logger.warning("cua-driver daemon may not have started (status: %s)", stdout2.decode().strip())


async def close_runtime(bundle: RuntimeBundle) -> None:
    """Close runtime-owned resources."""
    # Stop channels first
    if bundle.channel_manager:
        try:
            await bundle.channel_manager.stop_all()
        except Exception:
            logger.exception("Error stopping channels")
    if bundle.channel_bridge:
        try:
            await bundle.channel_bridge.stop()
        except Exception:
            logger.exception("Error stopping channel bridge")

    from daoyi.sandbox.session import stop_docker_sandbox

    await stop_docker_sandbox()
    # Extract local environment rules from session before closing
    try:
        from daoyi.personalization.session_hook import update_rules_from_session
        update_rules_from_session(bundle.engine.messages)
    except Exception:
        pass  # personalization is best-effort, never block session end

    await bundle.mcp_manager.close()
    await bundle.hook_executor.execute(
        HookEvent.SESSION_END,
        {"cwd": bundle.cwd, "event": HookEvent.SESSION_END.value},
    )


def _last_user_text(messages: list[ConversationMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user" and msg.text.strip():
            return msg.text.strip()
    return ""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _format_pending_tool_results(messages: list[ConversationMessage]) -> str | None:
    """Render a compact summary when we stop after tool execution but before the follow-up model turn."""
    if not messages:
        return None

    last = messages[-1]
    if last.role != "user":
        return None
    tool_results = [block for block in last.content if isinstance(block, ToolResultBlock)]
    if not tool_results:
        return None

    tool_uses_by_id: dict[str, ToolUseBlock] = {}
    assistant_text = ""
    for msg in reversed(messages[:-1]):
        if msg.role != "assistant":
            continue
        if not msg.tool_uses:
            continue
        assistant_text = msg.text.strip()
        for tu in msg.tool_uses:
            tool_uses_by_id[tu.id] = tu
        break

    lines: list[str] = [
        "Pending continuation: tool results were produced, but the model did not get a chance to respond yet."
    ]
    if assistant_text:
        lines.append(f"Last assistant message: {_truncate(assistant_text, 400)}")

    max_results = 3
    for tr in tool_results[:max_results]:
        tu = tool_uses_by_id.get(tr.tool_use_id)
        if tu is not None:
            raw_input = json.dumps(tu.input, ensure_ascii=True, sort_keys=True)
            lines.append(
                f"- {tu.name} {_truncate(raw_input, 200)} -> {_truncate(tr.content.strip(), 400)}"
            )
        else:
            lines.append(
                f"- tool_result[{tr.tool_use_id}] -> {_truncate(tr.content.strip(), 400)}"
            )

    if len(tool_results) > max_results:
        lines.append(f"(+{len(tool_results) - max_results} more tool results)")

    lines.append("To continue from these results, run: /continue [COUNT].")
    return "\n".join(lines)


def sync_app_state(bundle: RuntimeBundle) -> None:
    """Refresh UI state from current settings and dynamic keybindings."""
    settings = bundle.current_settings()
    if bundle.enforce_max_turns:
        bundle.engine.set_max_turns(settings.max_turns)
    provider = detect_provider(settings)
    bundle.app_state.set(
        model=settings.model,
        permission_mode=settings.permission.mode.value,
        theme=settings.theme,
        cwd=bundle.cwd,
        provider=provider.name,
        auth_status=auth_status(settings),
        base_url=settings.base_url or "",
        vim_enabled=settings.vim_mode,
        voice_enabled=settings.voice_mode,
        voice_available=provider.voice_supported,
        voice_reason=provider.voice_reason,
        fast_mode=settings.fast_mode,
        effort=settings.effort,
        passes=settings.passes,
        mcp_connected=sum(1 for status in bundle.mcp_manager.list_statuses() if status.state == "connected"),
        mcp_failed=sum(1 for status in bundle.mcp_manager.list_statuses() if status.state == "failed"),
        bridge_sessions=len(get_bridge_manager().list_sessions()),
        output_style=settings.output_style,
        keybindings=load_keybindings(),
    )


def refresh_runtime_client(bundle: RuntimeBundle) -> None:
    """Refresh the active runtime client after provider/auth/profile changes."""
    settings = bundle.current_settings()
    if not bundle.external_api_client:
        bundle.api_client = _resolve_api_client_from_settings(settings)
        bundle.engine.set_api_client(bundle.api_client)
        bundle.hook_executor.update_context(
            api_client=bundle.api_client,
            default_model=settings.model,
        )
    bundle.engine.set_model(settings.model)
    bundle.engine.set_effort(settings.effort)
    bundle.engine.set_permission_checker(PermissionChecker(settings.permission))
    system_prompt = build_runtime_system_prompt(
        settings,
        cwd=bundle.cwd,
        latest_user_prompt=_last_user_text(bundle.engine.messages),
        extra_skill_dirs=bundle.extra_skill_dirs,
        extra_plugin_roots=bundle.extra_plugin_roots,
        include_project_memory=bundle.include_project_memory,
    )
    bundle.engine.set_system_prompt(system_prompt)
    sync_app_state(bundle)


async def handle_line(
    bundle: RuntimeBundle,
    line: str,
    *,
    print_system: SystemPrinter,
    render_event: StreamRenderer,
    clear_output: ClearHandler,
    user_message: ConversationMessage | None = None,
) -> bool:
    """Handle one submitted line for either headless or TUI rendering."""
    if not bundle.external_api_client:
        bundle.hook_executor.update_registry(
            load_hook_registry(bundle.current_settings(), bundle.current_plugins())
        )

    command_context = CommandContext(
        engine=bundle.engine,
        hooks_summary=bundle.hook_summary(),
        mcp_summary=bundle.mcp_summary(),
        plugin_summary=bundle.plugin_summary(),
        cwd=bundle.cwd,
        tool_registry=bundle.tool_registry,
        app_state=bundle.app_state,
        session_backend=bundle.session_backend,
        session_id=bundle.session_id,
        extra_skill_dirs=bundle.extra_skill_dirs,
        extra_plugin_roots=bundle.extra_plugin_roots,
        memory_backend=bundle.memory_backend,
        include_project_memory=bundle.include_project_memory,
        process_table=(
            bundle.workflow_executor.process_table
            if bundle.workflow_executor
            else None
        ),
    )
    parsed = None if user_message is not None else (
        bundle.commands.lookup(line) or lookup_skill_slash_command(line, command_context)
    )
    if parsed is not None:
        command, args = parsed
        result = await command.handler(
            args,
            command_context,
        )
        if result.refresh_runtime:
            refresh_runtime_client(bundle)
        await _render_command_result(result, print_system, clear_output, render_event)
        if result.submit_prompt is not None:
            original_model = bundle.engine.model
            if result.submit_model:
                bundle.engine.set_model(result.submit_model)
            settings = bundle.current_settings()
            submit_prompt = result.submit_prompt
            system_prompt = build_runtime_system_prompt(
                settings,
                cwd=bundle.cwd,
                latest_user_prompt=submit_prompt,
                extra_skill_dirs=bundle.extra_skill_dirs,
                extra_plugin_roots=bundle.extra_plugin_roots,
                include_project_memory=bundle.include_project_memory,
            )
            bundle.engine.set_system_prompt(system_prompt)
            try:
                async for event in bundle.engine.submit_message(submit_prompt):
                    await render_event(event)
            except MaxTurnsExceeded as exc:
                await print_system(f"Stopped after {exc.max_turns} turns (max_turns).")
                pending = _format_pending_tool_results(bundle.engine.messages)
                if pending:
                    await print_system(pending)
            finally:
                if result.submit_model:
                    bundle.engine.set_model(original_model)
            bundle.session_backend.save_snapshot(
                cwd=bundle.cwd,
                model=bundle.engine.model,
                system_prompt=system_prompt,
                messages=bundle.engine.messages,
                usage=bundle.engine.total_usage,
                session_id=bundle.session_id,
                tool_metadata=bundle.engine.tool_metadata,
            )
        if result.continue_pending:
            settings = bundle.current_settings()
            if bundle.enforce_max_turns:
                bundle.engine.set_max_turns(settings.max_turns)
            system_prompt = build_runtime_system_prompt(
                settings,
                cwd=bundle.cwd,
                latest_user_prompt=_last_user_text(bundle.engine.messages),
                extra_skill_dirs=bundle.extra_skill_dirs,
                extra_plugin_roots=bundle.extra_plugin_roots,
                include_project_memory=bundle.include_project_memory,
            )
            bundle.engine.set_system_prompt(system_prompt)
            turns = result.continue_turns if result.continue_turns is not None else bundle.engine.max_turns
            try:
                async for event in bundle.engine.continue_pending(max_turns=turns):
                    await render_event(event)
            except MaxTurnsExceeded as exc:
                await print_system(f"Stopped after {exc.max_turns} turns (max_turns).")
                pending = _format_pending_tool_results(bundle.engine.messages)
                if pending:
                    await print_system(pending)
            bundle.session_backend.save_snapshot(
                cwd=bundle.cwd,
                model=settings.model,
                system_prompt=system_prompt,
                messages=bundle.engine.messages,
                usage=bundle.engine.total_usage,
                session_id=bundle.session_id,
                tool_metadata=bundle.engine.tool_metadata,
            )
        sync_app_state(bundle)
        return not result.should_exit

    settings = bundle.current_settings()
    if bundle.enforce_max_turns:
        bundle.engine.set_max_turns(settings.max_turns)
    latest_user_prompt = line or (user_message.text if user_message is not None else "")

    # ── Pre-classify intent with small model FIRST ────────────────
    # Small model should be the primary classifier
    pre_intent = None
    if bundle.workflow_executor:
        pre_intent = bundle.workflow_executor.pre_classify(
            latest_user_prompt,
            previous_intent=getattr(bundle.workflow_executor, '_last_intent', None),
        )
    # Track intent for short-continuation detection next turn
    if bundle.workflow_executor:
        bundle.workflow_executor._last_intent = pre_intent
    
    # If small model returns chat, check if user input indicates search intent
    # This is just a fallback, the small model should be the primary classifier
    if pre_intent is None or pre_intent == "chat":
        search_keywords = [
            "天气", "新闻", "最新", "今天", "现在", "行情", "股价", "股市",
            "weather", "news", "today", "current", "latest", "stock",
            "搜索", "查询", "搜一下", "查一下", "查找", "search", "find"
        ]
        lower_input = latest_user_prompt.lower()
        for kw in search_keywords:
            if kw in lower_input:
                pre_intent = "search"
                break

    pre_chat_failed = False

    # ── Skill-based capability check ─────────────────────────
    # For non-chat intents, check if matching skills exist. If skills
    # are installed but none match, report "no capability" and stop early.
    _no_capability_msg = None
    if pre_intent and pre_intent != "chat":
        try:
            from daoyi.task_workflow.skill_discovery import get_skill_matcher
            matcher = get_skill_matcher()
            all_skills = matcher.list_all_skills()
            if all_skills:
                matched = matcher.find_skills(latest_user_prompt, limit=3)
                if not matched:
                    _no_capability_msg = (
                        f"No matching skills found for intent '{pre_intent}'. "
                        "The agent does not have the capability to handle this request. "
                        "Try a different approach or install additional skill packages."
                    )
        except Exception:
            pass  # skill matching is optional; fall through to normal flow
    if _no_capability_msg:
        await print_system(f"[no-capability] {_no_capability_msg}")
        sync_app_state(bundle)
        return True

    # WorkflowExecutor fast paths (chat, workflow match, tool discovery)
    # are text-only — skip them when the user attached images.
    _has_images = (
        user_message is not None
        and any(isinstance(b, ImageBlock) for b in user_message.content)
    )

    if not _has_images:
        if pre_intent == "chat":
            # Pure chat — skip workflow matching/tool discovery entirely.
            async for event in bundle.workflow_executor.chat(
                latest_user_prompt, settings.model
            ):
                if isinstance(event, StatusEvent) and "retrying" in (event.message or ""):
                    pre_chat_failed = True
                    break  # model refused — fall through to agent loop
                await render_event(event)
            else:
                sync_app_state(bundle)
                return True
            # fall through to agent loop below

        # ── Intent hints for workflow matching ─────────────────
        _intent_prompt = latest_user_prompt
        if pre_intent in ("search", "file_ops", "code_review"):
            _intent_prompt = f"[intent: {pre_intent}] {latest_user_prompt}"

        # ── Check for matching workflow (fast path) ──────────────
        if bundle.workflow_registry and bundle.workflow_executor:
            matched_wf = bundle.workflow_registry.find(_intent_prompt)
            if matched_wf:
                await print_system(
                    f"[workflow] matched '{matched_wf.id}' "
                    f"({len(matched_wf.phases)} phases) — executing phase-by-phase…"
                )
                workflow_system_prompt = build_runtime_system_prompt(
                    settings,
                    cwd=bundle.cwd,
                    latest_user_prompt=latest_user_prompt,
                    extra_skill_dirs=bundle.extra_skill_dirs,
                    extra_plugin_roots=bundle.extra_plugin_roots,
                    include_project_memory=bundle.include_project_memory,
                )
                try:
                    async for event in bundle.workflow_executor.execute(
                        matched_wf,
                        latest_user_prompt,
                        system_prompt_base=workflow_system_prompt,
                    ):
                        await render_event(event)
                except Exception as exc:
                    await print_system(f"[workflow] execution error: {exc}")
                bundle.workflow_registry.increment_use(matched_wf.id)
                bundle.session_backend.save_snapshot(
                    cwd=bundle.cwd,
                    model=settings.model,
                    system_prompt=workflow_system_prompt,
                    messages=bundle.engine.messages,
                    usage=bundle.engine.total_usage,
                    session_id=bundle.session_id,
                    tool_metadata=bundle.engine.tool_metadata,
                )
                sync_app_state(bundle)
                return True
        # ──────────────────────────────────────────────────────────

        # ── No workflow matched — try tool discovery ─────────
        if bundle.workflow_registry and bundle.workflow_executor:
            discoverer = ToolDiscoverer(
                bundle.workflow_registry,
                TaskClassifier(bundle.workflow_registry),
            )
            discovered_wf = await discoverer.discover(_intent_prompt)
            if discovered_wf:
                await print_system(
                    f"[workflow] discovered new tool '{discovered_wf.id}' "
                    f"({len(discovered_wf.phases)} phases) — executing…"
                )
                workflow_system_prompt = build_runtime_system_prompt(
                    settings,
                    cwd=bundle.cwd,
                    latest_user_prompt=latest_user_prompt,
                    extra_skill_dirs=bundle.extra_skill_dirs,
                    extra_plugin_roots=bundle.extra_plugin_roots,
                    include_project_memory=bundle.include_project_memory,
                )
                try:
                    async for event in bundle.workflow_executor.execute(
                        discovered_wf,
                        latest_user_prompt,
                        system_prompt_base=workflow_system_prompt,
                    ):
                        await render_event(event)
                except Exception as exc:
                    await print_system(f"[workflow] discovery execution error: {exc}")
                bundle.session_backend.save_snapshot(
                    cwd=bundle.cwd,
                    model=settings.model,
                    system_prompt=workflow_system_prompt,
                    messages=bundle.engine.messages,
                    usage=bundle.engine.total_usage,
                    session_id=bundle.session_id,
                    tool_metadata=bundle.engine.tool_metadata,
                )
                sync_app_state(bundle)
                return True
        # ────────────────────────────────────────────────────────

        # ── Chat fast path (no workflow match, no tools) ────────
        _is_tool_cmd = any(
            latest_user_prompt.startswith(prefix)
            for prefix in ("run ", "bash ", "ls ", "pwd ", "cd ", "echo ",
                           "git ", "npm ", "pip ", "cargo ", "go ",
                           "打开 ", "启动 ", "关闭 ", "运行 ", "执行 ",
                           "搜索 ", "查询 ", "搜一下 ", "查一下 ")
        )
        if (
            not pre_chat_failed
            and not _is_tool_cmd
            and pre_intent not in ("search", "tool", "code", "file_ops", "code_review")
            and len(latest_user_prompt) < 300
            and bundle.workflow_executor
            and bundle.workflow_registry
        ):
            chat_used_tools = False
            async for event in bundle.workflow_executor.chat(
                latest_user_prompt, settings.model
            ):
                if isinstance(event, StatusEvent) and "retrying" in (event.message or ""):
                    chat_used_tools = True
                    break
                await render_event(event)  # defined below
            if not chat_used_tools:
                sync_app_state(bundle)
                return True
            # fall through to full agent loop

    # ── Normal Agent Loop (fallback) ─────────────────────────

    system_prompt = build_runtime_system_prompt(
        settings,
        cwd=bundle.cwd,
        latest_user_prompt=latest_user_prompt,
        extra_skill_dirs=bundle.extra_skill_dirs,
        extra_plugin_roots=bundle.extra_plugin_roots,
        include_project_memory=bundle.include_project_memory,
    )
    bundle.engine.set_system_prompt(system_prompt)
    session_start = __import__("time").time()
    try:
        async for event in bundle.engine.submit_message(user_message or line):
            await render_event(event)
    except MaxTurnsExceeded as exc:
        await print_system(f"Stopped after {exc.max_turns} turns (max_turns).")
        pending = _format_pending_tool_results(bundle.engine.messages)
        if pending:
            await print_system(pending)
        bundle.session_backend.save_snapshot(
            cwd=bundle.cwd,
            model=settings.model,
            system_prompt=system_prompt,
            messages=bundle.engine.messages,
            usage=bundle.engine.total_usage,
            session_id=bundle.session_id,
            tool_metadata=bundle.engine.tool_metadata,
        )
        _save_session_memory_best_effort(bundle, latest_user_prompt, "Max turns exceeded")
        sync_app_state(bundle)
        return True
    session_duration = __import__("time").time() - session_start

    # ── Learn from this session (async, best-effort) ─────
    learned_wf = None
    if bundle.workflow_learner and len(bundle.engine.messages) >= 4:
        try:
            learned_wf = bundle.workflow_learner.learn_from_session(
                bundle.engine.messages,
                user_input=latest_user_prompt,
                model=settings.model,
                duration_seconds=session_duration,
                tool_registry=bundle.tool_registry,
            )
        except Exception:
            pass  # learning is best-effort, never block user flow
    # ──────────────────────────────────────────────────────

    # ── Save session memory (best-effort) ────────────────
    try:
        from daoyi.kernel.memory import MemoryManager
        mem = MemoryManager()
        outcome = ""
        if bundle.workflow_executor and bundle.workflow_executor.memory:
            mem = bundle.workflow_executor.memory
        last_msg = bundle.engine.messages[-1] if bundle.engine.messages else None
        if last_msg:
            from daoyi.engine.messages import TextBlock
            for block in (last_msg.content if hasattr(last_msg, 'content') else []):
                if isinstance(block, TextBlock) and block.text:
                    outcome = block.text[:500]
                    break
        if bundle.workflow_executor and hasattr(bundle.workflow_executor, '_last_intent'):
            outcome = outcome or str(getattr(bundle.workflow_executor, '_last_intent', ''))
        mem.save_session_memory(
            session_id=bundle.session_id,
            user_intent=latest_user_prompt or "",
            outcome_summary=outcome or "Session completed",
        )
    except Exception:
        pass  # session memory is best-effort
    # ──────────────────────────────────────────────────────

    # ── Suggest workflow creation if no match existed ─────
    if not bundle.workflow_registry or not bundle.workflow_registry.find(latest_user_prompt):
        if bundle.workflow_learner and len(bundle.engine.messages) >= 4:
            keywords = bundle.workflow_learner._classifier.suggest_triggers(latest_user_prompt)
            if keywords:
                await print_system(
                    f"\n💡 检测到这是一次多步操作。输入 /workflow list 查看已注册的模板。"
                    f"下次类似任务可自动用 workflow 加速。"
                )
    # ──────────────────────────────────────────────────────

    bundle.session_backend.save_snapshot(
        cwd=bundle.cwd,
        model=settings.model,
        system_prompt=system_prompt,
        messages=bundle.engine.messages,
        usage=bundle.engine.total_usage,
        session_id=bundle.session_id,
        tool_metadata=bundle.engine.tool_metadata,
    )
    sync_app_state(bundle)
    return True


async def _render_command_result(
    result: CommandResult,
    print_system: SystemPrinter,
    clear_output: ClearHandler,
    render_event: StreamRenderer | None = None,
) -> None:
    if result.clear_screen:
        await clear_output()
    if result.replay_messages and render_event is not None:
        # Replay restored conversation messages as transcript events
        from daoyi.api.usage import UsageSnapshot

        await clear_output()
        await print_system("Session restored:")
        for msg in result.replay_messages:
            if msg.role == "user":
                await print_system(f"> {msg.text}")
            elif msg.role == "assistant" and msg.text.strip():
                await render_event(AssistantTextDelta(text=msg.text))
                await render_event(AssistantTurnComplete(message=msg, usage=UsageSnapshot()))
    if result.message and not result.replay_messages:
        await print_system(result.message)


def _save_session_memory_best_effort(
    bundle: RuntimeBundle,
    user_intent: str | None,
    outcome_summary: str,
) -> None:
    """Save session memory, best-effort (never blocks)."""
    try:
        from daoyi.kernel.memory import MemoryManager
        mem = MemoryManager()
        if bundle.workflow_executor and bundle.workflow_executor.memory:
            mem = bundle.workflow_executor.memory
        mem.save_session_memory(
            session_id=bundle.session_id,
            user_intent=(user_intent or ""),
            outcome_summary=outcome_summary,
        )
    except Exception:
        pass
