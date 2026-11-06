"""Read-only integration check against open Desktop reports. No LLM calls or writes."""
from backend.engine.context.live_source import LiveDesktopSource, find_instances
from backend.engine.dax.live_eval import LiveDesktopEvaluator
from backend.validation import validate


def main():
    instances = find_instances()
    if not instances:
        raise RuntimeError('Open a report in Power BI Desktop first')
    for instance in instances:
        ctx = LiveDesktopSource(instance.port).load()
        home = next(iter(ctx.tables))
        evaluator = LiveDesktopEvaluator(instance.port)
        objects = [{'kind':'dax_measure','name':'__V2_read_only_test','expression':'1 + 1'}]
        points = [{'filters':[], 'expected':2, 'tolerance':0}]
        result = validate(ctx,objects,home,evaluator,points)[0]
        assert result['ok'] and result['run_verified'] and result['sample'] == 2, result
        assert result['calibration'][0]['matched']
        table = evaluator.evaluate_table_expr('CALENDAR(DATE(2025,1,1),DATE(2025,1,3))')
        assert table.ok and table.run_verified, table
        invalid = evaluator.evaluate_measure('THIS_FUNCTION_DOES_NOT_EXIST()', home)
        assert not invalid.ok and invalid.run_verified
        print(f'Port {instance.port}: {len(ctx.tables)} tables, {sum(map(len,ctx.tables.values()))} columns; '
              'scalar evaluation, calibration, table evaluation and engine-error handling passed')


if __name__ == '__main__':
    main()
