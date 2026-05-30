"""Message bus module for decoupled channel-agent communication."""

from daoyi.channels.bus.events import InboundMessage, OutboundMessage
from daoyi.channels.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
