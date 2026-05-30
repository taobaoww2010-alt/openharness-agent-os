"""OpenHarness channels subsystem.

Provides a message-bus architecture for integrating chat platforms
(Telegram, Discord, Slack, etc.) with the OpenHarness query engine.

Usage::

    from daoyi.channels import BaseChannel, ChannelManager, MessageBus
"""

from daoyi.channels.bus.events import InboundMessage, OutboundMessage
from daoyi.channels.bus.queue import MessageBus
from daoyi.channels.impl.base import BaseChannel
from daoyi.channels.impl.manager import ChannelManager

__all__ = [
    "BaseChannel",
    "ChannelManager",
    "InboundMessage",
    "MessageBus",
    "OutboundMessage",
]
