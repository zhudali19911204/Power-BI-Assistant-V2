import copy
import json
import threading
import time
from dataclasses import asdict

import pytest

from backend.business_dictionary import BusinessEdit, OverviewEdit, select
from backend.database import encode
from backend.engine.context.base import Measure
from backend.powerbi import fingerprint
from backend.semantics import object_id
from tests.test_semantics import setup, ready
from tests.test_analysis import Provider, plan, result


def url(model, suffix=''):
    return f"/api/models/{model['id']}/semantics/business" + suffix


def term(**extra):
    return {'kind': 'term', 'title': '实际费用', 'meaning': '本模型的实际费用金额', 'aliases': ['费用实绩'],
            'targets': [{'object_id': object_id('measure', 'Fact', 'Actual Amount'), 'purpose': '费用金额指标'}], **extra}


def payload(service, model, question):
    prompt = service.prompt(model['id'], question)
    return json.loads(prompt.split('模型语义词典（未确认建议不可视为事实）：\n')[1])


def test_existing_dictionary_migrates_without_rewriting_native_facts_or_definitions(setup):
    app, client, model, ctx, _ = setup
    p = ready(app, model, ctx)
    del p['business_dictionary']
    for e in p['entries']:e.pop('usage_notes')
    app.state.semantics._store(model['id'], p)
    current = client.get(f"/api/models/{model['id']}/semantics").json()
    assert current['business_dictionary']['items'] == []
    assert all(e['usage_notes'] == '' for e in current['entries'])
    assert [{k: v for k, v in e.items() if k != 'usage_notes'} for e in current['entries']] == p['entries']


def test_crud_preserves_leading_zeros_checks_revisions_and_never_confirms_implicitly(setup):
    app, client, model, ctx, _ = setup
    r = client.put(url(model, '/overview'), json={'description': '用于费用和材料消耗分析', 'topics': ['费用'], 'confirmed': True})
    assert r.status_code == 200
    assert app.state.semantics.summary(model['id'])['can_answer']
    revision = r.json()['business_dictionary']['revision']
    body = term(filters=[{'object_id': object_id('column', 'Dim Cost Center', 'CC Number'), 'operator': 'in', 'values': ['0257', '0810']}])
    r = client.post(url(model, '/items'), json={**body, 'revision': revision})
    assert r.status_code == 200
    data = r.json()['business_dictionary'];item = data['items'][0]
    assert item['state'] == 'candidate' and item['filters'][0]['values'] == ['0257', '0810']
    path = url(model, '/items/' + item['id'])
    assert client.put(path, json={**body, 'confirmed': True, 'revision': revision}).status_code == 409
    r = client.put(path, json={**body, 'confirmed': True, 'revision': data['revision']})
    assert r.json()['business_dictionary']['items'][0]['state'] == 'confirmed'
    assert client.delete(path + '?revision=0').status_code == 409
    r = client.delete(path)
    assert r.status_code == 200 and r.json()['business_dictionary']['items'] == []
    assert item['id'] in r.json()['business_dictionary']['deleted_ids']
    assert client.delete(path).status_code == 404


@pytest.mark.parametrize('changes', [
    {'targets': [{'object_id': 'invented'}]}, {'targets': []},
    {'scope': 'topic'}, {'scope': 'objects'}, {'scope': 'objects', 'scope_object_ids': ['missing']},
    {'filters': [{'object_id': object_id('measure', 'Fact', 'Actual Amount'), 'values': ['1']}]},
    {'filters': [{'object_id': object_id('column', 'Dim Cost Center', 'CC Number'), 'values': ['1', '2']}]},
    {'aliases': ['']}, {'meaning': ''}, {'formula': 'invented DAX'},
])
def test_invalid_or_invented_business_mappings_are_rejected(setup, changes):
    _, client, model, _, _ = setup
    assert client.post(url(model, '/items'), json=term(**changes)).status_code == 422


