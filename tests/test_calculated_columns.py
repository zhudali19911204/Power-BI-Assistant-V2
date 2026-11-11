import json
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backend.engine.context.base import Column, Measure, ModelContext
from backend.engine.context.live_writer import LiveDesktopWriter, WriteResult
from backend.engine.core.artifact import ValidationResult
from backend.engine.dax.live_eval import LiveDesktopEvaluator
from backend.validation import candidates, validate
from tests.test_write_target import write_case
from tests.test_api import app, client, conversation, FakeProvider, send


def context():
    return ModelContext(tables={'Sales':[Column('Revenue','double','Sales'),Column('Cost','double','Sales')],
        'Other':[Column('Amount','double','Other')]},measures=[Measure('Total','Sales','SUM(Sales[Revenue])')])


def test_mixed_objects_keep_their_types_targets_order_and_expression():
    text='''```dax
Total Sales = SUM(Sales[Revenue])
```
```dax-column
'Sales'[Margin] = Sales[Revenue] - Sales[Cost] // keep comment
```
```dax
Dates = CALENDAR(DATE(2026,1,1),DATE(2026,12,31))
```'''
    objects=candidates(text,'dax')
    assert [o['kind'] for o in objects]==['dax_measure','dax_column','dax_table']
    assert objects[1]['home_table']=='Sales' and objects[1]['name']=='Margin'
    assert objects[1]['expression'].endswith('// keep comment')
    assert 'home_table' not in objects[0]
    # A scalar aggregation may also be a column; explicit type/target wins over a heuristic.
    assert candidates('```dax-column\nSales[Total Per Row] = SUM(Sales[Revenue])\n```','dax')[0]['kind']=='dax_column'


@pytest.mark.parametrize('tag',['dax','dax-column','DAX-COLUMN'])
def test_qualified_column_targets_preserve_chinese_escaping_and_equals_in_names(tag):
    text=f"```{tag}\n'事实''表'[金额=收入]]税] = '事实''表'[收入] * 2\n```"
    obj=candidates(text,'dax')[0]
    assert obj=={'kind':'dax_column','home_table':"事实'表",'name':'金额=收入]税','expression':"'事实''表'[收入] * 2"}
    obj=candidates('```dax\n事实表[金额] = 事实表[收入] * 2\n```','dax')[0]
    assert obj['kind']=='dax_column' and obj['home_table']=='事实表'


def test_column_marker_without_target_requests_table_instead_of_guessing():
    obj=candidates('```dax-column\nMargin = [Revenue] - [Cost]\n```','dax')[0]
    result=validate(context(),[obj],'Sales')[0]
    assert not result['ok'] and '所属表' in result['errors'][0]
    assert not candidates('```python\nSales[Margin] = 1\n```','dax')


def test_columns_use_their_own_row_context_not_the_measure_home_or_calibration():
    objects=candidates('```dax-column\nSales[Margin] = [Revenue] - [Cost]\n```','dax')
    calls=[]
    class Evaluator:
        def evaluate_column_expr(self,expression,table):
            calls.append((expression,table))
            return ValidationResult(ok=True,run_verified=True,sample='2 rows',warnings=['抽样不代表全量'])
    result=validate(context(),objects,'Other',Evaluator(),[{'filters':[],'expected':2,'tolerance':0}])[0]
    assert result['ok'] and result['run_verified'] and not result['calibration']
    assert calls==[('[Revenue] - [Cost]','Sales')]
    assert result['warnings']==['抽样不代表全量']
    assert result['verification_scope']=='row_sample'


def test_missing_columns_and_unwritten_column_dependencies_cannot_pass_validation():
    objects=candidates('```dax-column\nSales[One] = Sales[Missing]\n```\n```dax-column\nSales[Two] = Sales[One]+1\n```','dax')
    class NoReads:
        def evaluate_column_expr(self,*a):pytest.fail('Static failures must not execute')
    results=validate(context(),objects,'Sales',NoReads())
    assert all(not r['ok'] and not r['run_verified'] for r in results)


@pytest.mark.parametrize('rows',[[],[{'value':2} for _ in range(50)]])
def test_live_column_sample_is_readonly_bounded_escaped_and_honest_about_coverage(monkeypatch,rows):
    calls=[]
    class Session:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def query(self,dax,**options):calls.append((dax,options));return rows
    monkeypatch.setattr('backend.engine.dax.live_eval._LiveSession',Session)
    result=LiveDesktopEvaluator(60001,'test').evaluate_column_expr('2 // comment',"Sales'FY")
    assert result.ok and result.run_verified
    dax,options=calls[0]
    assert "SAMPLE ( 50, 'Sales''FY', 1, ASC )" in dax and 'SELECTCOLUMNS' in dax
    assert 'MEASURE' not in dax and 'COLUMN ' not in dax
    assert '2 // comment\n)' in dax
    assert options=={'max_rows':50,'timeout_seconds':20}
    assert str(len(rows)) in result.sample and any('全量' in w for w in result.warnings)
    if not rows:assert any('为空' in w for w in result.warnings)


