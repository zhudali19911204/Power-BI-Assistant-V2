"""Verify the candidate fields for each business term together in one Desktop read."""
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .analysis_budget import query_key
from .cancellation import check_cancelled
from .clarifications import ClarificationQuestion
from .database import now
from .engine.context.live_source import _LiveSession, find_adomd_dll, find_instances
from .field_discovery import field_stop_reason
from .fields import CandidateField, field_ref
from .values import exact_values


Term = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
MAX_CANDIDATES = 4


class ScopeCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    term: Term
    aliases: list[Term] = Field(default_factory=list, max_length=2)
    candidates: list[CandidateField] = Field(min_length=1, max_length=MAX_CANDIDATES)


class ScopeResolution(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r'^F[1-9]\d?$')]
    value: str = Field(min_length=1, max_length=500)
    basis: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]


@dataclass(frozen=True)
class Probe:
    term: str
    table: str
    column: str
    literals: tuple[str, ...]


def confirmed_filters(dictionary, term):
    """Only a single model-wide equality is interchangeable with a single lookup."""
    objects = {e['id']: (e['table'], e['name']) for e in dictionary.get('entries', []) if e['kind'] == 'column'}
    result = []
    for item in dictionary.get('business_dictionary', {}).get('items', []):
        if item['state'] != 'confirmed' or item['kind'] != 'term' or item['scope'] != 'model':
            continue
        if term.casefold() not in {s.casefold() for s in [item['title'], *item['aliases']]} or len(item['filters']) != 1:
            continue
        rule = item['filters'][0]
        target = objects.get(rule['object_id'])
        if target and len(rule['values']) == 1 and 0 < len(rule['values'][0]) <= 120:
            result.append((*target, rule['values'][0]))
    return result


def candidate_probes(scopes, ctx, bindings, hints, dictionary=None):
    """Prefer known leads. Add sibling levels only within an AI-selected hierarchy."""
    probes = []
    for scope in scopes:
        term = scope.term
        literals = [term]
        if not term.isdecimal():
            for alias in scope.aliases:
                if alias.casefold() not in {v.casefold() for v in literals}:
                    literals.append(alias)
        proposed = [(c.table, c.column) for c in scope.candidates]
        rules = confirmed_filters(dictionary or {}, term)
        known = [(b['table_name'], b['column_name']) for b in bindings if b['literal'].casefold() == term.casefold()]
        known.extend((table, column) for table, column, _ in rules)
        historical = sorted(hints.get(term.casefold(), ()), key=lambda pair: (pair not in proposed, pair))
        pairs = [p for p in known if ctx.has_column(*p)] + proposed[:1]
        pairs.extend(p for p in historical[:2] if ctx.has_column(*p))
        pairs.extend(proposed[1:])
        for table, name in proposed:
            level = re.fullmatch(r'(.+?)[\s_-]*([1-9]\d?)[a-zA-Z]?', name)
            if not level or level[1][-1].isdigit() or not ctx.has_column(table, name):
                continue
            siblings = re.compile(re.escape(level[1]) + r'[\s_-]*([1-9]\d?)([a-zA-Z]?)$', re.I)
            levels = [(int(m[1]), m[2], c.name) for c in ctx.tables[table] if (m := siblings.fullmatch(c.name))]
            number = int(level[2])
            levels.sort(key=lambda item: (item[0] >= number, abs(item[0] - number), bool(item[1])))
            pairs.extend((table, column) for _, _, column in levels)
        for table, column in list(dict.fromkeys(pairs))[:MAX_CANDIDATES]:
            defined = [v for t, c, v in rules if (t, c) == (table, column) and not term.isdecimal()]
            spellings = tuple(dict.fromkeys([term, *defined, *literals]))[:3]
            probes.append(Probe(term, table, column, spellings))
    return list(dict.fromkeys(probes))


