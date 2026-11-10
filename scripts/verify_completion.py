"""Exercise real completion metadata with synthetic evidence only; no Desktop reads/writes."""
import json

from backend.app import data_directory
from backend.database import Database
from backend.engine.llm.base import ChatMessage
from backend.engine.llm.diagnostics import GenerationReport
from backend.settings import SettingsService
from backend.streaming import completion_events


def main():
    directory = data_directory()
    settings = SettingsService(Database(directory / 'assistant.sqlite3'), directory)
    provider = settings.provider()
    report, answer = GenerationReport(), ''
    options = {'enable_thinking': False} if getattr(provider, 'can_disable_thinking', False) else {}
    try:
        for event in completion_events(provider,
                '这是接口验证，数据是程序生成的测试数据。只用一句中文说明数值，并引用 [Q1]。',
                [ChatMessage('user', '测试查询结果：{"evidence":[{"label":"Q1","ok":true,"rows":[{"Value":125}]}]}')],
                report=report, timeout=120, **options):
            if event['type'] == 'delta': answer += event['text']
        passed = '125' in answer and '[Q1]' in answer and report.finish_reason == 'stop'
        print(json.dumps({'completion_test': 'pass' if passed else 'unconfirmed', 'model': provider.model,
            'diagnostic': report.as_dict(), 'synthetic_evidence_only': True,
            'powerbi_data_sent': False, 'model_writes': 0, 'requests': 1}, ensure_ascii=True))
        if not passed: raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'completion_test': 'failed', 'error_type': type(exc).__name__,
                          'diagnostic': report.as_dict()}, ensure_ascii=True))
        raise SystemExit(1) from None
    finally:
        provider._client.close()


if __name__ == '__main__': main()
