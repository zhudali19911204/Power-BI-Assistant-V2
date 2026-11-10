import json
import threading
import socket
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
import httpx
import uvicorn

from backend.cancellation import CancellationToken, GenerationCancelled, cancellation_scope, check_cancelled
from backend.engine.context.live_source import _LiveSession
from backend.engine.llm.openai_compat import OpenAICompatProvider
from backend.schemas import MessageInput
from backend.streaming import BackgroundStream, completion_events, task_events
from tests.test_background_generation import setup as background_setup
from tests.test_analysis import setup as analysis_setup, plan, result
from tests.test_images import upload


def launch(app, cid, generation_id='a'*32, **extra):
    task = BackgroundStream(app.state.chat.begin(cid,
        MessageInput(content='测试中止', generation_id=generation_id, **extra), app.state.settings))
    return task


def wait_event(reader, kind):
    for line in reader:
        event = json.loads(line)
        if event['type'] == kind: return event
    raise AssertionError('Missing ' + kind)


@pytest.mark.parametrize('partial', ['', '已经输出的文字。'])
@pytest.mark.parametrize('mode', ['dax', 'mquery'])
def test_cancel_before_or_after_first_token_closes_request_and_keeps_history(tmp_path, monkeypatch, partial, mode):
    app, cid = background_setup(tmp_path)
    if mode=='mquery':
        model=app.state.powerbi.get(app.state.chat.conversation(cid)['model_id'])
        query=next(iter(model['context']['table_queries']))
        app.state.db.execute('UPDATE conversations SET mode=?,query_name=? WHERE id=?',(mode,query,cid))
    started, closed, resume = threading.Event(), threading.Event(), threading.Event()
    class Provider:
        def __init__(self): self._client = self
        def close(self): closed.set(); resume.set()
        def stream(self, *args, **opts):
            if partial: yield partial
            started.set()
            resume.wait(3)
            yield '不应追加的迟到内容'
    monkeypatch.setattr(app.state.settings, 'provider', Provider)
    task = launch(app, cid)
    reader = task.events()
    try:
        if partial: wait_event(reader, 'delta')
        assert started.wait(2)
        with TestClient(app) as client:
            state = client.get(f'/api/conversations/{cid}/generation').json()
            assert state['generation_id'] == 'a'*32
            assert client.post(f'/api/conversations/{cid}/cancel', json={'generation_id':'a'*32}).status_code == 200
            assert task.finished.wait(2) and closed.wait(2)
            saved = client.get(f'/api/conversations/{cid}/messages').json()[-1]
            assert saved['status'] == 'cancelled' and '已中止' in saved['content']
            assert partial in saved['content'] and '迟到内容' not in saved['content']
            assert not saved['artifacts']
            assert not client.get(f'/api/conversations/{cid}/generation').json()['running']
            assert saved['generations'][0]['diagnostic']['error_type'] == 'GenerationCancelled'
            assert '已中止' in client.get(f'/api/conversations/{cid}/export').text
        assert wait_event(reader, 'cancelled')['text'].startswith('已中止')
    finally:
        resume.set(); reader.close()


def test_cancel_is_idempotent_and_stale_id_cannot_stop_next_turn(tmp_path, monkeypatch):
    app, cid = background_setup(tmp_path)
    gates = [threading.Event(), threading.Event()]
    started = [threading.Event(), threading.Event()]
    providers = []
    class Provider:
        def __init__(self): self.index=len(providers); providers.append(self); self._client=self
        def close(self): gates[self.index].set()
        def stream(self, *args, **opts):
            started[self.index].set(); gates[self.index].wait(4); yield '新的一轮完成'
    monkeypatch.setattr(app.state.settings, 'provider', Provider)
    first=launch(app,cid)
    try:
        assert started[0].wait(2)
        app.state.chat.cancel(cid,'a'*32); app.state.chat.cancel(cid,'a'*32)
        assert first.finished.wait(2)
        second=launch(app,cid,'b'*32)
        assert started[1].wait(2)
        with TestClient(app) as client:
            assert client.post(f'/api/conversations/{cid}/cancel',json={'generation_id':'a'*32}).status_code==409
            assert client.post(f'/api/conversations/{cid}/cancel',json={}).status_code==422
            assert client.post('/api/conversations/missing/cancel',json={'generation_id':'b'*32}).status_code==404
        assert app.state.chat.generation_status(cid)['running'] and not gates[1].is_set()
        gates[1].set(); assert second.finished.wait(2)
        assert app.state.chat.messages(cid)[-1]['status']=='complete'
    finally:
        for gate in gates: gate.set()


