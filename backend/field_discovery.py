"""Reuse field evidence and finish bounded discovery with an explicit choice."""
import json
from datetime import datetime, timedelta, timezone

from .clarifications import ClarificationOption, ClarificationQuestion
from .fields import field_ref
from .language import wording
from .semantic_retrieval import _contains, _normalize


MAX_FIELD_CHECKS = 12
MAX_FIELD_SEARCHES = 3
CLARIFICATION_EVIDENCE_AGE = timedelta(minutes=15)


def _field_source(model):
    return [model['id'], model['fingerprint'], model['port'], model.get('report', {}).get('engine_pid')]


def _complete_observation(record):
    profile = record.get('profile') or {}
    return (record.get('ok') and profile.get('run_verified') and profile.get('truncated') is False
            and not any(v.get('value_truncated') or v.get('nonfinite') for v in profile.get('values', [])))


def clarification_field_sources(model, records):
    """Keep references to the original reads, including across successive questions."""
    ids = list(dict.fromkeys(r['id'] for r in records if _complete_observation(r)))
    return {'source': _field_source(model), 'ids': ids} if ids else None


def clarification_evidence(db, conversation_id, model, ctx, reply):
    """Return reusable reads and the verified field/value explicitly selected on this card."""
    if not reply:
        return [], None
    row = db.one('SELECT c.payload FROM message_clarifications c JOIN messages m ON m.id=c.message_id '
                 'WHERE m.conversation_id=? AND c.message_id=?', (conversation_id, reply.message_id))
    if not row:
        return [], None
    card = json.loads(row['payload'])
    saved = card.get('_field_sources')
    cutoff = (datetime.now(timezone.utc) - CLARIFICATION_EVIDENCE_AGE).isoformat()
    args = [model['id'], model['fingerprint'], conversation_id, cutoff]
    query = ('SELECT f.message_id,f.result FROM field_observations f JOIN messages m ON m.id=f.message_id '
             'LEFT JOIN field_value_profiles p ON p.model_id=? AND p.fingerprint=? '
             "AND p.table_name=json_extract(f.result,'$.table') AND p.column_name=json_extract(f.result,'$.column') "
             'WHERE m.conversation_id=? AND f.created_at>=? ')
    if saved:
        if saved['source'] != _field_source(model):
            return [], None
        query += 'AND f.id IN (SELECT value FROM json_each(?)) '
        query += "AND (p.checked_at IS NULL OR p.checked_at <= json_extract(f.result,'$.profile.checked_at')) "
        args.append(json.dumps(saved['ids']))
    else:
        # Older cards have no connection stamp. The latest field preview is cleared
        # on an engine change. Require the exact read time, not just a matching value.
        query += "AND f.message_id=? AND p.checked_at=json_extract(f.result,'$.profile.checked_at') "
        args.append(reply.message_id)
    choice = card.get('scope_binding', {}).get('choices', {}).get(reply.option_id)
    original_id = (saved or {}).get('choice_ids', {}).get(reply.option_id)
    selected = None
    records = []
    for row in db.all(query + 'ORDER BY f.created_at,f.rowid', args):
        record = json.loads(row['result'])
        if record.get('model_id') != model['id'] or not ctx.has_column(record['table'], record['column']) or not _complete_observation(record):
            continue
        records.append(record)
        same_read = record['id'] == original_id if original_id else row['message_id'] == reply.message_id
        if (choice and not reply.supplement and same_read
                and (record['label'], record['table'], record['column']) == (choice['field'], choice['table'], choice['column'])
                and any(v.get('value') == choice['value'] for v in record['profile']['values'])):
            selected = {**choice, 'pending_ids': (saved or {}).get('pending_ids', [])}
    return records, selected


def prior_literal_check(check, records):
    """An inferred equality needs proof of every literal in this specific column."""
    if not check.search_terms:
        return None
    for record in reversed(records):
        if (record['table'], record['column']) != (check.table, check.column):
            continue
        values = {v['value'] for v in record['profile']['values'] if isinstance(v.get('value'), str)}
        if all(term in values for term in check.search_terms):
            return record
        # A later complete check may have superseded an earlier hit.
        searched = {term.casefold() for term in record['search_terms']}
        if any(term not in values and (not searched or term.casefold() in searched) for term in check.search_terms):
            return None
    return None


def clarification_value_summary(records):
    """Show the planner a few original values. Full profiles stay local until requested."""
    summary = []
    for record in records:
        profile = record['profile']
        values = [v['value'] for v in profile['values'] if isinstance(v.get('value'), str) and 0 < len(v['value']) <= 120]
        if values:
            terms = {t.casefold() for t in record['search_terms']}
            values.sort(key=lambda value: value.casefold() not in terms)
            summary.append({'table': record['table'], 'column': record['column'],
                            'values': values[:4], 'checked_at': profile['checked_at']})
    return summary


