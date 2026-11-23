import copy
import json
import threading
from dataclasses import asdict

import pytest

from backend.business_dictionary import BusinessEdit, OverviewEdit, select
from backend.database import encode
from backend.engine.context.base import Column, Measure
from backend.powerbi import fingerprint
from backend.semantics import object_id
from tests.test_semantics import setup, ready, wait_ready, DictionaryProvider
from tests.test_analysis import Provider, plan, result
from tests.test_report_reuse import setup as report_setup, connect, context


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


def test_legacy_unmapped_convention_keeps_its_text_until_reviewed_with_a_mapping(setup):
    app, client, model, ctx, _ = setup
    service = app.state.semantics
    ready(app, model, ctx)
    body = term(kind='convention', title='默认金额单位', meaning='金额默认按千元展示', confirmed=True)
    profile = client.post(url(model, '/items'), json=body).json()
    original, = profile['business_dictionary']['items']
    original['targets'] = []
    service._store(model['id'], profile)

    assert service.summary(model['id'])['dictionary_notice']['pending_business'] == 1
    current = service.get(model['id'])['business_dictionary']
    pending, = current['items']
    assert pending['state'] == 'unresolved' and pending['meaning'] == original['meaning']
    assert service.get(model['id'])['business_dictionary'] == current
    selected = payload(service, model, '默认金额单位')['business_dictionary']
    assert not selected['conventions'] and selected['stale'][0]['id'] == original['id']
    assert original['meaning'] not in encode(selected)

    assert client.put(url(model, '/items/' + original['id']), json=body).is_success
    restored, = payload(service, model, '默认金额单位')['business_dictionary']['conventions']
    assert restored['state'] == 'confirmed' and restored['id'] == original['id']


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
    {'targets': [{'object_id': 'invented'}]},
    {'scope': 'topic'}, {'scope': 'objects'}, {'scope': 'objects', 'scope_object_ids': ['missing']},
    {'filters': [{'object_id': object_id('measure', 'Fact', 'Actual Amount'), 'values': ['1']}]},
    {'filters': [{'object_id': object_id('column', 'Dim Cost Center', 'CC Number'), 'values': ['1', '2']}]},
    {'aliases': ['']}, {'meaning': ''}, {'formula': 'invented DAX'},
])
def test_invalid_or_invented_business_mappings_are_rejected(setup, changes):
    _, client, model, _, _ = setup
    assert client.post(url(model, '/items'), json=term(**changes)).status_code == 422


def test_model_wide_term_can_be_saved_confirmed_and_retrieved_without_a_mapping(setup):
    app, client, model, ctx, _ = setup
    ready(app, model, ctx)
    body = term(scope='model', targets=[])
    response = client.post(url(model, '/items'), json=body)
    assert response.is_success, response.text
    item, = response.json()['business_dictionary']['items']
    assert item['state'] == 'candidate'
    response = client.put(url(model, '/items/' + item['id']), json={**body, 'confirmed': True})
    assert response.is_success, response.text
    saved, = payload(app.state.semantics, model, '解释费用实绩')['business_dictionary']['terms']
    assert saved['state'] == 'confirmed' and saved['meaning'] == body['meaning']
    assert saved['objects'] == [] and saved['filters'] == []


@pytest.mark.parametrize('confirmed', [False, True])
def test_convention_requires_a_target_or_filter_mapping_even_with_scope_objects(setup, confirmed):
    _, client, model, _, _ = setup
    field = object_id('column', 'Dim Cost Center', 'CC Number')
    body = {'kind': 'convention', 'title': '总部分析口径', 'meaning': '默认分析总部费用', 'confirmed': confirmed}
    endpoint = url(model, '/items')
    assert client.post(endpoint, json=body).status_code == 422
    assert client.post(endpoint, json={**body, 'scope': 'objects', 'scope_object_ids': [field]}).status_code == 422
    response = client.post(endpoint, json={**body, 'targets': [{'object_id': field}]})
    assert response.is_success, response.text
    item, = response.json()['business_dictionary']['items']
    item_url = endpoint + '/' + item['id']
    assert client.put(item_url, json=body).status_code == 422
    filters = [{'object_id': field, 'operator': 'eq', 'values': ['0257']}]
    response = client.put(item_url, json={**body, 'filters': filters})
    assert response.is_success, response.text
    updated, = response.json()['business_dictionary']['items']
    assert updated['id'] == item['id'] and updated['filters'] == filters and updated['targets'] == []
    assert updated['state'] == ('confirmed' if confirmed else 'candidate')


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
    common = {'kind': 'convention', 'meaning': '用户未指定时说明采用的默认条件', 'confirmed': True,
              'targets': [{'object_id': object_id('measure', 'Fact', 'Actual Amount')}]}
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


