import json
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import encode
from backend.engine.context.base import Column, Measure, ModelContext, Relationship
from backend.powerbi import fingerprint
from backend.semantics import SemanticEdit, object_id


@pytest.fixture
def setup(tmp_path,monkeypatch):
    app=create_app(tmp_path)
    ctx=ModelContext(tables={'Dim Cost Center':[Column('CC Number','string','Dim Cost Center')],
                            'Dim Material':[Column('Material Code','string','Dim Material')],
                            'Fact':[Column('Amount','double','Fact')]},
                     measures=[Measure('Actual Amount','Fact',"SUM('Fact'[Amount])")],
                     relationships=[])
    model=app.state.powerbi.save('真实模型测试','desktop',50001,ctx)
    monkeypatch.setattr(app.state.powerbi,'require_live',lambda *a,**k:(model,ctx))
    with TestClient(app) as client:
        conv=client.post('/api/conversations',json={'model_id':model['id'],'mode':'analysis'}).json()
        yield app,client,model,ctx,conv


def ready(app,model,ctx):
    p=app.state.semantics.ensure(model,ctx)
    p.update(status='ready',coverage=len(p['entries']))
    app.state.semantics._store(model['id'],p)
    return p


class DictionaryProvider:
    def __init__(self,entered=None,resume=None):self.calls=[];self.entered=entered;self.resume=resume;self._client=self;self.closed=False
    def close(self):self.closed=True
    def stream(self,system,messages,**opts):
        payload=json.loads(messages[0].content)
        self.calls.append((system,payload))
        if self.entered:self.entered.set()
        if self.resume:assert self.resume.wait(3)
        yield encode({'entries':[{'id':e['id'],'business_name':'候选名称','meaning':'根据对象名推测，业务含义待确认',
                                 'role':'候选角色','unit':'','aliases':[]} for e in payload['objects']]})


def wait_ready(app,model_id):
    deadline=time.monotonic()+4
    while model_id in app.state.semantics.jobs:
        assert time.monotonic()<deadline
        time.sleep(.01)
    return app.state.semantics.get(model_id)


def test_inventory_covers_every_table_field_and_measure_and_preserves_evidence(setup):
    app,client,model,ctx,conv=setup
    p=client.get(f"/api/models/{model['id']}/semantics").json()
    assert len(p['entries'])==len(ctx.tables)+sum(len(c) for c in ctx.tables.values())+len(ctx.measures)
    assert all(e['state']=='unresolved' for e in p['entries'])
    assert all(e['unit']=='' for e in p['entries'])
    measure=next(e for e in p['entries'] if e['kind']=='measure')
    assert measure['facts']['expression']=="SUM('Fact'[Amount])"
    assert p['status']=='inventory'


@pytest.mark.parametrize('mode',['dax','analysis'])
def test_semantic_build_is_a_prerequisite_for_real_model_generation(setup,monkeypatch,mode):
    app,client,model,ctx,conv=setup
    app.state.db.execute('UPDATE conversations SET mode=? WHERE id=?',(mode,conv['id']))
    def unexpected():raise AssertionError('No model generation before semantics are built')
    monkeypatch.setattr(app.state.settings,'provider',unexpected)
    r=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'分析金额'})
    assert r.status_code==409 and '语义' in r.json()['detail']
    assert not app.state.chat.generation_status(conv['id'])['running']
    assert not app.state.db.all('SELECT * FROM messages')


def test_background_build_reads_model_descriptions_and_never_auto_confirms_candidates(setup,monkeypatch):
    app,client,model,ctx,conv=setup
    provider=DictionaryProvider()
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    class Source:
        def __init__(self,*a):pass
        def semantic_metadata(self):return {('column','Dim Cost Center','CC Number'):{'description':'成本中心编号，保留前导零','actual_dtype':'string'}}
    monkeypatch.setattr('backend.semantics.LiveDesktopSource',Source)
    r=client.post(f"/api/models/{model['id']}/semantics/build",json={})
    assert r.status_code==200
    p=wait_ready(app,model['id'])
    assert p['status']=='ready' and p['coverage']==len(p['entries'])
    assert all(e['state']=='candidate' and not e['reviewed_at'] for e in p['entries'])
    entry=next(e for e in p['entries'] if e['name']=='CC Number')
    assert entry['facts']['description']=='成本中心编号，保留前导零'
    assert provider.closed
    assert not app.state.db.all('SELECT * FROM audit')


