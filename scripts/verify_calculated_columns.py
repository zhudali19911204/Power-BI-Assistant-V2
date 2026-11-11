"""Read-only row-context validation on Desktop plus local TOM object checks; never writes a model."""
import json
from pathlib import Path

from backend.database import encode, now
from backend.engine.context.amo import _cached
from backend.engine.context.live_source import find_instances, LiveDesktopSource
from backend.engine.context.live_writer import _t
from backend.engine.dax.live_eval import LiveDesktopEvaluator
from backend.validation import candidates, validate


def main():
    instances=find_instances()
    if len(instances)!=1:raise RuntimeError('只读检查需要恰好一个已打开的 Desktop 实例')
    instance=instances[0]
    source=LiveDesktopSource(instance.port);ctx=source.load()
    table=next(table for table,columns in ctx.tables.items() if columns)
    quoted="'"+table.replace("'","''")+"'"
    # A constant row expression returns no business values. Its target table is
    # real; no candidate is persisted and no writer/SaveChanges is ever called.
    objects=candidates(f'```dax-column\n{quoted}[__column_readonly_probe__] = 1 + 1\n```','dax')
    result=validate(ctx,objects,table,LiveDesktopEvaluator(instance.port,source.dll))[0]
    assert result['ok'] and result['run_verified'] and result['verification_scope']=='row_sample',result
    # Exercise the actual installed TOM types without any server connection.
    if not _cached():raise RuntimeError('TOM DLL 尚未缓存，请先准备现有写入组件后重试；测试不会下载或写入模型')
    import System
    typ=_t('CalculatedColumn');column=System.Activator.CreateInstance(typ)
    column.Name='__column_local_type_probe__';column.Expression='1+1';column.IsDataTypeInferred=True
    enum=System.Enum.Parse(_t('RefreshType'),'Calculate')
    assert str(column.Expression)=='1+1' and bool(column.IsDataTypeInferred) and str(enum)=='Calculate'
    report={'time':now(),'check':'pass','readonly_row_context_sample':True,
        'sample_description':result['sample'],'native_tom_type_created_locally':True,
        'datatype_inference_supported':True,'calculate_enum_supported':True,
        'desktop_model_writes':0,'business_values_returned':0,'external_model_requests':0,
        'user_database_writes':0,'physical_column_write_tested':False}
    target=Path(__file__).resolve().parents[1]/'artifacts';target.mkdir(exist_ok=True)
    (target/'calculated-columns-report.json').write_text(encode(report),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
