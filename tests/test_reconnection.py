import copy
import json
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.cancellation import CancellationToken, GenerationCancelled, cancellation_scope
from backend.database import encode, now, uid
from backend.engine.context.base import Column, Measure, ModelContext
from backend.powerbi import PowerBIService
from backend.reconnection import ConnectionIssue
from tests.test_analysis import setup as analysis_setup, result


def context():
    return ModelContext(tables={'Sales':[Column('Code','string','Sales'),Column('Amount','double','Sales')]},
                        measures=[Measure('Total','Sales',"SUM('Sales'[Amount])")])


@pytest.fixture
def setup(tmp_path,monkeypatch):
    app=create_app(tmp_path);service=app.state.powerbi
    service.reconnector.wait=lambda delay:None
    state={'instances':[SimpleNamespace(port=50001,pid=11)],'contexts':{50001:context()},
           'reports':{11:{'name':'Report','path':r'E:\Reports\Report.pbix'}},'loads':[],'failures':0}
    monkeypatch.setattr('backend.powerbi.find_instances',lambda:state['instances'])
    monkeypatch.setattr('backend.powerbi.reports_for_instances',lambda instances:state['reports'])
    class Source:
        def __init__(self,port):self.port=port
        def load(self):
            state['loads'].append(self.port)
            if state['failures']:
                state['failures']-=1;raise ConnectionResetError('synthetic read failure')
            return copy.deepcopy(state['contexts'][self.port])
    monkeypatch.setattr('backend.powerbi.LiveDesktopSource',Source)
    monkeypatch.setattr(app.state.settings,'provider',lambda *a:pytest.fail('Reconnect must not call a model'))
    model=service.connect(50001)
    state['loads'].clear()
    return app,service,model,state


def reopen(state,*,name='Report',path=r'E:\Reports\Report.pbix',port=60001,pid=22,ctx=None):
    state['instances']=[SimpleNamespace(port=port,pid=pid)]
    state['reports']={pid:{'name':name,'path':path}}
    state['contexts'][port]=ctx or context()


def test_transient_metadata_failure_is_retried_without_creating_a_snapshot(setup):
    app,service,model,state=setup;state['failures']=1
    restored,ctx=service.require_live(model['id'],unchanged=True)
    assert restored['live'] and state['loads']==[50001,50001]
    assert restored['connection']['status']=='connected'
    assert len(app.state.db.all('SELECT id FROM models'))==1


def test_reopen_rebinds_original_model_keeps_history_semantics_and_clears_value_caches(setup):
    app,service,model,state=setup
    stamp=now();cid=uid()
    app.state.db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?)',(cid,'saved','analysis',model['id'],'',stamp,stamp))
    dictionary=app.state.semantics.ensure(model)
    dictionary.update(status='ready',coverage=len(dictionary['entries']))
    app.state.semantics._store(model['id'],dictionary)
    app.state.db.execute('INSERT INTO model_value_lookups VALUES (?,?,?,?,?)',
        (model['id'],'0257',model['fingerprint'],'{}',stamp))
    app.state.db.execute('INSERT INTO field_value_profiles VALUES (?,?,?,?,?,?)',
        (model['id'],'Sales','Code',model['fingerprint'],'{}',stamp))
    reopen(state)
    restored,_=service.require_live(model['id'],unchanged=True)
    assert restored['id']==model['id'] and restored['port']==60001 and restored['live']
    assert restored['report']['engine_pid']==22
    assert service.get(model['id'])['fingerprint']==model['fingerprint']
    assert app.state.semantics.get(model['id'])==dictionary
    assert app.state.chat.conversation(cid)['model_id']==model['id']
    assert not app.state.db.all('SELECT * FROM model_value_lookups')
    assert not app.state.db.all('SELECT * FROM field_value_profiles')
    assert not app.state.db.all('SELECT * FROM audit')


def test_app_restart_restores_same_saved_model_and_new_port_after_identity_check(setup):
    app,service,model,state=setup
    reopened=PowerBIService(app.state.db);reopened.reconnector.wait=lambda delay:None
    assert not reopened.get(model['id'])['live']
    reopen(state)
    restored,_=reopened.require_live(model['id'])
    assert restored['live'] and restored['port']==60001


@pytest.mark.parametrize('name,path',[
    ('Other',r'E:\Reports\Other.pbix'),('Report',r'E:\Other\Report.pbix'),
])
def test_reused_port_cannot_attach_a_different_report_with_identical_schema(setup,name,path):
    app,service,model,state=setup
    reopen(state,name=name,path=path,port=50001)
    with pytest.raises(ConnectionIssue):service.require_live(model['id'])
    assert not service.get(model['id'])['live'] and not state['loads']
    assert service.get(model['id'])['report']['path']==r'E:\Reports\Report.pbix'


def test_multiple_same_report_candidates_require_selection(setup):
    app,service,model,state=setup
    reopen(state,path='')
    state['instances'].append(SimpleNamespace(port=60002,pid=23))
    state['reports'][23]={'name':'Report','path':''}
    with pytest.raises(ConnectionIssue) as failure:service.require_live(model['id'])
    assert failure.value.state=='ambiguous' and not state['loads']
    assert service.get(model['id'])['port']==50001


def test_unique_name_fallback_keeps_known_path_but_does_not_choose_among_siblings(setup):
    app,service,model,state=setup
    reopen(state,path='')
    restored,_=service.require_live(model['id'])
    assert restored['live'] and restored['report']['path']==r'E:\Reports\Report.pbix'
    reopen(state,path='',port=61001,pid=33)
    state['instances'].append(SimpleNamespace(port=61002,pid=34))
    state['reports'][34]={'name':'Report','path':r'E:\Different\Report.pbix'}
    with pytest.raises(ConnectionIssue) as failure:service.require_live(model['id'])
    assert failure.value.state=='ambiguous'