def test_user_review_wins_over_a_running_inference_and_survives_restart(setup,monkeypatch):
    app,client,model,ctx,conv=setup
    entered,resume=threading.Event(),threading.Event();provider=DictionaryProvider(entered,resume)
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr('backend.semantics.LiveDesktopSource',lambda *a:SimpleNamespace(semantic_metadata=lambda:{}))
    client.post(f"/api/models/{model['id']}/semantics/build",json={})
    try:
        assert entered.wait(2)
        entry=object_id('column','Dim Cost Center','CC Number')
        r=client.put(f"/api/models/{model['id']}/semantics/{entry}",json={
            'business_name':'成本中心编号','meaning':'成本中心的文本编号，前导零是编号的一部分','aliases':['成本中心'],'confirmed':True})
        assert r.status_code==200
    finally:resume.set()
    p=wait_ready(app,model['id'])
    saved=next(e for e in p['entries'] if e['id']==entry)
    assert saved['state']=='confirmed' and saved['business_name']=='成本中心编号'
    restarted=create_app(app.state.db.path.parent)
    assert next(e for e in restarted.state.semantics.get(model['id'])['entries'] if e['id']==entry)['state']=='confirmed'


@pytest.mark.parametrize('mode',['analysis','dax'])
def test_bare_0257_is_confirmed_before_any_provider_or_data_query(setup,monkeypatch,mode):
    app,client,model,ctx,conv=setup
    ready(app,model,ctx);app.state.db.execute('UPDATE conversations SET mode=? WHERE id=?',(mode,conv['id']))
    monkeypatch.setattr(app.state.settings,'provider',lambda:pytest.fail('Do not guess the code mapping'))
    r=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'分析2026年0257人员费用'})
    assert r.status_code==200
    events=[json.loads(line) for line in r.text.splitlines()]
    card=next(e['clarification'] for e in events if e['type']=='clarification')
    assert card['topic']=='编号归属' and '0257' in card['question']
    assert not app.state.db.all('SELECT * FROM analysis_queries')
    assert not app.state.db.all('SELECT * FROM conversation_bindings')


def test_number_mapping_only_becomes_authoritative_after_the_user_selects_a_field(setup,monkeypatch):
    app,client,model,ctx,conv=setup
    ready(app,model,ctx)
    initial=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'2026年0257人员费用'})
    assert initial.status_code==200
    messages=client.get(f"/api/conversations/{conv['id']}/messages").json();question=messages[-1]
    card=question['clarification']
    option=next(o for o in card['options'] if 'CC Number' in o['value'])
    from tests.test_analysis import Provider
    provider=Provider([encode({'clarification':None,'queries':[]})])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    response=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'选择编号归属',
        'clarification_reply':{'message_id':question['id'],'option_id':option['id']}})
    assert response.status_code==200
    bound=app.state.semantics.bindings(conv['id'])
    assert bound==[{'literal':'0257','table_name':'Dim Cost Center','column_name':'CC Number'}]
    assert '用户确认' in provider.calls[0][0] and 'CC Number' in provider.calls[0][0]
    other=client.post('/api/conversations',json={'model_id':model['id'],'mode':'analysis'}).json()
    assert not app.state.semantics.bindings(other['id'])


def test_old_assistant_assumption_is_not_treated_as_a_user_confirmation(setup):
    app,client,model,ctx,conv=setup
    ready(app,model,ctx)
    from backend.database import uid,now
    app.state.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(uid(),conv['id'],'user','看2026年0257费用','complete',now()))
    app.state.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(uid(),conv['id'],'assistant','0257是成本中心编号，CNY金额','complete',now()))
    r=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'继续找最高的月份'})
    assert r.status_code==200 and '编号归属' in r.text
    assert not app.state.semantics.bindings(conv['id'])


