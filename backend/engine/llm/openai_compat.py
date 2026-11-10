"""
OpenAI-compatible LLM provider.

Talks to any endpoint that speaks the OpenAI Chat Completions API (OpenAI itself, Azure-OpenAI, local
servers like Ollama/LM Studio, vLLM, etc.) via the `openai` SDK. `base_url` selects the endpoint. The
import is lazy, mirroring `claude.py`. Here the system prompt is the first message in the list (OpenAI
has no separate `system=` argument).
"""

from __future__ import annotations

from typing import Any, Iterator, cast
from urllib.parse import urlsplit

from .base import ChatMessage
from .diagnostics import DEFAULT_MAX_TOKENS, token_usage
from .reasoning import ReasoningLevel, reasoning_control
from ...cancellation import check_cancelled, close_on_cancel

_DEFAULT_MAX_TOKENS = DEFAULT_MAX_TOKENS


def _max_tokens(opts: dict[str, object]) -> int:
    value = opts.get("max_tokens", _DEFAULT_MAX_TOKENS)
    return int(value) if isinstance(value, (int, float, str)) else _DEFAULT_MAX_TOKENS


class OpenAICompatProvider:
    def __init__(self, api_key: str, model: str, base_url: str = "", *, enable_thinking: bool | None = None,
                 reasoning_level: ReasoningLevel | None = None) -> None:
        from openai import OpenAI  # lazy

        self._client = OpenAI(api_key=api_key, base_url=base_url or None)
        self.model = model
        self.base_url = base_url
        self.enable_thinking = enable_thinking
        self.reasoning_level = reasoning_level

    @staticmethod
    def _to_messages(system: str, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            content: Any=m.content
            if m.images:
                content=[{'type':'text','text':m.content}]+[{'type':'image_url','image_url':{
                    'url':f'data:{img.mime_type};base64,{img.data}','detail':'auto'}} for img in m.images]
            out.append({'role':m.role,'content':content})
        return out

    @staticmethod
    def _extra_body(opts: dict[str, object], default_thinking=None, base_url='') -> dict[str, Any]:
        """Match the endpoint's thinking protocol; do not send Qwen flags to Alibaba."""
        extra: dict[str, Any] = {}
        thinking = opts.get('enable_thinking', default_thinking)
        if thinking is not None:
            host = (urlsplit(base_url).hostname or '').lower()
            if host == 'aliyuncs.com' or host.endswith('.aliyuncs.com'):
                extra['enable_thinking'] = bool(thinking)
            elif host == 'api.deepseek.com':
                extra['thinking'] = {'type': 'enabled' if thinking else 'disabled'}
            else:
                extra['chat_template_kwargs'] = {'enable_thinking': bool(thinking)}
        return extra

    @property
    def can_disable_thinking(self):
        if getattr(self, 'reasoning_level', None) is not None:
            control = reasoning_control('openai_compat', self.model, self.base_url)
            return control.supported and control.can_disable
        host = (urlsplit(getattr(self, 'base_url', '')).hostname or '').lower()
        return (host == 'api.deepseek.com' or host == 'aliyuncs.com' or host.endswith('.aliyuncs.com')
                or 'qwen' in self.model.lower() or self.enable_thinking is not None)

    @property
    def can_reduce_reasoning(self):
        return reasoning_control('openai_compat', self.model, getattr(self, 'base_url', '')).supported

    def _request_options(self, opts: dict[str, object]) -> dict[str, Any]:
        level = opts.get('reasoning_level', getattr(self, 'reasoning_level', None))
        if level is None:
            return {'extra_body': self._extra_body(opts, self.enable_thinking, getattr(self, 'base_url', '')) or None}
        if level not in ('low', 'high'):
            raise ValueError('思考强度仅支持 low 或 high')
        selected: ReasoningLevel = 'high' if level == 'high' else 'low'
        control = reasoning_control('openai_compat', self.model, getattr(self, 'base_url', ''))
        return control.request_options(selected, disable=opts.get('enable_thinking') is False)

    def complete(self, system: str, messages: list[ChatMessage], **opts: object) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=cast(Any, self._to_messages(system, messages)),
            max_tokens=_max_tokens({'max_tokens': getattr(self, 'max_tokens', DEFAULT_MAX_TOKENS), **opts}),
            **self._request_options(opts),
        )
        return (resp.choices[0].message.content or "") if resp.choices else ""

    def stream(self, system: str, messages: list[ChatMessage], **opts: object) -> Iterator[str]:
        for event in self.stream_events(system, messages, **opts):
            if event['type'] == 'delta':
                yield event['text']

    def stream_events(self, system: str, messages: list[ChatMessage], **opts: object):
        check_cancelled()
        stream = self._client.chat.completions.create(
            model=self.model,
            messages=cast(Any, self._to_messages(system, messages)),
            max_tokens=_max_tokens({'max_tokens': getattr(self, 'max_tokens', DEFAULT_MAX_TOKENS), **opts}),
            **self._request_options(opts),
            stream=True,
            stream_options={'include_usage': True},
        )
        unregister = close_on_cancel(stream.close)
        try:
            yield {'type': 'status', 'text': '模型已连接，正在等待正文回复'}
            yield {'type': 'metadata', 'finish_reason_supported': True}
            reasoning_reported = False
            for chunk in cast(Any, stream):
                check_cancelled()
                # Mark genuine upstream frames, including reasoning-only output, without
                # forwarding private reasoning text to the UI or persisting it.
                yield {'type': 'activity'}
                metadata = {'type': 'metadata', 'request_id': getattr(chunk, 'id', None),
                            'usage': token_usage(getattr(chunk, 'usage', None))}
                if chunk.choices:
                    metadata['finish_reason'] = getattr(chunk.choices[0], 'finish_reason', None)
                yield metadata
                # Usage-only frames and reasoning-only frames have no final-answer content.
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if not reasoning_reported and (getattr(delta, 'reasoning_content', None) or getattr(delta, 'reasoning', None)):
                    # Report activity only, never expose the model's private reasoning text.
                    reasoning_reported = True
                    yield {'type': 'metadata', 'reasoning_received': True}
                    yield {'type': 'status', 'text': '模型正在分析，等待正文输出'}
                if delta.content:
                    yield {'type': 'delta', 'text': delta.content}
        finally:
            unregister()
            stream.close()
