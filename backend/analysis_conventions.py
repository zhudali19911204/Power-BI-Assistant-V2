"""Check how a business query accounts for the confirmed defaults in its prompt."""
from .analysis_budget import query_key
from .analysis_recovery import model_objects
from .scope_checks import _filter_literals


PLAN_GUIDANCE = (
    '在 scope 说明实际采用的约定。检查本批每条业务 DAX 及其度量，确保应采用的筛选已经落实。'
    '若用户覆盖、约定不适用、某项对比分母有不同口径，或通过其他等价表达实现，'
    '在该条 queries 项的可选字段 convention_exceptions 中按编号解释具体依据，例如 {"C1":"<用户原话、范围差异或实际使用的等价表达>"}。'
    '直接落实的筛选无需填写此项。仅关联对象的约定按用途处理；展示、单位等不直接体现在 DAX 中的做法，在 scope 或 assessment 中写出约定名称和处理方式。'
    '约定之间有影响结果的冲突且无法消解时沿用 clarification。')

SUMMARY_GUIDANCE = (
    '遵循适用的展示约定，说明实际采用的默认口径。结合成功查询的 DAX、scope 和 convention_exceptions 核实，'
    '只把有查询依据的筛选写成已采用；用户覆盖或仅用于比较的口径按实际结果说明。')


def _filter_accounted_for(condition, expressions, text_fields):
    field = condition['table'].casefold(), condition['column'].casefold()
    expected = {v.casefold() for v in condition['values']}
    if field in text_fields:
        expected = {'"' + v.replace('"', '""') + '"' for v in expected}
    found = [
        _filter_literals(code, *field, variables=True, positive_only=True, keep_quotes=field in text_fields)
        if field in objects else set() for objects, code in expressions
    ]
    # A different explicit filter on this field needs its own explanation even
    # when a native measure contains the default values.
    return found[0] == expected if found[0] else expected in found[1:]


def _objects_accounted_for(rule, expressions, description):
    # A table association alone is too broad to require a particular query shape.
    # A display convention can be addressed in the scope without modifying DAX.
    targets = {('' if t['kind'] == 'measure' else t['table'].casefold(), t['name'].casefold())
               for t in rule.get('targets', []) if t['kind'] in ('column', 'measure')}
    return (not targets or rule['title'].casefold() in description
            or any(targets & objects for objects, _ in expressions))


def convention_issues(plan, rules, ctx, verified_text_fields=()):
    if not rules and not any(q.convention_exceptions for q in plan.queries):
        return []

    measures = {m.name.casefold(): m.expression for m in ctx.measures if m.expression}
    text_fields = {(table.casefold(), c.name.casefold()) for table, columns in ctx.tables.items()
                   for c in columns if c.dtype == 'string'} | set(verified_text_fields)
    facts = {}

    def sources(dax):
        # Follow real measure references. Comments and strings cannot introduce a dependency.
        todo, seen, result = [dax], set(), []
        while todo:
            expression = todo.pop()
            if expression in seen:
                continue
            seen.add(expression)
            if expression not in facts:
                facts[expression] = model_objects(expression, ctx), query_key(expression).casefold()
            objects, code = facts[expression]
            result.append((objects, code))
            todo.extend(measures[name] for table, name in objects if not table and name in measures)
        return result

    issues = []
    description = (plan.scope + '\n' + plan.assessment).casefold()
    for index, query in enumerate(plan.queries):
        if query.convention_exceptions.keys() - rules.keys():
            issues.append({'path': f'queries[{index}].convention_exceptions', 'type': 'unchecked_convention',
                           'message': '说明中只可引用本轮列出的已确认约定编号'})
        pending = {label: rule for label, rule in rules.items() if label not in query.convention_exceptions}
        if not pending:
            continue
        expressions = sources(query.dax)
        missing = []
        for label, rule in pending.items():
            if rule.get('filters'):
                accounted = all(_filter_accounted_for(f, expressions, text_fields) for f in rule['filters'])
            else:
                accounted = _objects_accounted_for(rule, expressions, description)
            if not accounted:
                missing.append(label)
        if missing:
            issues.append({'path': f'queries[{index}].dax', 'type': 'unchecked_convention',
                           'conventions': missing,
                           'message': '请核对已确认约定 ' + '、'.join(missing) + ' 的完整筛选或关联对象用途，并交代本条查询的口径'})
    return issues