def test_an_unrelated_field_reference_does_not_resolve_a_bare_number(setup):
    app,client,model,ctx,conv=setup;ready(app,model,ctx)
    r=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':"计算 'Fact'[Amount]，0257每月费用"})
    assert '编号归属' in r.text and not app.state.semantics.bindings(conv['id'])


def test_an_explicit_qualified_field_assignment_is_user_intent_not_a_guess(setup):
    app,client,model,ctx,conv=setup;ready(app,model,ctx)
    from backend.schemas import MessageInput
    card=app.state.semantics.unresolved_code(conv,MessageInput(content="筛选 'Dim Material'[Material Code] = \"0257\"，按月统计"),ctx)
    assert card is None
    assert app.state.semantics.bindings(conv['id'])[0]['table_name']=='Dim Material'


def test_schema_change_invalidates_build_but_preserves_only_unchanged_reviewed_definitions(setup):
    app,client,model,ctx,conv=setup;ready(app,model,ctx)
    service=app.state.semantics
    entry_id=object_id('column','Dim Cost Center','CC Number')
    service.edit(model['id'],entry_id,SemanticEdit(business_name='成本中心',meaning='成本中心文本编号',confirmed=True))
    ctx.tables['Fact'].append(Column('New','string','Fact'))
    app.state.db.execute('UPDATE models SET context=?,fingerprint=? WHERE id=?',(encode(asdict(ctx)),fingerprint(ctx),model['id']))
    p=service.get(model['id'])
    assert p['status']=='inventory'
    assert next(e for e in p['entries'] if e['id']==entry_id)['state']=='confirmed'
    assert next(e for e in p['entries'] if e['name']=='New')['state']=='unresolved'


def test_invalid_semantic_edits_cannot_confirm_an_empty_business_definition(setup):
    app,client,model,ctx,conv=setup
    entry=object_id('column','Dim Cost Center','CC Number')
    assert client.put(f"/api/models/{model['id']}/semantics/{entry}",json={'confirmed':True}).status_code==422
    assert client.put(f"/api/models/{model['id']}/semantics/{entry}",json={'aliases':['x'*101]}).status_code==422
    assert client.put(f"/api/models/{model['id']}/semantics/missing",json={}).status_code==404


def test_connect_prepares_local_facts_without_calling_any_model_service(setup,monkeypatch):
    app,client,model,ctx,conv=setup
    monkeypatch.setattr('backend.semantics.find_instances',lambda:[SimpleNamespace(pid=55,port=model['port'])])
    monkeypatch.setattr('backend.semantics.LiveDesktopSource',lambda *a:SimpleNamespace(semantic_metadata=lambda:{
        ('column','Dim Cost Center','CC Number'):{'description':'成本中心文本编号','actual_dtype':'string'}}))
    monkeypatch.setattr(app.state.settings,'provider',lambda:pytest.fail('Connection must not send metadata to the model'))
    p=app.state.semantics.on_connect(model,app.state.settings)
    assert p['status']=='inventory' and p['metadata_status']=='read'
    assert next(e for e in p['entries'] if e['name']=='CC Number')['facts']['description']=='成本中心文本编号'
    assert all(e['state']=='unresolved' for e in p['entries'])


def test_reconnect_only_reuses_definitions_from_the_same_live_engine(setup,monkeypatch):
    app,client,model,ctx,conv=setup
    p=ready(app,model,ctx);p['connection_identity']='55:50001';app.state.semantics._store(model['id'],p)
    new=app.state.powerbi.save('再次连接','desktop',50001,ctx)
    monkeypatch.setattr('backend.semantics.find_instances',lambda:[SimpleNamespace(pid=55,port=50001)])
    assert app.state.semantics.on_connect(new,app.state.settings)['status']=='ready'
    other=app.state.powerbi.save('另一实例同结构','desktop',50001,ctx)
    monkeypatch.setattr('backend.semantics.find_instances',lambda:[SimpleNamespace(pid=77,port=50001)])
    monkeypatch.setattr('backend.semantics.LiveDesktopSource',lambda *a:SimpleNamespace(semantic_metadata=lambda:{}))
    assert app.state.semantics.on_connect(other,app.state.settings)['status']=='inventory'