def next_scope_batch(probes, records, budget):
    queues = defaultdict(deque)
    for p in probes:
        queues[p.term.casefold()].append(p)
    previous = defaultdict(list)
    rounds = defaultdict(set)
    for r in records:
        if r.get('scope_term'):
            topic = r['scope_term'].casefold()
            previous[topic, r['table'], r['column']].append(({v.casefold() for v in r['search_terms']}, r['ok']))
            rounds[topic].add(r['round'])
    selected, skipped = [], []
    charged = list(records)
    # On each pass give each term one available candidate, including when another
    # term has exhausted a particular column's allowance.
    while any(queues.values()):
        for topic, queue in queues.items():
            while queue:
                p = queue.popleft()
                key = topic, p.table, p.column
                terms = {v.casefold() for v in p.literals}
                if any(terms == old or (ok and terms <= old) for old, ok in previous[key]):
                    continue
                reason = field_stop_reason(p, charged, budget)
                if len(rounds[topic]) >= 2 or reason:
                    skipped.append((p, reason or '该原词已完成两批定向核对'))
                    continue
                selected.append(p)
                charged.append({'table': p.table, 'column': p.column})
                previous[key].append((terms, False))
                break
    return selected, skipped


def read_scope_batch(powerbi, model_id, probes):
    """Every requested comparison returns one indexed row, including exact misses."""
    results = [{'ok': False, 'profile': None, 'error': '候选字段不在当前模型中'} for _ in probes]
    with powerbi.lock:
        model, ctx = powerbi.require_live(model_id, unchanged=True)
        dtype = {(t, c.name): c.dtype for t, columns in ctx.tables.items() for c in columns}
        comparisons = {}
        for p in probes:
            if (p.table, p.column) in dtype:
                for literal in p.literals:
                    comparisons.setdefault((p.table, p.column, literal), len(comparisons))
        if not comparisons:
            return results, ''
        rows = []
        for (table, column, literal), index in comparisons.items():
            ref = field_ref(table, column)
            rows.append(f'VAR __pbiMatches = {exact_values(ref, literal)} RETURN '
                        f'ROW("__index", {index}, "__match", COALESCE(COUNTROWS(__pbiMatches), 0), '
                        f'"__value", MAXX(__pbiMatches, {ref} & ""))')
        query = 'EVALUATE ' + (rows[0] if len(rows) == 1 else 'UNION(\n' + ',\n'.join(rows) + '\n)')
        dll = find_adomd_dll(find_instances())
        if not dll:
            raise HTTPException(409, '未找到 Desktop 的 ADOMD 客户端')
        check_cancelled()
        with _LiveSession(model['port'], dll) as session:
            data = session.query(query, max_rows=len(rows) + 1, timeout_seconds=12)
    indexed = {}
    for row in data:
        row = {k.strip('[]'): v for k, v in row.items()}
        index = row.get('__index')
        if type(index) is not int or not 0 <= index < len(rows) or index in indexed:
            raise ValueError('精确查值返回的候选序号不完整或重复')
        indexed[index] = row
    checked_at = now()
    for i, p in enumerate(probes):
        if (p.table, p.column) not in dtype:
            continue
        values = []
        matched_terms = []
        for literal in p.literals:
            row = indexed.get(comparisons[p.table, p.column, literal], {})
            count, value = row.get('__match'), row.get('__value')
            if (type(count) is not int or count not in (0, 1) or '__value' not in row
                    or (count == 1 and (not isinstance(value, str) or not value or len(value) > 500))
                    or (count == 0 and value not in (None, ''))):
                results[i]['error'] = '精确查值返回不完整，仍需核实这个字段'
                break
            if count:
                matched_terms.append(literal)
                if value not in values:
                    values.append(value)
        else:
            results[i] = {'ok': True, 'error': '', 'profile': {
                'kind': 'exact', 'run_verified': True, 'dtype': dtype[p.table, p.column],
                'scope': '当前用户可读取的全模型范围，未应用报表页面筛选',
                'search_terms': list(p.literals), 'matched_terms': matched_terms, 'values': [{'value': v} for v in values],
                'truncated': False, 'checked_at': checked_at, 'dax_queries': []}}
    return results, query


def scope_evidence(records):
    groups = {}
    for r in records:
        if not r.get('scope_term'):
            continue
        term = r['scope_term']
        group = groups.setdefault(term.casefold(), {'term': term, 'matches': [], 'misses': [], 'incomplete': []})
        field = {'field': r['label'], 'table': r['table'], 'column': r['column']}
        if not r['ok']:
            group['incomplete'].append(field)
        elif r['profile']['values']:
            for entry in r['profile']['values']:
                group['matches'].append({**field, 'value': entry['value']})
        else:
            group['misses'].append(field)
    for r in records:
        group = groups.get(r.get('related_term', '').casefold())
        if not group:
            continue
        field = {'field': r['label'], 'table': r['table'], 'column': r['column']}
        if not r['ok']:
            group['incomplete'].append(field)
            continue
        profile = r['profile']
        if profile.get('truncated'):
            group['incomplete'].append({**field, 'reason': '取值清单已截断'})
        # Only actual matched values can be proposed. Unfiltered examples are not matches.
        if not profile.get('search_terms'):
            continue
        for entry in profile['values']:
            value = entry.get('value')
            if (isinstance(value, str) and value and len(value) <= 120 and not entry.get('value_truncated')
                    and (not group['term'].isdecimal() or value == group['term'])):
                match = {**field, 'value': value}
                if value.casefold() != group['term'].casefold():
                    match['match_kind'] = 'contains'
                group['matches'].append(match)
    return groups