@pytest.mark.parametrize('failed_stage',['connect','execute'])
def test_column_engine_errors_and_connection_errors_are_distinguished(monkeypatch,failed_stage):
    class Session:
        def __init__(self,*a):pass
        def __enter__(self):
            if failed_stage=='connect':raise OSError('test disconnected')
            return self
        def __exit__(self,*a):pass
        def query(self,*a,**k):raise RuntimeError('test bad column expression')
    monkeypatch.setattr('backend.engine.dax.live_eval._LiveSession',Session)
    result=LiveDesktopEvaluator(60001,'test').evaluate_column_expr('1','Sales')
    assert not result.ok and result.run_verified is (failed_stage=='execute')


@pytest.fixture
def column_case(write_case,monkeypatch):
    c=write_case;c.expression="'销售明细'[销售额] * 2"
    c.app.state.db.execute('UPDATE artifacts SET kind=?,expression=? WHERE id=?',('dax_column',c.expression,c.aid))
    class Evaluator:
        def __init__(self,port):c.calls.append(('evaluator',port))
        def evaluate_column_expr(self,expr,home):
            c.calls.append(('row_sample',expr,home))
            return ValidationResult(ok=True,run_verified=True,sample='50 rows',warnings=['仅抽样'])
    class Writer:
        def __init__(self,port):c.calls.append(('writer',port))
        def write_calculated_column(self,table,name,expr):
            c.calls.append(('column_write',table,name,expr))
            return WriteResult(True,'已创建并计算')
    monkeypatch.setattr('backend.app.LiveDesktopEvaluator',Evaluator)
    monkeypatch.setattr('backend.app.LiveDesktopWriter',Writer)
    return c


def test_api_renames_rehomes_rechecks_and_persists_written_column(column_case):
    c=column_case
    response=c.client.post(f'/api/artifacts/{c.aid}/apply',json={'name':'新计算列','home_table':'产品'})
    assert response.status_code==200,response.text
    assert ('row_sample',c.expression,'产品') in c.calls
    assert ('column_write','产品','新计算列',c.expression) in c.calls
    payload=response.json();assert payload['kind']=='dax_column' and payload['validation']['verification_scope']=='row_sample'
    saved=c.app.state.db.one('SELECT * FROM artifacts WHERE id=?',(c.aid,))
    assert (saved['kind'],saved['name'],saved['home_table'])==('dax_column','新计算列','产品') and saved['applied_at']
    audit=json.loads(c.app.state.db.one('SELECT detail FROM audit')['detail'])
    assert audit['kind']=='dax_column' and audit['name']=='新计算列'


@pytest.mark.parametrize('body',[
    {'name':'销售额'}, {'home_table':'不存在的表'}, {'name':'总销售额'}, {'overwrite':True},
])
def test_api_refuses_column_collisions_missing_table_and_overwrite(column_case,body):
    c=column_case
    response=c.client.post(f'/api/artifacts/{c.aid}/apply',json=body)
    assert response.status_code==409 and not c.calls
    assert not c.app.state.db.one('SELECT applied_at FROM artifacts WHERE id=?',(c.aid,))['applied_at']


def test_api_does_not_write_a_column_that_failed_its_new_row_sample(column_case,monkeypatch):
    c=column_case
    monkeypatch.setattr('backend.app.validate',lambda *a,**k:[asdict(ValidationResult(ok=False,run_verified=True,errors=['row error']))])
    response=c.client.post(f'/api/artifacts/{c.aid}/apply',json={})
    assert response.status_code==409 and not any(call[0]=='column_write' for call in c.calls)


def test_failed_full_column_calculation_keeps_artifact_unapplied(column_case,monkeypatch):
    c=column_case
    class Writer:
        def __init__(self,*a):pass
        def write_calculated_column(self,*a):return WriteResult(False,'测试：全量计算失败')
    monkeypatch.setattr('backend.app.LiveDesktopWriter',Writer)
    response=c.client.post(f'/api/artifacts/{c.aid}/apply',json={'name':'新列'})
    assert response.status_code==409 and '全量计算失败' in response.json()['detail']
    saved=c.app.state.db.one('SELECT * FROM artifacts WHERE id=?',(c.aid,))
    assert saved['name']=='候选金额' and saved['applied_at'] is None
    assert not json.loads(c.app.state.db.one('SELECT detail FROM audit')['detail'])['ok']


@pytest.fixture
def native_writer(monkeypatch):
    calls=[]
    class Columns(list):
        def Add(self,column):calls.append('add');self.append(column)
    columns=Columns()
    target=SimpleNamespace(Name='Sales',Columns=columns,Measures=[],Hierarchies=[],
        RequestRefresh=lambda kind:calls.append(('calculate',kind)))
    model=SimpleNamespace(Tables=[target],SaveChanges=lambda:calls.append('save'),UndoLocalChanges=lambda:calls.append('undo'))
    server=SimpleNamespace(Databases=[SimpleNamespace(Model=model)],Disconnect=lambda:calls.append('disconnect'))
    monkeypatch.setitem(sys.modules,'System',SimpleNamespace(Activator=SimpleNamespace(CreateInstance=lambda typ:SimpleNamespace()),
        Enum=SimpleNamespace(Parse=lambda typ,value:value)))
    monkeypatch.setattr('backend.engine.context.live_writer._t',lambda name:name)
    writer=LiveDesktopWriter(60001);monkeypatch.setattr(writer,'_connect',lambda:server)
    return SimpleNamespace(writer=writer,model=model,target=target,calls=calls,columns=columns)


