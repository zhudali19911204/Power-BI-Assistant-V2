import json
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.engine.core.artifact import ValidationResult
from backend.schemas import SettingsInput


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def conversation(client, mode="dax"):
    model = client.post('/api/models/demo', json={}).json()
    result = client.post('/api/conversations', json={
        'model_id': model['id'], 'mode': mode, 'query_name': '销售明细' if mode == 'mquery' else ''})
    assert result.status_code == 201
    return result.json(), model


class FakeProvider:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def stream(self, system, messages, **opts):
        self.calls.append((system, list(messages)))
        reply = next(self.replies)
        for part in (reply[:12], reply[12:]):
            yield part


def send(client, cid, content='计算总额', **extra):
    response = client.post(f'/api/conversations/{cid}/messages', json={'content': content, **extra})
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines()]


def test_local_only_and_csrf(client):
    assert client.get('/api/health').json()['platform'] == 'desktop'
    assert client.get('/').status_code == 200
    assert client.get('/assets/app.js').status_code == 200
    parser=client.get('/assets/vendor/markdown-it.mjs')
    assert parser.status_code == 200
    assert 'javascript' in parser.headers['content-type']
    assert client.get('/assets/messages.css').status_code == 200
    assert client.get('/api/health', headers={'Host': 'attacker.example'}).status_code == 400
    assert client.post('/api/models/demo', json={}, headers={'Origin': 'https://attacker.example'}).status_code == 403
    assert client.post('/api/models/demo', data='{}', headers={'Content-Type': 'text/plain'}).status_code == 415


def test_settings_secret_encryption_and_restart(client, app, tmp_path):
    key = 'test-private-api-key'
    result = client.put('/api/settings', json={'model': 'my-server-model', 'base_url': 'http://192.168.1.100:8000/v1', 'api_key': key})
    assert result.status_code == 200
    assert result.json()['ready']
    assert key not in result.text
    stored = app.state.db.one('SELECT value FROM settings')['value']
    assert key not in stored
    assert app.state.settings.key(app.state.settings.raw()) == key
    app2 = create_app(tmp_path)
    assert app2.state.settings.key(app2.state.settings.raw()) == key
    client.put('/api/settings', json={'model': 'new-model'})
    assert client.get('/api/settings').json()['has_key']
    client.put('/api/settings', json={'clear_key': True})
    assert not client.get('/api/settings').json()['has_key']


@pytest.mark.parametrize('address', ['file:///etc/passwd', 'https://user:secret@example.com/v1', 'https://example.com/v1?key=secret'])
def test_reject_invalid_model_endpoints(client, address):
    assert client.put('/api/settings', json={'base_url': address}).status_code == 422


