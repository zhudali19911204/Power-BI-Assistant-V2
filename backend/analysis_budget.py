"""Bound adaptive analysis by useful work, elapsed time and evidence size."""
import re
import time

from .database import encode


MAX_BATCH_QUERIES = 4
MAX_QUERIES = 12
MAX_BATCHES = 4
MAX_QUERY_ATTEMPTS = 18


class AnalysisBudget:
    def __init__(self, total, *, clock=None):
        self.clock = clock or time.monotonic
        self.total = total
        self.started = self.clock()
        self.deadline = self.started + total
        self.summary_reserve = min(180, total * .25)
        self.planning_seconds = min(10, total * .02)
        self.query_seconds = min(5, total * .01)
        self.field_seconds = min(3, total * .01)

    def remaining(self, *, summary=False):
        return max(0, self.deadline - self.clock() - (0 if summary else self.summary_reserve))

    def observe(self, kind, seconds):
        # Recent slow work is more informative than a fast early query.
        if kind == 'query':self.query_seconds = max(self.query_seconds, seconds)
        elif kind == 'plan':self.planning_seconds = max(self.planning_seconds, seconds)
        elif kind == 'field':self.field_seconds = max(self.field_seconds, seconds)

    def can_review_fields(self):
        return self.remaining() >= self.planning_seconds * 1.2

    def can_check_field(self):
        return self.remaining() >= (self.field_seconds + self.planning_seconds) * 1.2

    def can_query(self):
        return self.remaining() >= self.query_seconds * 1.2

    def can_plan(self):
        return self.remaining() >= (self.planning_seconds + self.query_seconds) * 1.2

    def batch_size(self, used):
        return min(MAX_BATCH_QUERIES, MAX_QUERIES - used,
                   max(0, int(self.remaining() / max(.001, self.query_seconds * 1.2))))


_DAX_PARTS = re.compile(r'''"(?:[^"]|"")*"|'(?:[^']|'')*'|\[(?:[^\]]|\]\])*\]|//[^\n]*|--[^\n]*|/\*[\s\S]*?\*/|\s+|[^\s]''')


def query_key(dax):
    """Ignore formatting/comments, but preserve literals and quoted identifiers."""
    parts = []
    for match in _DAX_PARTS.finditer(dax.strip().rstrip(';')):
        token = match[0]
        if token.isspace() or token.startswith(('//', '--', '/*')):continue
        parts.append(token if token.startswith(('"', "'", '[')) else token.casefold())
    return ''.join(parts)


def evidence_context(records, *, max_chars=96000, preview_rows=None):
    """Keep the latest attempt per Q label and disclose any prompt-only omissions.

    Persisted records/CSV remain untouched. Never crop a cell into a different value.
    """
    latest = {r['label']: r for r in records}
    result = []
    # Share the budget so early verbose results cannot hide later query outcomes.
    share = max_chars // max(1, len(latest))
    for record in latest.values():
        item = {k: record[k] for k in ('label', 'title', 'dax', 'ok', 'row_count', 'truncated',
                                       'columns', 'error', 'batch', 'scope') if k in record}
        item.update(rows=[], context_truncated=False, shown_rows=0)
        # The DAX itself is useful for checking filters, but a huge query must not
        # consume all space intended for its actual result.
        if len(item.get('dax', '')) > share // 2:
            item['dax'] = item['dax'][:share // 2]
            item['query_text_truncated'] = True
        size = len(encode(item))
        if size > share:
            item = {k: item[k] for k in ('label', 'ok', 'row_count', 'truncated')}
            item.update(rows=[], shown_rows=0, context_truncated=True, metadata_omitted=True)
            result.append(item)
            continue
        rows = record.get('rows', [])
        for row in rows if preview_rows is None else rows[:preview_rows]:
            cost = len(encode(row)) + 2
            if size + cost + 30 > share:break
            item['rows'].append(row);size += cost
        item['shown_rows'] = len(item['rows'])
        item['context_truncated'] = len(item['rows']) < len(rows)
        result.append(item)
    return result
