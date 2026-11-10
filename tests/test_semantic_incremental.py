import copy
import json
from dataclasses import asdict

import pytest

from backend.database import encode
from backend.engine.context.base import Column, Measure
from backend.powerbi import fingerprint
from backend.semantics import SemanticEdit, object_id
from tests.test_report_reuse import setup, connect, context
from tests.test_semantics import DictionaryProvider, wait_ready


def test_in_place_revision_keeps_candidates_and_reviews_and_builds_only_the_delta(setup,monkeypatch):
    app,client,state,engine,built=setup
    model=connect(client,engine());saved=built(model)
    changed=context();changed.tables['Sales'].append(Column('New','string','Sales'))
    state['contexts'][model['port']]=changed
    app.state.db.execute('UPDATE models SET context=?,fingerprint=? WHERE id=?',
        (encode(asdict(changed)),fingerprint(changed),model['id']))
    p=app.state.semantics.get(model['id'])
    pending={object_id('table','Sales','Sales'),object_id('column','Sales','New')}
    assert set(p['pending_ids'])==pending
    old={e['id']:e for e in saved['entries']}
    assert all(e==old[e['id']] for e in p['entries'] if e['id'] not in pending)
    provider=DictionaryProvider();monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    client.post(f"/api/models/{model['id']}/semantics/build",json={})
    finished=wait_ready(app,model['id'])
    assert finished['status']=='ready'
    assert {e['id'] for _,payload in provider.calls for e in payload['objects']}==pending
    assert finished['changes']['added']==1 and finished['changes']['modified']==1


@pytest.mark.parametrize('used',[False,True])
def test_shared_m_change_only_affects_its_transitive_users(setup,used):
    app,client,state,engine,built=setup
    original=context()
    original.shared_expressions={'Endpoint':'"old"','Staging':'let Source = Endpoint in Source'}
    original.table_queries['Sales']='let Source = Staging in Source' if used else 'let Source = "Endpoint" in Source // Endpoint'
    original.measures.append(Measure('Constant','Sales','1'))
    old=connect(client,engine(ctx=original));saved=built(old)
    changed=copy.deepcopy(original);changed.shared_expressions['Endpoint']='"new"'
    model=connect(client,engine(ctx=changed,port=60001,pid=77))
    p=app.state.semantics.get(model['id'])
    if used:
        assert set(p['pending_ids'])=={e['id'] for e in saved['entries']
            if e['table']=='Sales' and e['name']!='Constant'}
        assert p['status']=='inventory'
    else:
        assert p['status']=='ready' and not p['pending_ids'] and p['entries']==saved['entries']
        from backend.semantic_diff import changed_sources
        dynamic=copy.deepcopy(original)
        dynamic.table_queries['Sales']='Expression.Evaluate("Endpoint", #shared)'
        dynamic_changed=copy.deepcopy(dynamic);dynamic_changed.shared_expressions['Endpoint']='"new"'
        affected=changed_sources(asdict(dynamic),asdict(dynamic_changed))
        assert 'Sales' in affected and 'Dim' not in affected
    assert next(e for e in p['entries'] if e['name']=='Constant')['state']=='candidate'


def test_measure_changes_invalidate_transitive_dependents_and_retain_old_review_for_inspection(setup):
    app,client,state,engine,built=setup
    original=context();original.measures += [Measure('Derived','Sales','[Actual] * 2'),Measure('Chained','Sales','[Derived] + 1')]
    old=connect(client,engine(ctx=original));built(old)
    changed=copy.deepcopy(original);changed.measures[0].expression="SUM('Sales'[Amount]) * 1000"
    new=connect(client,engine(ctx=changed,port=60001,pid=77))
    p=app.state.semantics.get(new['id'])
    assert set(p['pending_ids'])=={object_id('measure','Sales',name) for name in ('Actual','Derived','Chained')}
    actual=next(e for e in p['entries'] if e['name']=='Actual')
    assert actual['state']=='unresolved' and not actual['reviewed_at'] and not actual['meaning']
    assert actual['previous_definition']['state']=='confirmed'
    prompt=app.state.semantics.prompt(new['id'])
    assert '用户确认的实际金额定义' not in prompt and '公式变化' in prompt
    # Unquoted Unicode tables are real column references; a table-name suffix
    # inside another quoted table must not create a spurious dependency.
    from backend.semantic_diff import dax_dependencies
    from backend.semantics import inventory
    ctx=context();ctx.tables['事实表']=[Column('金额','double','事实表')]
    ctx.tables['Fact Sales']=[Column('Value','double','Fact Sales')]
    ctx.measures=[Measure('中文金额','事实表','SUM(事实表[金额])'),Measure('Rows','Sales',"COUNTROWS('Fact Sales')")]
    objects=inventory(ctx)
    chinese=next(e for e in objects if e['name']=='中文金额')
    assert object_id('column','事实表','金额') in dax_dependencies(chinese,objects)
    rows=next(e for e in objects if e['name']=='Rows')
    refs=dax_dependencies(rows,objects)
    assert object_id('table','Fact Sales','Fact Sales') in refs
    assert object_id('table','Sales','Sales') not in refs


