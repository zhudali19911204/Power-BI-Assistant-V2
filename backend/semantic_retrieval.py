"""Question-focused semantic context, using local lexical evidence and model dependencies."""
import math
import re
import unicodedata
from collections import Counter, defaultdict, deque

from .database import encode
from .semantic_diff import dax_dependencies


ENTRY_BUDGET = 50000
MAX_MATCHES = 32
MAX_RELATIONSHIPS = 64
_STOP = set('the a an of for by in to and with please show explain analyze analysis '
            '请问 请帮 帮我 分析 解释 看看 查看 统计 这个 那个 如何 什么 一下 字段 指标 数据 模型'.split())
_QUALIFIED = re.compile(r"(?:'((?:[^']|'')+)'|([^\W\d]\w*))\s*\[((?:[^\]]|\]\])+)\]")


def _normalize(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(text or '')).casefold()).strip()


def _tokens(text):
    # CJK bigrams match phrases inside unsegmented questions; Latin boundaries keep
    # ID distinct from paid. Split snake/camel-case model identifiers as well.
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', str(text or ''))
    result = set()
    for word in re.findall(r'[a-z0-9]+|[\u3400-\u9fff]+', _normalize(text)):
        if re.fullmatch(r'[\u3400-\u9fff]+', word):
            result.update(word[i:i+2] for i in range(len(word)-1))
        elif len(word) > 1:
            result.add(word)
    return result - _STOP


def _contains(text, phrase):
    phrase = _normalize(phrase)
    if not phrase:
        return False
    # A single character only matches an explicit bracketed object reference.
    if len(phrase) == 1:
        return False
    latin = 'abcdefghijklmnopqrstuvwxyz0123456789_'
    start = text.find(phrase)
    while start >= 0:
        end = start + len(phrase)
        if (phrase[0] not in latin or start == 0 or text[start-1] not in latin) and (
                phrase[-1] not in latin or end == len(text) or text[end] not in latin):
            return True
        start = text.find(phrase, start + 1)
    return False


def _identity(entry):
    return entry['kind'], entry['table'], entry['name']


def _public(entry):
    result = {k: entry.get(k, '') for k in
              ('kind', 'table', 'name', 'business_name', 'meaning', 'role', 'unit', 'usage_notes', 'aliases', 'state', 'source')}
    result.update({key: entry.get('facts', {}).get(key, '') for key in
                   ('description', 'format_string', 'actual_dtype', 'source_column')})
    if entry['state'] == 'unresolved' and entry.get('change'):
        result['semantic_change'] = entry['change']
    return result


def _reference(entry):
    return {key: entry[key] for key in ('kind', 'table', 'name')}


