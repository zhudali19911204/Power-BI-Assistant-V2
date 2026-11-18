import json
import threading
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import Database, encode, now, uid
from backend.powerbi import context_from_dict
from backend.template_parameters import recognize_parameters


class Provider:
    def __init__(self):
        self.calls = []

    def stream(self, system, messages, **options):
        self.calls.append((system, messages))
        if '严格只返回 JSON' in system:
            yield encode({'scope': '使用本次指定的月份', 'queries': [
                {'title': '销售总额', 'dax': 'EVALUATE ROW("Value",[总销售额])'}]})
        else:
            yield '本次销售额来自当前查询。[Q1]'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    ctx = context_from_dict(app.state.powerbi.demo()['context'])
    model = app.state.powerbi.save('销售报表', 'desktop', 51413, ctx)
    monkeypatch.setattr(app.state.powerbi, 'require_live',
                        lambda model_id, **kw: (app.state.powerbi.get(model_id), ctx))
    monkeypatch.setattr(app.state.semantics, 'require_ready', lambda *a: None)
    monkeypatch.setattr(app.state.semantics, 'unresolved_code', lambda *a: None)
    provider = Provider()
    monkeypatch.setattr(app.state.settings, 'provider', lambda *a: provider)
    totals = [100]
    def execute(*args):
        return {'ok': True, 'run_verified': True, 'columns': ['Value'], 'rows': [{'Value': totals[0]}],
                'row_count': 1, 'truncated': False, 'warnings': [], 'error': '', 'seconds': .01}
    monkeypatch.setattr(app.state.chat.analysis, 'execute', execute)
    with TestClient(app) as client:
        yield app, client, model, provider, totals


def create(client, model, **extra):
    data = {'name': '月度销售', 'model_id': model['id'],
            'prompt': '分析 {{月份}} 销售额，并比较 {{月份}} 各地区销售。', 'defaults': {'月份': '2026-10'}, **extra}
    response = client.post('/api/analysis-templates', json=data)
    assert response.status_code == 201, response.text
    return response.json()


def run(client, template, model, **extra):
    body = {'model_id': model['id'], 'revision': template['revision'], 'values': {}, 'request_id': uid(), **extra}
    return client.post(f'/api/analysis-templates/{template["id"]}/runs', json=body), body


def finish(app, run_id):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        row = app.state.db.one('SELECT * FROM analysis_template_runs WHERE id=?', (run_id,))
        if row and row['status'] != 'running':
            return row
        time.sleep(.01)
    pytest.fail('Template run did not finish')


def test_repeated_runs_query_fresh_data_and_preserve_parameters_and_old_results(setup):
    app, client, model, provider, totals = setup
    template = create(client, model)
    first, body = run(client, template, model)
    assert first.status_code == 200, first.text
    finish(app, body['request_id'])
    first_id = first.json()['conversation']['id']
    original = client.get(f'/api/conversations/{first_id}/messages').json()
    assert original[0]['content'].count('2026-10') == 2
    assert original[-1]['analysis'][0]['rows'] == [{'Value': 100}]
    totals[0] = 275
    second, second_body = run(client, template, model, values={'月份': '2026-11'})
    finish(app, second_body['request_id'])
    second_id = second.json()['conversation']['id']
    assert first_id != second_id
    fresh = client.get(f'/api/conversations/{second_id}/messages').json()
    assert fresh[0]['content'].count('2026-11') == 2
    assert fresh[-1]['analysis'][0]['rows'] == [{'Value': 275}]
    assert client.get(f'/api/conversations/{first_id}/messages').json() == original
    assert '2026-11' in provider.calls[2][1][0].content
    assert '2026-10' not in provider.calls[2][1][0].content
    runs = client.get(f'/api/analysis-templates/{template["id"]}/runs').json()
    assert [r['values']['月份'] for r in runs] == ['2026-11', '2026-10']
    assert all(r['status'] == 'complete' for r in runs)


def test_duplicate_run_request_does_not_repeat_generation_even_while_active(setup, monkeypatch):
    app, client, model, provider, totals = setup
    entered, release = threading.Event(), threading.Event()
    original = app.state.chat.analysis.execute
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', blocked)
    template = create(client, model)
    first, body = run(client, template, model)
    try:
        assert entered.wait(5)
        duplicate, _ = run(client, template, model, **body)
        assert duplicate.json()['id'] == first.json()['id']
        assert duplicate.json()['conversation']['id'] == first.json()['conversation']['id']
        conflict, _ = run(client, template, model, **{**body, 'values': {'月份': '其他月份'}})
        assert conflict.status_code == 409
        assert len(app.state.db.all('SELECT * FROM analysis_template_runs')) == 1
    finally:
        release.set()
    finish(app, body['request_id'])
    assert len(provider.calls) == 2


