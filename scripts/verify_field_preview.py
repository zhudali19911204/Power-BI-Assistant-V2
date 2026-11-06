"""Read actual Desktop column profiles in a temporary workspace; never modify the model."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app import create_app
from backend.engine.context.live_source import find_instances
from backend.fields import FieldPreviewInput


def main():
    instances=find_instances()
    if not instances:raise RuntimeError('Open Power BI Desktop first')
    with TemporaryDirectory(prefix='field-preview-') as folder:
        app=create_app(Path(folder))
        model=app.state.powerbi.connect(instances[0].port)
        columns=[(table,c) for table,cols in model['context']['tables'].items() for c in cols if c['dtype']=='string']
        columns.sort(key=lambda pair:(not any(word in pair[1]['name'].lower() for word in ('category','class','group','类别','分类')),len(pair[1]['name'])))
        if not columns:raise RuntimeError('No string column available for verification')
        table,column=columns[0]
        fields=app.state.chat.analysis.fields
        preview=fields.preview(model['id'],FieldPreviewInput(table=table,column=column['name'],limit=10))
        stats=preview['statistics']
        assert preview['run_verified'] and stats['blank_count']<=stats['row_count']
        assert len(preview['values'])<=10
        if not preview['truncated']:assert sum(v['count'] for v in preview['values'])==stats['row_count']
        value=next((v['value'] for v in preview['values'] if isinstance(v['value'],str) and v['value']),None)
        searched=False
        if value:
            result=fields.preview(model['id'],FieldPreviewInput(table=table,column=column['name'],search=value[:12],limit=10))
            assert result['statistics']['matched_distinct_count']>0 and result['samples']
            searched=True
        print(json.dumps({'desktop_field_preview':'pass','model_writes':0,'actual_values_logged':False,
                          'table':table,'column':column['name'],'search_verified':searched,
                          'bounded_values':True,'blank_statistics_verified':True},ensure_ascii=False))


if __name__=='__main__':main()