def retrieve(entries, context, question='', *, history=(), bindings=(), max_chars=ENTRY_BUDGET):
    """Select definitions without upgrading candidate claims or importing old definitions.

    The character budget covers serialized entries (as in the previous prompt).
    Bounded diagnostics disclose relevant omissions. The full structural schema
    remains available separately; retrieval paths do not assert filter semantics.
    """
    objects = {e['id']: e for e in entries}
    identities = {_identity(e): e['id'] for e in entries}
    by_table = defaultdict(list)
    documents = {}
    frequency = Counter()
    for e in entries:
        by_table[e['table']].append(e['id'])
        labels = [e['name'], e.get('business_name', ''), *e.get('aliases', []), *e.get('search_aliases', [])]
        names = set().union(*(_tokens(label) for label in labels))
        description = _tokens(e.get('meaning', '') + ' ' + e.get('usage_notes', '') + ' ' + e.get('facts', {}).get('description', ''))
        documents[e['id']] = (labels, names, description)
        frequency.update(names | description)
    idf = {word: math.log(1 + (len(entries) + 1) / (count + 1)) for word, count in frequency.items()}

    def rank(text):
        normalized = _normalize(text)
        # Qualified references resolve duplicate names before unqualified matching.
        qualified_matches = list(_QUALIFIED.finditer(normalized))
        references = {((m[1] or m[2]).replace("''", "'"), m[3].replace(']]', ']'))
                      for m in qualified_matches}
        qualified = {e['id'] for e in entries if e['kind'] in ('column', 'measure') and
                     (_normalize(e['table']), _normalize(e['name'])) in references}
        qualified_names = {objects[key]['name'].casefold() for key in qualified}
        resolved = {(_normalize(objects[key]['table']), _normalize(objects[key]['name'])) for key in qualified}
        unqualified = normalized
        for match in reversed(qualified_matches):
            if ((match[1] or match[2]).replace("''", "'"), match[3].replace(']]', ']')) in resolved:
                unqualified = unqualified[:match.start()] + ' ' + unqualified[match.end():]
        words = _tokens(unqualified)
        scores = {}
        strong = {}
        for e in entries:
            key = e['id']
            labels, names, description = documents[key]
            exact = any(_contains(unqualified, label) for label in labels)
            bracket = e['kind'] == 'measure' and _normalize('[' + e['name'].replace(']', ']]') + ']') in unqualified
            if e['name'].casefold() in qualified_names and key not in qualified:
                continue
            score = 4 * sum(idf[t] for t in words & names) + 2 * sum(idf[t] for t in words & description)
            if exact or bracket:
                score += 30
                strong[key] = 1
            if key in qualified:
                score += 100
                strong[key] = 0
            if score >= 3:
                scores[key] = score
        for binding in bindings:
            literal = str(binding.get('literal', ''))
            if literal and re.search(r'(?<![a-zA-Z0-9_])' + re.escape(literal) + r'(?![a-zA-Z0-9_])', str(text)):
                key = identities.get(('column', binding['table_name'], binding['column_name']))
                if key:
                    scores[key] = scores.get(key, 0) + 100
                    strong[key] = 0
        return scores, strong

    current, strong = rank(question)
    scores = {key: (strong.get(key, 3), 0, score) for key, score in current.items()}
    # An explicit new metric/table starts a new subject. Dimension-only follow-ups
    # can retain prior user metrics; assistant guesses are never retrieval inputs.
    measure_tables = {e['table'] for e in entries if e['kind'] == 'measure'}

    def has_subject(matches):
        return any(objects[key]['kind'] == 'measure' or
                   (objects[key]['kind'] == 'table' and objects[key]['table'] in measure_tables)
                   for key in matches)

    new_subject = has_subject(strong)
    user_history = [m.get('content', '') for m in history if m.get('role') == 'user'][-4:]
    if not new_subject:
        for age, text in enumerate(reversed(user_history), 1):
            historical, historical_strong = rank(text[-6000:])
            for key, score in historical.items():
                proposed = (2 if key in historical_strong else 4, age, score / (age + 1))
                if key not in scores or proposed[:2] < scores[key][:2]:
                    scores[key] = proposed
            if has_subject(historical_strong):
                break

    def order(key):
        tier, origin, score = scores.get(key, (99, 99, 0))
        return tier, origin, -score, objects[key]['state'] != 'confirmed', _identity(objects[key])

    ranked = sorted(scores, key=order)
    mode = 'question' if ranked else ('overview' if not question.strip() else 'fallback')
    if ranked:
        roots = ranked[:MAX_MATCHES]
    else:
        # Round-robin model coverage avoids filling the fallback with one large table.
        groups = []
        for table in sorted(by_table):
            groups.append(deque(sorted(by_table[table], key=lambda key: (
                {'table': 0, 'measure': 1, 'column': 2}[objects[key]['kind']],
                objects[key]['state'] != 'confirmed', _identity(objects[key])))))
        roots = []
        while any(groups):
            for group in groups:
                if group:
                    roots.append(group.popleft())

    priority = {}
    reasons = {}

    def add(key, level, reason):
        if key not in objects:
            return
        if key not in priority or level < priority[key]:
            priority[key] = level
            reasons[key] = reason

    for index, key in enumerate(roots):
        add(key, 0 if ranked else index, '问题匹配' if ranked else '模型概览')

    relationships = context.get('relationships', [])
    selected_relations = set()
    if ranked:
        queue = deque(roots)
        visited = set()
        while queue:
            key = queue.popleft()
            if key in visited:
                continue
            visited.add(key)
            entry = objects[key]
            add(identities.get(('table', entry['table'], entry['table'])), 1, '所属表')
            # Directly requested tables need their fields/metrics, but a dependency
            # on a whole table must not recursively pull in every unrelated measure.
            if entry['kind'] == 'table' and key in roots:
                for child in by_table[entry['table']]:
                    add(child, 2, '命中表内对象')
                    queue.append(child)
            expression = entry.get('facts', {}).get('expression', '')
            if entry['kind'] == 'table':
                expression = context.get('calculated_tables', {}).get(entry['name'], '')
            if expression:
                source = {**entry, 'facts': {**entry['facts'], 'expression': expression}}
                for dependency in sorted(dax_dependencies(source, entries)):
                    add(dependency, 1, '公式依赖')
                    queue.append(dependency)

        terminals = {objects[key]['table'] for key in priority}
        graph = defaultdict(set)
        for relation in relationships:
            a, b = relation['from_table'], relation['to_table']
            graph[a].add(b)
            graph[b].add(a)

        def distances(source):
            result = {source: 0}
            pending = deque([source])
            while pending:
                node = pending.popleft()
                for neighbor in sorted(graph[node]):
                    if neighbor not in result:
                        result[neighbor] = result[node] + 1
                        pending.append(neighbor)
            return result

        # Include all equally short paths and parallel active/inactive edges, so
        # retrieval never silently chooses a date role or a business join path.
        path_edges = set()
        for source in sorted(terminals):
            distance = distances(source)
            pending = deque(sorted(terminals & distance.keys()))
            walked = set()
            while pending:
                node = pending.popleft()
                if node in walked:
                    continue
                walked.add(node)
                for neighbor in sorted(graph[node]):
                    if distance.get(neighbor) == distance[node] - 1:
                        path_edges.add(frozenset((node, neighbor)))
                        pending.append(neighbor)
        for index, relation in enumerate(relationships):
            if frozenset((relation['from_table'], relation['to_table'])) in path_edges:
                selected_relations.add(index)

        dates = set(context.get('date_tables', []))
        for index, relation in enumerate(relationships):
            ends = {relation['from_table'], relation['to_table']}
            if ends & terminals and ends & dates:
                selected_relations.add(index)
        related_tables = set(terminals)
        for index in sorted(selected_relations):
            relation = relationships[index]
            for side in ('from', 'to'):
                table, column = relation[side + '_table'], relation[side + '_column']
                related_tables.add(table)
                add(identities.get(('table', table, table)), 2, '关系路径')
                add(identities.get(('column', table, column)), 2, '关系连接字段')
        for table in sorted(related_tables):
            for key in by_table[table]:
                entry = objects[key]
                facts = entry.get('facts', {})
                if entry['kind'] == 'column' and (table in dates or
                        str(facts.get('actual_dtype') or facts.get('dtype', '')).lower() in ('date', 'datetime')):
                    add(key, 3, '日期上下文')

    # Keep looking after an oversized entry; later small relevant entries may fit.
    ordered = sorted(priority, key=lambda key: (priority[key], order(key)))
    selected, missing, used, included = [], [], 0, set()
    for key in ordered:
        entry = _public(objects[key])
        size = len(encode(entry))
        if used + size > max_chars:
            missing.append(key)
            continue
        selected.append(entry)
        included.add(key)
        used += size
    dropped_matches = [key for key in ranked[MAX_MATCHES:] if key not in priority]
    missing += dropped_matches
    kept_relations = []
    for index in sorted(selected_relations):
        relation = relationships[index]
        if len(kept_relations) < MAX_RELATIONSHIPS and all(
                identities.get(('column', relation[side + '_table'], relation[side + '_column'])) in included
                for side in ('from', 'to')):
            kept_relations.append(relation)
    return {'entries': selected, 'omitted': len(entries) - len(selected),
            'retrieval': {'mode': mode, 'matched': len(ranked), 'budget_chars': max_chars, 'used_chars': used,
                          'omitted_relevant': len(missing) if ranked else 0,
                          'omitted_overview': len(missing) if not ranked else 0,
                          'missing': [{**_reference(objects[key]), 'reason': reasons.get(key, '匹配数量上限')}
                                      for key in missing[:20]],
                          'relationships': kept_relations,
                          'omitted_relationships': len(selected_relations) - len(kept_relations)}}