def test_template_edit_delete_and_restart_preserve_existing_runs(setup, tmp_path):
    app, client, model, _, _ = setup
    template = create(client, model)
    response, body = run(client, template, model)
    finish(app, body['request_id'])
    old = app.state.db.one('SELECT snapshot FROM analysis_template_runs WHERE id=?', (body['request_id'],))
    path = f'/api/analysis-templates/{template["id"]}'
    edit = {'name': '改名', 'prompt': '新的 {{月份}} 分析', 'defaults': {'月份': '2026-12'}, 'revision': 1}
    assert client.put(path, json=edit).json()['revision'] == 2
    assert client.put(path, json=edit).status_code == 409
    assert run(client, template, model)[0].status_code == 409
    reopened = Database(tmp_path / 'assistant.sqlite3')
    assert reopened.one('SELECT name FROM analysis_templates')['name'] == '改名'
    assert reopened.one('SELECT snapshot FROM analysis_template_runs') == old
    assert client.delete(path + '?revision=1').status_code == 409
    assert client.delete(path + '?revision=2').status_code == 200
    assert client.get('/api/analysis-templates').json() == []
    assert client.get(f'/api/conversations/{response.json()["conversation"]["id"]}/messages').json()[-1]['analysis']


@pytest.mark.parametrize('values', [{'月份': ''}, {'unknown': 'x'}])
def test_invalid_parameters_do_not_create_conversations(setup, values):
    app, client, model, _, _ = setup
    template = create(client, model, defaults={})
    response, _ = run(client, template, model, values=values)
    assert response.status_code == 422
    assert not app.state.db.all('SELECT * FROM conversations')


def test_blank_or_omitted_values_use_last_run_then_current_defaults_per_template(setup):
    app, client, model, _, _ = setup
    template = create(client, model, prompt='比较 {{工厂}} 在 {{月份}} 的销售额', defaults={'工厂': '0810', '月份': '2026-05'})
    path = f'/api/analysis-templates/{template["id"]}'
    assert client.get(path).json()['last_values'] == {}
    first, first_body = run(client, template, model, values={'工厂': ' 0887 ', '月份': '   '})
    assert first.status_code == 200
    finish(app, first_body['request_id'])
    repeat, repeat_body = run(client, template, model, values={'月份': '2026-06'})
    assert repeat.status_code == 200
    snapshot = json.loads(finish(app, repeat_body['request_id'])['snapshot'])
    assert snapshot['values'] == {'工厂': '0887', '月份': '2026-06'}
    assert snapshot['prompt'] == '比较 0887 在 2026-06 的销售额'
    assert client.get(path).json()['defaults'] == template['defaults']
    assert client.get('/api/analysis-templates').json()[0]['last_values'] == snapshot['values']
    duplicate = run(client, template, model, **first_body)[0].json()
    assert (duplicate['id'], duplicate['conversation']['id']) == (first_body['request_id'], first.json()['conversation']['id'])
    assert len(client.get(path+'/runs').json()) == 2

    other = create(client, model, prompt=template['prompt'], defaults=template['defaults'])
    assert other['last_values'] == {}
    edited = client.put(path, json={'name': '按工厂', 'revision': 1, 'prompt': '{{工厂}} 的前 {{数量}} 项销售额',
                                   'defaults': {'工厂': '0001', '数量': '5'}}).json()
    assert edited['last_values'] == {'工厂': '0887'}
    third, body = run(client, edited, model, values={'工厂': '', '数量': '0'})
    assert third.status_code == 200
    assert json.loads(finish(app, body['request_id'])['snapshot'])['values'] == {'工厂': '0887', '数量': '0'}


