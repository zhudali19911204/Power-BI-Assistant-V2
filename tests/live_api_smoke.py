"""Real Desktop + HTTP API integration. The LLM reply is a fixture; DAX executes for real.

All EVALUATE definitions are query-scoped. This check never calls the write-back endpoint.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from backend.app import create_app


class ProbeProvider:
    def __init__(self, grounded):
        self.grounded = grounded

    def stream(self, system, messages, **opts):
        yield '```dax\n__V2_API_Grounded = ' + self.grounded + '\n```\n'
        yield '这是连接测试使用的固定表达式。\n```dax\n'
        yield '__V2_API_Probe = 1 + 1\n```'


def main():
    artifact_root = Path(__file__).resolve().parents[1] / 'artifacts'
    artifact_root.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='live-api-', dir=artifact_root) as directory:
        app = create_app(Path(directory))
        with TestClient(app) as client:
            instances = client.get('/api/desktop/instances').json()
            if not instances:
                raise RuntimeError('No open Power BI Desktop instance found')
            for instance in instances:
                response = client.post('/api/models/connect', json={'port': instance['port']})
                assert response.status_code == 200, response.text
                model = response.json()
                assert model['live'] and model['context']['tables']
                home = next(iter(model['context']['tables']))
                numeric = next(((name,col['name']) for name,cols in model['context']['tables'].items()
                                for col in cols if col['dtype'] in ('int64','double','decimal')), None)
                grounded = ("SUM('"+numeric[0].replace("'","''")+"'["+numeric[1].replace(']',']]')+"])"
                            if numeric else '1')
                app.state.settings.provider = lambda: ProbeProvider(grounded)
                conv = client.post('/api/conversations', json={'model_id': model['id']}).json()
                streamed = client.post(f"/api/conversations/{conv['id']}/messages", json={
                    'content': '连接测试，计算 1+1，并校准结果为2', 'home_table': home,
                    'points': [{'filters': [], 'expected': 2, 'tolerance': 0}]})
                assert streamed.status_code == 200, streamed.text
                events = [json.loads(line) for line in streamed.text.splitlines()]
                assert events[-1]['type'] == 'done', events
                objects = next(e for e in events if e['type'] == 'artifacts')['artifacts']
                assert len(objects) == 2
                assert objects[0]['validation']['ok'] and objects[0]['validation']['run_verified'], objects[0]
                obj = objects[-1]
                vr = obj['validation']
                assert vr['ok'] and vr['run_verified'] and vr['sample'] == 2, vr
                assert vr['calibration'][0]['matched'], vr
                saved = client.get(f"/api/conversations/{conv['id']}/messages").json()
                assert saved[-1]['artifacts'][0]['validation']['run_verified']
                exported = client.get(f"/api/conversations/{conv['id']}/export")
                assert exported.status_code == 200 and '已实跑' in exported.text
                assert not app.state.db.all('SELECT * FROM audit')
                print(f"Port {instance['port']}: API connect -> model context -> streamed fixture -> real DAX "
                      f"evaluation (2) -> calibration -> SQLite history -> export: PASS")
            print(f"LIVE_API_OK: {len(instances)} Desktop instances; no model writes; no real LLM requests")


if __name__ == '__main__':
    main()