def stalled_searches(records, checks, question):
    """Spot repeated, fruitless legacy searches for an original user term."""
    missed = {}
    productive = set()
    user_text = _normalize(question)
    if any(_contains(user_text, _normalize(c.column)) or field_ref(c.table,c.column).casefold() in user_text
           for c in checks):
        return []
    for record in records:
        profile = record.get('profile') or {}
        if record.get('scope_term') or not record['ok'] or not profile.get('run_verified'):
            continue
        terms = {term.casefold() for term in record.get('search_terms', [])}
        for term in terms:
            if not _contains(user_text, term):
                continue
            if profile.get('statistics', {}).get('matched_distinct_count') != 0:
                productive.add(term)
                continue
            evidence = missed.setdefault(term, {'rounds': set(), 'fields': set(), 'spellings': set()})
            evidence['rounds'].add(record['round'])
            evidence['fields'].add((record['table'], record['column']))
            evidence['spellings'].update(terms)
    stalled = []
    for check in checks:
        words = {v.casefold() for v in check.search_terms}
        # New aliases and genuine field-profile requests are still allowed.
        term = next((t for t in sorted(words) if t in missed and t not in productive
                     and len(missed[t]['rounds']) >= 2 and len(missed[t]['fields']) >= 2
                     and words <= missed[t]['spellings']), None)
        if not term:
            return []
        if term not in stalled:stalled.append(term)
    return stalled


def check_key(check):
    return check.table, check.column, tuple(sorted({term.strip().casefold() for term in check.search_terms}))


def cached_check(check, records):
    """Exact attempts are never repeated; complete supersets can answer new searches.

    A failed attempt is remembered without treating it as an empty successful read.
    Truncated results cover only their original search, never a narrower/new search.
    """
    key = check_key(check)
    for record in reversed(records):
        if record.get('scope_term'):continue  # Exact evidence cannot cover a substring search or a column profile.
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
        return wording(f'已达到本轮 {MAX_FIELD_CHECKS} 项字段检查上限', f'Reached the limit of {MAX_FIELD_CHECKS} field checks for this turn')
    if sum((r['table'], r['column']) == (check.table, check.column) for r in records) >= MAX_FIELD_SEARCHES:
        return wording(f'同一字段已完成 {MAX_FIELD_SEARCHES} 组搜索，已停止继续换词检查',
                       f'This field has reached its limit of {MAX_FIELD_SEARCHES} searches')
    if not budget.can_check_field():
        return wording('剩余时间不足以继续查值并评估结果，已保留结论生成时间', 'The remaining time is reserved for the summary')
    return ''


def discovery_question(records, ctx, reason, checks=()):
    """Offer values from one real field, without choosing or inventing a filter.

    Prefer the field still requested by the planner, then searches with actual
    matches. Each option preserves the full original value, including leading zeros.
    """
    preferred = {(check.table, check.column) for check in checks}
    ordered = sorted((r for r in reversed(records) if not r.get('scope_term')), key=lambda r: (
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
            option_value = wording(f'使用 {ref} 的实际取值 {literal} 作为本次筛选，保留此前已确认的其他条件',
                                   f'Filter {ref} by the observed value {literal} and keep the other confirmed conditions')
            if literal in seen or len(option_value) > 1000:continue
            seen.add(literal)
            options.append(ClarificationOption(label=(str(value)[:97] + '…') if len(str(value)) > 100 else str(value),
                value=option_value, detail=(wording('来自字段检查', 'From field check') + f" {record['label']} · {ref}")[:240]))
            if len(options) == 3:break
        if not options:continue
        options.append(ClarificationOption(label=wording('补充其他取值或范围', 'Enter another value or range'),
            value=wording(f'由我补充 {ref} 对应的筛选取值或范围', f'I will specify a filter value or range for {ref}')[:1000]))
        caveat = wording('取值样例已截断，未列出的值也可能存在。', ' The sample is truncated and may omit other values.') if profile.get('truncated') else ''
        context = reason + wording('。下列候选来自已成功读取的同一字段，业务含义仍待确认。',
                                   '. These values come from the same field. Confirm which one matches your definition.')
        return ClarificationQuestion(topic=wording('筛选口径', 'Filter scope'),
            question=wording('本次分析希望采用下面哪个筛选范围？', 'Which filter should this analysis use?'),
            context=(context + caveat)[:400], options=options)
    return ClarificationQuestion(topic=wording('筛选口径', 'Filter scope'),
        question=wording('本次分析应采用哪个分类定义？', 'Which category definition should this analysis use?'),
        context=(reason + wording('。本轮未取得可安全列出的候选取值，已有检查结果和错误已保留。',
                                  '. No candidate values are available. Previous checks and errors are saved.'))[:400], options=[
            ClarificationOption(label=wording('补充字段与取值', 'Specify a field and value'),
                value=wording('由我补充本次业务分类对应的真实字段和取值', 'I will specify the field and value for this category')),
            ClarificationOption(label=wording('先确认词典定义', 'Check the dictionary'),
                value=wording('先在业务词典确认本次业务分类对应的字段与取值，再继续分析',
                              'I will confirm the field and value in the business dictionary before continuing'))])