def test_business_aliases_retrieve_multiple_objects_without_becoming_confirmed_object_aliases(setup):
    app, client, model, ctx, _ = setup
    ready(app, model, ctx)
    targets = term()['targets'] + [{'object_id': object_id('column', 'Dim Material', 'Material Code'), 'purpose': '按物料拆分'}]
    client.post(url(model, '/items'), json=term(targets=targets))
    selected = payload(app.state.semantics, model, '分析费用实绩')
    knowledge = selected['business_dictionary']
    assert knowledge['terms'][0]['state'] == 'candidate'
    assert {e['name'] for e in knowledge['terms'][0]['objects']} == {'Actual Amount', 'Material Code'}
    assert {'Actual Amount', 'Material Code'} <= {e['name'] for e in selected['entries']}
    assert all('费用实绩' not in e['aliases'] for e in selected['entries'])
    assert all('search_aliases' not in e for e in selected['entries'])


def test_overview_and_scoped_conventions_and_budget_are_delivered_separately(setup):
    app, client, model, ctx, _ = setup
    ready(app, model, ctx)
    client.put(url(model, '/overview'), json={'description': '业务全貌说明', 'confirmed': True})
    client.post(url(model, '/items'), json=term(topic='费用', confirmed=True))
    common = {'kind': 'convention', 'meaning': '用户未指定时说明采用的默认条件', 'confirmed': True}
    for item in [dict(title='全局说明'), dict(title='费用分析约定', scope='topic', topic='费用'),
                 dict(title='采购分析约定', scope='topic', topic='采购'),
                 dict(title='成本中心约定', scope='objects', scope_object_ids=[object_id('column', 'Dim Cost Center', 'CC Number')])]:
        assert client.post(url(model, '/items'), json={**common, **item}).status_code == 200
    selected = payload(app.state.semantics, model, '分析费用实绩')
    data = selected['business_dictionary']
    assert data['overview']['description'] == '业务全貌说明'
    assert {e['title'] for e in data['conventions']} == {'全局说明', '费用分析约定'}
    fallback = payload(app.state.semantics, model, 'xyz 不相关问题')['business_dictionary']
    assert fallback['overview'] and {r['title'] for r in fallback['conventions']} == {'全局说明'}
    direct = payload(app.state.semantics, model, '解释成本中心约定')['business_dictionary']
    assert any(r['title'] == '成本中心约定' and r['scope'] == 'objects' for r in direct['conventions'])
    limited = select(app.state.semantics.get(model['id']), selected, '分析费用实绩', budget=100)
    assert limited['overview'] and limited['omitted'] >= 1


def test_formula_change_invalidates_referenced_knowledge_but_not_unrelated_records_or_reconfirmed_records(setup):
    app, client, model, ctx, _ = setup
    service = app.state.semantics
    ready(app, model, ctx)
    client.put(url(model, '/overview'), json={'description': '旧总览应待核对', 'confirmed': True})
    old = client.post(url(model, '/items'), json=term(confirmed=True)).json()['business_dictionary']['items'][0]
    other = term(title='物料编码', meaning='物料标识', aliases=[], targets=[{'object_id': object_id('column', 'Dim Material', 'Material Code')}], confirmed=True)
    client.post(url(model, '/items'), json=other)
    changed = copy.deepcopy(ctx)
    changed.measures = [Measure('Actual Amount', 'Fact', "SUM('Fact'[Amount]) * 2")]
    app.state.db.execute('UPDATE models SET context=?,fingerprint=? WHERE id=?', (encode(asdict(changed)), fingerprint(changed), model['id']))
    p = service.get(model['id'])
    assert p['business_dictionary']['overview']['state'] == 'unresolved'
    assert [e['state'] for e in p['business_dictionary']['items']] == ['unresolved', 'confirmed']
    selected = payload(service, model, '实际费用')['business_dictionary']
    assert not selected['overview'] and selected['stale']
    assert '本模型的实际费用金额' not in encode(selected)
    r = client.put(url(model, '/items/' + old['id']), json=term(confirmed=True, meaning='新确认的费用说明'))
    assert r.status_code == 200
    current_model = app.state.powerbi.get(model['id'])
    p = service._metadata(current_model, current_model['fingerprint'], {})
    assert p['business_dictionary']['items'][0]['state'] == 'confirmed'
    assert payload(service, model, '实际费用')['business_dictionary']['terms'][0]['meaning'] == '新确认的费用说明'


