"""
Claude (Anthropic) LLM provider.

Implements the `LLMProvider` protocol via the official `anthropic` SDK. The import is lazy so the rest
of the package (and the app) loads even when `anthropic` isn't installed — it's only needed once the
user actually selects Claude and runs a request.

Model IDs (per the anthropic SDK guidance): `claude-opus-4-8` (default, most capable), `claude-sonnet-4-6`
(balanced), `claude-haiku-4-5` (fast/cheap). The Messages API takes the system prompt as a separate
`system=` argument; `response.content` is a list of blocks — we concatenate the text blocks.
"""

from __future__ import annotations

from typing import Any, Iterator, cast

from .base import ChatMessage

_DEFAULT_MAX_TOKENS = 16000


def _max_tokens(opts: dict[str, object]) -> int:
    value = opts.get("max_tokens", _DEFAULT_MAX_TOKENS)
    return int(value) if isinstance(value, (int, float, str)) else _DEFAULT_MAX_TOKENS


class ClaudeProvider:
    def __init__(self, api_key: str, model: str) -> None:
        import anthropic  # lazy

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    @staticmethod
    def _to_messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in messages]

    def complete(self, system: str, messages: list[ChatMessage], **opts: object) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=_max_tokens(opts),
            system=system,
            messages=cast(Any, self._to_messages(messages)),
        )
        return "".join(block.text for block in resp.content if block.type == "text")

    def stream(self, system: str, messages: list[ChatMessage], **opts: object) -> Iterator[str]:
        with self._client.messages.stream(
            model=self.model,
            max_tokens=_max_tokens(opts),
            system=system,
            messages=cast(Any, self._to_messages(messages)),
        ) as stream:
            yield from stream.text_stream
