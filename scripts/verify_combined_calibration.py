"""Read-only Desktop check: two real filters, one expected result, one comparison."""
from backend.engine.context.live_source import LiveDesktopSource,_LiveSession,find_instances
from backend.engine.dax.live_eval import LiveDesktopEvaluator,_format_value
from backend.schemas import MessageInput
from backend.validation import validate


def reference(table,column):
    return "'"+table.replace("'","''")+"'["+column.replace(']',']]')+"]"


def main():
    instances=find_instances()
    if not instances:raise RuntimeError('请先打开本机 Power BI Desktop 模型')
    source=LiveDesktopSource(instances[0].port)
    ctx=source.load()
    selected=next(((table,[c for c in columns if c.dtype in ('string','int64','boolean')][:2])
                   for table,columns in ctx.tables.items()
                   if len([c for c in columns if c.dtype in ('string','int64','boolean')])>=2),None)
    if not selected:raise RuntimeError('模型中未找到可用于组合筛选测试的两个字段')
    table,columns=selected
    first,second=[reference(table,c.name) for c in columns]
    table_ref="'"+table.replace("'","''")+"'"
    query=f'EVALUATE TOPN(1,SUMMARIZE({table_ref},{first},{second}),{first},ASC,{second},ASC)'
    with _LiveSession(source.port,source.dll) as session:
        rows=session.query(query)
    if not rows:raise RuntimeError('所选模型表没有可用于测试的行')
    values=list(rows[0].values())
    request=MessageInput(content='组合筛选校准测试',points=[{
        'filters':[{'table':table,'column':c.name,'value':value} for c,value in zip(columns,values)],
        'expected':1,'tolerance':0}])
    expression=(f'VAR SelectedA = SELECTEDVALUE({first})\nVAR SelectedB = SELECTEDVALUE({second})\n'
                f'RETURN IF(SelectedA={_format_value(values[0])} && SelectedB={_format_value(values[1])},1,0)')
    objects=[{'name':'__V2_CombinedCalibration','kind':'dax_measure','expression':expression}]
    result=validate(ctx,objects,table,LiveDesktopEvaluator(source.port),[p.model_dump() for p in request.points])[0]
    assert result['ok'] and result['run_verified'],result['errors']
    assert len(result['calibration'])==1
    assert len(result['calibration'][0]['filters'])==2
    assert result['calibration'][0]['matched'] and result['calibration'][0]['actual']==1
    print('PASS: two real Desktop filter conditions combined; one expected result; one comparison; no model writes')


if __name__=='__main__':main()
