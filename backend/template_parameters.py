"""Recognize editable literals in an analysis task using local, grounded hints."""
import re
from collections import defaultdict

from .values import CODE_PATTERN


PARAMETER = re.compile(r'\{\{\s*([\w\u4e00-\u9fff][\w\u4e00-\u9fff -]{0,39}?)\s*\}\}')
PARAMETER_LIMIT = 12


def parameter_names(prompt, query_names=()):
    text_names = (m.group(1).strip() for m in PARAMETER.finditer(prompt))
    names = list(dict.fromkeys([*text_names, *query_names]))
    remainder = PARAMETER.sub('', prompt)
    if '{{' in remainder or '}}' in remainder or len(names) > PARAMETER_LIMIT:
        raise ValueError('参数请使用 {{月份}} 这样的名称，最多 12 项，每个名称最多 40 字')
    return names


# Protect existing markers, Markdown code, and DAX table/column references.
PROTECTED = re.compile(r"\{\{[^{}]*\}\}|```[\s\S]*?(?:```|\Z)|`[^`\n]*`|"
                       r"(?:'(?:''|[^'\n])*'|(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*)(?=[ \t]*\[)|"
                       r"\[(?:\]\]|[^\[\]\n])*\]")
YEAR, MONTH, DAY = r'(?:19|20)[0-9]{2}', r'(?:1[0-2]|0?[1-9])', r'(?:3[01]|[12][0-9]|0?[1-9])'
YEAR_MONTH = rf'{YEAR}(?:[-/.]{MONTH}|[ \t]*年[ \t]*{MONTH}[ \t]*月)'
DATE = rf'{YEAR}(?:[-/.]{MONTH}[-/.]{DAY}|[ \t]*年[ \t]*{MONTH}[ \t]*月[ \t]*{DAY}[ \t]*日)'
LEFT, RIGHT = r'(?<![A-Za-z0-9_])', r'(?![A-Za-z0-9_])'
PERIODS = [(name, re.compile(LEFT + pattern + RIGHT, re.I)) for name, pattern in (
    ('预测版本', rf'(?:FC|FCT|Forecast)[_-]{YEAR}[_-][0-9]{{1,2}}\+[0-9]{{1,2}}'),
    ('日期', DATE), ('月份', YEAR_MONTH), ('日期', rf'{MONTH}月{DAY}日'),
    ('季度', rf'(?:{YEAR}[ \t]*(?:年[ \t]*)?)?(?:Q[1-4]|第?[1-4一二三四]季度)'))]
SIMPLE_PERIODS = [(name, re.compile(LEFT + pattern + RIGHT)) for name, pattern in (
    ('预测年份', rf'{YEAR}(?=[ \t]*年?[ \t]*预测)'),
    ('年份', YEAR), ('月份', rf'{MONTH}(?=[ \t]*月)'))]

DIMENSION = r'成本中心|公司代码|公司|工厂|部门|区域|地区|城市|物料|产品线|产品|品牌|客户|供应商|渠道|费用类别|类别|品类|科目|版本'
CODE = r'[A-Za-z0-9]+(?:[-_+./][A-Za-z0-9]+)*'
QUOTED = r"""“([^”\n]{1,120})”|「([^」\n]{1,120})」|"([^"\n]{1,120})"|‘([^’\n]{1,120})’|'([^'\n]{1,120})'"""
COLUMN_FILTER = re.compile(rf'\[(?P<label>(?:\]\]|[^\[\]\n])+)\][ \t]*(?:={{1,2}}|为|等于)[ \t]*(?:{QUOTED}|({CODE}){RIGHT})')
QUALIFIED = re.compile(
    rf'(?P<label>{DIMENSION})(?:编号|编码|代码|名称)?[ \t]*(?:[：:=]|为|是|等于)?[ \t]*'
    rf'(?:{QUOTED}|({CODE}){RIGHT})', re.I)
CHINESE_FILTER = re.compile(
    rf'(?P<label>{DIMENSION})(?:名称)?[ \t]*(?:[：:=]|为|等于)[ \t]*'
    r'(?P<value>[\u3400-\u9fff]{1,24}?)(?=的|在|和|与|或|以及|并|按|[\s，,。.!！?？、；;）)]|$)')
CODE_BEFORE_LABEL = re.compile(rf'{LEFT}(?P<value>{CODE})[ \t]*(?P<label>{DIMENSION})')
TASK_VERB = r'分析|对比|比较|查看|查询|统计|看看|看下'
CHINESE_BEFORE_LABEL = re.compile(
    rf'(?:{TASK_VERB}|以及|[\s，,：:、与和在]|(?<=\d[年月日])|^)'
    rf'(?P<value>(?:(?!{TASK_VERB}|[各的与和在]|所有|每个|哪个|哪些|不同|地区|区域|工厂|部门)[\u3400-\u9fff]){{2,8}}?)(?P<label>地区|区域|工厂|部门)')
