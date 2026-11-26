import json
import threading
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.analysis_templates import suggested_template_name
from backend.database import Database, encode, now, uid
from backend.powerbi import context_from_dict
from backend.template_parameters import parameter_names, recognize_parameters
from backend.template_queries import compile_queries, prepare_queries


class Provider:
    def __init__(self):
        self.calls = []

    def stream(self, system, messages, **options):
        self.calls.append((system, messages))
        if '严格只返回 JSON' in system:
            query = 'EVALUATE ROW("Value",[总销售额])'
            if 'actual first-run values:' in system:
                task = json.JSONDecoder().raw_decode(system.partition('actual first-run values:\n')[2])[0]
                args = ', '.join(f'"Input{i}", {{{{{name}}}}}' for i,name in enumerate(parameter_names(task['task'])))
                if args:
                    query = f'EVALUATE ROW("Value",[总销售额], {args})'
            yield encode({'scope': '使用本次指定的月份', 'queries': [
                {'title': '销售总额', 'dax': query}]})
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
    assert fresh[-1]['analysis'][0]['dax'] == original[-1]['analysis'][0]['dax']
    assert fresh[-1]['analysis'][0]['parameters'] == {'P1':'2026-11'}
    assert original[-1]['analysis'][0]['parameters'] == {'P1':'2026-10'}
    assert len(provider.calls) == 3 and '严格只返回 JSON' not in provider.calls[-1][0]
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


def test_concurrent_first_runs_prepare_one_definition(setup, monkeypatch):
    app, client, model, provider, _ = setup
    entered, release = threading.Event(), threading.Event()
    stream = provider.stream

    def wait_for_plan(system, messages, **options):
        if 'actual first-run values:' in system:
            entered.set()
            assert release.wait(5)
        yield from stream(system, messages, **options)

    monkeypatch.setattr(provider, 'stream', wait_for_plan)
    template = create(client, model)
    first, request = run(client, template, model)
    try:
        assert first.status_code == 200 and entered.wait(5)
        replay, _ = run(client, template, model, **request)
        assert replay.json()['id'] == first.json()['id']
        concurrent, _ = run(client, template, model, values={'月份': '2027-01'})
        assert concurrent.status_code == 409
        assert len(app.state.db.all('SELECT id FROM analysis_template_runs')) == 1
    finally:
        release.set()
    first_snapshot = json.loads(finish(app, request['request_id'])['snapshot'])
    later, request = run(client, template, model, values={'月份': '2027-01'})
    assert later.status_code == 200
    later_snapshot = json.loads(finish(app, request['request_id'])['snapshot'])
    assert first_snapshot['queries'] == later_snapshot['queries']
    assert sum('actual first-run values:' in system for system, _ in provider.calls) == 1


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


def test_blank_or_omitted_values_prefer_saved_defaults_then_last_values_per_template(setup):
    app, client, model, _, _ = setup
    template = create(client, model, prompt='比较 {{工厂}} 在 {{月份}} 的销售额', defaults={'工厂': '', '月份': '2026-05'})
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
                                   'defaults': {'工厂': '0001', '数量': '5'},
                                   'queries':[{'title':'前几项', 'dax':'EVALUATE TOPN(VALUE({{数量}}),FILTER(\'工厂\',\'工厂\'[编号]={{工厂}}))'}]}).json()
    assert edited['last_values'] == {'工厂': '0887'}
    third, body = run(client, edited, model, values={'工厂': '', '数量': '0'})
    assert third.status_code == 200
    assert json.loads(finish(app, body['request_id'])['snapshot'])['values'] == {'工厂': '0001', '数量': '0'}