def _match_option(term, match):
    ref = field_ref(match['table'], match['column'])
    value = json.dumps(match['value'], ensure_ascii=False)
    return {'label': f"{match['column']} · {match['value']}"[:100],
            'value': f'本次将“{term}”按 {ref} 的实际取值 {value} 筛选，保留已确定的其他口径',
            'detail': f"{match['field']} · {ref}"[:240]}


def scope_question(group, reason):
    term = group['term']
    options = []
    for m in group['matches']:
        option = _match_option(term, m)
        if len(option['value']) <= 1000 and all(o['value'] != option['value'] for o in options):
            options.append(option)
        if len(options) == 4:
            break
    if not options:
        options.append({'label': '指定字段和取值', 'value': f'由我说明“{term}”在当前报表使用的字段和取值'})
    if len(options) < 4:
        options.append({'label': '补充业务定义', 'value': f'由我补充“{term}”的业务定义或适用范围'})
    question = f'本次分析中的“{term}”具体指哪个口径？'
    if '?' in term or '？' in term:
        question = '本次分析中的这个原词具体指哪个口径？'
        reason = f'待确认原词为“{term}”。' + reason
    return ClarificationQuestion(topic='筛选口径', question=question, context=reason[:400], options=options)


def question_scope(question, groups):
    text = question.casefold()
    for quoted in (True, False):
        found = []
        for group in groups.values():
            term = group['term'].casefold()
            pattern = '“' + re.escape(term) + '”' if quoted else _term_pattern(term)
            if re.search(pattern, text):
                found.append(group)
        if found:
            return found[0] if len(found) == 1 else None
    return None


def annotate_card(card, groups):
    """Only choices constructed from a verified match can become a conversation binding."""
    focused = question_scope(card['question'], groups)
    if not focused and card['context'].startswith('待确认原词为“'):
        focused = question_scope(card['context'].split('”。', 1)[0] + '”', groups)
    if not focused:
        return card
    choices = {}
    for m in focused['matches']:
        if m['value'].casefold() != focused['term'].casefold():
            continue
        option = _match_option(focused['term'], m)
        for shown in card['options']:
            if shown['value'] == option['value']:
                choices[shown['id']] = {'table': m['table'], 'column': m['column'], 'field': m['field'], 'value': m['value']}
    if choices:
        card['scope_binding'] = {'literal': focused['term'], 'choices': choices}
    for group in groups.values():
        if group is focused or group['incomplete'] or len(group['matches']) != 1:
            continue
        m = group['matches'][0]
        note = f" 同次核对中，“{group['term']}”在 {field_ref(m['table'], m['column'])} 命中 {m['value']}。"
        if len(card['context'] + note) <= 400:
            card['context'] += note
    return card


def _field_pattern(table, column):
    quoted = field_ref(table, column)
    plain = table + '[' + column.replace(']', ']]') + ']'
    return '(?:' + re.escape(quoted.casefold()) + r'|(?<![\w\x27])' + re.escape(plain.casefold()) + ')'


def _filter_literals(code, table, column, *, keep_quotes=False):
    """Read simple equality, IN and TREATAS filters. Leave expression-valued filters to the planner."""
    ref = _field_pattern(table, column)
    atom = r'(?:"(?:[^"]|"")*"|-?\d+(?:\.\d+)?)'
    many = '(' + atom + '(?:,' + atom + ')*)'
    values = re.findall(ref + r'={1,2}(' + atom + r')(?=[),]|&&|\|\||$)', code)
    lists = re.findall(r'treatas\(\{' + many + r'\},' + ref + r'\)', code)
    lists += re.findall(ref + r'in\{' + many + r'\}', code)
    for sequence in lists:
        values.extend(re.findall(atom, sequence))
    if keep_quotes:
        return set(values)
    return {v[1:-1].replace('""', '"') if v.startswith('"') else v for v in values}


