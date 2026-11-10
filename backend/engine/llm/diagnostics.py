"""Completion metadata only. Never retain prompts, images, or private reasoning."""
from dataclasses import dataclass, field, asdict
import re

DEFAULT_MAX_TOKENS = 16000


def token_usage(value):
    def get(obj, name):
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

    result = {}
    for target, names in (
        ('prompt_tokens', ('prompt_tokens', 'input_tokens')),
        ('completion_tokens', ('completion_tokens', 'output_tokens')),
        ('total_tokens', ('total_tokens',)),
    ):
        for name in names:
            number = get(value, name)
            if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                result[target] = number
                break
    details = get(value, 'completion_tokens_details') or get(value, 'output_tokens_details')
    reasoning = get(details, 'reasoning_tokens')
    if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0:
        result['reasoning_tokens'] = reasoning
    return result


@dataclass
class GenerationReport:
    max_tokens: int = DEFAULT_MAX_TOKENS
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)
    request_id: str | None = None
    reasoning_received: bool = False
    finish_reason_supported: bool = False
    stream_ended: bool = False
    output_chars: int = 0
    seconds: float = 0
    error_type: str | None = None

    def update(self, metadata):
        reason = metadata.get('finish_reason')
        if reason:
            reason = {'max_tokens': 'length', 'end_turn': 'stop', 'stop_sequence': 'stop',
                      'tool_use': 'tool_calls', 'refusal': 'content_filter'}.get(reason, reason)
            self.finish_reason = reason if reason in {
                'stop', 'length', 'content_filter', 'tool_calls', 'aborted',
                'insufficient_system_resource', 'pause_turn',
            } else 'unknown'
        self.usage.update(token_usage(metadata.get('usage')))
        # Providers have already normalized usage, including reasoning_tokens.
        usage = metadata.get('usage')
        if isinstance(usage, dict):
            reasoning = usage.get('reasoning_tokens')
            if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0:
                self.usage['reasoning_tokens'] = reasoning
        request_id = metadata.get('request_id')
        if isinstance(request_id, str) and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,160}', request_id):
            self.request_id = request_id
        self.reasoning_received |= metadata.get('reasoning_received') is True
        self.finish_reason_supported |= metadata.get('finish_reason_supported') is True

    def as_dict(self):
        return asdict(self)

    def interruption(self):
        if self.finish_reason == 'length':
            return f'模型因长度限制停止（本次输出额度 {self.max_tokens} token），请在模型服务设置中调整输出额度或缩小任务范围'
        reasons = {
            'content_filter': '模型服务过滤了本次回复，请调整问题后重试',
            'tool_calls': '模型返回了工具调用，未按当前流程返回完整正文',
            'insufficient_system_resource': '模型服务因推理资源不足中断了回复',
            'aborted': '模型服务中止了本次生成',
            'pause_turn': '模型暂停了本次生成，尚未完成回复',
            'unknown': '模型返回了未识别的结束状态，无法确认回复完整',
        }
        if self.finish_reason in reasons:
            return reasons[self.finish_reason]
        if self.stream_ended and self.finish_reason_supported and not self.finish_reason:
            return '模型响应流结束但未收到结束标记，可能被上游提前中断'
        return None

    def empty_message(self):
        problem = self.interruption()
        if problem:
            if self.finish_reason == 'length' and self.reasoning_received:
                return problem + '；本次收到思考数据，但未收到结论正文'
            return problem
        if self.reasoning_received:
            return '模型仅返回思考数据，没有返回结论正文'
        return '模型结束响应但未返回有效正文'