def test_saved_material_reaches_the_query_preview_snapshot_and_summary_after_a_previous_run(setup, monkeypatch):
    app, client, model, provider, _ = setup
    original_query = {'title':'{{物料}} 月度价格', 'dax':'EVALUATE ROW("Material",{{物料}})'}
    template = create(client, model, prompt='查看 {{物料}}。按 {{物料}} 筛选。',
                      defaults={'物料':'XR40009'}, queries=[original_query])
    path = '/api/analysis-templates/' + template['id']
    execute = app.state.chat.analysis.execute
    commands = []

    def query(model, dax, parameters):
        commands.append((dax, parameters))
        return {**execute(model, dax), 'columns':['Material'], 'rows':[{'Material':parameters['P1']}]}

    monkeypatch.setattr(app.state.chat.analysis, 'execute', query)

    def run_for(version, values, material):
        result, request = run(client, version, model, values=values)
        assert result.status_code == 200, result.text
        snapshot = json.loads(finish(app, request['request_id'])['snapshot'])
        assert snapshot['values'] == {'物料':material}
        assert snapshot['prompt'] == f'查看 {material}。按 {material} 筛选。'
        assert commands[-1] == ('EVALUATE ROW("Material",@P1)', {'P1':material})
        summary = json.loads(provider.calls[-1][1][-1].content.split('\n',1)[1])
        assert summary['template_parameters'] == snapshot['values']
        evidence = summary['evidence'][0]
        assert evidence['columns'] == ['Material']
        assert evidence['rows'] == [[material]]
        conversation = result.json()['conversation']['id']
        message = client.get(f'/api/conversations/{conversation}/messages').json()[-1]
        draft = client.get(f'/api/messages/{message["id"]}/analysis-template').json()
        assert draft['name'] == f'{material} 月度价格'
        assert draft['queries'] == [original_query]
        assert draft['prompt'] == version['prompt']

    run_for(template, {}, 'XR40009')
    response = client.put(path, json={'revision':template['revision'], 'name':template['name'],
                                      'prompt':template['prompt'], 'defaults':{'物料':'XR40010'}})
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited['queries'] == template['queries']
    assert edited['last_values'] == {'物料':'XR40009'}
    assert edited['run_values'] == {'物料':'XR40010'}
    run_for(edited, {}, 'XR40010')
    run_for(edited, {'物料':'XR40011'}, 'XR40011')
    assert client.get(path).json()['run_values'] == {'物料':'XR40010'}
    run_for(edited, {'物料':'   '}, 'XR40010')
    assert [r['values']['物料'] for r in reversed(client.get(path+'/runs').json())] == [
        'XR40009', 'XR40010', 'XR40011', 'XR40010']


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
    fixed = client.post('/api/analysis-templates/parameters/recognize', json={
        'model_id': model['id'], 'source_message_id': 'answer', 'prompt': prompt,
        'queries': [{'title':'费用', 'dax':'EVALUATE FILTER(工厂, 工厂[CC Number]="0887" && [总销售额]>0)'}],
    })
    assert fixed.status_code == 200
    bindable = [item['name'] for item in fixed.json()['candidates'] if item['bindable']]
    assert bindable == ['成本中心']
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
    # An older run could store its entire expanded task as the query scope.
    app.state.db.execute("UPDATE analysis_queries SET result=json_set(result,'$.scope',?) WHERE message_id=?",
                         (messages[0]['content'], messages[-1]['id']))
    legacy_draft = client.get(f'/api/messages/{messages[-1]["id"]}/analysis-template').json()
    assert legacy_draft['name'] == '销售总额'
    client.delete(f'/api/conversations/{conversation_id}')
    assert next(r for r in client.get('/api/analysis-templates').json() if r['id'] == saved.json()['id'])['source_message_id'] is None
    assert client.get(f'/api/messages/{messages[0]["id"]}/analysis-template').status_code == 404


def test_template_uses_only_the_selected_turn_and_successful_scope_after_topic_changes(setup, monkeypatch):
    app, client, model, provider, _ = setup
    first_scope = '筛选十月原材料价格环比波动超过 5% 的物料'
    current_scope = '2026 年 XR40010 的月度移动平均单价和标准价对比'
    chart_scope = 'XR40010 在 2026 年的月度移动平均单价'
    questions = ['列出波动较大的原材料', '换成按月看这个物料的两项单价', '帮我生成折线图\n\n横轴按月份。']
    replies = iter([
        encode({'scope':first_scope, 'queries':[{'title':'原材料价格环比波动', 'dax':'EVALUATE ROW("Change",1)'}]}),
        '已取得环比数据。[Q1]',
        encode({'scope':current_scope + '。按月份排序。', 'queries':[
            {'title':'月度移动平均单价', 'dax':'EVALUATE ROW("Moving price",2)'},
            {'title':'标准价对比', 'dax':'EVALUATE ROW("Standard price",3)'}]}),
        '已取得该物料的两项价格数据。[Q1][Q2]',
        encode({'scope':chart_scope, 'queries':[{'title':'月度移动平均单价', 'dax':'EVALUATE ROW("Price",2)'}]}),
        '已取得折线图数据。[Q1]',
        '已按本次查询生成折线图。[Q1]',
    ])
    monkeypatch.setattr(provider, 'stream', lambda *args, **kwargs: iter([next(replies)]))
    cid = client.post('/api/conversations', json={'model_id':model['id'], 'mode':'analysis'}).json()['id']
    path = f'/api/conversations/{cid}/messages'
    for prompt in questions:
        response = client.post(path, json={'content':prompt})
        assert response.status_code == 200, response.text
        assert json.loads(response.text.splitlines()[-1])['type'] == 'done', response.text
    answers = [m for m in client.get(path).json() if m['role'] == 'assistant']
    drafts = [client.get(f'/api/messages/{m["id"]}/analysis-template').json() for m in answers]
    assert [draft['name'] for draft in drafts] == [first_scope, current_scope, chart_scope]
    assert [draft['prompt'] for draft in drafts] == [
        question + '\n\n分析口径（保存前请核对）：\n' + scope for question,scope in
        zip(questions, [first_scope, current_scope+'。按月份排序。', chart_scope])]
    saved = client.post('/api/analysis-templates', json={**drafts[-1], 'name':'我的物料价格月报'})
    assert saved.status_code == 201 and saved.json()['name'] == '我的物料价格月报'
    response, request = run(client, saved.json(), model)
    assert response.status_code == 200
    finished = finish(app, request['request_id'])
    assert finished['status'] == 'finished' and json.loads(finished['snapshot'])['prompt'] == drafts[-1]['prompt']