def test_removed_reference_requires_rebinding_before_confirmation(setup):
    app, client, model, ctx, _ = setup
    item = client.post(url(model, '/items'), json=term(confirmed=True)).json()['business_dictionary']['items'][0]
    changed = copy.deepcopy(ctx);changed.measures = []
    app.state.db.execute('UPDATE models SET context=?,fingerprint=? WHERE id=?', (encode(asdict(changed)), fingerprint(changed), model['id']))
    assert client.put(url(model, '/items/' + item['id']), json=term(confirmed=True)).status_code == 422
    assert app.state.semantics.get(model['id'])['business_dictionary']['items'][0]['state'] == 'unresolved'


class DraftProvider:
    def __init__(self, entered=None, resume=None, invalid=False):
        self.entered=entered;self.resume=resume;self.invalid=invalid;self._client=self;self.closed=False
    def close(self):self.closed=True
    def stream(self, system, messages, **kwargs):
        assert 'schema' in json.loads(messages[0].content)
        if self.entered:self.entered.set()
        if self.resume:assert self.resume.wait(5)
        yield encode({'overview': {'description': 'AI 提议的模型用途', 'confirmed': True},
                      'items': [term(confirmed=True, targets=[{'object_id': 'invented'}])] if self.invalid else [term(confirmed=True)]})


def wait_job(app, model):
    deadline=time.monotonic()+5
    while model['id'] in app.state.semantics.business.jobs:
        assert time.monotonic()<deadline
        time.sleep(.01)
    return app.state.semantics.get(model['id'])['business_dictionary']


def test_explicit_ai_generation_keeps_all_output_candidate_and_preserves_existing_overview(setup, monkeypatch):
    app, client, model, ctx, _ = setup
    client.put(url(model, '/overview'), json={'description': '用户已确认的用途', 'confirmed': True})
    provider=DraftProvider()
    monkeypatch.setattr(app.state.settings, 'provider', lambda *args:provider)
    assert client.post(url(model, '/suggest'), json={}).status_code == 200
    data=wait_job(app, model)
    assert data['generation']['status'] == 'ready' and provider.closed
    assert data['overview']['description'] == '用户已确认的用途'
    assert data['items'][0]['state'] == 'candidate' and data['items'][0]['reviewed_at'] is None


@pytest.mark.parametrize('edit_during_generation', [True, False])
def test_ai_generation_rejects_invented_objects_and_does_not_overwrite_concurrent_user_edits(setup, monkeypatch, edit_during_generation):
    app, client, model, _, _ = setup
    entered=threading.Event();resume=threading.Event()
    provider=DraftProvider(entered, resume, invalid=not edit_during_generation)
    monkeypatch.setattr(app.state.settings, 'provider', lambda *args:provider)
    client.post(url(model, '/suggest'), json={})
    assert entered.wait(2)
    if edit_during_generation:client.put(url(model, '/overview'), json={'description': '生成期间的手动编辑', 'confirmed': True})
    resume.set();data=wait_job(app, model)
    assert not data['items']
    if edit_during_generation:
        assert data['overview']['description'] == '生成期间的手动编辑'
        assert '未写入' in data['generation']['stage']
    else:assert data['generation']['status'] == 'error'


def test_query_planning_and_final_summary_share_business_context(setup, monkeypatch):
    app, client, model, ctx, conv = setup
    ready(app, model, ctx)
    client.put(url(model, '/overview'), json={'description': '工厂费用分析模型', 'confirmed': True})
    client.post(url(model, '/items'), json=term(confirmed=True))
    provider=Provider([plan(), '实际费用为 125。[Q1]'])
    monkeypatch.setattr(app.state.settings, 'provider', lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis, 'execute', lambda *args:result())
    r=client.post(f"/api/conversations/{conv['id']}/messages", json={'content':'分析费用实绩'})
    assert r.status_code == 200 and len(provider.calls) == 2
    dictionaries=[json.loads(c[0].split('模型语义词典（未确认建议不可视为事实）：\n')[1].split('\n本轮编号')[0].split('\n本会话')[0])['business_dictionary'] for c in provider.calls]
    assert dictionaries[0] == dictionaries[1]
    assert dictionaries[1]['overview']['description'] == '工厂费用分析模型'
    assert dictionaries[1]['terms'][0]['title'] == '实际费用'