def test_incomplete_model_response_cannot_mark_the_dictionary_fully_built(setup,monkeypatch):
    app,client,model,ctx,conv=setup
    from tests.test_analysis import Provider
    provider=Provider([encode({'entries':[]})])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr('backend.semantics.LiveDesktopSource',lambda *a:SimpleNamespace(semantic_metadata=lambda:{}))
    client.post(f"/api/models/{model['id']}/semantics/build",json={})
    p=wait_ready(app,model['id'])
    assert p['status']=='error' and p['coverage']==0
    assert all(e['state']=='unresolved' for e in p['entries'])


def test_all_definitions_can_be_confirmed_manually_without_external_semantic_generation(setup):
    app,client,model,ctx,conv=setup
    p=app.state.semantics.ensure(model,ctx)
    for entry in p['entries']:
        app.state.semantics.edit(model['id'],entry['id'],SemanticEdit(meaning='用户填写的实际业务定义',confirmed=True))
    p=app.state.semantics.get(model['id'])
    assert p['status']=='ready' and p['coverage']==len(p['entries'])
    assert all(e['state']=='confirmed' for e in p['entries'])


def test_invalid_batch_is_retried_without_counting_unvalidated_objects(setup,monkeypatch):
    app,client,model,ctx,conv=setup
    class NullableUnitProvider(DictionaryProvider):
        def stream(self,system,messages,**opts):
            payload=json.loads(messages[0].content);self.calls.append((system,payload))
            if len(self.calls)==2:
                assert '格式问题类型' in messages[-1].content
            yield encode({'entries':[{'id':entry['id'],'business_name':'候选名称','meaning':'业务含义待确认',
                'role':'候选角色','unit':None if len(self.calls)==1 else '', 'aliases':[]} for entry in payload['objects']]})
    provider=NullableUnitProvider()
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr('backend.semantics.LiveDesktopSource',lambda *a:SimpleNamespace(semantic_metadata=lambda:{}))
    app.state.semantics.start(model['id'],app.state.settings)
    profile=wait_ready(app,model['id'])
    assert profile['status']=='ready' and len(provider.calls)==2
    assert profile['coverage']==len(profile['entries'])
    assert any(issue['location'][-1]=='unit' and issue['type']=='string_type' for issue in profile['diagnostics'])
    assert all('input' not in issue for issue in profile['diagnostics'])


def test_failed_build_resumes_only_the_remaining_objects_and_preserves_completed_entries(setup,monkeypatch):
    app,client,model,ctx,conv=setup
    ctx.tables['Fact'] += [Column(f'Column{i}','string','Fact') for i in range(25)]
    model.update(context=asdict(ctx),fingerprint=fingerprint(ctx))
    app.state.db.execute('UPDATE models SET context=?,fingerprint=? WHERE id=?',(encode(asdict(ctx)),model['fingerprint'],model['id']))
    class Interrupted(DictionaryProvider):
        def stream(self,*a,**k):
            if self.calls:raise OSError('simulated transport interruption')
            yield from super().stream(*a,**k)
    provider=Interrupted()
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr('backend.semantics.LiveDesktopSource',lambda *a:SimpleNamespace(semantic_metadata=lambda:{}))
    app.state.semantics.start(model['id'],app.state.settings)
    partial=wait_ready(app,model['id'])
    assert partial['status']=='error' and partial['coverage']==20
    completed=partial['entries'][:20]
    provider=DictionaryProvider()
    app.state.semantics.start(model['id'],app.state.settings)
    finished=wait_ready(app,model['id'])
    assert finished['status']=='ready' and finished['resume_from']==20
    assert finished['entries'][:20]==completed
    assert {e['id'] for e in provider.calls[0][1]['objects']}=={e['id'] for e in partial['entries'][20:]}
