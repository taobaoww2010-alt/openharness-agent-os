"""ChannelBridge: bridges the MessageBus to the model for channel chat replies.

Intent-based routing with minimal tool set::

  1. Small model classifies intent (chat / search / tool / code / ...)
  2. Only intent-relevant tools are sent to the remote API (2-5 tools max)
  3. Tool calls are executed client-side via the ToolRegistry
  4. Results are fed back to the model for the final reply
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from daoyi.channels.bus.events import InboundMessage, OutboundMessage
from daoyi.channels.bus.queue import MessageBus
from daoyi.llm.small_model import SmallModelClient

if TYPE_CHECKING:
    from daoyi.api.client import SupportsStreamingMessages
    from daoyi.tools.base import ToolRegistry

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
_log_file = logging.FileHandler("/tmp/daoyi-channel.log", mode="a")
_log_file.setLevel(logging.DEBUG)
_log_file.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
logger.addHandler(_log_file)

# ── Intent → minimal tool names ──
_INTENT_TOOLS: dict[str, list[str]] = {
    "search": ["web_search", "web_fetch"],
    "code": ["bash", "read_file", "write_file", "edit_file", "glob", "grep"],
    "tool": ["bash", "skill_executor", "glob", "grep"],
    "code_review": ["read_file", "grep", "glob"],
    "file_ops": ["read_file", "write_file", "edit_file", "glob", "grep"],
}

_CHAT_TIMEOUT = 60.0
_CHAT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant named DaoYi. "
    "Answer concisely in the same language as the user. "
    "When answering with search results, cite your sources. "
    "You have access to tools — use them when needed."
)


class ChannelBridge:
    """Bridges inbound channel messages to the model with intent-filtered tools.

    1. Small model classifies the user's intent
    2. Only intent-relevant tools are sent to the remote API (avoids server hang
       from 43-tool schema while still enabling tool execution)
    3. Tool calls are executed client-side; results are fed back to the model
    """

    def __init__(
        self,
        *,
        api_client: SupportsStreamingMessages,
        model: str,
        bus: MessageBus,
        tool_registry: Any | None = None,
        cwd: str | None = None,
    ) -> None:
        self._api_client = api_client
        self._model = model
        self._bus = bus
        self._tool_registry = tool_registry
        self._cwd = cwd or "."
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="channel-bridge")
        logger.info("ChannelBridge started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("ChannelBridge stopped")

    async def run(self) -> None:
        self._running = True
        try:
            await self._loop()
        finally:
            self._running = False

    async def _loop(self) -> None:
        logger.info("ChannelBridge loop started (waiting for inbound messages)")
        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self._bus.consume_inbound(),
                    timeout=1.0,
                )
                logger.info(
                    "ChannelBridge processing message from %s/%s: %.50s",
                    msg.channel, msg.chat_id, msg.content,
                )
                await self._handle(msg)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("ChannelBridge: unhandled error processing message")

    async def _handle(self, msg: InboundMessage) -> None:
        logger.info(
            "ChannelBridge received from %s/%s: %.80s",
            msg.channel, msg.chat_id, msg.content,
        )

        reply_text = await self._get_reply(msg.content)
        if not reply_text:
            logger.info("ChannelBridge: empty reply, skipping publish")
            return

        outbound = OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=reply_text,
            metadata={"_session_key": msg.session_key},
        )
        await self._bus.publish_outbound(outbound)
        logger.info(
            "ChannelBridge published reply to %s/%s (%d chars): %.60s",
            msg.channel, msg.chat_id, len(reply_text), reply_text,
        )

    # ------------------------------------------------------------------
    # Reply generation: classify → route
    # ------------------------------------------------------------------

    async def _get_reply(self, text: str) -> str:
        intent = await self._classify_intent(text)
        logger.info("ChannelBridge: classified intent='%s' for: %.50s", intent, text)
        reply = await self._try_chat_reply(text, intent=intent)
        if reply:
            return reply
        return "[Error: chat reply failed]"

    # ------------------------------------------------------------------
    # Intent classification
    # ------------------------------------------------------------------

    async def _classify_intent(self, text: str) -> str:
        if not SmallModelClient.is_available():
            logger.debug("ChannelBridge: small model not available, defaulting to 'chat'")
            return "chat"
        try:
            model = SmallModelClient.get_instance()
            loop = asyncio.get_running_loop()
            intent = await loop.run_in_executor(None, model.classify, text)
            if intent:
                logger.info("ChannelBridge: small model classified as '%s'", intent)
                return intent
        except Exception:
            logger.exception("ChannelBridge: classification failed, defaulting to 'chat'")
        return "chat"

    # ------------------------------------------------------------------
    # Reply: intent-filtered tools → API → execute → final reply
    # ------------------------------------------------------------------

    async def _try_chat_reply(self, text: str, *, intent: str = "chat") -> str:
        from daoyi.api.client import (
            ApiMessageRequest,
            ApiMessageCompleteEvent,
            ApiTextDeltaEvent,
        )
        from daoyi.engine.messages import ConversationMessage, TextBlock

        # 1. Build filtered tool schema from intent
        tool_names = _INTENT_TOOLS.get(intent, [])
        tools = self._build_tool_schemas(tool_names)

        # 2. Single request — if tools are called, results replace 2nd API round
        request = ApiMessageRequest(
            model=self._model,
            messages=[
                ConversationMessage(
                    role="user",
                    content=[TextBlock(text=text)],
                ),
            ],
            system_prompt=_CHAT_SYSTEM_PROMPT,
            max_tokens=4096,
            tools=tools,
        )

        parts: list[str] = []
        final_event: ApiMessageCompleteEvent | None = None

        logger.info(
            "ChannelBridge: → remote API (intent=%s, tools=%d, timeout=%.1fs)",
            intent, len(tools), _CHAT_TIMEOUT,
        )
        stream = self._api_client.stream_message(request)
        try:
            async with asyncio.timeout(_CHAT_TIMEOUT):
                async for event in stream:
                    if isinstance(event, ApiTextDeltaEvent):
                        parts.append(event.text)
                    elif isinstance(event, ApiMessageCompleteEvent):
                        final_event = event
        except asyncio.TimeoutError:
            logger.warning("ChannelBridge: api timed out")
            return ""
        except Exception:
            logger.exception("ChannelBridge: api failed")
            return ""

        if final_event is None:
            return "".join(parts).strip() or ""

        # Check for tool calls
        tool_blocks = [
            b for b in final_event.message.content
            if getattr(b, "type", None) == "tool_use"
            or (hasattr(b, "name") and hasattr(b, "input"))
        ]
        if not tool_blocks:
            reply = "".join(parts).strip()
            if reply:
                logger.info("ChannelBridge: text reply (%.60s)", reply)
            return reply

        # Execute tool calls client-side — no 2nd API round trip
        logger.info(
            "ChannelBridge: executing %d tool call(s) -> direct reply",
            len(tool_blocks),
        )
        tool_results = await self._execute_tool_calls(tool_blocks)
        reply = self._format_tool_reply(tool_results)
        logger.info("ChannelBridge: tool-based reply (%d chars)", len(reply))
        return reply

    # ------------------------------------------------------------------
    # Helpers: tool schema, execution, reply formatting
    # ------------------------------------------------------------------

    def _format_tool_reply(self, tool_results: list[dict[str, Any]]) -> str:
        """Format tool execution results into a reply string — no 2nd API call."""
        if not tool_results:
            return ""

        lines: list[str] = []
        for r in tool_results:
            content = r.get("content", "") or ""
            if r.get("is_error"):
                lines.append(content or "查询出错，请稍后重试。")
            elif content.strip():
                lines.append(content.strip())
            else:
                lines.append("未找到相关结果。")

        return "\n\n".join(lines)

    def _build_tool_schemas(self, tool_names: list[str]) -> list[dict[str, Any]]:
        """Return API schema only for the named tools."""
        if not self._tool_registry or not tool_names:
            return []
        schemas = []
        for name in tool_names:
            tool = self._tool_registry.get(name)
            if tool is not None:
                schemas.append(tool.to_api_schema())
        return schemas

    async def _execute_tool_calls(
        self,
        tool_blocks: list[Any],
    ) -> list[dict[str, Any]]:
        """Execute tool calls and return ToolResultBlock-compatible dicts."""
        if not self._tool_registry:
            return []

        from daoyi.tools.base import ToolExecutionContext

        cwd_path = Path(self._cwd) if self._cwd else Path.cwd()
        results: list[dict[str, Any]] = []
        for block in tool_blocks:
            tool_id = getattr(block, "id", "") or ""
            tool_name = getattr(block, "name", "") or ""
            tool_input = getattr(block, "input", None) or {}
            if not tool_name:
                continue

            tool = self._tool_registry.get(tool_name)
            if tool is None:
                logger.warning("ChannelBridge: unknown tool '%s', skipping", tool_name)
                results.append({
                    "tool_use_id": tool_id,
                    "type": "tool_result",
                    "content": f"Error: unknown tool '{tool_name}'",
                    "is_error": True,
                })
                continue

            try:
                parsed = tool.input_model.model_validate(tool_input)
                exec_context = ToolExecutionContext(cwd=cwd_path)
                tool_result = await tool.execute(parsed, exec_context)
                results.append({
                    "tool_use_id": tool_id,
                    "type": "tool_result",
                    "content": tool_result.output,
                    "is_error": tool_result.is_error,
                })
                logger.info(
                    "ChannelBridge: tool '%s' done (%d chars)",
                    tool_name, len(tool_result.output),
                )
            except Exception as exc:
                logger.exception("ChannelBridge: tool '%s' failed", tool_name)
                results.append({
                    "tool_use_id": tool_id,
                    "type": "tool_result",
                    "content": f"Error: {exc}",
                    "is_error": True,
                })
        return results
