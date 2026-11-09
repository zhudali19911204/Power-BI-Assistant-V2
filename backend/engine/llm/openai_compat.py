"""
OpenAI-compatible LLM provider.

Talks to any endpoint that speaks the OpenAI Chat Completions API (OpenAI itself, Azure-OpenAI, local
servers like Ollama/LM Studio, vLLM, etc.) via the `openai` SDK. `base_url` selects the endpoint. The
import is lazy, mirroring `claude.py`. Here the system prompt is the first message in the list (OpenAI
has no separate `system=` argument).
"""

from __future__ import annotations

from typing import Any, Iterator, cast

from .base import ChatMessage

_DEFAULT_MAX_TOKENS = 16000


def _max_tokens(opts: dict[str, object]) -> int:
    value = opts.get("max_tokens", _DEFAULT_MAX_TOKENS)
    return int(value) if isinstance(value, (int, float, str)) else _DEFAULT_MAX_TOKENS


class OpenAICompatProvider:
    def __init__(self, api_key: str, model: str, base_url: str = "", *, enable_thinking: bool | None = None) -> None:
        from openai import OpenAI  # lazy

        self._client = OpenAI(api_key=api_key, base_url=base_url or None)
        self.model = model
        self.enable_thinking = enable_thinking

    @staticmethod
    def _to_messages(system: str, messages: list[ChatMessage]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        if system:
            out.append({"role": "system", "content": system})
        out.extend({"role": m.role, "content": m.content} for m in messages)
        return out

    @staticmethod
    def _extra_body(opts: dict[str, object], default_thinking=None) -> dict[str, Any]:
        """Qwen self-hosting through llama.cpp/vLLM uses the chat template switch."""
        extra: dict[str, Any] = {}
        thinking = opts.get('enable_thinking', default_thinking)
        if thinking is not None:
            extra['chat_template_kwargs'] = {'enable_thinking': bool(thinking)}
        return extra

    def complete(self, system: str, messages: list[ChatMessage], **opts: object) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=cast(Any, self._to_messages(system, messages)),
            max_tokens=_max_tokens(opts),
            extra_body=self._extra_body(opts, self.enable_thinking) or None,
        )
        return (resp.choices[0].message.content or "") if resp.choices else ""

    def stream(self, system: str, messages: list[ChatMessage], **opts: object) -> Iterator[str]:
        for event in self.stream_events(system, messages, **opts):
            if event['type'] == 'delta':
                yield event['text']

    def stream_events(self, system: str, messages: list[ChatMessage], **opts: object):
        stream = self._client.chat.completions.create(
            model=self.model,
            messages=cast(Any, self._to_messages(system, messages)),
            max_tokens=_max_tokens(opts),
            extra_body=self._extra_body(opts, self.enable_thinking) or None,
            stream=True,
        )
        try:
            yield {'type': 'status', 'text': '模型已连接，正在等待正文回复'}
            reasoning_reported = False
            for chunk in cast(Any, stream):
                # Mark genuine upstream frames, including reasoning-only output, without
                # forwarding private reasoning text to the UI or persisting it.
                yield {'type': 'activity'}
                # Usage-only frames and reasoning-only frames have no final-answer content.
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if not reasoning_reported and (getattr(delta, 'reasoning_content', None) or getattr(delta, 'reasoning', None)):
                    # Report activity only, never expose the model's private reasoning text.
                    reasoning_reported = True
                    yield {'type': 'status', 'text': '模型正在分析，等待正文输出'}
                if delta.content:
                    yield {'type': 'delta', 'text': delta.content}
        finally:
            stream.close()