def test_generation_stream_grounding_history_and_export(client, app, monkeypatch):
    conv, model = conversation(client)
    provider = FakeProvider(['计算如下：\n```dax\n销售合计 = SUM(\'销售明细\'[销售额])\n```', '可以继续解释。'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    events = send(client, conv['id'])
    assert events[-1]['type'] == 'done'
    obj = next(e for e in events if e['type'] == 'artifacts')['artifacts'][0]
    assert obj['validation']['ok']
    assert not obj['validation']['run_verified']
    assert '销售明细' in provider.calls[0][0]
    assert len(client.get(f"/api/conversations/{conv['id']}/messages").json()) == 2
    send(client, conv['id'], '解释这段公式')
    assert len(provider.calls[1][1]) == 3
    exported = client.get(f"/api/conversations/{conv['id']}/export")
    assert '未经实跑' in exported.text
    assert client.post(f"/api/artifacts/{obj['id']}/apply", json={}).status_code == 409
    assert client.delete(f"/api/conversations/{conv['id']}").status_code == 200
    assert not app.state.db.all('SELECT * FROM artifacts')


def test_invalid_references_repair_then_pass(client, app, monkeypatch):
    conv, _ = conversation(client)
    provider = FakeProvider(['```dax\n合计 = SUM(\'不存在\'[金额])\n```', '```dax\n合计 = SUM(\'销售明细\'[销售额])\n```'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    events = send(client, conv['id'])
    assert not any(e['type'] == 'reset' for e in events)
    assert len(provider.calls) == 2
    assert next(e for e in events if e['type'] == 'artifacts')['artifacts'][0]['validation']['ok']
    text=''.join(e['text'] for e in events if e['type']=='delta')
    assert provider.calls[0][1][0].content=='计算总额'
    assert '不存在' in text and '销售明细' in text
    assert '验证修订 · 第 1 轮' in text
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['content']==text
    assert saved['content'].startswith('```dax\n合计 = SUM(\'不存在\'[金额])\n```')
    assert '不存在' in client.get(f"/api/conversations/{conv['id']}/export").text


def test_failed_repair_preserves_the_original_response_in_database(client,app,monkeypatch):
    conv,_=conversation(client)
    original='模型评价原文。\n```dax\nTotal = SUM(\'Missing\'[Amount])\n```'
    provider=FakeProvider([original,''])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    events=send(client,conv['id'],'评价下这个模型')
    assert events[-1]['type']=='error'
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['content'].startswith(original)
    assert saved['status']=='error'
    assert '验证修订' in saved['content']


def test_mquery_is_static_only(client, app, monkeypatch):
    conv, _ = conversation(client, 'mquery')
    provider = FakeProvider(['```powerquery\nlet\n    Source = #table({"x"}, {{1}})\nin\n    Source\n```'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    obj = next(e for e in send(client, conv['id']) if e['type'] == 'artifacts')['artifacts'][0]
    assert obj['kind'] == 'm_script'
    assert not obj['validation']['run_verified']
    assert client.post(f"/api/artifacts/{obj['id']}/apply", json={}).status_code == 409


def test_failed_response_saved_and_lock_released(client, app, monkeypatch):
    conv, _ = conversation(client)
    provider = FakeProvider([''])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    events = send(client, conv['id'])
    assert events[-1]['type'] == 'error'
    assert not app.state.chat.locks[conv['id']].locked()
    messages = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert messages[-1]['status'] == 'error'


def test_idle_timeout_preserves_streamed_text_history_and_export(client, app, monkeypatch):
    import threading
    resume = threading.Event()
    conv, _ = conversation(client)

    class Provider:
        idle_timeout = 0.08
        generation_timeout = 2
        def stream(self, *args, **kwargs):
            yield '这段已经生成的回复需要保留。'
            resume.wait(2)
            yield '迟到的内容'

    monkeypatch.setattr(app.state.settings, 'provider', lambda: Provider())
    try:
        events = send(client, conv['id'], '评价模型')
        assert events[-1]['type'] == 'error' and '未返回新数据' in events[-1]['text']
        assert not any(e['type'] == 'reset' for e in events)
        saved = client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
        assert saved['status'] == 'error'
        assert saved['content'].startswith('这段已经生成的回复需要保留。')
        assert '迟到的内容' not in saved['content']
        assert '这段已经生成的回复需要保留。' in client.get(f"/api/conversations/{conv['id']}/export").text
        assert not app.state.chat.generation_status(conv['id'])['running']
    finally:
        resume.set()


def test_orphan_stream_does_not_keep_history_in_connecting_state(client,app):
    conv,_=conversation(client)
    from backend.database import uid,now
    app.state.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(
        uid(),conv['id'],'assistant','','streaming',now()))
    messages=client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert messages[-1]['status']=='error'
    assert '请求已中断' in messages[-1]['content']


def test_live_history_reports_progress_and_partial_text(client,app):
    conv,_=conversation(client)
    from backend.database import uid,now
    message_id=uid()
    app.state.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(
        message_id,conv['id'],'assistant','部分回复','streaming',now()))
    import threading
    lock=threading.Lock();lock.acquire()
    app.state.chat.locks[conv['id']]=lock
    app.state.chat.progress[message_id]={'stage':'模型正在分析','elapsed':5}
    app.state.chat.active_messages[conv['id']]=message_id
    try:
        messages=client.get(f"/api/conversations/{conv['id']}/messages").json()
        assert messages[-1]['status']=='streaming'
        assert messages[-1]['progress']['stage']=='模型正在分析'
        assert messages[-1]['content']=='部分回复'
    finally:
        lock.release()


def test_live_snapshot_never_reactivated_implicitly(client, app, tmp_path):
    _, model = conversation(client)
    app.state.powerbi.live_ids.add(model['id'])
    assert app.state.powerbi.get(model['id'])['live']
    assert not create_app(tmp_path).state.powerbi.get(model['id'])['live']


def test_write_revalidates_and_audits(client, app, monkeypatch):
    conv, model = conversation(client)
    provider = FakeProvider(['```dax\n销售合计 = SUM(\'销售明细\'[销售额])\n```'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda: provider)
    obj = next(e for e in send(client, conv['id']) if e['type'] == 'artifacts')['artifacts'][0]
    validation = {**obj['validation'], 'run_verified': True}
    app.state.db.execute('UPDATE artifacts SET validation=? WHERE id=?', (json.dumps(validation), obj['id']))
    from backend.powerbi import context_from_dict
    monkeypatch.setattr(app.state.powerbi, 'require_live', lambda *a, **k: (model, context_from_dict(model['context'])))
    calls = []
    class Evaluator:
        def __init__(self, *a): pass
        def evaluate_with_defines(self, *a):
            calls.append('validate')
            return ValidationResult(ok=True, run_verified=True, sample=2)
    class Writer:
        def __init__(self, *a): pass
        def write_measure(self, *a, **k):
            calls.append(('write',k['overwrite']))
            from backend.engine.context.live_writer import WriteResult
            return WriteResult(True,'已写入')
    monkeypatch.setattr('backend.app.LiveDesktopEvaluator', Evaluator)
    monkeypatch.setattr('backend.app.LiveDesktopWriter', Writer)
    monkeypatch.setattr(app.state.powerbi, 'update_after_write', lambda *a: None)
    response = client.post(f"/api/artifacts/{obj['id']}/apply",json={})
    assert response.status_code == 200
    assert calls == ['validate',('write',False)]
    assert app.state.db.one('SELECT applied_at FROM artifacts')['applied_at']
    assert len(app.state.db.all('SELECT * FROM audit')) == 1


def test_historical_static_artifacts_can_be_revalidated_as_a_group(client,app,monkeypatch):
    conv,model=conversation(client)
    provider=FakeProvider(['```dax\nBase = 1\n```\n```dax\nTarget = [Base]*2\n```'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    send(client,conv['id'])
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    before=saved['content']
    from backend.powerbi import context_from_dict
    monkeypatch.setattr(app.state.powerbi,'require_live',lambda *a,**k:(model,context_from_dict(model['context'])))
    observed=[]
    class Evaluator:
        def __init__(self,*a):pass
        def evaluate_with_defines(self,home,definitions,target):
            observed.append((definitions,target))
            if target=='Target' and len(definitions)==1:
                return ValidationResult(ok=False,run_verified=True,errors=['Missing Base'])
            return ValidationResult(ok=True,run_verified=True,sample=2)
    monkeypatch.setattr('backend.chat.LiveDesktopEvaluator',Evaluator)
    response=client.post(f"/api/messages/{saved['id']}/validate",json={})
    assert response.status_code==200,response.text
    assert all(o['validation']['ok'] and o['validation']['run_verified'] for o in response.json()['artifacts'])
    assert any(len(definitions)==2 and target=='Target' for definitions,target in observed)
    assert client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]['content']==before


def test_multiple_failed_repairs_never_delete_any_previously_streamed_text(client,app,monkeypatch):
    conv,_=conversation(client)
    replies=[f'第{n}版分析。\n```dax\nTotal = SUM(\'Missing{n}\'[Amount])\n```' for n in range(3)]
    provider=FakeProvider(replies)
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    events=send(client,conv['id'])
    assert not any(e['type']=='reset' for e in events)
    text=''.join(e['text'] for e in events if e['type']=='delta')
    assert all(reply in text for reply in replies)
    assert text.count('### 验证修订')==2
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
    assert saved['content']==text
    assert all(not o['validation']['ok'] for o in saved['artifacts'])


def test_new_calibration_requests_cannot_have_multiple_expected_results(client,app):
    conv,_=conversation(client)
    response=client.post(f"/api/conversations/{conv['id']}/messages",json={
        'content':'校准这个度量',
        'points':[{'filters':[],'expected':100},{'filters':[],'expected':200}]})
    assert response.status_code==422
    assert not app.state.db.all('SELECT * FROM messages')


def test_calibration_schema_accepts_many_filters_and_one_result():
    from backend.schemas import MessageInput
    request=MessageInput(content='校准这个度量',points=[{
        'filters':[{'table':'客户','column':'地区','value':'华东'},
                   {'table':'日期','column':'年份','value':2026}],
        'expected':1200,'tolerance':0.01}])
    assert len(request.points)==1 and len(request.points[0].filters)==2
    assert request.points[0].expected==1200
