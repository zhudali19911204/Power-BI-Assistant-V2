"""Reuse field evidence and finish bounded discovery with an explicit choice."""
import json

from .clarifications import ClarificationOption, ClarificationQuestion
from .fields import field_ref


MAX_FIELD_CHECKS = 12
MAX_FIELD_SEARCHES = 3


def check_key(check):
    return check.table, check.column, tuple(sorted({term.strip().casefold() for term in check.search_terms}))


def cached_check(check, records):
    """Exact attempts are never repeated; complete supersets can answer new searches.

    A failed attempt is remembered without treating it as an empty successful read.
    Truncated results cover only their original search, never a narrower/new search.
    """
    key = check_key(check)
    for record in reversed(records):
        if (record['table'], record['column']) != key[:2]:continue
        profile = record.get('profile') or {}
        terms = tuple(sorted({term.strip().casefold() for term in record.get('search_terms', profile.get('search_terms', []))}))
        if terms == key[2]:return record
        if (record.get('ok') and profile.get('truncated') is False
                and not any(value.get('value_truncated') for value in profile.get('values', []))
                and (not terms or (key[2] and set(key[2]).issubset(terms)))):
            return record
    return None


def field_stop_reason(check, records, budget):
    if len(records) >= MAX_FIELD_CHECKS:
        return f'已达到本轮 {MAX_FIELD_CHECKS} 项字段检查上限'
    if sum((r['table'], r['column']) == (check.table, check.column) for r in records) >= MAX_FIELD_SEARCHES:
        return f'同一字段已完成 {MAX_FIELD_SEARCHES} 组搜索，已停止继续换词检查'
    if not budget.can_check_field():
        return '剩余时间不足以继续查值并评估结果，已保留结论生成时间'
    return ''


def discovery_question(records, ctx, reason, checks=()):
    """Offer values from one real field, without choosing or inventing a filter.

    Prefer the field still requested by the planner, then searches with actual
    matches. Each option preserves the full original value, including leading zeros.
    """
    preferred = {(check.table, check.column) for check in checks}
    ordered = sorted(reversed(records), key=lambda r: (
        (r['table'], r['column']) not in preferred,
        not bool((r.get('profile') or {}).get('search_terms'))))
    for record in ordered:
        profile = record.get('profile') or {}
        if not record.get('ok') or not profile.get('run_verified'):continue
        if not ctx.has_column(record['table'], record['column']):continue
        ref = field_ref(record['table'], record['column'])
        options = [];seen = set()
        for entry in profile.get('values', []):
            value = entry.get('value')
            if value is None or value == '' or entry.get('value_truncated') or entry.get('nonfinite'):continue
            literal = json.dumps(value, ensure_ascii=False, allow_nan=False)
            option_value = f'使用 {ref} 的实际取值 {literal} 作为本次筛选，保留此前已确认的其他条件'
            if literal in seen or len(option_value) > 1000:continue
            seen.add(literal)
            options.append(ClarificationOption(label=(str(value)[:97] + '…') if len(str(value)) > 100 else str(value),
                value=option_value, detail=f"来自字段检查 {record['label']} · {ref}"[:240]))
            if len(options) == 3:break
        if not options:continue
        options.append(ClarificationOption(label='补充其他取值或范围', value=f'由我补充 {ref} 对应的筛选取值或范围'[:1000]))
        caveat = '取值样例已截断，未列出的值也可能存在。' if profile.get('truncated') else ''
        return ClarificationQuestion(topic='筛选口径', question='本次分析希望采用下面哪个筛选范围？',
            context=(reason + '。下列候选来自已成功读取的同一字段，业务含义仍待确认。' + caveat)[:400], options=options)
    return ClarificationQuestion(topic='筛选口径', question='本次分析应采用哪个分类定义？',
        context=(reason + '。本轮未取得可安全列出的候选取值，已有检查结果和错误已保留。')[:400], options=[
            ClarificationOption(label='补充字段与取值', value='由我补充本次业务分类对应的真实字段和取值'),
            ClarificationOption(label='先确认词典定义', value='先在业务词典确认本次业务分类对应的字段与取值，再继续分析')])
