import json
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backend.database import encode, now, uid
from backend.engine.context.base import Measure
from backend.engine.context.live_writer import LiveDesktopWriter, WriteResult
from backend.engine.core.artifact import ValidationResult
from backend.powerbi import context_from_dict
from tests.test_api import app, client, conversation


@pytest.fixture
def write_case(app,client,monkeypatch):
    conv,model=conversation(client)
    mid,aid=uid(),uid()
    expression="SUM('销售明细'[销售额])"
    prior=asdict(ValidationResult(ok=True,run_verified=True))
    app.state.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(mid,conv['id'],'assistant','原始回复','complete',now()))
    app.state.db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)',
        (aid,mid,model['id'],'dax_measure','候选金额',expression,'销售明细',encode(prior),None,now()))
    ctx=context_from_dict(model['context']);calls=[]
    monkeypatch.setattr(app.state.powerbi,'require_live',lambda *a,**k:({**model,'port':60001},ctx))
    class Evaluator:
        def __init__(self,port):calls.append(('evaluator',port))
        def evaluate_with_defines(self,home,definitions,target):
            calls.append(('validate',home,definitions,target))
            return ValidationResult(ok=True,run_verified=True,sample=2)
        def evaluate_with_defines_at_slice(self,home,definitions,target,filters):
            calls.append(('calibrate',home,definitions,target,filters))
            return ValidationResult(ok=True,run_verified=True,sample=2)
    class Writer:
        def __init__(self,port):calls.append(('writer',port))
        def write_measure(self,home,name,expr,**options):
            calls.append(('write',home,name,expr,options))
            return WriteResult(True,'已写入')
    monkeypatch.setattr('backend.app.LiveDesktopEvaluator',Evaluator)
    monkeypatch.setattr('backend.app.LiveDesktopWriter',Writer)
    monkeypatch.setattr(app.state.powerbi,'update_after_write',lambda *a:None)
    return SimpleNamespace(app=app,client=client,ctx=ctx,aid=aid,mid=mid,cid=conv['id'],expression=expression,calls=calls,prior=prior)


def test_custom_name_and_home_are_validated_written_audited_and_persisted(write_case):
    c=write_case
    r=c.client.post(f'/api/artifacts/{c.aid}/apply',json={'name':'  新的销售金额  ','home_table':' 产品 '})
    assert r.status_code==200,r.text
    assert ('validate','产品',[('新的销售金额',c.expression)],'新的销售金额') in c.calls
    assert ('write','产品','新的销售金额',c.expression,{'overwrite':False}) in c.calls
    assert ('writer',60001) in c.calls
    saved=c.app.state.db.one('SELECT * FROM artifacts WHERE id=?',(c.aid,))
    assert saved['name']=='新的销售金额' and saved['home_table']=='产品' and saved['applied_at']
    assert json.loads(saved['validation'])['run_verified']
    audit=json.loads(c.app.state.db.one('SELECT detail FROM audit')['detail'])
    assert (audit['name'],audit['home_table'])==('新的销售金额','产品')
    response=r.json()
    assert response['name']==saved['name'] and response['applied_at']==saved['applied_at']
    message=c.client.get(f'/api/conversations/{c.cid}/messages').json()[-1]
    assert message['content']=='原始回复' and message['artifacts'][0]['name']=='新的销售金额'


@pytest.mark.parametrize('body',[
    {'name':''},{'name':'   '},{'name':'x'*301},{'name':'a\nb'},{'home_table':''},{'home_table':'  '},
    {'home_table':'x'*301},{'home_table':'a\x00b'},
])
def test_invalid_target_is_rejected_before_any_native_work(write_case,body):
    c=write_case
    r=c.client.post(f'/api/artifacts/{c.aid}/apply',json=body)
    assert r.status_code==422 and not c.calls
    assert not c.app.state.db.one('SELECT applied_at FROM artifacts WHERE id=?',(c.aid,))['applied_at']


@pytest.mark.parametrize('body',[
    {'home_table':'已经删除的表'},
    {'home_table':'销售明细','name':'销售额'},
    {'home_table':'销售明细','name':'总销售额'},
    {'home_table':'产品','name':'总销售额','overwrite':True},
])
def test_missing_table_column_collision_and_measure_conflicts_prevent_writes(write_case,body):
    c=write_case
    r=c.client.post(f'/api/artifacts/{c.aid}/apply',json=body)
    assert r.status_code==409 and not c.calls
    saved=c.app.state.db.one('SELECT * FROM artifacts WHERE id=?',(c.aid,))
    assert saved['name']=='候选金额' and not saved['applied_at']