def test_legacy_task_removes_only_verified_history_and_preserves_bindings_and_old_runs(setup, tmp_path):
    app, client, model, _, _ = setup
    db = app.state.db
    cid = client.post('/api/conversations', json={'model_id':model['id'], 'mode':'analysis'}).json()['id']
    first, trend, chart = '筛选涨跌超过5%的物料', '看 2026 年 XR40007 的趋势', '生成折线图\n\n按月展示'
    for question in (first, trend, chart):
        app.state.chat.images.save_user(cid, question)
    mid = uid()
    db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)', (mid,cid,'assistant','折线图结果','complete',now()))
    dax = 'EVALUATE ROW("Material",{{物料}},"Year",VALUE({{年份}}))'
    db.execute('INSERT INTO analysis_queries VALUES (?,?,?,?,?,?,?)',
               (uid(),mid,model['id'],'月度单价',dax,encode({'ok':True,'scope':'月度移动平均单价'}),now()))
    defaults = {'年份':'2026','物料':'XR40007'}
    current = chart + '\n\n分析口径（保存前请核对）：\n{{物料}} 的月度移动平均单价'
    legacy = first + '\n\n看 {{年份}} 年 {{物料}} 的趋势\n\n' + current
    saved = create(client,model,prompt=legacy,defaults=defaults,source_message_id=mid)
    manual = create(client,model,prompt='我的固定说明\n\n'+legacy,defaults=defaults,source_message_id=mid)
    response, request = run(client,saved,model)
    assert response.status_code == 200
    archived = finish(app,request['request_id'])['snapshot']
    answer = app.state.chat.messages(response.json()['conversation']['id'])[-1]['id']
    db.execute('PRAGMA user_version=1')
    with TestClient(create_app(tmp_path)) as upgraded:
        cleaned = upgraded.get('/api/analysis-templates/'+saved['id']).json()
        assert cleaned['prompt'] == current and cleaned['revision'] == saved['revision']+1
        assert cleaned['defaults'] == defaults and cleaned['queries'] == saved['queries']
        assert upgraded.get('/api/analysis-templates/'+manual['id']).json()['prompt'] == manual['prompt']
        assert upgraded.get('/api/messages/'+answer+'/analysis-template').json()['prompt'] == current
    assert db.one('SELECT snapshot FROM analysis_template_runs WHERE id=?',(request['request_id'],))['snapshot'] == archived
    with TestClient(create_app(tmp_path)) as reopened:
        assert reopened.get('/api/analysis-templates/'+saved['id']).json()['revision'] == cleaned['revision']