class SummaryProvider(DictionaryProvider):
    def __init__(self,response,entered=None,resume=None):
        super().__init__();self.response=response;self.summaries=[];self.entered_summary=entered;self.resume_summary=resume

    def stream(self,system,messages,**opts):
        evidence=json.loads(messages[0].content)
        if 'object_definitions' not in evidence:
            yield from super().stream(system,messages,**opts)
            return
        self.summaries.append(evidence)
        if self.entered_summary:self.entered_summary.set()
        if self.resume_summary:assert self.resume_summary.wait(4)
        yield encode(self.response)


def test_build_finishes_objects_then_writes_only_overview(setup,monkeypatch):
    app,client,model,ctx,_=setup
    data=client.post(url(model,'/items'),json=term(confirmed=True)).json()['business_dictionary']
    provider=SummaryProvider({'description':'费用模型用途','topics':['费用']})
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    endpoint=f"/api/models/{model['id']}/semantics/build"
    assert client.post(endpoint,json={}).status_code==200
    profile=wait_ready(app,model['id']);result=profile['business_dictionary']
    assert profile['status']=='ready' and result['items']==data['items']
    assert result['overview']['description']=='费用模型用途' and result['overview']['state']=='candidate'
    evidence,=provider.summaries
    assert evidence['counts']['tables']==len(ctx.tables)
    assert all(e['state']=='candidate' and e['meaning'] for e in evidence['object_definitions'])
    assert term()['title'] not in encode(evidence)
    assert client.post(url(model,'/suggest'),json={}).status_code==404
    provider.response={'description':'刷新后的模型总结'}
    client.post(endpoint,json={'mode':'full'})
    updated=wait_ready(app,model['id'])['business_dictionary']
    assert updated['overview']['description']=='刷新后的模型总结' and updated['items']==data['items']


def test_overview_failure_retries_without_rebuilding_objects_or_accepting_ai_terms(setup,monkeypatch):
    app,client,model,ctx,_=setup
    ready(app,model,ctx)
    provider=SummaryProvider({'description':'概览','items':[term()]})
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    endpoint=f"/api/models/{model['id']}/semantics/build"
    client.post(endpoint,json={})
    failed=wait_ready(app,model['id'])
    assert failed['status']=='error' and not failed['pending_ids'] and not failed['business_dictionary']['items']
    provider.response={'description':'仅总结对象词典'}
    client.post(endpoint,json={});done=wait_ready(app,model['id'])
    assert done['status']=='ready' and done['entries']==failed['entries']
    assert done['business_dictionary']['overview']['description']=='仅总结对象词典' and not provider.calls


def test_incremental_overview_uses_preserved_definitions_and_model_relationships(report_setup,monkeypatch):
    app,client,_,engine,built=report_setup
    original=built(connect(client,engine()))
    preserved=next(entry for entry in original['entries'] if entry['state']=='confirmed')
    ctx=context();ctx.tables['Sales'].append(Column('Department','string','Sales'))
    model=connect(client,engine(port=61111,pid=77,ctx=ctx))
    provider=SummaryProvider({'description':'已综合关系和对象生成总结'})
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    assert client.post(f"/api/models/{model['id']}/semantics/build",json={}).status_code==200
    result=wait_ready(app,model['id'])
    changed_ids={obj['id'] for _,batch in provider.calls for obj in batch['objects']}
    assert result['status']=='ready' and object_id('column','Sales','Department') in changed_ids and preserved['id'] not in changed_ids
    evidence,=provider.summaries
    assert any(obj['meaning']==preserved['meaning'] and obj['state']=='confirmed' for obj in evidence['object_definitions'])
    assert evidence['relationships']==[asdict(ctx.relationships[0])]


def test_overview_summary_preserves_concurrent_manual_edit(setup,monkeypatch):
    app,client,model,ctx,_=setup
    ready(app,model,ctx)
    entered=threading.Event();resume=threading.Event()
    provider=SummaryProvider({'description':'自动概览'},entered,resume)
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    client.post(f"/api/models/{model['id']}/semantics/build",json={})
    try:
        assert entered.wait(2)
        client.put(url(model,'/overview'),json={'description':'生成期间手动编辑','confirmed':True})
    finally:resume.set()
    data=wait_ready(app,model['id'])['business_dictionary']
    assert data['overview']['description']=='生成期间手动编辑' and data['overview']['state']=='confirmed'


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
    dictionaries=[json.JSONDecoder().raw_decode(c[0].split('模型语义词典（未确认建议不可视为事实）：\n',1)[1])[0]['business_dictionary'] for c in provider.calls]
    assert dictionaries[0] == dictionaries[1]
    assert dictionaries[1]['overview']['description'] == '工厂费用分析模型'
    assert dictionaries[1]['terms'][0]['title'] == '实际费用'
