"""Verify real Desktop code attribution and the two generation prompts without model writes."""
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import now
from backend.engine.context.live_source import find_instances


def main():
    app = create_app()
    ports = {i.port for i in find_instances()}
    row = next((r for r in app.state.db.all(
        'SELECT s.model_id,s.payload,m.port FROM model_semantics s JOIN models m ON m.id=s.model_id '
        'ORDER BY s.updated_at DESC') if r['port'] in ports and json.loads(r['payload'])['status'] == 'ready'), None)
    if not row:
        raise RuntimeError('请打开已完成语义梳理的 Power BI Desktop 模型后重试')
    model = app.state.powerbi.get(row['model_id'])
    app.state.powerbi.require_live(model['id'], unchanged=True)
    started = time.monotonic()
    created = []
    calls = []

    class ObservedProvider:
        # Transport stays local in this regression check. The engine queries are real;
        # a fake answer lets us verify prompt injection without any business aggregation.
        def stream(self, system, messages, **opts):
            evidence = system.split('本轮编号实际查值证据', 1)[1]
            calls.append({'company_code_in_value_evidence': 'Company Code' in evidence,
                          'calendar_not_recommended': '周编号' not in evidence})
            yield '已按实际字段取值识别编号。'

    report = {'time': now(), 'model_writes': 0, 'external_model_requests': 0}
    original_provider = app.state.settings.provider
    app.state.settings.provider = lambda *a, **k: ObservedProvider()
    with TestClient(app) as client:
        try:
            for mode in ('dax', 'analysis'):
                conv = client.post('/api/conversations', json={'model_id': model['id'], 'mode': mode,
                    'title': '编号真实取值联调'}).json()
                created.append(conv['id'])
                response = client.post(f"/api/conversations/{conv['id']}/messages", json={'content': '0257 是什么意思'})
                assert response.status_code == 200
                events = [json.loads(line) for line in response.text.splitlines()]
                assert events[-1]['type'] == 'done' and not any(e['type'] == 'clarification' for e in events)
                text = ''.join(e.get('text', '') for e in events if e['type'] == 'delta')
                assert 'Dim_Plant[Company Code]' in text, '当前真实模型中的编号归属与预期不符'
                saved = client.get(f"/api/conversations/{conv['id']}/messages").json()[-1]
                assert saved['status'] == 'complete' and not saved['analysis'] and not saved['artifacts']
                report[mode] = {'local_explanation': True, 'real_value_match': True,
                                'no_filter_question': True, 'saved': True}
                print('STATUS:', mode, '真实编号解释通过', flush=True)
            lookup = app.state.db.one('SELECT payload FROM model_value_lookups WHERE model_id=? AND literal=?',
                                      (model['id'], '0257'))
            evidence = json.loads(lookup['payload'])
            report.update(searched_fields=evidence['searched_fields'], total_fields=evidence['total_fields'],
                          complete=evidence['complete'], matched_fields=evidence['matches'])
            # Exercise both actual system-prompt builders with a transport stand-in. In
            # analysis, stop after receiving a clarification: no metric query can run.
            class PlanProvider(ObservedProvider):
                def stream(self, system, messages, **opts):
                    for _ in super().stream(system, messages, **opts): pass
                    yield json.dumps({'queries': [], 'clarification': {
                        'topic': '测试指标', 'question': '选择哪个测试指标？', 'context': '',
                        'options': [{'label': '常量一', 'value': '使用常量一'},
                                    {'label': '常量二', 'value': '使用常量二'}]}}, ensure_ascii=False)

            for index, cid in enumerate(created):
                selected_provider = ObservedProvider if index == 0 else PlanProvider
                app.state.settings.provider = lambda *a, cls=selected_provider, **k: cls()
                response = client.post(f'/api/conversations/{cid}/messages', json={'content': '分析0257；本次只测试字段依据，不执行金额查询'})
                events = [json.loads(line) for line in response.text.splitlines()]
                assert events[-1]['type'] == 'done'
                if not evidence['complete'] or len(evidence['matches']) != 1:
                    card = next(e['clarification'] for e in events if e['type'] == 'clarification')
                    assert card['value_evidence']['matches']
            if evidence['complete'] and len(evidence['matches']) == 1:
                assert len(calls) == 2 and all(all(call.values()) for call in calls)
                report['dax_and_analysis_prompt_evidence'] = True
            else:
                assert not calls
                report['ambiguous_or_partial_mapping_clarified'] = True
        finally:
            app.state.settings.provider = original_provider
            for cid in created:
                response = client.delete(f'/api/conversations/{cid}')
                assert response.status_code == 200
    report['seconds'] = round(time.monotonic() - started, 2)
    target = Path(__file__).resolve().parents[1] / 'artifacts'
    target.mkdir(exist_ok=True)
    (target / 'value-grounding-report.json').write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