def test_manual_sync_after_reopen_can_resolve_new_port_with_changed_schema(setup):
    app,service,model,state=setup
    changed=context();changed.tables['Sales'].append(Column('New','string','Sales'))
    reopen(state,ctx=changed)
    with TestClient(app) as client:
        response=client.post(f"/api/models/{model['id']}/refresh",json={})
    assert response.status_code==200
    fresh=response.json()
    assert fresh['id']!=model['id'] and fresh['port']==60001
    assert len(fresh['context']['tables']['Sales'])==3


@pytest.mark.parametrize('change',['column','formula'])
def test_changed_model_does_not_replace_snapshot_or_continue_queries(setup,change):
    app,service,model,state=setup
    changed=context()
    if change=='column':changed.tables['Sales'][1].dtype='string'
    else:changed.measures[0].expression='123'
    reopen(state,ctx=changed)
    with pytest.raises(ConnectionIssue) as failure:service.require_live(model['id'])
    assert failure.value.state=='changed'
    saved=service.get(model['id'])
    assert saved['port']==50001 and saved['context']==model['context']
    assert saved['connection']['status']=='changed' and not saved['live']


def test_dmv_order_and_cardinality_do_not_falsely_break_connection(setup):
    app,service,model,state=setup
    state['contexts'][50001].tables['Sales'].reverse()
    state['contexts'][50001].tables['Sales'][0].cardinality=1000
    restored,_=service.require_live(model['id'],unchanged=True)
    assert restored['live'] and restored['fingerprint']==model['fingerprint']


def test_real_table_named_cardinality_is_not_ignored_by_schema_check(setup):
    app,service,model,state=setup
    state['contexts'][50001].tables['cardinality']=[Column('Code','string','cardinality')]
    saved=service.connect(50001)
    state['contexts'][50001].tables.pop('cardinality')
    with pytest.raises(ConnectionIssue) as failure:service.require_live(saved['id'])
    assert failure.value.state=='changed'


def test_unnamed_report_cannot_jump_to_another_port_with_same_schema(setup):
    app,service,model,state=setup
    app.state.db.execute('UPDATE model_reports SET payload=? WHERE model_id=?',
        (encode({'engine_pid':11,'engine_port':50001}),model['id']))
    reopen(state,name='',path='')
    with pytest.raises(ConnectionIssue):service.require_live(model['id'])
    assert not state['loads']


def test_missing_desktop_recovers_on_later_poll_with_backoff(setup):
    app,service,model,state=setup
    state['instances']=[]
    for expected in [2,5,10,20,30,30]:
        with pytest.raises(ConnectionIssue):service.require_live(model['id'])
        assert service.get(model['id'])['connection']['retry_in']==expected
    reopen(state)
    restored,_=service.require_live(model['id'])
    assert restored['live'] and restored['connection']['retry_in']==20


def test_connection_poll_deduplicates_and_does_not_wait_for_metadata(setup,monkeypatch):
    app,service,model,state=setup
    entered,resume=threading.Event(),threading.Event();calls=[]
    original=service.require_live
    def blocked(*args,**kwargs):
        calls.append(True);entered.set();resume.wait(3);return original(*args,**kwargs)
    monkeypatch.setattr(service,'require_live',blocked)
    service.reconnector.due[model['id']]=0
    try:
        with TestClient(app) as client:
            start=time.monotonic()
            response=client.get(f"/api/models/{model['id']}/connection")
            assert response.status_code==200 and time.monotonic()-start<1
            assert entered.wait(1)
            for _ in range(3):client.get(f"/api/models/{model['id']}/connection")
            assert len(calls)==1
            resume.set()
            deadline=time.monotonic()+2
            while service.reconnector.jobs and time.monotonic()<deadline:time.sleep(.01)
            client.get(f"/api/models/{model['id']}/connection")
            assert len(calls)==1
    finally:resume.set()


def test_health_check_skips_busy_model_without_marking_disconnected(setup):
    app,service,model,state=setup
    service.reconnector.due[model['id']]=0
    with service.lock:
        service.reconnector.poll(model['id'])
        deadline=time.monotonic()+2
        while service.reconnector.jobs and time.monotonic()<deadline:time.sleep(.01)
    assert not state['loads'] and service.get(model['id'])['live']


def test_cancel_interrupts_reconnect_wait_and_does_not_continue_loading(setup):
    app,service,model,state=setup
    token=CancellationToken('a'*32);state['failures']=1
    def wait(delay):
        if delay:token.cancel()
        token.check()
    service.reconnector.wait=wait
    with cancellation_scope(token),pytest.raises(GenerationCancelled):service.require_live(model['id'])
    assert state['loads']==[50001]


@pytest.mark.parametrize('failure',[ConnectionResetError('lost'),ValueError('DAX syntax error')])
def test_analysis_retries_transport_disconnect_once_but_not_dax_errors(analysis_setup,monkeypatch,failure):
    app,client,conv=analysis_setup
    monkeypatch.setattr('backend.analysis.find_adomd_dll',lambda *a:'fake')
    monkeypatch.setattr('backend.analysis.find_instances',lambda:[])
    calls=[]
    class Session:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def query(self,*args,**kwargs):
            calls.append(True)
            if len(calls)==1:raise failure
            return [{'Value':125}]
    monkeypatch.setattr('backend.analysis._LiveSession',Session)
    executed=app.state.chat.analysis.execute(app.state.powerbi.get(conv['model_id']),'EVALUATE ROW("Value",125)')
    assert len(calls)==(2 if isinstance(failure,ConnectionError) else 1)
    assert executed['ok']==isinstance(failure,ConnectionError)