@pytest.mark.parametrize('stage', ['plan', 'repair', 'summary'])
def test_analysis_cancel_skips_subsequent_calls_and_keeps_evidence(analysis_setup, monkeypatch, stage):
    app, client, conv = analysis_setup
    reached, resume = threading.Event(), threading.Event()
    calls, queries = [], []
    class Provider:
        def __init__(self): self._client=self
        def close(self): resume.set()
        def stream(self, system, messages, **opts):
            calls.append(system)
            blocked = (stage=='plan' and len(calls)==1) or (stage!='plan' and len(calls)==2)
            if blocked:
                if stage=='summary': yield '已经生成的分析结论。[Q1]'
                reached.set(); resume.wait(4)
                yield '迟到输出'; return
            yield plan()
    monkeypatch.setattr(app.state.settings,'provider',Provider)
    def execute(*args): queries.append(args); return result(ok=stage!='repair')
    monkeypatch.setattr(app.state.chat.analysis,'execute',execute)
    image=upload(client)
    task=launch(app,conv['id'],image_ids=[image['id']])
    try:
        assert reached.wait(2)
        response=client.post(f"/api/conversations/{conv['id']}/cancel",json={'generation_id':'a'*32})
        assert response.status_code==200 and task.finished.wait(2)
        saved=client.get(f"/api/conversations/{conv['id']}/messages").json()
        assert saved[0]['images'][0]['id']==image['id']
        assert saved[-1]['status']=='cancelled' and '迟到输出' not in saved[-1]['content']
        assert len(calls)==(1 if stage=='plan' else 2)
        assert len(queries)==(0 if stage=='plan' else 1)
        assert len(saved[-1]['analysis'])==len(queries)
        if stage=='summary': assert '已经生成的分析结论' in saved[-1]['content']
        assert not app.state.chat.generation_status(conv['id'])['running']
    finally: resume.set()


def test_cancel_during_grounding_saves_images_without_calling_model(analysis_setup, monkeypatch):
    app,client,conv=analysis_setup
    reached,resume=threading.Event(),threading.Event()
    def grounding(*args): reached.set();resume.wait(3);check_cancelled()
    monkeypatch.setattr(app.state.semantics,'unresolved_code',grounding)
    monkeypatch.setattr(app.state.settings,'provider',lambda:pytest.fail('Model must not be called'))
    image=upload(client);task=launch(app,conv['id'],image_ids=[image['id']])
    try:
        assert reached.wait(2)
        app.state.chat.cancel(conv['id'],'a'*32)
        assert task.finished.wait(2)
        saved=client.get(f"/api/conversations/{conv['id']}/messages").json()
        assert saved[0]['images'][0]['id']==image['id'] and saved[-1]['status']=='cancelled'
    finally:resume.set()


