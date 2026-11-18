"""Decide when a failed business query has a grounded alternative worth one more read."""
import re

from .analysis_budget import query_key
from .engine.dax.artifact import _BRACKET, _COLUMN_REF, _code_only
from .scope_checks import _filter_literals, _term_pattern


RECOVERY_RULE = (
    '先用已有成功结果判断原问题是否还缺关键数据。只在有依据改用不同取数方法时，从 recovery_candidates 选择一个原 Q。'
    '优先直接取现有度量所需的基础值，如按两个同期月份读取度量后再比较。保留已确定的指标、期间、版本和实体筛选，'
    '沿用原有明确的字段取值条件。不要重跑成功的查询。'
    '替代方案只返回一条 queries 项，并附 "recovery":{"for_query":"Q编号","missing":"缺少哪项关键证据",'
    '"basis":"引用真实 [度量] 或 \'表\'[列]，说明具体怎样换一种查法"}。'
    '已有结果足够时返回空 queries，并在 assessment 中简述原因。缺少新依据时请结束该失败项。'
    '确实需要用户决定业务口径时返回 clarification。')

_POSITION = re.compile(r'(?:\bquery|查询)\s*[（(]\s*\d+\s*[,，]\s*\d+\s*[)）]|\bline\s+\d+\s*,?\s*column\s+\d+', re.I)
_SERVICE_ERROR = re.compile(r'权限|拒绝访问|超时|access (?:is )?denied|permission|(?:un|not )authori[sz]ed|'
                            r'forbidden|insufficient privileges|time[ -]?out|timed out', re.I)
_OPERATION = re.compile(r'\b[\w.]+(?=\s*\()|\b(?:var|return|order\s+by|in)\b|&&|\|\||[+*/<>=-]', re.I)
_IDENTIFIER = re.compile(r"'(?:''|[^'])*'|\[(?:\]\]|[^\]])*\]")


def model_objects(text, ctx):
    """Return real qualified columns and measures; quoted string values and comments are not references."""
    code = _code_only(text)
    measures = {m.name.casefold(): m.table.casefold() for m in ctx.measures}
    tables = {t.casefold(): t for t in ctx.tables}
    objects, qualified = set(), set()
    for match in _COLUMN_REF.finditer(code):
        table = (match[1] or match[2]).replace("''", "'").casefold()
        name = match[3].replace(']]', ']').casefold()
        if table in tables or match[1]:
            qualified.add(match.end())
        if table in tables and any(c.name.casefold() == name for c in ctx.tables[tables[table]]):
            objects.add((table, name))
        elif measures.get(name) == table:
            objects.add(('', name))
    for match in _BRACKET.finditer(code):
        name = match[1].replace(']]', ']').casefold()
        if match.end() not in qualified and name in measures:
            objects.add(('', name))
    return objects


def _diagnostic(error):
    return ' '.join(_POSITION.sub('', error).casefold().split())


def _measure_clue(task, attempts, successes, ctx):
    folded = task.casefold()
    used = set().union(*(model_objects(r['dax'], ctx) for r in attempts))
    for record in successes:
        shared = sorted(name for table, name in used & model_objects(record['dax'], ctx) if not table)
        if shared:
            return f"{record['label']} 已成功读取 [{shared[0]}]，可用同一度量补查原问题尚缺的基础值。"
    for measure in ctx.measures:
        name = measure.name.casefold()
        if ('', name) in used or name not in folded:
            continue
        if re.search(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])', task, re.I):
            return f'原问题点名的模型度量 [{measure.name}] 尚未直接取数，可核对它能否提供缺失的基础值。'
    return ''


