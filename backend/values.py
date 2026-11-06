"""Local value evidence shared by field previews, semantic explanations and generation."""
import json
import re
import time

from .database import encode, now
from .engine.context.live_source import _LiveSession, find_adomd_dll, find_instances
from .fields import field_ref


CODE_PATTERN = r'(?<![A-Za-z0-9_.])0\d{2,11}(?![A-Za-z0-9_.])'
MAX_LOOKUP_FIELDS = 96


def codes_in(text):
    return list(dict.fromkeys(re.findall(CODE_PATTERN, text)))


def exact_value_query(fields, literal):
    # String comparison keeps 0257 distinct from numeric 257. Identifiers come from the
    # live schema, not the user. No amounts, measures, or model writes are involved.
    quoted = '"' + literal.replace('"', '""') + '"'
    rows = [f'ROW("__index", {i}, "__match", COALESCE(COUNTROWS(FILTER('
            f'DISTINCT({field_ref(table, column)}), '
            f'({field_ref(table, column)} & "") == {quoted})), 0))'
            for i, (table, column) in enumerate(fields)]
    return 'EVALUATE ' + (rows[0] if len(rows) == 1 else 'UNION(' + ', '.join(rows) + ')')


class ValueEvidence:
    def __init__(self, db, powerbi):
        self.db, self.powerbi = db, powerbi

    def remember(self, model, profile):
        # A bounded preview is a hint for future lookups, never a complete value index.
        compact = {k: profile[k] for k in ('statistics', 'values', 'samples', 'search_terms',
                                          'truncated', 'scope', 'checked_at')}
        self.db.execute('INSERT OR REPLACE INTO field_value_profiles VALUES (?,?,?,?,?,?)',
                        (model['id'], profile['table'], profile['column'], model['fingerprint'],
                         encode(compact), profile['checked_at']))

    def preview_hints(self, model, literal):
        hints = set()
        for row in self.db.all('SELECT * FROM field_value_profiles WHERE model_id=? AND fingerprint=?',
                               (model['id'], model['fingerprint'])):
            p = json.loads(row['payload'])
            if any(v.get('value') == literal and not v.get('value_truncated')
                   for v in p['values'] + p['samples']):
                hints.add((row['table_name'], row['column_name']))
        return hints

    def locate(self, model_id, literal, entries, deadline=None):
        if not re.fullmatch(r'0\d{2,11}', literal):
            raise ValueError('仅支持有前导零的短编号查值')
        started = time.monotonic()
        deadline = min(started + 15, deadline) if deadline is not None else started + 15
        with self.powerbi.lock:
            model, ctx = self.powerbi.require_live(model_id, unchanged=True)
            metadata = {(e['table'], e['name']): e for e in entries if e['kind'] == 'column'}
            fields = []
            for table, columns in ctx.tables.items():
                for c in columns:
                    dtype = metadata.get((table, c.name), {}).get('facts', {}).get('actual_dtype') or c.dtype
                    # Leading-zero codes cannot be native dates, booleans or numbers.
                    if str(dtype).lower() not in ('string', 'unknown', 'variant', ''):
                        continue
                    fields.append((table, c.name))
            hints = self.preview_hints(model, literal)
            fields.sort(key=lambda pair: (pair not in hints,
                not bool(re.search(r'code|number|编号|编码|\bid\b|key', pair[1], re.I)),
                not bool(re.search(r'^dim|维度', pair[0], re.I)), pair))
            result = {'literal': literal, 'matches': [], 'searched_fields': 0,
                      'total_fields': len(fields), 'complete': False, 'errors': [],
                      'scope': '当前用户可读取模型中的文本及未知类型字段，未应用报表页面筛选',
                      'checked_at': now(), 'run_verified': False}
            dll = find_adomd_dll(find_instances()) if fields and time.monotonic() < deadline else None
            if fields and time.monotonic() >= deadline:
                result['errors'].append('已达到本轮查值时间上限')
            elif fields and not dll:
                result['errors'].append('未找到 Desktop 的 ADOMD 客户端，尚未验证编号归属')
            elif fields:
                try:
                    with _LiveSession(model['port'], dll) as session:
                        for start in range(0, min(len(fields), MAX_LOOKUP_FIELDS), 12):
                            if time.monotonic() >= deadline:
                                result['errors'].append('已达到本轮查值时间上限')
                                break
                            batch = fields[start:min(start + 12, MAX_LOOKUP_FIELDS)]
                            timeout = max(1, min(5, int(deadline-time.monotonic())+1))
                            rows = session.query(exact_value_query(batch, literal), max_rows=len(batch), timeout_seconds=timeout)
                            normalized = [{k.strip('[]'): v for k, v in row.items()} for row in rows]
                            if (len(normalized) != len(batch) or
                                {r.get('__index') for r in normalized} != set(range(len(batch))) or
                                any(r.get('__match') not in (0, 1) for r in normalized)):
                                raise ValueError('字段查值返回不完整，不能据此判断没有匹配')
                            result['searched_fields'] += len(batch)
                            for row in normalized:
                                if row['__match']:
                                    table, column = batch[row['__index']]
                                    result['matches'].append({'table': table, 'column': column})
                except Exception as exc:
                    result['errors'].append('部分字段未完成实际查值（' + type(exc).__name__ + '）')
            result.update(complete=result['searched_fields'] == len(fields) and not result['errors'],
                          run_verified=result['searched_fields'] > 0,
                          seconds=round(time.monotonic() - started, 3))
            self.db.execute('INSERT OR REPLACE INTO model_value_lookups VALUES (?,?,?,?,?)',
                            (model_id, literal, model['fingerprint'], encode(result), result['checked_at']))
        return result

    def for_prompt(self, model, literals):
        records = []
        for literal in literals[:8]:
            row = self.db.one('SELECT payload FROM model_value_lookups WHERE model_id=? AND literal=? AND fingerprint=?',
                              (model['id'], literal, model['fingerprint']))
            if row:
                records.append(json.loads(row['payload']))
        return records
