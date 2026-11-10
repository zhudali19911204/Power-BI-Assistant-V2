import json
from types import SimpleNamespace

import pytest

from backend.engine.llm.claude import ClaudeProvider
from backend.engine.llm.diagnostics import GenerationReport
from backend.engine.llm.openai_compat import OpenAICompatProvider
from backend.streaming import completion_events
from tests.test_analysis import setup, plan, result, send
from tests.test_images import upload


@pytest.mark.parametrize('url,expected', [
    ('https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1', {'enable_thinking': False}),
    ('https://dashscope-intl.aliyuncs.com/compatible-mode/v1', {'enable_thinking': False}),
    ('https://api.deepseek.com/v1', {'thinking': {'type': 'disabled'}}),
    ('http://10.1.2.3:8080/v1', {'chat_template_kwargs': {'enable_thinking': False}}),
    ('https://aliyuncs.com.example/v1', {'chat_template_kwargs': {'enable_thinking': False}}),
])
def test_thinking_switch_matches_endpoint_and_override_does_not_change_profile(url, expected):
    assert OpenAICompatProvider._extra_body({'enable_thinking': False}, True, url) == expected
    assert OpenAICompatProvider._extra_body({}, None, url) == {}


def test_openai_finish_reason_usage_and_request_id_survive_stream_without_private_reasoning():
    requests, closed = [], []

    class Stream:
        def __iter__(self):
            for content, reasoning, reason in [(None, 'private thought', None), ('结论', None, 'length')]:
                yield SimpleNamespace(id='chatcmpl-test', usage=None, choices=[SimpleNamespace(
                    finish_reason=reason, delta=SimpleNamespace(content=content, reasoning_content=reasoning))])
            yield SimpleNamespace(id='chatcmpl-test', choices=[], usage=SimpleNamespace(
                prompt_tokens=30, completion_tokens=16000, total_tokens=16030,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=15998)))

        def close(self): closed.append(True)

    def create(**kwargs): requests.append(kwargs); return Stream()
    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider.model = 'deepseek-v4.1-flash-1017'
    provider.base_url = 'https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
    provider.enable_thinking = True
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    report = GenerationReport()
    events = list(completion_events(provider, '', [], report=report))
    assert [e['text'] for e in events if e['type'] == 'delta'] == ['结论']
    assert not any(e['type'] == 'metadata' for e in events)
    assert report.finish_reason == 'length' and report.stream_ended
    assert report.usage == {'prompt_tokens': 30, 'completion_tokens': 16000,
                            'total_tokens': 16030, 'reasoning_tokens': 15998}
    assert report.request_id == 'chatcmpl-test' and report.reasoning_received
    assert 'private thought' not in json.dumps(report.as_dict()) + str(events)
    assert requests[0]['stream_options'] == {'include_usage': True}
    assert requests[0]['extra_body'] == {'enable_thinking': True}
    assert closed == [True]


def test_anthropic_start_and_delta_usage_merge_and_stop_reason_is_normalized():
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __iter__(self):
            yield SimpleNamespace(type='message_start', message=SimpleNamespace(
                id='msg_test', usage=SimpleNamespace(input_tokens=40, output_tokens=1)))
            yield SimpleNamespace(type='content_block_delta', delta=SimpleNamespace(type='text_delta', text='正文'))
            yield SimpleNamespace(type='message_delta', delta=SimpleNamespace(stop_reason='end_turn'),
                                  usage=SimpleNamespace(output_tokens=8))

    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider.model = 'claude-test'
    provider._client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **opts: Stream()))
    report = GenerationReport()
    list(completion_events(provider, '', [], report=report))
    assert report.finish_reason == 'stop'
    assert report.usage == {'prompt_tokens': 40, 'completion_tokens': 8}
    assert report.interruption() is None


class EventProvider:
    max_tokens = 32000
    model = 'test-multimodal'
    profile_id = 'test-profile'
    enable_thinking = True
    can_disable_thinking = True

    def __init__(self, summaries): self.summaries = iter(summaries); self.calls = []

    def stream_events(self, system, messages, **opts):
        self.calls.append((list(messages), opts))
        yield {'type': 'metadata', 'finish_reason_supported': True, 'request_id': 'request-test'}
        if len(self.calls) == 1:
            yield {'type': 'delta', 'text': plan()}
            yield {'type': 'metadata', 'finish_reason': 'stop'}
            return
        text, reason, reasoning = next(self.summaries)
        if reasoning:
            yield {'type': 'metadata', 'reasoning_received': True, 'private_reasoning': 'never save this'}
        if text:
            yield {'type': 'delta', 'text': text}
        yield {'type': 'metadata', 'finish_reason': reason,
               'usage': {'completion_tokens': 32000 if reason == 'length' else 10,
                         'reasoning_tokens': 32000 if reasoning else 0}}


def wire(setup, monkeypatch, summaries):
    app, client, conv = setup
    provider = EventProvider(summaries)
    queries = []
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    def execute(*args): queries.append(args); return result()
    monkeypatch.setattr(app.state.chat.analysis, 'execute', execute)
    return app, client, conv, provider, queries


