"""Read-only real Desktop reconnect verification using an isolated assistant database."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app import create_app
from backend.database import encode, now
from backend.engine.context.live_source import find_instances
from backend.powerbi import PowerBIService


def main():
    instances=find_instances()
    if not instances:raise RuntimeError('请先打开 Power BI Desktop 报表')
    directory=Path(__file__).resolve().parents[1]/'artifacts'
    directory.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='reconnect-',dir=directory) as folder:
        app=create_app(Path(folder));service=app.state.powerbi
        model=service.connect(instances[0].port)
        dictionary=app.state.semantics.ensure(model)
        expected=app.state.db.one('SELECT payload FROM model_semantics WHERE model_id=?',(model['id'],))['payload']
        restarted=PowerBIService(app.state.db)
        first,_=restarted.require_live(model['id'],unchanged=True)
        assert first['live'] and first['port']==instances[0].port
        has_identity=bool(first['report'].get('name'))
        if has_identity:
            # Only the isolated SQLite pointer is changed. The actual Desktop process,
            # its port, report data and user assistant database are never modified.
            obsolete=1 if first['port']!=1 else 2
            app.state.db.execute('UPDATE models SET port=? WHERE id=?',(obsolete,model['id']))
            restored,_=restarted.require_live(model['id'],unchanged=True)
            assert restored['live'] and restored['id']==model['id'] and restored['port']==instances[0].port
        assert app.state.db.one('SELECT payload FROM model_semantics WHERE model_id=?',(model['id'],))['payload']==expected
        assert len(app.state.db.all('SELECT id FROM models'))==1 and not app.state.db.all('SELECT * FROM audit')
        report={'time':now(),'reconnect_test':'pass','assistant_restart_restored':True,
                'obsolete_port_rebound_to_real_engine':has_identity,'model_id_preserved':True,
                'dictionary_entries_preserved':len(dictionary['entries']),
                'desktop_restarted':False,'business_values_read':False,'model_writes':0,'llm_requests':0}
        (directory/'reconnect-report.json').write_text(encode(report),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':main()
