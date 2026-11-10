"""Model and endpoint capabilities shared by request construction and settings UI."""
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

ReasoningLevel = Literal['low', 'high']


@dataclass(frozen=True)
class ReasoningControl:
    mode: Literal['effort', 'switch', 'unsupported']
    protocol: Literal['none', 'enable_thinking', 'thinking', 'adaptive', 'template'] = 'none'
    can_disable: bool = True
    note: str = ''

    @property
    def supported(self) -> bool:
        return self.mode != 'unsupported'

    def public(self, level: ReasoningLevel) -> dict[str, object]:
        description = self.note
        if not description:
            description = ('发送原生 low / high 推理强度；高强度通常更慢，不保证更准确。'
                           if self.mode == 'effort' else
                           '此接口仅支持思考开关：低强度使用标准回复，高强度开启深度思考。')
        effective = '低强度思考' if level == 'low' else '高强度思考'
        if self.mode == 'switch':
            effective = '标准回复 · 思考已关闭' if level == 'low' else '深度思考已开启'
        if not self.supported:
            effective = '服务端默认 · 强度不可调'
        return {'supported': self.supported, 'mode': self.mode,
                'description': description, 'effective_label': effective}

    def request_options(self, level: ReasoningLevel, *, disable: bool = False) -> dict[str, Any]:
        if not self.supported:
            return {}
        enabled = (level == 'high' if self.mode == 'switch' else True)
        if disable and self.can_disable:
            enabled = False
        body: dict[str, Any] = {}
        if self.protocol == 'enable_thinking':
            body['enable_thinking'] = enabled
        elif self.protocol in ('thinking', 'adaptive'):
            body['thinking'] = {'type': ('adaptive' if self.protocol == 'adaptive' else 'enabled')
                               if enabled else 'disabled'}
        elif self.protocol == 'template':
            body['chat_template_kwargs'] = {'enable_thinking': enabled}
        options: dict[str, Any] = {}
        if body:
            options['extra_body'] = body
        if self.mode == 'effort' and enabled:
            options['reasoning_effort'] = 'low' if disable else level
        return options


def reasoning_control(provider: str, model: str, base_url: str) -> ReasoningControl:
    unsupported = ReasoningControl('unsupported', note=
        '尚未确认此接口和模型的强度参数，按服务端默认运行；不会发送可能无效的思考开关。')
    fixed = ReasoningControl('unsupported', note=
        '此版本固定思考，不支持低／高强度切换。需要提速时请换用支持调节强度的版本。')
    if provider != 'openai_compat':
        return ReasoningControl('unsupported', note=
            '当前 Anthropic Messages 接口未适配强度调节；国内模型建议使用对应的 OpenAI 兼容接口。')
    host = (urlsplit(base_url).hostname or '').lower()
    name = model.lower().split('/')[-1]

    def domain(value: str) -> bool:
        return host == value or host.endswith('.' + value)

    if domain('aliyuncs.com'):
        if name.startswith('glm-5.3'):
            return ReasoningControl('effort', can_disable=False)
        if name.startswith(('glm-5', 'glm-4.7', 'glm-4.6', 'glm-4.5')):
            return ReasoningControl('effort' if name.startswith('glm-5') else 'switch', 'enable_thinking')
        if name.startswith(('deepseek-v4.1', 'deepseek-v4-flash-0731', 'deepseek-v4-pro-0813')):
            return ReasoningControl('effort', 'enable_thinking')
        if name.startswith(('deepseek-v4', 'deepseek-v3')):
            return ReasoningControl('switch', 'enable_thinking')
        if name.startswith(('minimax-m3.1', 'kimi-k3')):
            return ReasoningControl('effort', can_disable=False)
        if name.startswith(('minimax-m2', 'kimi-k2.7', 'deepseek-r1', 'qwq')) or 'thinking' in name:
            return fixed
        if name.startswith(('qwen3.8-2.4t', 'qwen3.7-max-preview', 'qwen3.7-max-2026-05-17')):
            return fixed
        if name.startswith(('qwen3', 'qwen-plus', 'qwen-max', 'qwen-flash', 'kimi-k2.5', 'kimi-k2.6')):
            return ReasoningControl('switch', 'enable_thinking')
        return unsupported
    if host == 'api.deepseek.com':
        if name.startswith(('deepseek-v4', 'deepseek-flash', 'deepseek-pro')):
            return ReasoningControl('effort', 'thinking')
        if name in ('deepseek-chat', 'deepseek-reasoner'):
            return ReasoningControl('switch', 'thinking')
        return unsupported
    if domain('bigmodel.cn') or host == 'api.z.ai':
        if name.startswith('glm-5.3'):
            return ReasoningControl('effort', 'thinking', can_disable=False)
        if name.startswith(('glm-5', 'glm-4.7', 'glm-4.6', 'glm-4.5')):
            return ReasoningControl('switch', 'thinking')
        return unsupported
    if host in ('api.moonshot.cn', 'api.moonshot.ai'):
        if name.startswith('kimi-k3'):
            return ReasoningControl('effort', can_disable=False)
        if name.startswith(('kimi-k2.5', 'kimi-k2.6')):
            return ReasoningControl('switch', 'thinking')
        return fixed if name.startswith(('kimi-k2.7', 'kimi-k2-thinking')) else unsupported
    if host in ('api.minimax.cn', 'api.minimax.io', 'api.minimaxi.com'):
        if name.startswith('minimax-m3.1'):
            return ReasoningControl('effort', can_disable=False)
        if name == 'minimax-m3':
            return ReasoningControl('switch', 'adaptive')
        return fixed if name.startswith('minimax-m2') else unsupported
    if domain('volces.com'):
        if name.startswith(('doubao-seed-2-', 'doubao-seed-evolving', 'doubao-seed-character',
                            'deepseek-v4-1', 'deepseek-v4-pro-ga', 'deepseek-v4-flash-ga')):
            return ReasoningControl('effort', 'thinking')
        if name.startswith(('glm-5-3', 'kimi-k3')):
            return ReasoningControl('effort', can_disable=False)
        if name.startswith(('doubao-seed-1-', 'doubao-1-5', 'glm-5-2')):
            return ReasoningControl('switch', 'thinking')
        return ReasoningControl('unsupported', note=
            '此方舟模型或部署别名尚无法确认强度能力；请使用支持思考调节的公开模型 ID。')
    if host == 'api.hunyuan.cloud.tencent.com':
        if name.startswith('hunyuan-a13b'):
            return ReasoningControl('switch', 'enable_thinking')
        return ReasoningControl('unsupported', note=
            '此混元版本尚未确认支持强度切换；旧版 T1／Turbo 不使用通用思考开关。')
    if domain('baidubce.com'):
        return ReasoningControl('unsupported', note=
            '此文心／千帆接口的强度参数尚未确认，暂按服务端默认运行，避免发送无效参数。')
    # Private Qwen services already use the standard chat-template switch.
    if 'qwen3' in name and not any(domain(d) for d in ('siliconflow.cn', 'siliconflow.com')):
        if 'thinking' in name or 'instruct' in name:
            return fixed if 'thinking' in name else unsupported
        return ReasoningControl('switch', 'template', note=
            '自建 Qwen 使用模板开关：低强度关闭思考，高强度开启；服务端需支持 chat_template_kwargs。')
    return unsupported