def recovery_candidates(records, repair_hints, ctx, task, successes):
    first = {r['label']: r for r in records if r['attempt'] == 1 and not r['ok']}
    candidates = {}
    for r in records:
        original = first.get(r['label'])
        if not original or r['attempt'] != 2 or r['ok'] or not r.get('run_verified') or r.get('connection_error'):
            continue
        error = r.get('error', '')
        if not error.strip() or _SERVICE_ERROR.search(error):
            continue
        hint = repair_hints.get(r['label'], '')
        if hint and not model_objects(hint, ctx):
            hint = ''
        if _diagnostic(original['error']) == _diagnostic(error) and not hint:
            hint = _measure_clue(task, (original, r), successes, ctx)
            if not hint:
                continue
        # The regular evidence already contains the repaired DAX and its failure.
        # Include the first attempt only for eligible queries and bound long expressions.
        candidates[r['label']] = {'first_error': original['error'], 'first_dax': original['dax'][:2000],
                                   'first_dax_truncated': len(original['dax']) > 2000, 'alternative_hint': hint}
    return candidates


def _operations(dax):
    code = _IDENTIFIER.sub(' ', _code_only(dax)).casefold()
    return tuple(''.join(token.split()) for token in _OPERATION.findall(code))


def _period_extension(table, column, extra, fixed, calendars, requested):
    # Fiscal models may expose a year column without marking their date table.
    if table not in calendars and not re.fullmatch(r'年|年份|年度|会计年度|(?:fiscal[ _]?|calendar[ _]?)?year', column):
        return False
    prior_year = re.search(r'同比|去年|上年|两年同期|(?:last|previous|prior)[ -]+year|year[ -]+over[ -]+year|\byoy\b', requested)
    # For YoY keep the period itself, including the month/day in a YYYYMM or ISO date key.
    prior_values = set()
    if prior_year:
        prior_values = {str(int(v[:4]) - 1) + v[4:] for v in fixed if re.match(r'[1-9][0-9]{3}', v)}
    return all(v in prior_values or (v and re.search(_term_pattern(v), requested)) for v in extra)


def recovery_problem(plan, candidates, evidence, ctx, field_records, task):
    target = plan.recovery.for_query
    if target not in candidates:
        return '该查询没有可用的额外补救机会'
    dax = plan.queries[0].dax
    objects = model_objects(dax, ctx)
    if not model_objects(plan.recovery.basis, ctx) & objects:
        return '替代方案的依据没有引用本次查询实际使用的模型度量或字段'
    attempts = [r for r in evidence if r['label'] == target]
    if any(query_key(r['dax']) == query_key(dax) for r in evidence):
        return '替代方案重复了已执行的查询'
    old_objects = [model_objects(r['dax'], ctx) for r in attempts]
    operations = _operations(dax)
    if any(refs == objects and _operations(r['dax']) == operations for r, refs in zip(attempts, old_objects)):
        return '替代方案仍使用已经失败的取数方法'

    # Preserve explicit equality/IN/TREATAS filters and confirmed literal bindings.
    # A calendar comparison may need both periods in the replacement result.
    old_codes = [query_key(r['dax']).casefold() for r in attempts]
    new_code = query_key(dax).casefold()
    calendars = {t.casefold() for t in ctx.date_tables}
    requested = (task+'\n'+attempts[0]['scope']).casefold()
    columns = set().union(*old_objects)
    for table, column in sorted(columns):
        if not table:
            continue
        old = [_filter_literals(code, table, column) for code in old_codes]
        confirmed = {str(v).casefold() for r in field_records if r['table'].casefold() == table and r['column'].casefold() == column
                     if (v := r.get('confirmed_value') or r.get('adopted_value')) is not None}
        observed = set.union(*old)
        named = {v for v in observed if v and re.search(_term_pattern(v), requested)}
        fixed = named | set.intersection(*old) | (confirmed & observed)
        if not fixed:
            continue
        new = _filter_literals(new_code, table, column)
        extra = new - fixed
        if not fixed.issubset(new) or (extra and not _period_extension(table, column, extra, fixed, calendars, requested)):
            return f'替代查询未保留原筛选 {table}[{column}] 的取值'
    return ''