def repeats_confirmed_choice(plan, choice):
    """A redundant citation may be dropped only when every query already applies the chosen filter."""
    if not choice or len(plan.scope_resolutions) != 1:
        return False
    decision = plan.scope_resolutions[0]
    value = choice['value'].casefold()
    text_literal = '"' + value.replace('"', '""') + '"'
    return (decision.field == choice['field'] and decision.value.casefold() == value
            and all(_filter_literals(query_key(q.dax).casefold(), choice['table'], choice['column'], keep_quotes=True) == {text_literal}
                    for q in plan.queries))


def filter_conflict(records, groups, plan):
    code = '\n'.join(query_key(q.dax).casefold() for q in plan.queries)
    for r in records:
        term = r.get('scope_term', '')
        if not r['ok'] or not term or not groups[term.casefold()]['matches']:
            continue
        values = _filter_literals(code, r['table'], r['column'])
        matched = {v.casefold() for v in r['profile']['matched_terms']}
        absent = {v.casefold() for v in r['search_terms']} - matched
        lost_zero = term.startswith('0') and term.isascii() and term.isdecimal() and str(int(term)) in values
        if values & absent or (lost_zero and term not in values):
            return term, f"{r['label']} 的真实查值与计划中 {field_ref(r['table'], r['column'])} 的筛选不一致，请核对“{term}”的字段和原始值。"
    return None


def _term_pattern(term):
    literal = re.escape(term.casefold())
    if term.isascii():
        return r'(?<![a-z0-9_])' + literal + r'(?![a-z0-9_])'
    return literal


def _named_by_user(user_text, term, names, quote=''):
    source = user_text.casefold()
    literal = _term_pattern(term)
    gap = r'[\s为是等于编号编码的:=：“”「」‘’"\'\-]{0,8}'
    for name in names:
        name = name.casefold()
        if not name or name == term.casefold():
            continue
        role = re.escape(name)
        if (re.search(role + gap + literal + '|' + literal + gap + role, source)
                or (quote and quote in source and name in quote and re.search(literal,quote))):
            return True
    return False


def _column_sources(table, column, matches, entry):
    names = [field_ref(table, column)]
    if not any(m['column'].casefold() == column.casefold() and m['table'] != table for m in matches):
        names.append(column)
        if entry.get('state') == 'confirmed':
            names.extend([entry.get('business_name', ''), *entry.get('aliases', [])])
    definitions = [entry.get('facts', {}).get('description', '')]
    if entry.get('state') == 'confirmed':
        definitions.append(entry.get('meaning', ''))
    return names, definitions


def immediate_scope_question(groups, ctx, user_messages, bindings, dictionary):
    """Offer an exact field collision immediately when no source can distinguish its hits."""
    entries = {(e['table'], e['name']): e for e in dictionary.get('entries', []) if e['kind'] == 'column'}
    for group in groups:
        term, matches = group['term'], group['matches']
        key = term.casefold()
        fields = {(m['table'], m['column']) for m in matches}
        if (len(fields) < 2 or group['incomplete']
                or any(m['value'].casefold() != key or m.get('match_kind') for m in matches)):
            continue
        # Only ask about a term the user actually supplied. An attached image can
        # contain the field choice, which is available to the planner but not here.
        literal = re.compile(_term_pattern(term))
        turns = [m for m in user_messages if literal.search(m.content.casefold())]
        if not turns or any(m.images for m in turns) or user_messages[-1].images:
            continue
        if any(b['literal'].casefold() == key for b in bindings):
            continue
        mappings = {(t, c, v.casefold()) for t, c, v in confirmed_filters(dictionary, term)}
        if len(mappings) == 1:
            continue
        # A measure can establish the field even when the question uses its business
        # name. Leave the choice and its validation to the planner if such a filter exists.
        dax_literal = key.replace('"', '""')
        expressions = '\n'.join(query_key(m.expression).casefold() for m in ctx.measures
                                if dax_literal in (m.expression or '').casefold())
        for table, column in fields | {(m['table'], m['column']) for m in group['misses']}:
            entry = entries.get((table, column), {})
            names, definitions = _column_sources(table, column, matches, entry)
            # Possible references merit review. Only select_scope can accept one.
            names.append(table + '[' + column.replace(']', ']]') + ']')
            if entry.get('state') == 'candidate':
                names.extend([entry.get('business_name', ''), *entry.get('aliases', [])])
            names = {n.casefold() for n in names if n and n.casefold() != key}
            if (any(n in m.content.casefold() for m in turns for n in names)
                    or any(text and literal.search(text.casefold()) for text in definitions)
                    or key in _filter_literals(expressions, table, column)):
                break
        else:
            return scope_question(group, '多个字段都含有这个原值。请选择本次用于筛选的字段。')
    return None