def test_native_query_cancel_is_sent_and_worker_context_stops_following_query():
    entered,cancelled,finished=threading.Event(),threading.Event(),threading.Event()
    calls=[]
    class Command:
        def ExecuteReader(self):
            entered.set(); cancelled.wait(3)
            raise OSError('query interrupted')
        def Cancel(self): cancelled.set()
    session=_LiveSession.__new__(_LiveSession)
    session._conn=SimpleNamespace(CreateCommand=lambda:Command())
    token=CancellationToken('a'*32)
    def work():
        try:
            session.query('EVALUATE ROW("x",1)')
            calls.append('second-query')
        finally: finished.set()
    def stop(): assert entered.wait(2);token.cancel()
    stopper=threading.Thread(target=stop);stopper.start()
    with cancellation_scope(token),pytest.raises(GenerationCancelled):
        list(task_events(work,timeout=5))
    assert cancelled.wait(2) and finished.wait(2) and not calls
    stopper.join(2)


def test_openai_stream_is_closed_on_cancel_even_when_sdk_is_waiting():
    entered,closed=threading.Event(),threading.Event()
    class Stream:
        def __iter__(self):
            entered.set();closed.wait(3)
            if False:yield
        def close(self):closed.set()
    provider=OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider.model='test';provider.enable_thinking=None
    provider._client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k:Stream())))
    token=CancellationToken('a'*32)
    def stop():assert entered.wait(2);token.cancel()
    worker=threading.Thread(target=stop);worker.start()
    with cancellation_scope(token),pytest.raises(GenerationCancelled):
        list(completion_events(provider,'',[],timeout=5))
    assert closed.wait(2);worker.join(2)


def test_cancelling_one_background_conversation_does_not_stop_another(tmp_path, monkeypatch):
    app,first=background_setup(tmp_path)
    with TestClient(app) as client:
        model_id=app.state.chat.conversation(first)['model_id']
        second=client.post('/api/conversations',json={'model_id':model_id}).json()['id']
    gates=[threading.Event(),threading.Event()];started=[threading.Event(),threading.Event()];allocated=[]
    class Provider:
        def __init__(self):self.index=len(allocated);allocated.append(self);self._client=self
        def close(self):gates[self.index].set()
        def stream(self,*args,**opts):
            started[self.index].set();gates[self.index].wait(4);yield '另一会话完成'
    monkeypatch.setattr(app.state.settings,'provider',Provider)
    a=launch(app,first);b=launch(app,second,'b'*32)
    try:
        assert all(event.wait(2) for event in started)
        app.state.chat.cancel(first,'a'*32);assert a.finished.wait(2)
        assert app.state.chat.generation_status(second)['running'] and not gates[1].is_set()
        gates[1].set();assert b.finished.wait(2)
        assert app.state.chat.messages(second)[-1]['status']=='complete'
    finally:
        for gate in gates:gate.set()


def test_real_http_cancel_terminates_stream_and_preserves_partial_text(tmp_path, monkeypatch):
    app,cid=background_setup(tmp_path)
    resume=threading.Event()
    class Provider:
        def __init__(self):self._client=self
        def close(self):resume.set()
        def stream(self,*args,**opts):
            yield 'HTTP 已输出文字。';resume.wait(5);yield '迟到文字'
    monkeypatch.setattr(app.state.settings,'provider',Provider)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
    worker=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);worker.start()
    try:
        deadline=time.monotonic()+3
        while not server.started and time.monotonic()<deadline:time.sleep(.01)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=5,trust_env=False) as client:
            with client.stream('POST',f'/api/conversations/{cid}/messages',json={
                'content':'中止当前生成','generation_id':'a'*32}) as response:
                lines=response.iter_lines()
                wait_event(lines,'delta')
                stopped=client.post(f'/api/conversations/{cid}/cancel',json={'generation_id':'a'*32})
                assert stopped.status_code==200
                assert wait_event(lines,'cancelled')['text'].startswith('已中止')
                list(lines)
            saved=client.get(f'/api/conversations/{cid}/messages').json()[-1]
            assert saved['status']=='cancelled' and 'HTTP 已输出文字。' in saved['content']
            assert '迟到文字' not in saved['content']
    finally:
        resume.set();server.should_exit=True;worker.join(5);sock.close()
