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
from .diagnostics import DEFAULT_MAX_TOKENS, token_usage
from ...cancellation import check_cancelled, close_on_cancel

_DEFAULT_MAX_TOKENS = DEFAULT_MAX_TOKENS


def _max_tokens(opts: dict[str, object]) -> int:
    value = opts.get("max_tokens", _DEFAULT_MAX_TOKENS)
    return int(value) if isinstance(value, (int, float, str)) else _DEFAULT_MAX_TOKENS


class ClaudeProvider:
    def __init__(self, api_key: str, model: str) -> None:
        import anthropic  # lazy

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    @staticmethod
    def _to_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out=[]
        for m in messages:
            content: Any=m.content
            if m.images:
                content=[{'type':'image','source':{'type':'base64','media_type':img.mime_type,'data':img.data}}
                         for img in m.images]+[{'type':'text','text':m.content}]
            out.append({'role':m.role,'content':content})
        return out

    def complete(self, system: str, messages: list[ChatMessage], **opts: object) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=_max_tokens({'max_tokens': getattr(self, 'max_tokens', DEFAULT_MAX_TOKENS), **opts}),
            system=system,
            messages=cast(Any, self._to_messages(messages)),
        )
        return "".join(block.text for block in resp.content if block.type == "text")

    def stream(self, system: str, messages: list[ChatMessage], **opts: object) -> Iterator[str]:
        for event in self.stream_events(system, messages, **opts):
            if event['type'] == 'delta':
                yield event['text']

    def stream_events(self, system: str, messages: list[ChatMessage], **opts: object):
        check_cancelled()
        with self._client.messages.stream(
            model=self.model,
            max_tokens=_max_tokens({'max_tokens': getattr(self, 'max_tokens', DEFAULT_MAX_TOKENS), **opts}),
            system=system,
            messages=cast(Any, self._to_messages(messages)),
        ) as stream:
            unregister = close_on_cancel(stream.close) if callable(getattr(stream, 'close', None)) else lambda: None
            try:
                yield from self._stream_events(stream)
            finally:
                unregister()

    @staticmethod
    def _stream_events(stream):
        yield {'type': 'status', 'text': '模型已连接，正在等待正文回复'}
        yield {'type': 'metadata', 'finish_reason_supported': True}
        reasoning_reported = False
        for event in stream:
            check_cancelled()
            yield {'type': 'activity'}
            if event.type == 'message_start':
                yield {'type': 'metadata', 'usage': token_usage(event.message.usage),
                       'request_id': event.message.id}
            elif event.type == 'message_delta':
                yield {'type': 'metadata', 'usage': token_usage(getattr(event, 'usage', None)),
                       'finish_reason': getattr(event.delta, 'stop_reason', None)}
            if event.type != 'content_block_delta':
                continue
            delta = event.delta
            if delta.type == 'text_delta' and delta.text:
                yield {'type': 'delta', 'text': delta.text}
            elif delta.type == 'thinking_delta' and not reasoning_reported:
                reasoning_reported = True
                yield {'type': 'metadata', 'reasoning_received': True}
                yield {'type': 'status', 'text': '模型正在分析，等待正文输出'}