def test_recognize_multiple_values_in_text_and_scope_without_model_calls(setup):
    app, client, model, provider, _ = setup
    prompt = (" 💡看下0887 overhead 2026-5 实际费用和最新2026预测费用的差异，按'Dim_Cost Element'[Class 3I]拆分。"
              "\n分析口径：成本中心0887，Overhead，2026年5月，版本FC_2026_8+4，总销售额。")
    stamp = now()
    app.state.db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?)', ('source', '来源', 'analysis', model['id'], '', stamp, stamp))
    app.state.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)', ('answer', 'source', 'assistant', '已分析', 'complete', stamp))
    app.state.db.execute('INSERT INTO field_observations VALUES (?,?,?,?)', (uid(), 'answer', encode({
        'model_id': model['id'], 'column': 'Class 2', 'adopted_value': 'Overhead'}), stamp))
    for value, column in [('0887','CC Number'), ('overhead','其他字段')]:
        app.state.db.execute('INSERT INTO scope_value_hints VALUES (?,?,?,?,?)', (model['id'], model['fingerprint'], value, '历史维度', column))
    response = client.post('/api/analysis-templates/parameters/recognize', json={'model_id': model['id'], 'source_message_id': 'answer', 'prompt': prompt})
    assert response.status_code == 200, response.text
    candidates = response.json()['candidates']
    assert [(v['name'], v['value']) for v in candidates] == [
        ('成本中心', '0887'), ('Class 2', 'overhead'), ('月份', '2026-5'), ('预测年份', '2026'),
        ('预测版本', 'FC_2026_8+4'), ('指标', '总销售额')]
    for v, spellings in zip(candidates[:3], [['0887', '0887'], ['overhead', 'Overhead'], ['2026-5', '2026年5月']]):
        assert [prompt[a:b] for a,b in v['spans']] == spellings
    assert not provider.calls
    assert client.get('/api/analysis-templates').json() == []


def test_recognition_keeps_identifiers_and_existing_parameters_and_separates_roles():
    prompt = ("比较 工厂0810 和 成本中心0810，2025年与2026年的各月，部门为研发，产品“蒸汽炉”，对比华东地区。"
              "\n{{年份}}、`2025-11`、[FY 2027]、'2028'[Class 3I]、F1，排名前10项")
    found = recognize_parameters(prompt)
    assert {v['name']: v['value'] for v in found} == {
        '工厂': '0810', '成本中心': '0810', '年份2': '2025', '年份3': '2026', '部门': '研发',
        '产品': '蒸汽炉', '地区': '华东', '排名数量': '10'}
    assert all(len(v['spans']) == 1 for v in found)

    scoped = recognize_parameters("按'Dim 2026'[CC Number]='0887'和[Legal Entity]=0887筛选，`[CC Number]=0999`。")
    assert [(v['name'], v['value']) for v in scoped] == [('CC Number', '0887'), ('Legal Entity', '0887')]


def test_placeholders_are_validated_and_values_are_not_recursively_interpreted(setup):
    app, client, model, _, _ = setup
    bad = client.post('/api/analysis-templates', json={
        'name': '坏参数', 'prompt': '{{broken', 'model_id': model['id']})
    assert bad.status_code == 422
    template = create(client, model)
    response, body = run(client, template, model, values={'月份': '{{another}}'})
    finish(app, body['request_id'])
    messages = client.get(f'/api/conversations/{response.json()["conversation"]["id"]}/messages').json()
    assert '{{another}}' in messages[0]['content']


def test_unrelated_report_is_rejected_but_same_report_new_snapshot_can_run(setup):
    app, client, model, _, _ = setup
    template = create(client, model)
    other = app.state.powerbi.save('销售报表', 'desktop', 51414, context_from_dict(model['context']))
    assert run(client, template, other)[0].status_code == 409
    for item in [model, other]:
        app.state.db.execute('INSERT INTO model_reports VALUES (?,?,?)',
                             (item['id'], encode({'name': '销售报表', 'path': 'C:/Reports/sales.pbix'}), now()))
    response, body = run(client, template, other)
    assert response.status_code == 200
    finish(app, body['request_id'])
    assert response.json()['conversation']['model_id'] == other['id']


def test_disconnected_run_has_visible_error_and_can_be_retried_as_new_run(setup, monkeypatch):
    app, client, model, _, _ = setup
    template = create(client, model)
    def disconnected(*args, **kw):
        raise HTTPException(409, '报表连接已断开')
    monkeypatch.setattr(app.state.powerbi, 'require_live', disconnected)
    response, body = run(client, template, model)
    row = finish(app, body['request_id'])
    assert row['status'] == 'error'
    messages = client.get(f'/api/conversations/{response.json()["conversation"]["id"]}/messages').json()
    assert messages[-1]['status'] == 'error' and '断开' in messages[-1]['content']
    assert client.get(f'/api/analysis-templates/{template["id"]}/runs').json()[0]['status'] == 'error'


