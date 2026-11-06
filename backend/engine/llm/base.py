"""
LLM abstraction.

The whole product talks to language models *only* through `LLMProvider`. Concrete providers (Claude,
OpenAI-compatible, …) implement this interface in M3; switching vendor is then a config change, never a
business-code change. Defining it as a `Protocol` keeps providers decoupled — a class is a valid
`LLMProvider` if it has the right methods, no inheritance required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant"]


@dataclass
class ChatMessage:
    """One turn in a conversation. System guidance is passed separately (see `complete`)."""

    role: Role
    content: str


def user(content: str) -> ChatMessage:
    """Convenience constructor for a single user message."""
    return ChatMessage(role="user", content=content)


@runtime_checkable
class LLMProvider(Protocol):
    """Provider-agnostic chat interface.

    `system` is the system prompt (e.g. `DAX_SYSTEM_PROMPT`); `messages` is the conversation.
    `**opts` carries provider-tolerant knobs such as `temperature` or `max_tokens` — implementations
    should ignore options they don't understand rather than error, so callers stay portable.
    """

    def complete(self, system: str, messages: list[ChatMessage], **opts: object) -> str:
        """Return the full completion as a string."""
        ...

    def stream(self, system: str, messages: list[ChatMessage], **opts: object) -> Iterator[str]:
        """Yield completion text incrementally (for responsive UIs)."""
        ...