def test_relationship_changes_invalidate_measures_but_preserve_unrelated_constant(setup):
    app,client,state,engine,built=setup
    original=context();original.measures.append(Measure('Constant','Sales','1'))
    old=connect(client,engine(ctx=original));built(old)
    changed=copy.deepcopy(original);changed.relationships[0].is_active=False
    new=connect(client,engine(ctx=changed,port=60001,pid=77))
    p=app.state.semantics.get(new['id'])
    assert object_id('measure','Sales','Actual') in p['pending_ids']
    assert object_id('measure','Sales','Constant') not in p['pending_ids']


def test_metadata_only_update_on_same_snapshot_invalidates_only_changed_object(setup):
    app,client,state,engine,built=setup
    model=connect(client,engine());built(model)
    state['metadata'][('column','Sales','Code')]={'description':'新的业务说明','actual_dtype':'string',
        'source_column':'Code','format_string':'','expression':''}
    p=app.state.semantics.on_connect(app.state.powerbi.get(model['id']),app.state.settings)
    assert p['pending_ids']==[object_id('column','Sales','Code')]
    assert p['changes']['modified']==1
    assert app.state.semantics.summary(model['id'])['can_answer']


def test_deleted_objects_are_archived_and_not_injected_as_current_semantics(setup):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    changed=context();changed.tables['Sales']=[];changed.measures=[];changed.relationships=[]
    new=connect(client,engine(ctx=changed,port=60001,pid=77))
    p=app.state.semantics.get(new['id'])
    assert p['changes']['removed']==3
    assert {e['name'] for e in p['removed_entries']}=={'Amount','Code','Actual'}
    assert not any(e['table']=='Sales' and e['kind'] in ('column','measure') for e in p['entries'])
    assert '用户确认的实际金额定义' not in app.state.semantics.prompt(new['id'])


def test_newest_revision_wins_over_an_older_exact_schema(setup):
    app,client,state,engine,built=setup
    first=connect(client,engine());built(first)
    changed=context();changed.tables['Sales'].append(Column('New','string','Sales'))
    second=connect(client,engine(ctx=changed,port=60001,pid=77));built(second)
    key=object_id('column','Sales','Amount')
    app.state.semantics.edit(second['id'],key,SemanticEdit(meaning='最新修订的金额定义',confirmed=True))
    third=connect(client,engine(ctx=context(),port=61001,pid=88))
    p=app.state.semantics.get(third['id'])
    assert p['reuse']['source_model_id']==second['id']
    assert next(e for e in p['entries'] if e['id']==key)['meaning']=='最新修订的金额定义'


def test_no_change_build_does_not_create_a_provider_or_run_a_job(setup):
    app,client,state,engine,built=setup
    model=connect(client,engine());built(model)
    r=client.post(f"/api/models/{model['id']}/semantics/build",json={})
    assert r.status_code==200 and r.json()['status']=='ready'
    assert not app.state.semantics.jobs
    # An empty inventory is not historical business knowledge, even if its schema changes.
    from backend.semantics import inventory, can_answer
    from backend.semantic_diff import reconcile
    original=context();changed=copy.deepcopy(original)
    changed.tables['Sales'].append(Column('New','string','Sales'))
    entries,_,_=reconcile(inventory(changed),inventory(original),asdict(original),asdict(changed))
    assert not can_answer({'status':'inventory','entries':entries})