def test_save_from_analysis_keeps_scope_not_old_results_and_survives_source_deletion(setup):
    app, client, model, _, _ = setup
    template = create(client, model)
    response, body = run(client, template, model)
    finish(app, body['request_id'])
    conversation_id = response.json()['conversation']['id']
    messages = client.get(f'/api/conversations/{conversation_id}/messages').json()
    seed = client.get(f'/api/messages/{messages[-1]["id"]}/analysis-template').json()
    assert '使用本次指定的月份' in seed['prompt']
    assert '100' not in seed['prompt']
    saved = client.post('/api/analysis-templates', json=seed)
    assert saved.status_code == 201, saved.text
    client.delete(f'/api/conversations/{conversation_id}')
    assert next(r for r in client.get('/api/analysis-templates').json() if r['id'] == saved.json()['id'])['source_message_id'] is None
    assert client.get(f'/api/messages/{messages[0]["id"]}/analysis-template').status_code == 404


def test_provider_failure_before_first_message_stays_visible_in_history(setup, monkeypatch):
    app, client, model, _, _ = setup
    terminal_messages = []
    original_execute = app.state.db.execute
    def observe_terminal(sql, parameters=()):
        if sql.startswith("UPDATE analysis_template_runs SET status='error'"):
            row = app.state.db.one('SELECT conversation_id FROM analysis_template_runs WHERE id=?', (parameters[-1],))
            terminal_messages.append(app.state.db.all('SELECT role,status FROM messages WHERE conversation_id=? ORDER BY rowid',
                                                     (row['conversation_id'],)))
        return original_execute(sql, parameters)
    monkeypatch.setattr(app.state.db, 'execute', observe_terminal)
    def unavailable(*args):
        raise HTTPException(409, '模型服务配置已失效')
    monkeypatch.setattr(app.state.settings, 'provider', unavailable)
    template = create(client, model)
    response, body = run(client, template, model)
    assert finish(app, body['request_id'])['status'] == 'error'
    cid = response.json()['conversation']['id']
    assert not app.state.chat.generation_status(cid)['running']
    messages = client.get(f'/api/conversations/{cid}/messages').json()
    assert [m['role'] for m in messages] == ['user', 'assistant']
    assert messages[-1]['status'] == 'error' and '配置已失效' in messages[-1]['content']
    assert len(terminal_messages) == 1
    assert [message['role'] for message in terminal_messages[0]] == ['user', 'assistant']
    assert terminal_messages[0][-1]['status'] == 'error'


def test_template_run_can_be_cancelled_through_existing_conversation_controls(setup, monkeypatch):
    app, client, model, _, _ = setup
    entered, release = threading.Event(), threading.Event()
    original = app.state.chat.analysis.execute
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', blocked)
    template = create(client, model)
    response, body = run(client, template, model)
    cid = response.json()['conversation']['id']
    try:
        assert entered.wait(5)
        assert client.post(f'/api/conversations/{cid}/cancel', json={'generation_id': body['request_id']}).status_code == 200
    finally:
        release.set()
    finish(app, body['request_id'])
    assert client.get(f'/api/conversations/{cid}/messages').json()[-1]['status'] == 'cancelled'
    assert client.get(f'/api/analysis-templates/{template["id"]}/runs').json()[0]['status'] == 'cancelled'


def test_template_clarification_stays_in_run_conversation(setup, monkeypatch):
    app, client, model, provider, _ = setup
    class Clarifying:
        def stream(self, *args, **kwargs):
            yield encode({'clarification': {'question': '采用哪种销售口径？', 'topic': '指标',
                'options': [{'label': '销售额', 'value': '按总销售额统计'}, {'label': '销售数量', 'value': '按数量统计'}]}, 'queries': []})
    monkeypatch.setattr(app.state.settings, 'provider', lambda *a: Clarifying())
    template = create(client, model)
    response, body = run(client, template, model)
    finish(app, body['request_id'])
    cid = response.json()['conversation']['id']
    assert client.get(f'/api/analysis-templates/{template["id"]}/runs').json()[0]['status'] == 'clarification'
    monkeypatch.setattr(app.state.settings, 'provider', lambda *a: provider)
    followup = client.post(f'/api/conversations/{cid}/messages', json={'content': '使用总销售额'})
    assert followup.status_code == 200
    records = client.get(f'/api/analysis-templates/{template["id"]}/runs').json()
    assert len(records) == 1 and records[0]['status'] == 'complete'