def test_same_table_case_insensitive_collision_requires_explicit_overwrite(write_case):
    c=write_case;c.ctx.measures.append(Measure('Existing','产品','1'))
    body={'name':'existing','home_table':'产品'}
    assert c.client.post(f'/api/artifacts/{c.aid}/apply',json=body).status_code==409
    assert not c.calls
    r=c.client.post(f'/api/artifacts/{c.aid}/apply',json={**body,'overwrite':True})
    assert r.status_code==200
    assert ('write','产品','existing',c.expression,{'overwrite':True}) in c.calls


def test_new_target_failing_validation_keeps_original_artifact(write_case,monkeypatch):
    c=write_case
    monkeypatch.setattr('backend.app.validate',lambda *a,**k:[asdict(ValidationResult(ok=False,run_verified=True,errors=['test invalid target']))])
    r=c.client.post(f'/api/artifacts/{c.aid}/apply',json={'name':'新名称','home_table':'产品'})
    assert r.status_code==409 and not any(call[0]=='write' for call in c.calls)
    saved=c.app.state.db.one('SELECT * FROM artifacts WHERE id=?',(c.aid,))
    assert saved['name']=='候选金额' and saved['home_table']=='销售明细' and not saved['applied_at']


def test_changed_name_and_home_reuse_original_calibration(write_case):
    c=write_case
    prior={**c.prior,'calibration':[{'filters':[{'table':'产品','column':'类别','value':'A'}],
        'expected':2,'tolerance':0.01}]}
    c.app.state.db.execute('UPDATE artifacts SET validation=? WHERE id=?',(encode(prior),c.aid))
    r=c.client.post(f'/api/artifacts/{c.aid}/apply',json={'name':'校准金额','home_table':'产品'})
    assert r.status_code==200,r.text
    assert ('calibrate','产品',[('校准金额',c.expression)],'校准金额',[('产品','类别','A')]) in c.calls
    assert r.json()['validation']['calibration'][0]['matched']


def test_failed_native_write_keeps_original_target_and_records_attempt(write_case,monkeypatch):
    c=write_case
    class FailedWriter:
        def __init__(self,*args):pass
        def write_measure(self,*args,**kwargs):return WriteResult(False,'测试写入失败')
    monkeypatch.setattr('backend.app.LiveDesktopWriter',FailedWriter)
    r=c.client.post(f'/api/artifacts/{c.aid}/apply',json={'name':'新名称','home_table':'产品'})
    assert r.status_code==409
    saved=c.app.state.db.one('SELECT * FROM artifacts WHERE id=?',(c.aid,))
    assert (saved['name'],saved['home_table'],saved['applied_at'])==('候选金额','销售明细',None)
    audit=json.loads(c.app.state.db.one('SELECT detail FROM audit')['detail'])
    assert not audit['ok'] and audit['name']=='新名称' and audit['home_table']=='产品'


def test_context_sync_failure_still_returns_and_records_successful_target(write_case,monkeypatch):
    c=write_case
    def failed_sync(*args):raise OSError('test context read failed')
    monkeypatch.setattr(c.app.state.powerbi,'update_after_write',failed_sync)
    r=c.client.post(f'/api/artifacts/{c.aid}/apply',json={'name':'新名称','home_table':'产品'})
    assert r.status_code==200 and r.json()['ok']
    assert '手动同步' in r.json()['detail']
    assert (r.json()['name'],r.json()['home_table'])==('新名称','产品')
    assert c.app.state.db.one('SELECT applied_at FROM artifacts WHERE id=?',(c.aid,))['applied_at']


@pytest.mark.parametrize('overwrite',[False,True])
def test_native_writer_respects_case_insensitive_overwrite_and_selected_name(monkeypatch,overwrite):
    measure=SimpleNamespace(Name='Existing',Expression='1')
    changes=[]
    model=SimpleNamespace(Tables=[SimpleNamespace(Name='Target',Measures=[measure])],SaveChanges=lambda:changes.append('save'))
    server=SimpleNamespace(Databases=[SimpleNamespace(Model=model)],Disconnect=lambda:changes.append('disconnect'))
    monkeypatch.setitem(sys.modules,'System',SimpleNamespace())
    writer=LiveDesktopWriter(60001);monkeypatch.setattr(writer,'_connect',lambda:server)
    result=writer.write_measure('Target','existing','2',overwrite=overwrite)
    assert result.ok is overwrite
    assert (measure.Name,measure.Expression)==(('existing','2') if overwrite else ('Existing','1'))
    assert changes==(['save','disconnect'] if overwrite else ['disconnect'])
