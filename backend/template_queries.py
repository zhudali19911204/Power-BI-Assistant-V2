"""Bind template values to fixed, read-only DAX commands sent through XMLA."""
import re
from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .analysis import readonly_query
from .analysis_budget import MAX_QUERIES
from .fields import field_ref
from .language import wording
from .template_parameters import PARAMETER, parameter_names


class SavedQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=160)
    dax: str = Field(min_length=1, max_length=16000)


# Markers are expressions. DAX strings, identifiers and comments remain opaque.
_SLOT = r'\{\{\s*(?P<name>[\w][\w -]{0,39}?)(?:\.(?P<part>year|month|day))?\s*\}\}'
_TOKENS = re.compile(
    r'(?P<comment>//[^\n]*|--[^\n]*|/\*[\s\S]*?\*/)|'
    r'(?P<date>\bdt"[^"]*")|(?P<string>"(?:[^"]|"")*")|'
    r"(?P<identifier>'(?:[^']|'')*'|\[(?:[^\]]|\]\])*\])|"
    + _SLOT + r'|(?P<xml>@[\w]+)|(?P<number>(?:\d*\.\d+|\d+)(?:[eE][+-]?\d+)?)|'
    r'(?P<word>[\w]+)|(?P<space>\s+)|(?P<other>.)', re.I)
