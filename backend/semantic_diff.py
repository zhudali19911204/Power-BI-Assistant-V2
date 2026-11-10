"""Compare semantic objects and their explicit M/DAX dependencies, without calling an LLM."""
import copy
import re

from .database import encode
from .engine.dax.artifact import _code_only
from .engine.mquery.artifact import _strip_strings_comments


DEFINITION_KEYS = ('business_name', 'meaning', 'role', 'unit', 'aliases', 'state', 'source', 'reviewed_at')
FACT_LABELS = {'columns': '表内字段', 'date_table': '日期表标记', 'dtype': '数据类型',
    'actual_dtype': '实际数据类型', 'relationships': '关系', 'expression': '公式',
    'description': '模型描述', 'format_string': '显示格式', 'source_column': '源字段',
    'data_category': '数据类别'}
_DAX_REF = re.compile(r"(?:'((?:[^']|'')+)'|([^\W\d]\w*))\s*\[((?:[^\]]|\]\])+)\]")


def normalized(value):
    if isinstance(value, dict):return {k: normalized(v) for k, v in sorted(value.items())}
    if isinstance(value, list):return sorted((normalized(v) for v in value), key=encode)
    return value


def source_snapshot(context):
    return {key: copy.deepcopy(context.get(key, {})) for key in
            ('table_queries', 'shared_expressions', 'calculated_tables')}


def changed_sources(before, after):
    """Follow named M queries/parameters; an unused shared expression affects no table."""
    def queries(context):return {**context.get('shared_expressions', {}), **context.get('table_queries', {})}
    old, new = queries(before), queries(after)
    affected = {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}
    dependencies = {}
    dynamic = set()
    # Include removed dependencies as well, so deleting a referenced query invalidates its users.
    for context in (old, new):
        names = set(context)
        for name, expression in context.items():
            code = _strip_strings_comments(expression)
            if re.search(r'\bExpression\.Evaluate\b|#shared\b',code):dynamic.add(name)
            quoted = re.findall(r'#"((?:[^"]|"")*)"', code)
            bare = re.findall(r'(?<![\w.])[\w]+', re.sub(r'#"(?:[^"]|"")*"', ' ', code))
            dependencies.setdefault(name, set()).update(({s.replace('""', '"') for s in quoted} | set(bare)) & names - {name})
    # The target of a dynamic M lookup cannot be proven statically. Recheck its
    # owning query when named sources change, rather than silently trusting it.
    if affected:affected.update(dynamic)
    while True:
        more = {name for name, refs in dependencies.items() if refs & affected} - affected
        if not more:break
        affected.update(more)
    calculated_before, calculated_after = before.get('calculated_tables', {}), after.get('calculated_tables', {})
    affected.update(name for name in calculated_before.keys() | calculated_after.keys()
                    if calculated_before.get(name) != calculated_after.get(name))
    return affected


def dax_dependencies(entry, objects):
    code = _code_only(entry['facts'].get('expression', ''))
    refs = set()
    qualified = list(_DAX_REF.finditer(code))
    for match in qualified:
        table = (match[1] or match[2]).replace("''", "'").casefold()
        name = match[3].replace(']]', ']').casefold()
        refs.update(e['id'] for e in objects if e['kind'] in ('column', 'measure')
                    and e['table'].casefold() == table and e['name'].casefold() == name)
    # Mask qualified refs before finding bare measures and whole-table arguments.
    for match in reversed(qualified):code = code[:match.start()] + ' ' * len(match[0]) + code[match.end():]
    for match in re.finditer(r'\[((?:[^\]]|\]\])+)\]', code):
        name = match[1].replace(']]', ']').casefold()
        refs.update(e['id'] for e in objects if e['kind'] == 'measure' and e['name'].casefold() == name)
    code = re.sub(r'\[(?:[^\]]|\]\])+\]', ' ', code)
    quoted = re.findall(r"'((?:[^']|'')*)'",code)
    bare = re.findall(r'[^\W\d]\w*',re.sub(r"'(?:[^']|'')*'",' ',code))
    tables = {name.replace("''", "'").casefold() for name in quoted} | {name.casefold() for name in bare}
    for table in (e for e in objects if e['kind'] == 'table'):
        if table['name'].casefold() in tables:
            refs.add(table['id'])
    return refs