def test_structural_and_metadata_deltas_accumulate_without_losing_previous_review(setup):
    app,client,state,engine,built=setup
    model=connect(client,engine());saved=built(model)
    changed=context();changed.tables['Sales'].append(Column('New','string','Sales'))
    state['contexts'][model['port']]=changed
    app.state.db.execute('UPDATE models SET context=?,fingerprint=? WHERE id=?',
        (encode(asdict(changed)),fingerprint(changed),model['id']))
    app.state.semantics.get(model['id'])
    # A second metadata read discovers a distinct change after the structural delta.
    state['metadata'][('measure','Sales','Actual')]={'description':'新的指标说明','format_string':'0.00'}
    p=app.state.semantics.on_connect(app.state.powerbi.get(model['id']),app.state.settings)
    # The first read of a previously unknown fact is enrichment, not proof of a change.
    assert p['changes']['added']==1 and p['changes']['modified']==1
    state['metadata'][('measure','Sales','Actual')]['description']='再次修订的指标说明'
    p=app.state.semantics.on_connect(app.state.powerbi.get(model['id']),app.state.settings)
    assert p['changes']['added']==1 and p['changes']['modified']==2
    actual=next(e for e in p['entries'] if e['name']=='Actual')
    assert actual['previous_definition']['meaning']=='用户确认的实际金额定义'
    state['metadata'][('measure','Sales','Actual')]['description']='第三次修订'
    p=app.state.semantics.on_connect(app.state.powerbi.get(model['id']),app.state.settings)
    actual=next(e for e in p['entries'] if e['name']=='Actual')
    assert actual['previous_definition']['meaning']=='用户确认的实际金额定义'


@pytest.mark.parametrize('status',['inventory','building','error'])
def test_latest_all_changed_revision_is_still_reusable_history(setup,status):
    app,client,state,engine,built=setup
    original=context();original.table_queries['Dim']='let Source = 1 in Source'
    first=connect(client,engine(ctx=original));built(first)
    changed=copy.deepcopy(original)
    changed.table_queries={name:'let Source = 2 in Source' for name in original.table_queries}
    second=connect(client,engine(ctx=changed,port=60001,pid=77))
    p=app.state.semantics.get(second['id'])
    assert all(e['state']=='unresolved' for e in p['entries'])
    assert app.state.semantics.summary(second['id'])['can_answer']
    p['status']=status;app.state.semantics._store(second['id'],p)
    third=connect(client,engine(ctx=changed,port=61001,pid=88))
    reused=app.state.semantics.get(third['id'])
    assert reused['reuse']['source_model_id']==second['id']
    assert reused['has_history'] and reused['coverage']==0
    app.state.semantics.require_ready(third,changed)
    assert next(e for e in reused['entries'] if e['name']=='Actual')['previous_definition']['state']=='confirmed'


def test_reconnecting_during_an_interrupted_delta_keeps_completed_batches(setup,monkeypatch):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    changed=context();changed.tables['Sales'] += [Column(f'New{i}','string','Sales') for i in range(25)]
    second=connect(client,engine(ctx=changed,port=60001,pid=77))
    class Interrupted(DictionaryProvider):
        def stream(self,*args,**kwargs):
            if self.calls:raise OSError('test second batch failure')
            yield from super().stream(*args,**kwargs)
    provider=Interrupted();monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    client.post(f"/api/models/{second['id']}/semantics/build",json={})
    partial=wait_ready(app,second['id'])
    assert partial['status']=='error' and len(partial['pending_ids'])==6
    third=connect(client,engine(ctx=changed,port=61001,pid=88))
    p=app.state.semantics.get(third['id'])
    assert p['pending_ids']==partial['pending_ids']
    provider=DictionaryProvider()
    client.post(f"/api/models/{third['id']}/semantics/build",json={})
    finished=wait_ready(app,third['id'])
    assert finished['status']=='ready'
    assert {e['id'] for _,payload in provider.calls for e in payload['objects']}==set(partial['pending_ids'])


@pytest.mark.parametrize('mode',['dax','analysis'])
@pytest.mark.parametrize('build_status',['inventory','building','error'])
def test_partial_dictionary_allows_real_chat_while_delta_is_pending(setup,monkeypatch,mode,build_status):
    app,client,state,engine,built=setup
    old=connect(client,engine());built(old)
    changed=context();changed.tables['Sales'].append(Column('New','string','Sales'))
    new=connect(client,engine(ctx=changed,port=60001,pid=77))
    p=app.state.semantics.get(new['id']);p['status']=build_status
    app.state.semantics._store(new['id'],p)
    if build_status=='building':app.state.semantics.jobs[new['id']]=True
    from tests.test_analysis import Provider
    provider=Provider([encode({'clarification':None,'queries':[]})] if mode=='analysis' else ['请依据实际字段理解金额。'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    conv=client.post('/api/conversations',json={'model_id':new['id'],'mode':mode}).json()
    try:
        r=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'解释已有金额字段'})
        assert r.status_code==200,r.text
        assert provider.calls and '增量语义使用规则' in provider.calls[0][0]
        assert not any(e.get('type')=='error' for e in map(json.loads,r.text.splitlines()))
    finally:app.state.semantics.jobs.pop(new['id'],None)