def test_template_name_uses_available_query_metadata_and_fits_the_editor():
    record = {'executed_title':'Query 2', 'columns_json':encode(['Month','Net Sales'])}
    assert suggested_template_name([record], ['Monthly net sales by plant. Sorted by month.']) == 'Monthly net sales by plant'
    assert suggested_template_name([record], []) == 'Month / Net Sales'
    by_month = {**record, 'executed_title':'Net sales'}
    assert suggested_template_name([by_month, by_month], ['January 2026']) == 'Net sales · January 2026'
    long_name = suggested_template_name([{**record, 'executed_title':'月度价格' * 40}], [])
    assert len(long_name) <= 120 and long_name.endswith('…')


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
    answer = app.state.chat.messages(cid)[-1]['id']
    draft = client.get(f'/api/messages/{answer}/analysis-template').json()
    assert draft['prompt'].split('\n\n',1)[0] == '使用总销售额'
    assert draft['defaults'] == template['defaults']
    snapshot = app.state.db.one('SELECT snapshot FROM analysis_template_runs WHERE conversation_id=?', (cid,))
    assert '{{月份}}' in json.loads(snapshot['snapshot'])['queries'][0]['dax']
    records = client.get(f'/api/analysis-templates/{template["id"]}/runs').json()
    assert len(records) == 1 and records[0]['status'] == 'complete'
    client.delete('/api/conversations/' + cid)
    rerun, request = run(client, template, model, values={'月份':'2026-11'})
    assert rerun.status_code == 200 and finish(app, request['request_id'])['status'] == 'finished'
    assert len(provider.calls) == 3


def test_source_queries_survive_deletion_restart_and_only_receive_new_bound_values(setup,tmp_path,monkeypatch):
    app,client,model,provider,_=setup
    stamp=now()
    cid=client.post('/api/conversations',json={'model_id':model['id'],'mode':'analysis'}).json()['id']
    app.state.chat.images.save_user(cid,'Sales for 0087 in 2026-05')
    mid=uid()
    app.state.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(mid,cid,'assistant','Old analysis','complete',stamp))
    source_dax='EVALUATE FILTER(\'Sales\',\'Sales\'[Plant]="0087")'
    date_dax='EVALUATE ROW("Start",DATE(2026,5,1),"End",DATE(2026,5,31))'
    for label,dax,ok in [('Q1','EVALUATE Broken()',False),('Q1',source_dax,True),('Q2',date_dax,True)]:
        qid=uid()
        record={'id':qid,'label':label,'title':label+' · 2026-05' if ok else 'Obsolete price check','dax':dax,'ok':ok,
                'scope':'Sales scope' if ok else 'Discard this scope',
                'attempt':1,'row_count':1,'columns':['Prior'],'rows':[{'Prior':99991}]}
        app.state.db.execute('INSERT INTO analysis_queries VALUES (?,?,?,?,?,?,?)',(qid,mid,model['id'],record['title'],dax,encode(record),stamp))
    seed=client.get(f'/api/messages/{mid}/analysis-template').json()
    assert 'Obsolete' not in seed['name'] and 'Discard' not in seed['name']
    assert [q['dax'] for q in seed['queries']]==[source_dax,date_dax]
    seed.update(prompt='Sales for {{Plant}} in {{Month}}',defaults={'Plant':'0087','Month':'2026-05'})
    response=client.post('/api/analysis-templates',json=seed)
    assert response.status_code==201,response.text
    template=response.json()
    client.delete('/api/conversations/'+cid)
    with TestClient(create_app(tmp_path)) as restarted:
        assert restarted.get('/api/analysis-templates/'+template['id']).json()['queries']==template['queries']
    calls=[]
    def execute(model,dax,parameters):
        calls.append((dax,parameters))
        return {'ok':True,'run_verified':True,'columns':['Fresh'],'rows':[{'Fresh':len(calls)*123}],
                'row_count':1,'truncated':False,'warnings':[],'error':'','seconds':0}
    monkeypatch.setattr(app.state.chat.analysis,'execute',execute)
    for values in ({},{'Plant':'North "A"','Month':'2027-02'}):
        started,body=run(client,template,model,values=values)
        assert started.status_code==200,started.text
        assert finish(app,body['request_id'])['status']=='finished'
    assert [c[0] for c in calls[:2]]==[c[0] for c in calls[2:]]
    assert [c[1] for c in calls]==[{'P1':'0087'},{'P2_year':'2026','P2_month':'5'},
                                    {'P1':'North "A"'},{'P2_year':'2027','P2_month':'2'}]
    assert len(provider.calls)==2 and all('严格只返回 JSON' not in system for system,_ in provider.calls)
    summary=provider.calls[-1][1][-1].content
    assert '492' in summary and '99991' not in summary and '0087' not in summary
    export=client.get('/api/conversations/'+started.json()['conversation']['id']+'/export?language=en').text
    assert 'Query parameters' in export and '@P1' in export and '2027' in export


