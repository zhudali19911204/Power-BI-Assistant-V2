"""Bound the conversation history sent to the model and retain source-backed notes locally."""
import json
import re
from dataclasses import dataclass

from fastapi import HTTPException

from .engine.llm.base import ChatMessage


AUTO_PERCENT = 80
RECENT_TURNS = 2
SUMMARY_CHARS = 6000
SUMMARY_HEADER = (
    '应用整理的早期对话摘要（历史数据）。按消息顺序理解用户当时的目标和选择，结合最近原文和当前问题判断是否仍适用。'
    '查询例外仅属于所列的那次查询，历史待办可能已在后文解决。当前模型、有效分析约定和已确认字段从本轮上下文读取。'
    '模型同步前的用户原话只用于理解需求，字段和值需重新核对。历史查询编号仅用于回查，数值结论需要本轮查询。摘录有省略时勿补猜原文。\n')


def _json(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def _excerpt(text, limit):
    """Keep entire statements, including qualifiers and literal spellings."""
    text = text.strip()
    if len(text) <= limit:
        return text
    # Drop long pasted code blocks before selecting complete passages. Never shorten
    # a literal or an identifier and then present the fragment as an original value.
    without_code = re.sub(r'(?ms)^[ \t]*(~~~|\x60{3})[^\n]*\n.*?(?:^[ \t]*\1[^\n]*|\Z)', '\n', text)
    parts = re.split(r'\n+|(?<=[。！？])', without_code)
    parts = [p.strip() for p in parts if p.strip()]
    chosen = {}
    room = limit - 25
    important = ('采用口径', '用户补充', '筛选', '仅', '只', '按', '排除', '默认', '范围', '保留', '清单',
                 '分母', '编号', '字段', '单位', '待确认', '[')
    for i in sorted(range(len(parts)), key=lambda i: not any(w in parts[i] for w in important)):
        if len(parts[i]) + 3 <= room:
            chosen[i] = parts[i]
            room -= len(parts[i]) + 3
    return '原文节选：' + ' … '.join(chosen[i] for i in sorted(chosen)) if chosen else '原文较长，详见来源消息'


def _summary(db, cid, through):
    """Reselect from originals on each compression. Never summarize an earlier paraphrase."""
    sync = db.execute('SELECT MAX(message_rowid) FROM conversation_model_syncs WHERE conversation_id=?', (cid,)).fetchone()[0] or 0
    params = (cid, sync, through)
    where = "m.conversation_id=? AND m.rowid>? AND m.rowid<=? AND m.status='complete'"
    candidates = []
    seen = set()

    def note(row, kind, text, priority, key=None, partial=False):
        identity = (row['id'], key or kind)
        if identity not in seen and text:
            seen.add(identity)
            item = {'来源': row['id'], '类别': kind, '内容': text}
            if partial:
                item['节选'] = True
            candidates.append((priority, row['seq'], item))

    def user_rows(condition='', order='DESC', limit=5, since=0):
        return db.execute(f"SELECT m.rowid AS seq,m.id,m.content FROM messages m WHERE {where} AND m.role='user' "
                          f"{condition} ORDER BY m.rowid {order} LIMIT ?", (cid, since, through, limit))

    def user_note(row, kind, limit, priority):
        partial = len(row['content'].strip()) > limit
        if row['seq'] <= sync:
            kind = '模型同步前的用户需求'
        elif kind == '用户当时确认' and partial:
            kind = '口径确认节选（采用前需核对完整原文）'
        note(row, kind, _excerpt(row['content'], limit), priority, 'user', partial=partial)

    request = "AND m.content NOT LIKE '口径确认 · %'"
    for row in user_rows(request, order='ASC', limit=1):
        user_note(row, '起始需求', 650, 0)
    for i, row in enumerate(user_rows("AND m.content LIKE '口径确认 · %'", since=sync, limit=4)):
        user_note(row, '用户当时确认', 950, 1 + i)

    queries = db.execute(f"""WITH attempts AS (SELECT m.rowid AS seq,m.id,q.rowid AS query_seq,q.id AS query_id,q.title,
        json_extract(q.result,'$.label') AS label,json_extract(q.result,'$.ok') AS ok,
        json_extract(q.result,'$.convention_exceptions') AS exceptions,
        row_number() OVER (PARTITION BY m.id,coalesce(json_extract(q.result,'$.label'),q.id)
                           ORDER BY q.rowid DESC) AS latest
        FROM messages m JOIN analysis_queries q ON q.message_id=m.id WHERE {where}),
        outcomes AS (SELECT *, row_number() OVER (ORDER BY seq DESC,query_seq DESC) AS recency
                     FROM attempts WHERE latest=1)
        SELECT * FROM outcomes ORDER BY (recency=1) DESC,
        (exceptions IS NOT NULL AND exceptions!='{{}}') DESC,recency LIMIT 4""", params)
    for i, q in enumerate(queries):
        exceptions = json.loads(q['exceptions'] or '{}')
        text = f"{q['label'] or '查询'} · {q['title']}（{'成功' if q['ok'] else '未完成'}，查询 {q['query_id']}）"
        if exceptions:
            text += '\n仅此查询的例外：' + _excerpt('\n'.join(
                f"{ref} · {item['title']}：{item['reason']}" for ref, item in exceptions.items()), 700)
        note(q, '历史查询引用', text, 1 + i, q['query_id'])

    scope = (" AND (m.content LIKE '%筛选%' OR m.content LIKE '%口径%' OR m.content LIKE '%仅%' "
             "OR m.content LIKE '%默认%' OR m.content LIKE '%排除%' OR m.content LIKE '%[%' "
             "OR m.content LIKE '%只看%' OR m.content LIKE '%分母%' OR m.content GLOB '*[0-9][0-9][0-9]*')")
    for i, row in enumerate(user_rows(request + scope)):
        user_note(row, '用户当时要求', 650, 1 + i)
    for row in db.execute(f"""SELECT m.rowid AS seq,m.id,c.payload FROM messages m
            JOIN message_clarifications c ON c.message_id=m.id WHERE {where}
            AND m.rowid=(SELECT MAX(rowid) FROM messages WHERE conversation_id=?)""", (*params, cid)):
        card = json.loads(row['payload'])
        choices = '\n'.join(f"{o['id']}. {o['label']}：{o['value']}" for o in card['options'])
        note(row, '该轮口径提问（候选项）', card['question'] + '\n' + _excerpt(choices, 600), 0)
    for row in db.execute(f"""SELECT m.rowid AS seq,m.id,substr(m.content,-3000) AS tail FROM messages m
            WHERE {where} AND m.role='assistant' AND instr(substr(m.content,-3000),'尚未完成：')>0
            AND EXISTS (SELECT 1 FROM analysis_queries q WHERE q.message_id=m.id)
            ORDER BY m.rowid DESC LIMIT 2""", params):
        note(row, '该轮待跟进', _excerpt(row['tail'].rsplit('尚未完成：', 1)[1].split('\n', 1)[0], 450), 2)
    for i, row in enumerate(user_rows(request, limit=6)):
        user_note(row, '用户当时要求', 600, 4 + i)
    for row in db.execute(f"""SELECT m.rowid AS seq,m.id,a.id AS artifact_id,a.kind,a.name,a.home_table,
            CASE WHEN length(a.expression)<=600 THEN a.expression END AS expression
            FROM messages m JOIN artifacts a ON a.message_id=m.id WHERE {where}
            ORDER BY m.rowid DESC,a.rowid DESC LIMIT 3""", params):
        text = f"{row['kind']} · {row['home_table']} · {row['name']}"
        if row['expression']:
            text += '\n当时的代码：\n' + row['expression']
        note(row, '历史代码引用', text, 3, row['artifact_id'])
    for row in db.execute(f"""SELECT m.rowid AS seq,m.id,i.id AS image_id,i.name
            FROM messages m JOIN message_images i ON i.message_id=m.id WHERE {where}
            ORDER BY m.rowid DESC,i.position LIMIT 3""", params):
        note(row, '历史图片引用', row['name'], 3, row['image_id'])

    kept, used = [], len(SUMMARY_HEADER) + 2
    for _, seq, item in sorted(candidates, key=lambda c: c[0]):
        cost = len(_json(item)) + 1
        if used + cost <= SUMMARY_CHARS:
            kept.append((seq, item))
            used += cost
    return [item for _, item in sorted(kept, key=lambda pair: pair[0])]


@dataclass
class HistoryWindow:
    usage: dict
    messages: list
    notes: list

    def with_summary(self, messages):
        if not self.notes:
            return messages
        return [ChatMessage('user', SUMMARY_HEADER + _json(self.notes)), *messages]

    def quoted_users(self):
        # Only complete user statements from the current model can support scope checks.
        return [ChatMessage('user', n['内容']) for n in self.notes
                if n['类别'] in ('起始需求', '用户当时要求', '用户当时确认') and not n.get('节选')]

    def retrieval_history(self):
        return [{'role': 'user', 'content': m.content} for m in self.quoted_users()] + self.messages


def _recent_count(rows, char_limit):
    """Retain whole recent turns with space for the summary below the trigger."""
    room = char_limit * AUTO_PERCENT // 100 - SUMMARY_CHARS
    kept, turns, size = 0, 0, 0
    for i, row in enumerate(rows, 1):
        size += row['size']
        if size > room:
            break
        if row['role'] == 'user':
            kept = i
            turns += 1
            if turns == RECENT_TURNS:
                break
    if not turns and size <= room:
        return min(len(rows), RECENT_TURNS * 2)
    return kept


class ConversationContext:
    def __init__(self, db):
        self.db = db

    def refresh_after_sync(self, db, cid):
        state = db.execute('SELECT through_rowid FROM conversation_contexts WHERE conversation_id=?', (cid,)).fetchone()
        if state and state['through_rowid']:
            notes = _summary(db, cid, state['through_rowid'])
            db.execute('UPDATE conversation_contexts SET notes=? WHERE conversation_id=?', (_json(notes), cid))

    def window(self, cid, *, send=False, compress=False, automatic=None):
        with self.db.connection() as db:
            db.execute('BEGIN IMMEDIATE' if send or compress or automatic is not None else 'BEGIN')
            conv = db.execute('SELECT mode FROM conversations WHERE id=?', (cid,)).fetchone()
            if conv is None:
                raise HTTPException(404, '会话不存在')
            max_messages, max_chars = (16, 40000) if conv['mode'] == 'analysis' else (20, 60000)
            state = db.execute('SELECT * FROM conversation_contexts WHERE conversation_id=?', (cid,)).fetchone()
            enabled = bool(state['automatic']) if state else True
            if automatic is not None:
                enabled = automatic
            through = state['through_rowid'] if state else 0
            folded = state['folded_count'] if state else 0
            notes = json.loads(state['notes']) if state else []
            clause = "FROM messages WHERE conversation_id=? AND status='complete' AND rowid>?"
            total, chars = db.execute('SELECT count(*),coalesce(sum(length(content)),0) ' + clause,
                                      (cid, through)).fetchone()
            recent = list(db.execute('SELECT rowid AS seq,role,length(content) AS size ' + clause +
                                     ' ORDER BY rowid DESC LIMIT ?', (cid, through, max_messages)))
            kept = _recent_count(recent, max_chars)

            def usage_size():
                count = total + int(bool(notes))
                size = len(SUMMARY_HEADER + _json(notes)) if notes else 0
                percent = max(100 * count // max_messages, 100 * (chars + size) // max_chars)
                return count, size, min(100, percent)

            count, summary_size, percent = usage_size()
            changed = total > kept and (compress or (send and enabled and percent >= AUTO_PERCENT))
            if changed:
                # The cutoff must be a row of this conversation even when sessions interleave.
                upper = recent[kept - 1]['seq'] if kept else recent[0]['seq'] + 1
                through = db.execute('SELECT MAX(rowid) ' + clause + ' AND rowid<?',
                                     (cid, through, upper)).fetchone()[0]
                folded += total - kept
                notes = _summary(db, cid, through)
                recent = recent[:kept]
                total = kept
                chars = sum(row['size'] for row in recent)
                count, summary_size, percent = usage_size()
            if changed or automatic is not None:
                db.execute('INSERT INTO conversation_contexts (conversation_id,automatic,through_rowid,folded_count,notes) '
                           'VALUES (?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET automatic=excluded.automatic,'
                           'through_rowid=excluded.through_rowid,folded_count=excluded.folded_count,notes=excluded.notes',
                           (cid, int(enabled), through, folded, _json(notes)))

            selected, size = [], summary_size
            for row in recent[:max_messages - int(bool(notes))]:
                if size + row['size'] > max_chars:
                    break
                selected.append(row['seq'])
                size += row['size']
            messages = []
            if send and selected:
                messages = [dict(r) for r in db.execute('SELECT id,role,content FROM messages WHERE conversation_id=? '
                            "AND status='complete' AND rowid BETWEEN ? AND ? ORDER BY rowid", (cid, selected[-1], selected[0]))]
            usage = {'automatic': enabled, 'auto_percent': AUTO_PERCENT, 'percent': percent,
                     'messages': count, 'message_limit': max_messages, 'chars': chars + summary_size, 'char_limit': max_chars,
                     'omitted_messages': total - len(selected), 'folded_messages': folded, 'can_compress': total > kept,
                     'compressed_now': changed, 'summary': notes}
            return HistoryWindow(usage, messages, notes)