def reconcile(entries, previous, before_context, after_context):
    """Keep unchanged definitions exactly; changed definitions become non-authoritative history."""
    old = {e['id']: e for e in previous}
    fresh = {e['id']: e for e in entries}
    reasons = {}
    affected_sources = changed_sources(before_context, after_context)
    for entry in entries:
        prior = old.get(entry['id'])
        if prior is None:
            reasons[entry['id']] = ['新增对象']
            continue
        differences = [FACT_LABELS.get(key, key) + '变化' for key, value in entry['facts'].items()
                       if key in prior['facts'] and normalized(prior['facts'][key]) != normalized(value)]
        if entry['kind']!='measure' and entry['table'] in affected_sources:
            differences.append('Power Query 或计算表来源变化')
        if differences:reasons[entry['id']] = differences
    removed = set(old) - set(fresh)
    objects = list(old.values()) + list(fresh.values())
    dependencies = {}
    for entry in objects:
        if entry['kind'] == 'measure' or (entry['kind'] == 'column' and entry['facts'].get('expression')):
            dependencies.setdefault(entry['id'], set()).update(dax_dependencies(entry, objects))
    # A changed relationship can alter measures throughout its connected component.
    relation_tables = set()
    all_relations = []
    for entry in objects:
        all_relations.extend(entry['facts'].get('relationships', []))
    for entry in entries:
        if '关系变化' in reasons.get(entry['id'], []):
            relation_tables.add(entry['table'])
    while True:
        expanded = relation_tables | {table for r in all_relations
            if {r['from_table'], r['to_table']} & relation_tables for table in (r['from_table'], r['to_table'])}
        if expanded == relation_tables:break
        relation_tables = expanded
    for entry in entries:
        if entry['kind'] != 'measure':continue
        refs = dependencies.get(entry['id'], set())
        if any(e['id'] in refs and e['table'] in relation_tables for e in objects):
            reasons.setdefault(entry['id'], []).append('相关筛选关系变化')
    while True:
        affected = set(reasons) | removed
        more = {key for key, refs in dependencies.items() if key in fresh and key not in affected and refs & affected}
        if not more:break
        reasons.update({key: ['依赖的字段或度量变化'] for key in more})
    result = []
    for entry in entries:
        prior = old.get(entry['id'])
        if prior and entry['id'] not in reasons:
            saved = copy.deepcopy(prior)
            saved['facts'] = entry['facts']
            result.append(saved)
        else:
            entry = copy.deepcopy(entry)
            entry.update(business_name='', meaning='', role='', unit='', aliases=[], state='unresolved',
                         source='模型已变化 · 待更新' if prior else '尚未解释', reviewed_at=None)
            entry['change'] = {'kind': 'modified' if prior else 'added', 'reasons': reasons[entry['id']]}
            if prior:
                if prior['state'] in ('candidate','confirmed') or prior.get('previous_definition'):
                    entry['previous_definition'] = copy.deepcopy(prior.get('previous_definition')
                        if prior['state']=='unresolved' and prior.get('previous_definition') else
                        {k: prior[k] for k in DEFINITION_KEYS})
                if prior['state']=='unresolved' and prior.get('change'):
                    entry['change']={'kind':prior['change']['kind'],
                        'reasons':list(dict.fromkeys(prior['change']['reasons']+entry['change']['reasons']))}
            result.append(entry)
    items = [{'id': e['id'], 'kind': e['kind'], 'table': e['table'], 'name': e['name'],
              'change': 'modified' if e['id'] in old else 'added', 'reasons': reasons[e['id']]}
             for e in entries if e['id'] in reasons]
    items += [{'id': old[key]['id'], 'kind': old[key]['kind'], 'table': old[key]['table'],
               'name': old[key]['name'], 'change': 'removed', 'reasons': ['对象已删除']} for key in sorted(removed)]
    changes = {'preserved': sum(e['id'] not in reasons and e['state'] in ('candidate', 'confirmed') for e in result),
               'added': sum(e['id'] not in old for e in entries),
               'modified': sum(key in old for key in reasons), 'removed': len(removed), 'items': items}
    return result, changes, [copy.deepcopy(old[key]) for key in sorted(removed)]
