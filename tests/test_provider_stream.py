from types import SimpleNamespace

from backend.engine.llm.openai_compat import OpenAICompatProvider
from backend.engine.llm.claude import ClaudeProvider


def test_reasoning_frames_report_activity_without_exposing_private_text():
    requests=[]
    closed=[]
    class Stream:
        def __iter__(self):
            for text,reasoning in [(None,'private reasoning'),('正文',None)]:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                    content=text,reasoning_content=reasoning))])
        def close(self):closed.append(True)
    def create(**kwargs):
        requests.append(kwargs)
        return Stream()
    provider=OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider.model='qwen'
    provider.enable_thinking=False
    provider._client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    events=list(provider.stream_events('test',[]))
    assert any(e['type']=='status' and '正在分析' in e['text'] for e in events)
    assert [e['text'] for e in events if e['type']=='delta']==['正文']
    assert 'private reasoning' not in str(events)
    assert requests[0]['extra_body']=={'chat_template_kwargs':{'enable_thinking':False}}
    assert closed==[True]
    assert sum(e['type'] == 'activity' for e in events) == 2


def test_anthropic_reasoning_and_body_frames_report_activity_without_private_text():
    closed = []

    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): closed.append(True)
        def __iter__(self):
            for kind, text in [('thinking_delta', 'private reasoning'), ('text_delta', '正文')]:
                yield SimpleNamespace(type='content_block_delta', delta=SimpleNamespace(type=kind, text=text))

    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider.model = 'test-claude'
    provider._client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kwargs: Stream()))
    events = list(provider.stream_events('test', []))
    assert sum(e['type'] == 'activity' for e in events) == 2
    assert [e['text'] for e in events if e['type'] == 'delta'] == ['正文']
    assert 'private reasoning' not in str(events)
    assert list(provider.stream('test', [])) == ['正文']
    assert closed == [True, True]