_PERIOD = re.compile(r'((?:19|20)\d{2})\s*(?:[-/.]|年)\s*(\d{1,2})(?:(?:[-/.]|月)\s*(\d{1,2})日?)?月?')
_NUMERIC = re.compile(r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?')


def fail(zh, en):
    raise HTTPException(422, wording(zh, en))


def period(value):
    match = _PERIOD.fullmatch(value.strip())
    if match:
        year, month, day = match.groups()
        try:
            return date(int(year), int(month), int(day or '1')), day is not None
        except ValueError:
            pass
    return None


def _tokens(dax):
    return [t for t in _TOKENS.finditer(dax) if t.lastgroup not in ('space', 'comment')]


def query_parameter_names(queries):
    return list(dict.fromkeys(token['name'].strip() for query in queries if '{{' in query['dax']
                              for token in _TOKENS.finditer(query['dax']) if token['name']))


def query_columns(queries):
    """Qualified references in the commands disambiguate aliases shared by several model tables."""
    fields = set()
    for query in queries:
        tokens = _tokens(query['dax'])
        for table, column in zip(tokens, tokens[1:]):
            if not column[0].startswith('['):
                continue
            if table[0].startswith("'"):
                name = table[0][1:-1].replace("''", "'")
            elif table.lastgroup == 'word' and table.end() == column.start():
                name = table[0]
            else:
                continue
            fields.add((name.casefold(), column[0][1:-1].replace(']]', ']').casefold()))
    return fields


def _replace(text, replacements):
    for start, end, value in reversed(replacements):
        text = text[:start] + value + text[end:]
    return text


def _is_alias(function, index):
    # These arguments create output columns. Only value arguments can change.
    if function == 'ROW':
        return index % 2 == 0
    if function in ('ADDCOLUMNS', 'SELECTCOLUMNS'):
        return index % 2 == 1
    return function in ('SUMMARIZE', 'SUMMARIZECOLUMNS', 'GROUPBY')


def _marker(name, part=''):
    return '{{' + name + ('.' + part if part else '') + '}}'


def _calendar_date(name, day='1'):
    return f'DATE(VALUE({_marker(name, "year")}),VALUE({_marker(name, "month")}),{day})'


def _date_expression(name, sample, year, month, day, exclusive):
    when, full = sample
    if (year, month) == (when.year, when.month):
        if full:
            return _calendar_date(name, 'VALUE(' + _marker(name, 'day') + ')') if day == when.day else None
        if day == monthrange(year, month)[1]:
            return 'EOMONTH(' + _calendar_date(name) + ',0)'
        return _calendar_date(name, str(day))
    if not full and exclusive and day == 1 and year * 12 + month == when.year * 12 + when.month + 1:
        return 'EDATE(' + _calendar_date(name) + ',1)'
    return None


def _date_value(name, sample, tokens, i):
    # DATE(year,month,day) has eight significant tokens. A whole period can
    # cover a calendar month end or the exclusive start of the following month.
    args = [t[0] for t in tokens[i:i+8]]
    if not sample or len(args) != 8 or args[0].upper() != 'DATE' or args[1::2] != ['(', ',', ',', ')']:
        return None
    parts = args[2:7:2]
    if not all(v.isdecimal() and len(v) <= width for v, width in zip(parts, (4, 2, 2))):
        return None
    year, month, day = map(int, parts)
    return _date_expression(name, sample, year, month, day, i > 0 and tokens[i-1][0] == '<')


def binding_field(table, column):
    if table.isidentifier():
        return table + '[' + column.replace(']', ']]') + ']'
    return field_ref(table, column)


def _field_before(tokens, index):
    if index < 0 or not tokens[index][0].startswith('['):
        return ''
    table = tokens[index-1][0] if index and tokens[index-1].lastgroup in ('identifier', 'word') else ''
    column = tokens[index][0]
    if not table:
        return column
    if table.startswith("'"):
        table = table[1:-1].replace("''", "'")
    return binding_field(table, column[1:-1].replace(']]', ']'))


def _role(tokens, i, stack):
    """Keep a repeated value in unrelated columns from becoming one filter."""
    left = i - 1
    while left >= 0 and tokens[left][0] in ('=', '<', '>'):
        left -= 1
    if left < i - 1:
        field = _field_before(tokens, left)
        if field:
            return field
        if left >= 1 and tokens[left-1][0].upper() == 'VAR':
            return 'var ' + tokens[left][0].casefold()
        # YEAR('Calendar'[Date]) = 2026, or MONTH([Date]) = 5.
        if left >= 3 and tokens[left][0] == ')' and tokens[left-1][0].startswith('['):
            start = left - 2 if tokens[left-2][0] == '(' else left - 3
            if start > 0 and tokens[start][0] == '(' and tokens[start-1][0].upper() in ('YEAR', 'MONTH', 'DAY'):
                return tokens[start-1][0].lower()
        return ''
    if stack and stack[-1][0] == '{':
        start = stack[-1][2]
        if start >= 2 and tokens[start-1][0].upper() == 'IN':
            return _field_before(tokens, start-2)
        for end in range(i+1, len(tokens)-2):
            if tokens[end][0] == '}':
                for index in (end+2, end+3):
                    if index < len(tokens) and tokens[index][0].startswith('['):
                        return _field_before(tokens, index)
                break
        return ''
    if stack:
        function, arg, _ = stack[-1]
        if function == 'ROW' and i >= 2:
            return 'row ' + tokens[i-2][0].casefold()
        if function == 'TOPN' and arg == 0:
            return 'ranking limit'
        if function == 'DATE':
            return 'date component ' + str(arg)
    return None


def _calendar_scalar(role, literal, sample, name, quoted):
    if not sample or role is None:
        return None
    when, full = sample
    field = role.removeprefix('var ').rsplit('[', 1)[-1].rstrip(']').casefold().replace(' ', '').replace('_', '')
    for part, aliases in (('year', ('year', 'fiscalyear', 'calendaryear', '年', '年份', '年度')),
                          ('month', ('month', 'monthnumber', 'monthno', '月份', '月', '月序号')),
                          ('day', ('day', 'dayofmonth', '日'))):
        if (field in aliases and (part != 'day' or full) and len(literal) <= 4
                and literal.isdecimal() and int(literal) == getattr(when, part)):
            value = f'VALUE({_marker(name, part)})'
            mask = '0' * len(literal)
            return f'FORMAT({value},"{mask}")' if quoted else value
    if literal == when.strftime('%Y%m') and not full:
        value = f'(VALUE({_marker(name, "year")})*100+VALUE({_marker(name, "month")}))'
        return f'FORMAT({value},"000000")' if quoted else value
    if quoted and period(literal) == sample:
        # A text month key can use a different spelling from the task. Recreate
        # the column's separators and padding from the bound date components.
        components = iter(('year', 'month', 'day'))
        expressions = []
        for chunk in re.findall(r'\d+|\D+', literal):
            if chunk.isdecimal():
                part = next(components)
                width = len(chunk) if '年' not in literal or part == 'year' or chunk.startswith('0') else 1
                mask = '0' * width
                expressions.append(f'FORMAT(VALUE({_marker(name, part)}),"{mask}")')
            else:
                expressions.append('"' + chunk + '"')
        return '(' + ' & '.join(expressions) + ')'
    return None


def _literal_binding(name, sample, calendar, token, role, exclusive):
    if not sample:
        return None
    raw = token[0]
    if token.lastgroup == 'date':
        at = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})(?:T00:00:00)?', raw[3:-1], re.I)
        if at and calendar:
            year, month, day = map(int, at.groups())
            expression = _date_expression(name, calendar, year, month, day, exclusive)
            if expression:
                return 'calendar', expression
        return None
    quoted = token.lastgroup == 'string'
    literal = raw[1:-1].replace('""', '"') if quoted else raw
    converted = _calendar_scalar(role, literal, calendar, name, quoted)
    if converted:
        return 'calendar', converted
    if literal == sample:
        marker = _marker(name)
        return role, marker if quoted else f'VALUE({marker})'
    return None


