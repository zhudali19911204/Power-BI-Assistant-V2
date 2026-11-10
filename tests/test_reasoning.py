from types import SimpleNamespace

import pytest

from backend.engine.llm.openai_compat import OpenAICompatProvider
from backend.engine.llm.reasoning import reasoning_control


@pytest.mark.parametrize('model,url,low,high', [
    ('qwen3.8-max', 'https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
     {'extra_body': {'enable_thinking': False}}, {'extra_body': {'enable_thinking': True}}),
    ('qwen3.8-flash-next-unsloth-ud-q4_k_xl', 'http://10.1.2.3:8080/v1',
     {'extra_body': {'chat_template_kwargs': {'enable_thinking': False}}},
     {'extra_body': {'chat_template_kwargs': {'enable_thinking': True}}}),
    ('deepseek-v4.1-flash-1017', 'https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
     {'extra_body': {'enable_thinking': True}, 'reasoning_effort': 'low'},
     {'extra_body': {'enable_thinking': True}, 'reasoning_effort': 'high'}),
    ('deepseek-v4-pro', 'https://dashscope.aliyuncs.com/compatible-mode/v1',
     {'extra_body': {'enable_thinking': False}}, {'extra_body': {'enable_thinking': True}}),
    ('deepseek-flash', 'https://api.deepseek.com/v1',
     {'extra_body': {'thinking': {'type': 'enabled'}}, 'reasoning_effort': 'low'},
     {'extra_body': {'thinking': {'type': 'enabled'}}, 'reasoning_effort': 'high'}),
    ('glm-5.3', 'https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
     {'reasoning_effort': 'low'}, {'reasoning_effort': 'high'}),
    ('glm-5.3', 'https://open.bigmodel.cn/api/paas/v4',
     {'extra_body': {'thinking': {'type': 'enabled'}}, 'reasoning_effort': 'low'},
     {'extra_body': {'thinking': {'type': 'enabled'}}, 'reasoning_effort': 'high'}),
    ('glm-4.7', 'https://open.bigmodel.cn/api/paas/v4',
     {'extra_body': {'thinking': {'type': 'disabled'}}},
     {'extra_body': {'thinking': {'type': 'enabled'}}}),
    ('kimi-k3.1-pro', 'https://api.moonshot.cn/v1',
     {'reasoning_effort': 'low'}, {'reasoning_effort': 'high'}),
    ('kimi-k2.6', 'https://api.moonshot.ai/v1',
     {'extra_body': {'thinking': {'type': 'disabled'}}},
     {'extra_body': {'thinking': {'type': 'enabled'}}}),
    ('MiniMax-M3.1-Flash', 'https://api.minimax.cn/v1',
     {'reasoning_effort': 'low'}, {'reasoning_effort': 'high'}),
    ('MiniMax-M3', 'https://api.minimax.io/v1',
     {'extra_body': {'thinking': {'type': 'disabled'}}},
     {'extra_body': {'thinking': {'type': 'adaptive'}}}),
    ('MiniMax/MiniMax-M3.1-Flash', 'https://dashscope.aliyuncs.com/compatible-mode/v1',
     {'reasoning_effort': 'low'}, {'reasoning_effort': 'high'}),
    ('doubao-seed-2-1-pro-260915', 'https://ark.cn-beijing.volces.com/api/v3',
     {'extra_body': {'thinking': {'type': 'enabled'}}, 'reasoning_effort': 'low'},
     {'extra_body': {'thinking': {'type': 'enabled'}}, 'reasoning_effort': 'high'}),
    ('kimi-k3-1-pro-261024', 'https://ark.cn-beijing.volces.com/api/v3',
     {'reasoning_effort': 'low'}, {'reasoning_effort': 'high'}),
    ('glm-5-2-260617', 'https://ark.cn-beijing.volces.com/api/v3',
     {'extra_body': {'thinking': {'type': 'disabled'}}},
     {'extra_body': {'thinking': {'type': 'enabled'}}}),
    ('hunyuan-a13b', 'https://api.hunyuan.cloud.tencent.com/v1',
     {'extra_body': {'enable_thinking': False}}, {'extra_body': {'enable_thinking': True}}),
])
def test_endpoint_specific_reasoning_parameters(model, url, low, high):
    control = reasoning_control('openai_compat', model, url)
    assert control.supported
    assert control.request_options('low') == low
    assert control.request_options('high') == high
    assert control.public('low')['effective_label'] != control.public('high')['effective_label']


@pytest.mark.parametrize('provider,model,url', [
    ('claude', 'glm-5.3', 'https://open.bigmodel.cn/api/anthropic'),
    ('openai_compat', 'glm-5.3', 'https://proxy.example/v1'),
    ('openai_compat', 'deepseek-flash', 'https://api.deepseek.com.example/v1'),
    ('openai_compat', 'glm-5.3', 'https://aliyuncs.com.example/v1'),
    ('openai_compat', 'MiniMax-M2.5', 'https://api.minimax.cn/v1'),
    ('openai_compat', 'kimi-k2.7-code', 'https://api.moonshot.cn/v1'),
    ('openai_compat', 'qwen3.8-2.4t-a95b', 'https://dashscope.aliyuncs.com/compatible-mode/v1'),
    ('openai_compat', 'qwen3-32b-thinking', 'http://localhost:8000/v1'),
    ('openai_compat', 'ep-unknown', 'https://ark.cn-beijing.volces.com/api/v3'),
    ('openai_compat', 'hunyuan-t1', 'https://api.hunyuan.cloud.tencent.com/v1'),
    ('openai_compat', 'ernie-unverified', 'https://qianfan.baidubce.com/v2'),
])
def test_unverified_or_fixed_models_do_not_pretend_to_support_strength(provider, model, url):
    control = reasoning_control(provider, model, url)
    assert not control.supported
    assert control.request_options('low') == control.request_options('high') == {}
    assert control.public('low')['effective_label'] == '服务端默认 · 强度不可调'
    assert control.public('low')['description']


def test_sync_and_stream_send_the_same_strength_and_preserve_legacy_override():
    calls = []

    class Stream:
        def __iter__(self): return iter(())
        def close(self): pass

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs.get('stream'):
            return Stream()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='OK'))])

    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider.model = 'glm-5.3'
    provider.base_url = 'https://open.bigmodel.cn/api/paas/v4'
    provider.enable_thinking = None
    provider.reasoning_level = 'high'
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert provider.complete('', []) == 'OK'
    list(provider.stream_events('', []))
    for call in calls:
        assert call['reasoning_effort'] == 'high'
        assert call['extra_body'] == {'thinking': {'type': 'enabled'}}
    assert provider.can_reduce_reasoning and not provider.can_disable_thinking
    list(provider.stream_events('', [], reasoning_level='low'))
    assert calls[-1]['reasoning_effort'] == 'low'
    provider.complete('', [], enable_thinking=False)
    assert calls[-1]['reasoning_effort'] == 'low'
    assert calls[-1]['extra_body'] == {'thinking': {'type': 'enabled'}}
    assert provider.reasoning_level == 'high'
    with pytest.raises(ValueError, match='low'):
        provider.complete('', [], reasoning_level='invalid')
