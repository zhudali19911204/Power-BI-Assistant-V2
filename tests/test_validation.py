from backend.engine.context.base import Column, ModelContext
from backend.engine.core.artifact import ValidationResult
from backend.validation import candidates, validate


def test_multiple_filters_with_single_result_only_target_final_measure():
    ctx = ModelContext(tables={'Sales': [Column('Amount','double','Sales'),Column('Region','string','Sales')]})
    objects = candidates('```dax\nBase = 1\n```\n```dax\nTarget = [Base] * 2\n```','dax')
    class Evaluator:
        def evaluate_with_defines(self,*args):
            return ValidationResult(ok=True,sample=2,run_verified=True)
        def evaluate_with_defines_at_slice(self,home,defs,target,filters):
            assert target == 'Target'
            assert filters==[('Sales','Amount',3),('Sales','Region','East')]
            return ValidationResult(ok=True,sample=4,run_verified=True)
    points=[{'filters':[{'table':'Sales','column':'Amount','value':3},
                       {'table':'Sales','column':'Region','value':'East'}], 'expected':4,'tolerance':0.01}]
    results=validate(ctx,objects,'Sales',Evaluator(),points)
    assert results[0]['ok']
    assert not results[0]['calibration']
    assert results[1]['ok']
    assert results[1]['run_verified']
    assert len(results[1]['calibration'])==1
    assert results[1]['calibration'][0]['matched']


def test_arbitrary_code_fences_are_not_executable_artifacts():
    assert not candidates('```python\nprint("hello")\n```','dax')
    assert not candidates('```json\n{"a":1}\n```','mquery')


def test_static_checks_ignore_comments_and_strings_and_support_escaped_names():
    from backend.engine.dax.artifact import DaxMeasureArtifact
    ctx = ModelContext(tables={"Sales'FY": [Column('A]B','double',"Sales'FY")]})
    code = '''// Missing[Column] (\n SUM('Sales''FY'[A]]B]) + LEN("Missing[Column] (")'''
    result = DaxMeasureArtifact(code,'Test').validate(ctx)
    assert result.ok, result.errors
    assert not result.run_verified


def test_connection_failure_is_not_marked_as_engine_execution(monkeypatch):
    from backend.engine.dax.live_eval import LiveDesktopEvaluator
    class FailingSession:
        def __init__(self,*a): pass
        def __enter__(self): raise RuntimeError('Disconnected')
        def __exit__(self,*a): pass
    monkeypatch.setattr('backend.engine.dax.live_eval._LiveSession',FailingSession)
    result = LiveDesktopEvaluator(123,'fake-dll').evaluate_measure('1+1','Sales')
    assert not result.ok and not result.run_verified