def _suggest(dax, samples):
    tokens, stack = _tokens(dax), []
    calendar = {name: period(sample) for name, sample in samples.items()}
    skip = -1
    previous = ''
    for i, token in enumerate(tokens):
        if i <= skip:
            continue
        raw = token[0]
        if raw.upper() == 'DATE':
            matches = [(name, value) for name, sample in calendar.items()
                       if (value := _date_value(name, sample, tokens, i))]
            if matches:
                for name, value in matches:
                    yield name, 'calendar', token.start(), tokens[i+7].end(), value
                skip = i + 7
                previous = ')'
                continue
        if raw == '(':
            stack.append([previous.upper(), 0, i])
        elif raw == '{':
            stack.append(['{', 0, i])
        elif raw in (')', '}'):
            if stack:
                stack.pop()
        elif raw == ',' and stack:
            stack[-1][1] += 1
        elif token.lastgroup in ('string', 'number', 'date'):
            function, arg = stack[-1][:2] if stack else ('', 0)
            role = _role(tokens, i, stack)
            if role is not None and not (token.lastgroup == 'string' and _is_alias(function, arg)):
                exclusive = i > 0 and tokens[i-1][0] == '<'
                for name, sample in samples.items():
                    binding = _literal_binding(name, sample, calendar[name], token, role, exclusive)
                    if binding:
                        context, expression = binding
                        yield name, context, *token.span(), expression
        previous = raw


def query_suggestions(queries, candidates):
    locations, options, shared = [], defaultdict(dict), set()
    for index, query in enumerate(queries, 1):
        locations_in_query = defaultdict(list)
        suggestions = _suggest(query['dax'], candidates) if candidates else ()
        for name, role, start, end, expression in suggestions:
            key = role.casefold() if role else f'Q{index}@{start}'
            context = ' '.join(query['dax'][max(0, start-26):end+12].split())
            label = wording('日期筛选', 'Date filter') if role == 'calendar' else role or f'Q{index} · {context}'
            options[name][key] = label
            locations_in_query[start, end].append((name, key, expression))
        for matches in locations_in_query.values():
            if len(matches) > 1:
                shared.update(name for name, _, _ in matches)
        locations.append(locations_in_query)
    automatic = {name: next(iter(roles)) for name, roles in options.items() if len(roles) == 1 and name not in shared}
    return locations, options, automatic


