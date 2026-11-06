import json
import threading
import socket
import time

import pytest
import httpx
import uvicorn
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import now, uid
from backend.schemas import MessageInput
from backend.streaming import BackgroundStream


def setup(tmp_path):
    app = create_app(tmp_path)
    model = app.state.powerbi.demo()
    cid, stamp = uid(), now()
    app.state.db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?)', (
        cid, '后台测试', 'dax', model['id'], '', stamp, stamp))
    return app, cid


def test_disconnect_does_not_stop_generation_and_final_content_is_saved(tmp_path, monkeypatch):
    app, cid = setup(tmp_path)
    resume = threading.Event()

    class Provider:
        def stream(self, *a, **k):
            yield '首段文字。'
            assert resume.wait(3)
            yield '后台继续完成。'

    monkeypatch.setattr(app.state.settings, 'provider', lambda: Provider())
    source = app.state.chat.begin(cid, MessageInput(content='解释模型'), app.state.settings)
    task = BackgroundStream(source)
    reader = task.events()
    try:
        while json.loads(next(reader))['type'] != 'delta':
            pass
        assert app.state.chat.generation_status(cid)['running']
        reader.close()
        assert task.detached.is_set()
        # Losing the viewer leaves the task running until the actual provider finishes.
        assert app.state.chat.generation_status(cid)['running']
        resume.set()
        assert task.finished.wait(3)
        saved = app.state.chat.messages(cid)[-1]
        assert saved['status'] == 'complete'
        assert saved['content'] == '首段文字。后台继续完成。'
        assert not app.state.chat.generation_status(cid)['running']
    finally:
        resume.set()


def test_slow_viewer_uses_bounded_buffer_without_blocking_the_worker():
    generated = []

    def source():
        for i in range(1000):
            generated.append(i)
            yield json.dumps({'type': 'delta', 'text': str(i)}) + '\n'

    task = BackgroundStream(source(), buffer_size=2)
    assert task.finished.wait(3)
    assert len(generated) == 1000
    assert task.output.qsize() <= 2
    assert json.loads(next(task.events()))['type'] == 'background'


def test_different_conversations_run_independently_and_duplicate_is_rejected(tmp_path, monkeypatch):
    app, first = setup(tmp_path)
    original = app.state.chat.conversation(first)
    second, stamp = uid(), now()
    app.state.db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?)', (
        second, '另一会话', 'dax', original['model_id'], '', stamp, stamp))
    started = [threading.Event(), threading.Event()]
    resume = [threading.Event(), threading.Event()]
    allocated = []

    class Provider:
        def __init__(self, index):
            self.index = index

        def stream(self, *a, **k):
            started[self.index].set()
            assert resume[self.index].wait(3)
            yield f'会话{self.index}的结果'

    def provider():
        allocated.append(len(allocated))
        return Provider(allocated[-1])

    monkeypatch.setattr(app.state.settings, 'provider', provider)
    tasks = []
    try:
        for cid in (first, second):
            tasks.append(BackgroundStream(app.state.chat.begin(cid, MessageInput(content='评价模型'), app.state.settings)))
        assert all(event.wait(3) for event in started)
        with pytest.raises(HTTPException) as failure:
            app.state.chat.begin(first, MessageInput(content='重复请求'), app.state.settings)
        assert failure.value.status_code == 409
        assert len(allocated) == 2
        resume[0].set()
        assert tasks[0].finished.wait(3)
        assert not app.state.chat.generation_status(first)['running']
        assert app.state.chat.generation_status(second)['running']
        resume[1].set()
        assert tasks[1].finished.wait(3)
        assert app.state.chat.messages(first)[-1]['content'] == '会话0的结果'
        assert app.state.chat.messages(second)[-1]['content'] == '会话1的结果'
    finally:
        for event in resume:
            event.set()


def test_api_exposes_status_and_pins_model_profile_at_submission(tmp_path, monkeypatch):
    app, cid = setup(tmp_path)
    called = []

    class Provider:
        def stream(self, *a, **k):
            yield '已使用提交时的模型'

    def provider(profile_id=None):
        called.append(profile_id)
        return Provider()

    monkeypatch.setattr(app.state.settings, 'provider', provider)
    with TestClient(app) as client:
        result = client.post(f'/api/conversations/{cid}/messages', json={
            'content': '评价模型', 'llm_profile_id': 'selected-profile'})
        assert result.status_code == 200
        assert json.loads(result.text.splitlines()[-1])['type'] == 'done'
        assert called == ['selected-profile']
        assert not client.get(f'/api/conversations/{cid}/generation').json()['running']
        assert client.get('/api/conversations').json()[0]['generating'] is False
        assert client.get('/api/conversations/missing/generation').status_code == 404


def test_bad_service_configuration_releases_conversation_lock(tmp_path, monkeypatch):
    app, cid = setup(tmp_path)

    def broken():
        raise HTTPException(409, '请配置模型')

    monkeypatch.setattr(app.state.settings, 'provider', broken)
    with pytest.raises(HTTPException):
        app.state.chat.begin(cid, MessageInput(content='评价模型'), app.state.settings)
    assert not app.state.chat.generation_status(cid)['running']


def test_real_http_disconnect_keeps_task_alive_while_another_conversation_completes(tmp_path, monkeypatch):
    app, cid = setup(tmp_path)
    resume = threading.Event()

    class Provider:
        def stream(self, system, messages, **opts):
            if messages[-1].content == '第二任务':
                yield '另一会话独立完成'
                return
            yield '首段已显示。'
            assert resume.wait(5)
            yield '断连后后台完成。'

    monkeypatch.setattr(app.state.settings, 'provider', lambda: Provider())
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    base = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    worker = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert worker.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        with httpx.Client(base_url=base, timeout=5) as client:
            with client.stream('POST', f'/api/conversations/{cid}/messages', json={'content': '后台任务'}) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if json.loads(line)['type'] == 'delta':
                        break
            # The real network stream is now closed, while the provider is still waiting.
            assert client.get(f'/api/conversations/{cid}/generation').json()['running']
            assert client.get('/api/conversations').json()[0]['generating']
            model = app.state.chat.conversation(cid)['model_id']
            second = client.post('/api/conversations', json={'model_id': model, 'title': '第二会话'}).json()['id']
            result = client.post(f'/api/conversations/{second}/messages', json={'content': '第二任务'})
            assert result.status_code == 200
            assert json.loads(result.text.splitlines()[-1])['type'] == 'done'
            assert client.get(f'/api/conversations/{cid}/generation').json()['running']
            resume.set()
            deadline = time.monotonic() + 5
            while client.get(f'/api/conversations/{cid}/generation').json()['running']:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            saved = client.get(f'/api/conversations/{cid}/messages').json()[-1]
            assert saved['status'] == 'complete'
            assert saved['content'] == '首段已显示。断连后后台完成。'
            assert client.get(f'/api/conversations/{second}/messages').json()[-1]['content'] == '另一会话独立完成'
    finally:
        resume.set()
        server.should_exit = True
        worker.join(5)
        sock.close()