def select_scope(group, plan, ctx, user_text, bindings, dictionary):
    """Check a proposed interpretation against the actual hit and independent evidence."""
    term = group['term']
    matches = group['matches']
    choices = {(m['table'], m['column'], m['value']) for m in matches}
    declarations = [d for d in plan.scope_resolutions if any(d.field == m['field'] for m in matches)]
    dax = '\n'.join(query_key(q.dax) for q in plan.queries).casefold()
    measures = [m for m in ctx.measures if ('[' + m.name.replace(']', ']]') + ']').casefold() in dax]
    expressions = '\n'.join(query_key(m.expression).casefold() for m in measures)
    if not matches:
        used_measures = {(m.table,m.name) for m in measures}
        named = any(m.name.casefold() == term.casefold() for m in measures)
        reviewed = any(e['kind']=='measure' and (e['table'],e['name']) in used_measures and e['state']=='confirmed'
                       and term.casefold() in {s.casefold() for s in [e.get('business_name',''), *e.get('aliases',[])]}
                       for e in dictionary.get('entries',[]))
        if named or reviewed:
            return None, None
        for miss in group['misses']:
            table, column = miss['table'], miss['column']
            if (_named_by_user(user_text,term,[field_ref(table,column),column])
                    and term.casefold() in _filter_literals(dax,table,column)):
                # An explicitly requested field can legitimately give an empty business result.
                return None, None
        return None, scope_question(group, '已检查的候选字段尚无可用匹配，请补充这个原词的业务定义。')
    if len(declarations) > 1:
        return None, scope_question(group, '同一原词有多种采用说明，需要确定本次使用的定义。')
    chosen = None
    if declarations:
        decision = declarations[0]
        chosen = next((m for m in matches if m['field'] == decision.field
                       and m['value'].casefold() == decision.value.casefold()), None)
        if not chosen:
            return None, scope_question(group, '规划使用的取值与实际读取的取值不一致。')
    elif len(choices) == 1:
        chosen = matches[0]
    else:
        applied = [m for m in matches if m['value'].casefold() in _filter_literals(
            dax + '\n' + expressions, m['table'], m['column'])]
        if len({(m['table'],m['column'],m['value']) for m in applied}) == 1:
            chosen = applied[0]
    if chosen:
        table, column, value = chosen['table'], chosen['column'], chosen['value']
        used = value.casefold() in _filter_literals(dax+'\n'+expressions, table, column)
        if len(choices) == 1 and not group['incomplete'] and not chosen.get('match_kind'):
            return (chosen if used else None), None
        bound = value.casefold()==term.casefold() and any(b['literal'].casefold() == term.casefold()
            and (b['table_name'], b['column_name']) == (table, column) for b in bindings)
        entries = dictionary.get('entries', [])
        entry = next((e for e in entries if (e['kind'], e['table'], e['name']) == ('column', table, column)), {})
        names, definitions = _column_sources(table, column, matches, entry)
        basis = declarations[0].basis.casefold() if declarations else ''
        explicit = _named_by_user(user_text,term,names,basis)
        documented = any(text and basis and text.casefold() in basis and term.casefold() in text.casefold()
                         and value.casefold() in text.casefold() for text in definitions)
        mapped = {(t,c,v.casefold()) for t,c,v in confirmed_filters(dictionary,term)} == {(table,column,value.casefold())}
        defined = value.casefold() in _filter_literals(expressions, table, column)
        source_code=dax+'\n'+expressions
        declared_reference=bool(declarations and re.search(_field_pattern(table,column),source_code)
                                and ('"'+value.replace('"','""')+'"').casefold() in source_code)
        if (used or declared_reference) and (bound or explicit or mapped or defined or documented):
            return chosen, None
    return None, scope_question(group, '实际取值仍有多个可能的归属，或部分候选未能完成检查。请选择本次采用的范围。')