def test_fixed_multibatch_keeps_order_and_never_requests_repairs_or_alternative_queries(setup,monkeypatch):
    app,client,model,provider,_=setup
    queries=[{'title':f'Q{i}','dax':f'EVALUATE ROW("x",{i})'} for i in range(6)]
    template=create(client,model,prompt='Run six saved queries',defaults={},queries=queries)
    calls=[]
    def execute(model,dax):
        calls.append(dax)
        return {'ok':len(calls)%2==0,'run_verified':True,'rows':[{'x':42}] if len(calls)%2==0 else [],
                'columns':['x'],'row_count':int(len(calls)%2==0),'truncated':True,'seconds':0,
                'warnings':[],'error':'' if len(calls)%2==0 else 'Field no longer available'}
    monkeypatch.setattr(app.state.chat.analysis,'execute',execute)
    response,body=run(client,template,model)
    finish(app,body['request_id'])
    evidence=app.state.chat.messages(response.json()['conversation']['id'])[-1]['analysis']
    assert calls==[q['dax'] for q in queries]
    assert [q['batch'] for q in evidence]==[1,1,1,1,2,2]
    assert len(provider.calls)==1 and '严格只返回 JSON' not in provider.calls[0][0]
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:{**evidence[0],'ok':False,'rows':[],'row_count':0})
    response,body=run(client,template,model)
    assert finish(app,body['request_id'])['status']=='error'
    assert app.state.chat.messages(response.json()['conversation']['id'])[-1]['status']=='error'
    assert len(provider.calls)==1
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *a: {
        **evidence[0], 'ok':True, 'rows':[], 'row_count':0, 'truncated':False, 'error':''})
    response, body = run(client, template, model)
    assert finish(app, body['request_id'])['status'] == 'finished'
    assert '返回 0 行' in app.state.chat.messages(response.json()['conversation']['id'])[-1]['content']
    assert len(provider.calls) == 1


def test_legacy_template_adopts_saved_queries_and_rejects_unbound_or_invalid_parameters(setup,tmp_path):
    app,client,model,provider,_=setup
    template=create(client,model)
    response,body=run(client,template,model)
    finish(app,body['request_id'])
    path='/api/analysis-templates/'+template['id']
    original=client.get(path).json()['queries']
    app.state.db.execute('UPDATE analysis_templates SET queries_json=NULL WHERE id=?',(template['id'],))
    with TestClient(create_app(tmp_path)) as restarted:
        restarted.delete('/api/conversations/'+response.json()['conversation']['id'])
        assert restarted.get(path).json()['queries'] == original
    assert client.get(path).json()['queries']==original
    bad={'title':'Unbound','dax':'EVALUATE ROW("月份","{{月份}}") // {{月份}}'}
    assert client.put(path,json={'name':'Bad','revision':1,'prompt':'{{月份}}','queries':[bad]}).status_code==422
    assert client.get(path).json()['queries']==original
    numeric=create(client,model,prompt='Top {{n}}',defaults={'n':'5'},queries=[{'title':'Top','dax':'EVALUATE ROW("n",VALUE({{n}}))'}])
    assert run(client,numeric,model,values={'n':'all'})[0].status_code==422
    assert client.get('/api/analysis-templates/'+numeric['id']+'/runs').json()==[]
    assert len(provider.calls)==2


def test_bindings_skip_identifiers_aliases_and_conflicting_fields_and_preserve_date_boundaries():
    prompt='Sales in {{Month}} at {{Plant}}'
    where='\'Sales\'[Plant]="0087" && \'Sales\'[Date]>=DATE(2026,12,1) && \'Sales\'[Date]<DATE(2027,1,1)'
    raw=[{'title':'2026-12 sales','dax':'// 0087\nEVALUATE SELECTCOLUMNS(FILTER(\'Sales\','+where+'),"0087",\'Sales\'[0087])'}]
    prepared=prepare_queries(raw,prompt,{'Month':'2026-12','Plant':'0087'})
    cmd=compile_queries(prepared,parameter_names(prompt),{'Month':'2028-02','Plant':'0025'})[0]
    assert cmd.title=='2028-02 sales' and 'EDATE(' in cmd.dax and 'DATE(2027,1,1)' not in cmd.dax
    assert cmd.dax.startswith('// 0087') and ',"0087",\'Sales\'[0087]' in cmd.dax
    assert cmd.parameters=={'P2':'0025','P1_year':'2028','P1_month':'2'}
    ambiguous=[{'title':'Filters','dax':'EVALUATE FILTER(T,T[A]="0087" && T[B]="0087")'}]
    assert prepare_queries(ambiguous,'{{Plant}}',{'Plant':'0087'})==ambiguous
    with pytest.raises(HTTPException):compile_queries(ambiguous,['Plant'])