@pytest.mark.parametrize('reason', ['stop', 'length', None])
def test_empty_summary_retries_once_with_same_evidence_images_and_budget(setup, monkeypatch, reason):
    app, client, conv, provider, queries = wire(setup, monkeypatch, [('', reason, True), ('实际总额125。[Q1]', 'stop', False)])
    image = upload(client)
    response = client.post(f"/api/conversations/{conv['id']}/messages", json={
        'content': '根据截图分析实际数据', 'image_ids': [image['id']]})
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]['type'] == 'done'
    assert len(provider.calls) == 3 and len(queries) == 1
    assert all(opts['max_tokens'] == 32000 for _, opts in provider.calls)
    assert provider.calls[-1][1]['enable_thinking'] is False
    assert provider.enable_thinking is True
    first, retry = provider.calls[1][0], provider.calls[2][0]
    assert first[-1].content == retry[-2].content and first[0].images == retry[0].images
    assert first[0].images and '125' in first[-1].content
    assert not any(e['type'] == 'reset' for e in events)
    saved = client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['content'].startswith('### 分析口径') and '实际总额125' in saved['content']
    summaries = [r for r in saved['generations'] if r['stage'] == 'analysis_summary']
    assert [r['attempt'] for r in summaries] == [1, 2]
    assert summaries[0]['diagnostic']['finish_reason'] == reason
    assert summaries[0]['diagnostic']['reasoning_received']
    assert 'never save this' not in json.dumps(saved)
    client.delete(f"/api/conversations/{conv['id']}")
    assert not app.state.db.all('SELECT * FROM llm_generations')


def test_two_empty_summaries_stop_and_explain_budget_exhaustion(setup, monkeypatch):
    app, client, conv, provider, queries = wire(setup, monkeypatch, [('', 'length', True)] * 2)
    events = send(client, conv)
    assert events[-1]['type'] == 'error'
    assert '长度限制' in events[-1]['text'] and '32000' in events[-1]['text']
    assert '重试一次' in events[-1]['text'] and '思考数据' in events[-1]['text']
    assert len(provider.calls) == 3 and len(queries) == 1
    saved = client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['status'] == 'error' and saved['analysis'][0]['ok']
    assert saved['content'].startswith('### 分析口径')
    assert '125' in client.get(f"/api/conversations/{conv['id']}/export").text


@pytest.mark.parametrize('reason', ['length', None, 'content_filter'])
def test_partial_summary_is_preserved_and_never_replaced_by_retry(setup, monkeypatch, reason):
    app, client, conv, provider, queries = wire(setup, monkeypatch, [('已生成的结论。[Q1]', reason, False)])
    events = send(client, conv)
    assert events[-1]['type'] == 'error' and len(provider.calls) == 2
    saved = client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert '已生成的结论。[Q1]' in saved['content'] and saved['analysis'][0]['ok']
    assert saved['generations'][-1]['diagnostic']['finish_reason'] == reason


def test_filtered_empty_summary_is_not_retried(setup, monkeypatch):
    app, client, conv, provider, queries = wire(setup, monkeypatch, [('', 'content_filter', False)])
    events = send(client, conv)
    assert events[-1]['type'] == 'error' and '过滤' in events[-1]['text']
    assert len(provider.calls) == 2


def test_diagnostics_capture_transport_error_without_exception_contents():
    class Provider:
        def stream(self, *args, **opts):
            yield '保留文字'
            raise OSError('private endpoint or key must not be recorded')
    reports = []
    with pytest.raises(OSError):
        list(completion_events(Provider(), '', [], on_report=lambda report: reports.append(report.as_dict())))
    assert reports[0]['error_type'] == 'OSError' and reports[0]['output_chars'] == 4
    assert 'private endpoint' not in json.dumps(reports)


@pytest.mark.parametrize('mode,code', [
    ('dax', '```dax\nTest = 1\n```'),
    ('mquery', '```powerquery\nlet Source = 1 in Source\n```'),
])
def test_truncated_code_is_preserved_but_not_marked_valid_or_importable(setup, monkeypatch, mode, code):
    app, client, conv = setup
    ctx = app.state.powerbi.get(conv['model_id'])['context']
    query_name = next(iter(ctx['table_queries'])) if mode == 'mquery' else ''
    body = {'mode': mode, 'model_id': conv['model_id'], 'query_name': query_name}
    target = client.post('/api/conversations', json=body).json()

    class Provider:
        max_tokens = 16000
        def stream_events(self, *args, **opts):
            yield {'type': 'delta', 'text': code}
            yield {'type': 'metadata', 'finish_reason': 'length'}

    monkeypatch.setattr(app.state.settings, 'provider', lambda: Provider())
    events = send(client, target)
    assert events[-1]['type'] == 'error' and '长度限制' in events[-1]['text']
    saved = client.get(f"/api/conversations/{target['id']}/messages").json()[-1]
    assert saved['status'] == 'error' and code in saved['content'] and not saved['artifacts']
    assert saved['generations'][0]['diagnostic']['finish_reason'] == 'length'