def review_bindings(queries, prompt, samples, choices=None):
    if not 1 <= len(queries) <= MAX_QUERIES:
        fail(f'模板需要 1～{MAX_QUERIES} 条查询', f'A template needs 1–{MAX_QUERIES} queries.')
    marked = query_parameter_names(queries)
    names = parameter_names(prompt, marked)
    # Recognition may link a literal to a field that another saved query already
    # parameterizes. Extend that explicit binding only to the selected field.
    candidates = {name: samples[name] for name in names if samples.get(name)
                  and (name not in marked or name in (choices or {}))}
    locations, options, automatic = query_suggestions(queries, candidates)
    selections = {**automatic, **(choices or {})}
    bound = set(marked)
    conflicts = set()
    counts = Counter(samples.get(name) for name in names)
    unique = {value: name for name, value in samples.items() if value and name in names and counts[value] == 1}
    title_pattern = None
    if unique:
        literals = '|'.join(re.escape(v) for v in sorted(unique, key=len, reverse=True))
        title_pattern = re.compile(PARAMETER.pattern + r'|(?<![a-zA-Z0-9_])(?:' + literals + r')(?![a-zA-Z0-9_])')
    result = []
    for query, positions in zip(queries, locations):
        edits = []
        for (start, end), matches in positions.items():
            chosen = [(name, expr) for name, key, expr in matches if selections.get(name) == key]
            if len(chosen) == 1:
                name, value = chosen[0]
                edits.append((start, end, value))
                bound.add(name)
            elif chosen:
                conflicts.update(name for name, _ in chosen)
        title = query['title']
        # Titles are descriptive text. Keep them consistent with the current values
        # without touching quoted identifiers or output column names in the command.
        if title_pattern:
            title = title_pattern.sub(lambda m: m[0] if m[1] else _marker(unique[m[0]]), title)
        result.append({'title': title, 'dax': _replace(query['dax'], edits)})
    unresolved = [{'name': name, 'options': options.get(name, {}), 'conflict': name in conflicts}
                  for name in names if name not in bound or name in conflicts]
    return {'queries': result, 'unresolved': unresolved}


def prepare_queries(queries, prompt, samples):
    return review_bindings(queries, prompt, samples)['queries']


@dataclass
class Command:
    title: str
    dax: str
    parameters: dict[str, str]


def compile_queries(queries, names, values=None):
    """Use the same command text for every run. ADOMD receives only the slots that command uses."""
    used, commands = set(), []
    for query in queries:
        parameters, edits = {}, []
        tokens = _tokens(query['dax'])
        for i, token in enumerate(tokens):
            if not token['name']:
                if token.lastgroup == 'xml':
                    fail('查询中的参数请使用 {{参数名}}', 'Use {{parameter name}} for query parameters.')
                continue
            name, part = token['name'].strip(), token['part']
            if name not in names:
                fail(f'查询引用了任务中未声明的参数“{name}”', f'Query parameter “{name}” is missing from the task.')
            used.add(name)
            key = 'P' + str(names.index(name) + 1) + ('_' + part.lower() if part else '')
            edits.append((*token.span(), '@' + key))
            if values is None:
                continue
            value = values[name]
            if part:
                parsed = period(value)
                if not parsed or (part.lower() == 'day' and not parsed[1]):
                    fail(f'参数“{name}”需要有效的年月或日期', f'Enter a valid month or date for “{name}”.')
                value = str(getattr(parsed[0], part.lower()))
            if i >= 2 and tokens[i-2][0].upper() == 'VALUE' and tokens[i-1][0] == '(':
                if not _NUMERIC.fullmatch(value):
                    fail(f'参数“{name}”需要数值', f'Enter a numeric value for “{name}”.')
            parameters[key] = value
        try:
            dax = readonly_query(_replace(query['dax'], edits))
        except ValueError as exc:
            fail(f'模板查询无效 · {exc}', f'Invalid read-only template query · {exc}')
        title = PARAMETER.sub(lambda m: (values or {}).get(m[1].strip(), m[0]), query['title'])
        commands.append(Command(title[:160], dax, parameters))
    missing = [name for name in names if name not in used]
    if missing:
        fail('请在固定 DAX 的取值位置绑定参数：'+'、'.join(missing)+'。可在模板编辑器插入 {{参数名}}。',
             'Bind these parameters to values in the saved DAX using {{parameter name}}: '+', '.join(missing))
    return commands