def test_native_new_column_is_calculated_in_the_same_save_and_infers_its_type(native_writer):
    c=native_writer;result=c.writer.write_calculated_column('Sales','Margin','[Revenue]-[Cost]')
    assert result.ok
    assert c.calls==['add',('calculate','Calculate'),'save','disconnect']
    column=c.columns[0]
    assert (column.Name,column.Expression,column.IsDataTypeInferred)==('Margin','[Revenue]-[Cost]',True)


@pytest.mark.parametrize('conflict',['column','measure','hierarchy','table'])
def test_native_checks_collisions_again_before_saving(native_writer,conflict):
    c=native_writer
    if conflict=='column':c.columns.append(SimpleNamespace(Name='MARGIN'))
    if conflict=='measure':c.target.Measures.append(SimpleNamespace(Name='margin'))
    if conflict=='hierarchy':c.target.Hierarchies.append(SimpleNamespace(Name='margin'))
    if conflict=='table':c.model.Tables=[]
    result=c.writer.write_calculated_column('Sales','Margin','1')
    assert not result.ok and c.calls==['disconnect']


@pytest.mark.parametrize('stage',['calculate','save'])
def test_native_calculation_errors_report_failure_and_discard_local_changes(native_writer,stage):
    c=native_writer
    def fail(*args):raise RuntimeError('test calculation failed')
    if stage=='calculate':c.target.RequestRefresh=fail
    else:c.model.SaveChanges=fail
    result=c.writer.write_calculated_column('Sales','Margin','1')
    assert not result.ok and '计算失败' in result.detail
    assert c.calls[-2:]==['undo','disconnect']


def test_chat_recognizes_a_column_and_saves_its_actual_home_table(client,app,monkeypatch):
    conv,model=conversation(client)
    provider=FakeProvider(['```dax-column\n\'产品\'[新分类] = \'产品\'[类别] & "test"\n```'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    events=send(client,conv['id'])
    artifact=next(e for e in events if e['type']=='artifacts')['artifacts'][0]
    assert artifact['kind']=='dax_column' and artifact['home_table']=='产品'
    assert artifact['validation']['ok'] and not artifact['validation']['run_verified']
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]['artifacts'][0]
    assert saved['home_table']=='产品' and saved['kind']=='dax_column'
    assert 'dax-column' in provider.calls[0][0]


def test_desktop_column_chat_repairs_then_revalidates_without_using_measure_context(client,app,monkeypatch):
    from backend.powerbi import context_from_dict
    from backend.engine.dax.prompts import build_chat_system_prompt
    ctx=context_from_dict(app.state.powerbi.demo()['context'])
    model=app.state.powerbi.save('测试 Desktop','desktop',60001,ctx)
    dictionary=app.state.semantics.ensure(model,ctx)
    dictionary.update(status='ready',coverage=len(dictionary['entries']))
    app.state.semantics._store(model['id'],dictionary)
    monkeypatch.setattr(app.state.powerbi,'require_live',lambda *a,**k:(model,ctx))
    monkeypatch.setattr(app.state.semantics,'unresolved_code',lambda *a:None)
    conv=client.post('/api/conversations',json={'model_id':model['id'],'mode':'dax'}).json()
    provider=FakeProvider(['```dax-column\n\'产品\'[New Group] = INVALID_COLUMN_EXPR()\n```',
        '```dax-column\n\'产品\'[New Group] = \'产品\'[类别] & "test"\n```'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    samples=[]
    class Evaluator:
        def __init__(self,*a):pass
        def evaluate_column_expr(self,expression,table):
            samples.append((expression,table))
            return ValidationResult(ok=not expression.startswith('INVALID'),run_verified=True,
                errors=['invalid test column'] if expression.startswith('INVALID') else [])
    monkeypatch.setattr('backend.chat.LiveDesktopEvaluator',Evaluator)
    events=send(client,conv['id'],content='在产品表新增计算列')
    artifact=next(e for e in events if e['type']=='artifacts')['artifacts'][0]
    assert artifact['home_table']=='产品' and artifact['validation']['run_verified']
    assert artifact['validation']['verification_scope']=='row_sample' and artifact['validation']['ok']
    assert len(provider.calls)==2 and len(samples)==2 and all(t=='产品' for _,t in samples)
    assert 'dax-column' in build_chat_system_prompt('test schema')
    mid=artifact['message_id']
    response=client.post(f'/api/messages/{mid}/validate',json={})
    assert response.status_code==200 and samples[-1][1]=='产品'
    assert response.json()['artifacts'][0]['kind']=='dax_column'
