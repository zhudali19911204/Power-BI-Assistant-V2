"""Bounded, read-only column profiles built from validated model identifiers."""
import datetime
import math
import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .database import encode, now
from .engine.context.live_source import _LiveSession, find_adomd_dll, find_instances


class FieldPreviewInput(BaseModel):
    table: str = Field(min_length=1, max_length=300)
    column: str = Field(min_length=1, max_length=300)
    search: str = Field(default='', max_length=120)
    limit: int = Field(default=50, ge=1, le=100)

    @field_validator('search')
    @classmethod
    def trimmed(cls, value):
        return value.strip()


class FieldCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    table: str = Field(min_length=1, max_length=300)
    column: str = Field(min_length=1, max_length=300)
    search_terms: list[str] = Field(default_factory=list, max_length=3)
    reason: str = Field(default='', max_length=240)

    @field_validator('search_terms')
    @classmethod
    def terms(cls, values):
        terms = list(dict.fromkeys(v.strip() for v in values if v.strip()))
        if any(len(v) > 120 for v in terms):
            raise ValueError('每个搜索词最多 120 字')
        return terms


def field_ref(table, column):
    return "'" + table.replace("'", "''") + "'[" + column.replace(']', ']]') + ']'


def _literal_search(term):
    # CONTAINSSTRING treats *, ? and ~ as wildcards/escapes. Search user text literally.
    term = term.replace('~', '~~').replace('*', '~*').replace('?', '~?')
    return '"' + term.replace('"', '""') + '"'


def profile_queries(table, column, dtype, terms, limit):
    ref = field_ref(table, column)
    tab = "'" + table.replace("'", "''") + "'"
    predicate = ' || '.join(f'CONTAINSSTRING({ref} & "", {_literal_search(term)})' for term in terms)
    matching = f'FILTER(DISTINCT({ref}), {predicate})' if terms else f'DISTINCT({ref})'
    empty = f'COALESCE(COUNTROWS(FILTER({tab}, NOT ISBLANK({ref}) && {ref} == "")), 0)' if dtype == 'string' else 'BLANK()'
    stats = (f'EVALUATE ROW("row_count", COALESCE(COUNTROWS({tab}), 0), '
             f'"distinct_count", COALESCE(COUNTROWS(DISTINCT({ref})), 0), '
             f'"blank_count", COALESCE(COUNTROWS(FILTER({tab}, ISBLANK({ref}))), 0), '
             f'"empty_string_count", {empty}, '
             f'"matched_distinct_count", COALESCE(COUNTROWS({matching}), 0))')

    def values_query(count, filtered):
        # The filterTable argument is always a table expression, never a naked predicate.
        filter_arg = f'FILTER(ALL({ref}), {predicate}), ' if filtered and terms else ''
        count_name='__pbi_preview_count'+('_' if column.casefold()=='__pbi_preview_count' else '')
        return (f'EVALUATE TOPN({count}, SELECTCOLUMNS(SUMMARIZECOLUMNS({ref}, {filter_arg}'
                f'"{count_name}", COUNTROWS({tab})), "__value", {ref}, "__count", [{count_name}]), '
                f'[__count], DESC, [__value], ASC) ORDER BY [__count] DESC, [__value] ASC')

    return stats, values_query(limit + 1, True), values_query(6, False) if terms else None


def _row(row):
    return {key.strip('[]'): value for key, value in row.items()}


def _value(value,max_chars=500):
    if isinstance(value,float) and not math.isfinite(value):
        return {'value':None,'value_truncated':False,'nonfinite':True}
    if isinstance(value, (datetime.date, datetime.datetime)):
        value = value.isoformat()
    truncated = isinstance(value, str) and len(value) > max_chars
    return {'value': value[:max_chars] if truncated else value, 'value_truncated': truncated}


class FieldService:
    def __init__(self, powerbi):
        self.powerbi = powerbi

    def preview(self, model_id, body, *, search_terms=None, automatic=False):
        terms = search_terms if search_terms is not None else ([body.search] if body.search else [])
        started = time.monotonic()
        with self.powerbi.lock:
            model, ctx = self.powerbi.require_live(model_id, unchanged=True)
            if not ctx.has_column(body.table, body.column):
                raise HTTPException(422, '该字段不在当前模型中，请同步模型后重试')
            column = next(c for c in ctx.tables[body.table] if c.name == body.column)
            queries = profile_queries(body.table, body.column, column.dtype, terms, body.limit)
            dll = find_adomd_dll(find_instances())
            if not dll:
                raise HTTPException(409, '未找到 Desktop 的 ADOMD 客户端')
            try:
                with _LiveSession(model['port'], dll) as session:
                    timeout = 8 if automatic else 15
                    stats = session.query(queries[0], max_rows=1, timeout_seconds=timeout)
                    data = session.query(queries[1], max_rows=body.limit + 1, timeout_seconds=timeout)
                    samples = session.query(queries[2], max_rows=6, timeout_seconds=timeout) if queries[2] else data[:6]
            except Exception as exc:
                raise HTTPException(422, '读取字段取值失败：' + str(exc).splitlines()[0][:240]) from exc
        if not stats:
            raise HTTPException(422, '字段统计未返回结果，不能据此判断没有数据')
        statistics = _row(stats[0])
        values = []
        used = 0
        cap = 6000 if automatic else 16000
        for raw in data[:body.limit]:
            raw = _row(raw)
            entry = {**_value(raw['__value']), 'count': raw['__count']}
            size = len(encode(entry).encode('utf-8'))
            if used + size > cap:
                break
            values.append(entry)
            used += size
        profile = {'model_id': model_id, 'table': body.table, 'column': body.column, 'dtype': column.dtype,
                'statistics': statistics, 'values': values, 'samples': [_value(_row(row)['__value'],120) for row in samples],
                'search_terms': terms, 'limit': body.limit,
                'truncated': len(data) > len(values) or statistics['matched_distinct_count'] > len(values)
                             or any(v['value_truncated'] for v in values),
                'scope': '当前用户可读取的全模型范围，未应用报表页面筛选',
                'checked_at': now(), 'seconds': round(time.monotonic() - started, 3),
                'dax_queries': [q for q in queries if q], 'run_verified': True}
        semantics = getattr(self.powerbi, 'semantics', None)
        if semantics:
            semantics.values.remember(model, profile)
        return profile