RANK = re.compile(r'(?i:(?<![a-z])top)[ \t]*(\d{1,4})(?!\d)|前[ \t]*(\d{1,4})(?=[ \t]*[名位个项条家])')
BUSINESS_WORD = re.compile(r'(?<![A-Za-z0-9_./-])[A-Za-z][A-Za-z0-9]{1,29}(?![A-Za-z0-9_./(\[-])')


def field_label(column):
    return re.sub(r'[^\w -]+', ' ', column).strip(' -')[:40].rstrip()


def recognize_parameters(prompt, measures=(), field_values=()):
    """Offsets refer to Unicode code points in the exact, untrimmed editor text."""
    used_names = set(parameter_names(prompt))
    occupied = bytearray(len(prompt))
    for match in PROTECTED.finditer(prompt):
        occupied[match.start():match.end()] = b'\x01' * len(match[0])
    hits = []

    def add(start, end, label, key=None):
        if not label or not 0 < end - start <= 500 or any(occupied[start:end]):
            return False
        value = prompt[start:end]
        if value != value.strip():
            return False
        occupied[start:end] = b'\x01' * (end - start)
        hits.append((start, end, label, key or value.casefold()))
        return True

    def periods(patterns):
        for label, pattern in patterns:
            for match in pattern.finditer(prompt):
                # Keep the question and its saved scope in sync across calendar spellings.
                key = tuple(map(int, re.findall(r'[0-9]+', match[0]))) if label in ('日期', '月份') else None
                add(*match.span(), label, key)

    explicit = defaultdict(set)
    # A literal after a real field reference can have the same spelling as a
    # date or another field's value. Keep the field and only select its value.
    for match in COLUMN_FILTER.finditer(prompt):
        label = field_label(match['label'])
        if add(*match.span(match.lastindex), label):
            explicit[match[match.lastindex].casefold()].add(label)
    periods(PERIODS)
    for match in RANK.finditer(prompt):
        add(*match.span(match.lastindex), '排名数量')

    # Explicit labels distinguish, for example, a factory and a cost center with the same code.
    for pattern in (QUALIFIED, CHINESE_FILTER, CODE_BEFORE_LABEL, CHINESE_BEFORE_LABEL):
        for match in pattern.finditer(prompt):
            group = 'value' if 'value' in pattern.groupindex else match.lastindex
            start, end = match.span(group)
            value = match[group]
            if re.fullmatch(YEAR, value) and re.match(r'\s*年', prompt[end:]):
                continue
            if add(start, end, match['label']):
                explicit[value.casefold()].add(match['label'])

    periods(SIMPLE_PERIODS)

    known_fields = defaultdict(set)
    for value, column in field_values:
        name = field_label(column)
        if value and name:
            known_fields[value.casefold()].add(name)
    terms = {word: next(iter(fields)) for word, fields in known_fields.items() if len(fields) == 1}
    for measure in measures:
        if len(measure) >= 2:
            terms.setdefault(measure.casefold(), '指标')
    for value, labels in explicit.items():
        if len(labels) == 1:
            terms[value] = next(iter(labels))
        else:
            terms.pop(value, None)
    folded = prompt.casefold()
    for term in sorted((t for t in terms if t in folded), key=len, reverse=True):
        left = LEFT if term[0].isascii() and term[0].isalnum() else ''
        right = RIGHT if term[-1].isascii() and term[-1].isalnum() else ''
        for match in re.finditer(left + re.escape(term) + right, prompt, re.I):
            add(*match.span(), terms[term])

    for match in re.finditer(CODE_PATTERN, prompt):
        add(*match.span(), '编号')
    for match in re.finditer(QUOTED, prompt):
        add(*match.span(match.lastindex), '业务词')
    # Also recognize isolated English business terms in Chinese questions, e.g. "0887 overhead".
    # A sequence of English prose is too ambiguous to split into individual parameters.
    if re.search(r'[\u3400-\u9fff]', prompt):
        for match in BUSINESS_WORD.finditer(prompt):
            if re.fullmatch(r'F\d+|TOP|DAX|SQL', match[0], re.I):
                continue
            before = prompt[max(0, match.start()-8):match.start()]
            after = prompt[match.end():match.end()+8]
            if re.search(r'[A-Za-z][ \t]+$', before) or re.match(r'[ \t]+[A-Za-z]', after):
                continue
            add(*match.span(), '业务词')

    candidates, suffixes = {}, defaultdict(lambda: 1)
    for start, end, label, key in sorted(hits):
        identity = (label, key)
        if identity not in candidates:
            name = label
            while name in used_names:
                suffixes[label] += 1
                suffix = str(suffixes[label])
                name = label[:40-len(suffix)] + suffix
            used_names.add(name)
            candidates[identity] = {'name': name, 'value': prompt[start:end], 'spans': []}
        candidates[identity]['spans'].append((start, end))
    return list(candidates.values())