def test_month_binds_text_keys_date_literals_and_date_parts():
    prompt = 'Sales for {{Month}}'
    raw = [{'title': 'Sales', 'dax': '''EVALUATE FILTER('Calendar',
        'Calendar'[Period]="2026-05" && YEAR('Calendar'[Date])=2026 && MONTH([Date])=5 &&
        'Calendar'[Date]>=dt"2026-05-01" && 'Calendar'[Date]<dt"2026-06-01")'''}]
    prepared = prepare_queries(raw, prompt, {'Month': '2026年5月'})
    may = compile_queries(prepared, ['Month'], {'Month': '2026年5月'})[0]
    feb = compile_queries(prepared, ['Month'], {'Month': '2028-02'})[0]
    assert may.dax == feb.dax
    assert all(original not in feb.dax for original in ('2026', '=5', 'dt"'))
    assert 'FORMAT(VALUE(@P1_month),"00")' in feb.dax and 'EDATE(' in feb.dax
    assert feb.parameters == {'P1_year': '2028', 'P1_month': '2'}


def test_binding_review_resolves_repeated_codes_by_field_without_changing_other_literals(setup):
    _, client, model, _, _ = setup
    dax = 'EVALUATE FILTER(T,T[Plant]="0087" && T[Vendor]="0087")'
    preview = {'queries':[{'title':'Codes', 'dax':dax}], 'prompt':'{{Plant}} and {{Vendor}}',
               'samples':{'Plant':'0087','Vendor':'0087'}}
    route = '/api/analysis-templates/queries/preview'
    offered = client.post(route, json=preview).json()
    assert offered['queries'][0]['dax'] == dax
    assert [item['name'] for item in offered['unresolved']] == ['Plant','Vendor']
    assert offered['unresolved'][0]['options'] == {'t[plant]':'T[Plant]', 't[vendor]':'T[Vendor]'}
    conflict = client.post(route, json={**preview, 'choices':dict.fromkeys(preview['samples'],'t[plant]')}).json()
    assert all(item['conflict'] for item in conflict['unresolved'])
    assert conflict['queries'][0]['dax'] == dax
    resolved = client.post(route, json={**preview, 'choices':{'Plant':'t[plant]', 'Vendor':'t[vendor]'}}).json()
    assert resolved['unresolved'] == []
    template = create(client, model, prompt=preview['prompt'],defaults=preview['samples'],queries=resolved['queries'])
    command = compile_queries(template['queries'], template['parameters'], {'Plant':'0003','Vendor':'0031'})[0]
    assert command.dax == 'EVALUATE FILTER(T,T[Plant]=@P1 && T[Vendor]=@P2)'
    assert command.parameters == {'P1':'0003','P2':'0031'}


def test_query_only_parameters_are_listed_and_passed_to_queries_and_summary_from_one_form(setup):
    app, client, model, provider, _ = setup
    query = 'EVALUATE ROW("{{Alias}}",{{物料}},"月份",VALUE({{月份.month}})) // {{注释}}'
    template = create(client, model, prompt='查看月度价格', defaults={'物料':'XR40007','月份':'2026-10'},
                      queries=[{'title':'月度价格','dax':query}])
    assert template['parameters'] == ['物料','月份']
    assert client.get('/api/analysis-templates').json()[0]['parameters'] == template['parameters']
    started, body = run(client,template,model,values={'物料':'XR40099','月份':'2026-11'})
    assert started.status_code == 200, started.text
    assert finish(app, body['request_id'])['status'] == 'finished'
    evidence = app.state.chat.messages(started.json()['conversation']['id'])[-1]['analysis'][0]
    assert evidence['parameters'] == {'P1':'XR40099','P2_month':'11'}
    payload = json.loads(provider.calls[-1][1][-1].content.split('\n', 1)[1])
    assert payload['template_parameters'] == {'物料':'XR40099','月份':'2026-11'}
    path = '/api/analysis-templates/' + template['id']
    changed = client.put(path, json={'revision':1,'name':'新名称','prompt':template['prompt'],'defaults':template['defaults']})
    assert changed.status_code == 200 and changed.json()['last_values']['物料'] == 'XR40099'
