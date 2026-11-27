"""Versioned DAX templates. Each run executes its saved commands against live data."""
import json
import re
import threading
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .database import encode, now, uid
from .reports import same_report
from .language import Language, SAVED_QUERY_SCOPE, wording
from .schemas import MessageInput
from .streaming import BackgroundStream
from .template_parameters import PARAMETER, PARAMETER_LIMIT, parameter_names, recognize_parameters
from .template_queries import (
    SavedQuery, binding_field, compile_queries, prepare_queries, query_columns, query_parameter_names,
    query_suggestions,
)
from .analysis_budget import MAX_QUERIES


Value = Annotated[str, Field(max_length=500)]
TEMPLATE_NAME_LIMIT = 120
QUERY_LABEL = re.compile(r'(?:Q\s*\d+|(?:DAX\s*)?(?:query(?:\s+results?)?|查询(?:结果)?)\s*#?\s*\d*)', re.I)
SCOPE_HEADINGS = ('分析口径（保存前请核对）：', 'Analysis scope (review before saving):')

TEMPLATE_SELECT = """
    SELECT t.*, m.name AS model_name,
           (SELECT json_extract(r.snapshot, '$.values') FROM analysis_template_runs r
            WHERE r.template_id=t.id ORDER BY r.created_at DESC,r.rowid DESC LIMIT 1) AS last_values_json
    FROM analysis_templates t JOIN models m ON m.id=t.model_id
"""


class TemplateEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=TEMPLATE_NAME_LIMIT)
    prompt: str = Field(min_length=1, max_length=12000)
    defaults: dict[str, Value] = Field(default_factory=dict, max_length=PARAMETER_LIMIT)
    revision: int | None = Field(default=None, ge=1)
    queries: list[SavedQuery] | None = Field(default=None, max_length=MAX_QUERIES)
    samples: dict[str, Value] = Field(default_factory=dict, max_length=PARAMETER_LIMIT)

    @model_validator(mode='after')
    def valid_parameters(self):
        parameter_names(self.prompt)
        return self


class TemplateCreate(TemplateEdit):
    model_id: str
    source_message_id: str | None = None


class QueryPreview(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    samples: dict[str, Value] = Field(default_factory=dict, max_length=PARAMETER_LIMIT)
    queries: list[SavedQuery] = Field(min_length=1, max_length=MAX_QUERIES)
    choices: dict[str, str] = Field(default_factory=dict, max_length=PARAMETER_LIMIT)


class TemplateScan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str = Field(min_length=1, max_length=12000)
    model_id: str
    source_message_id: str | None = None
    queries: list[SavedQuery] = Field(default_factory=list,max_length=MAX_QUERIES)
    samples: dict[str, Value] = Field(default_factory=dict, max_length=PARAMETER_LIMIT)


class TemplateRun(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_id: str
    revision: int = Field(ge=1)
    values: dict[str, Value] = Field(default_factory=dict, max_length=PARAMETER_LIMIT)
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    llm_profile_id: str | None = None
    language: Language = 'zh-CN'


def suggested_template_name(queries, query_scopes):
    """Name the selected reply using only metadata from its successful queries."""
    titles = []
    for query in queries:
        title = ' '.join(query['executed_title'].split())
        if title and not QUERY_LABEL.fullmatch(title) and title not in titles:
            titles.append(title)
    scopes = []
    for raw_scope in query_scopes:
        scope = raw_scope.strip()
        if scope and scope not in SAVED_QUERY_SCOPE:
            line = ' '.join(scope.split())
            phrase = re.split(r'[。！？]|(?<=[.!?])\s+', line, maxsplit=1)[0].rstrip('.!?')
            if phrase and phrase not in scopes:
                scopes.append(phrase)

    name = ' / '.join(titles)
    scope = ' / '.join(scopes)
    if not titles and not scopes:
        columns = dict.fromkeys(col.strip() for q in queries for col in json.loads(q['columns_json'] or '[]')
                                if isinstance(col, str) and col.strip())
        name = ' / '.join(columns) or wording('分析模板', 'Analysis template')
    elif not titles:
        name = scope
    elif scope and len(scope) <= TEMPLATE_NAME_LIMIT and all(t.casefold() in scope.casefold() for t in titles):
        name = scope
    else:
        # Include distinct scope details when they fit alongside the business titles.
        details = [s for s in scopes if s.casefold() not in name.casefold()]
        with_scope = ' · '.join([name, *details])
        if len(with_scope) <= TEMPLATE_NAME_LIMIT:
            name = with_scope
    return name if len(name) <= TEMPLATE_NAME_LIMIT else name[:TEMPLATE_NAME_LIMIT-1].rstrip() + '…'


def expand(template, supplied):
    names = template['parameters']
    if set(supplied) - set(names):
        raise HTTPException(422, '包含模板未定义的参数')
    values = {name: supplied.get(name, '').strip() or template['run_values'][name] for name in names}
    missing = [name for name in names if not values[name]]
    if missing:
        raise HTTPException(422, '请填写参数：' + '、'.join(missing))
    # A single substitution pass: values are plain text, never DAX/code interpolation.
    text = PARAMETER.sub(lambda match: values[match.group(1).strip()], template['prompt'])
    if len(text) > 24000:
        raise HTTPException(422, '替换参数后的任务过长，请缩短模板或参数值')
    return text, values


def single_turn_legacy_prompt(db, source_id, prompt, defaults):
    """Recognize the old eight-message prefix by comparing it with its source messages."""
    if not source_id or '\n\n' not in prompt:
        return prompt
    turns = db.execute("""SELECT u.content FROM messages a JOIN messages u ON u.conversation_id=a.conversation_id
                          WHERE a.id=? AND u.role='user' AND u.rowid<a.rowid ORDER BY u.rowid DESC LIMIT 8""",
                       (source_id,)).fetchall()
    if len(turns) < 2:
        return prompt
    older = '\n\n'.join(row['content'] for row in reversed(turns[1:]))
    current = turns[0]['content']
    separators = older.count('\n\n') + 1
    parts = prompt.split('\n\n', separators)
    if len(parts) != separators + 1:
        return prompt

    def resolve(text):
        return PARAMETER.sub(lambda m: defaults.get(m[1].strip(), m[0]), text)

    if resolve('\n\n'.join(parts[:-1])) != older:
        return prompt
    tail = resolve(parts[-1])
    if tail == current or any(tail.startswith(f'{current}\n\n{heading}\n') for heading in SCOPE_HEADINGS):
        return parts[-1]
    return prompt


class TemplateExecution:
    """The definition and values belong to this run even if the template is edited."""
    def __init__(self, snapshot, persist):
        self.queries = snapshot['queries']
        self.values = snapshot['values']
        self.prompt = snapshot['task_template']
        self.names = parameter_names(self.prompt, query_parameter_names(self.queries))
        self.persist = persist
        self.commands = compile_queries(self.queries, self.names, self.values) if self.queries else []

    def planning_instruction(self):
        return ('Prepare the complete set of reusable DAX queries for this template in one plan. '
                'Bind each task parameter as a scalar expression {{name}} without DAX quotes; use VALUE({{name}}) '
                'for numeric expressions. For dates, use VALUE({{name.year}}), VALUE({{name.month}}) and '
                'VALUE({{name.day}}) as appropriate. Keep identifiers and column aliases fixed. '
                'Use stable titles without parameter values. Task and actual first-run values:\n'
                + encode({'task':self.prompt, 'parameters':self.values}))

    def freeze(self, queries):
        self.queries = prepare_queries(queries, self.prompt, self.values)
        # Preserve the first definition even when a parameter still needs an explicit
        # mapping. Editing the saved code is then possible without asking the planner again.
        self.queries = self.persist(self.queries)
        self.commands = compile_queries(self.queries, self.names, self.values)
        return self.commands


class AnalysisTemplates:
    def __init__(self, db, powerbi, chat, settings):
        self.db, self.powerbi, self.chat, self.settings = db, powerbi, chat, settings
        self.lock = threading.RLock()
        # A process restart cannot resume its old generation threads.
        db.execute("UPDATE analysis_template_runs SET status='error',error='上次运行已中断，请查看已保留的结果' WHERE status='running'")
        # Take a copy before a user can delete a legacy template's source conversation.
        for row in db.all(TEMPLATE_SELECT+""" WHERE t.queries_json IS NULL AND
                (t.source_message_id IS NOT NULL OR EXISTS(SELECT 1 FROM analysis_template_runs r WHERE r.template_id=t.id))"""):
            self.restore_queries(self.unpack(row))
        self.upgrade_saved_tasks()

    def upgrade_saved_tasks(self):
        with self.db.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('PRAGMA user_version').fetchone()[0] >= 2:
                return
            rows = db.execute("""SELECT id,source_message_id,prompt,defaults_json,queries_json FROM analysis_templates
                                 WHERE source_message_id IS NOT NULL AND instr(prompt, ?) > 0""", ('\n\n',)).fetchall()
            for row in rows:
                defaults = json.loads(row['defaults_json'])
                prompt = single_turn_legacy_prompt(db, row['source_message_id'], row['prompt'], defaults)
                if prompt != row['prompt']:
                    queries = json.loads(row['queries_json'] or '[]')
                    names = parameter_names(prompt, query_parameter_names(queries))
                    defaults = {key:value for key,value in defaults.items() if key in names}
                    db.execute("""UPDATE analysis_templates SET prompt=?,defaults_json=?,revision=revision+1,updated_at=?
                                  WHERE id=?""", (prompt, encode(defaults), now(), row['id']))
            db.execute('PRAGMA user_version=2')

    def unpack(self, row, *, detail=True):
        row = dict(row)
        row['defaults'] = json.loads(row.pop('defaults_json'))
        queries = json.loads(row.pop('queries_json') or '[]')
        row['parameters'] = parameter_names(row['prompt'], query_parameter_names(queries))
        previous = json.loads(row.pop('last_values_json') or '{}')
        row['last_values'] = {name: previous[name] for name in row['parameters'] if previous.get(name)}
        row['query_count'] = len(queries)
        if detail:
            row['queries'] = queries
            # The form, task preview and DAX execution share these starting values.
            row['run_values'] = {name: row['defaults'].get(name, '').strip() or row['last_values'].get(name, '').strip()
                                 for name in row['parameters']}
        return row

    def get(self, template_id):
        with self.lock:
            row = self.db.one(TEMPLATE_SELECT + ' WHERE t.id=?', (template_id,))
            if not row:
                raise HTTPException(404, '分析模板不存在')
            template = self.unpack(row)
            if not template['queries']:
                self.restore_queries(template)
            return template

    def catalog(self):
        return [self.unpack(row, detail=False) for row in self.db.all(TEMPLATE_SELECT + ' ORDER BY t.updated_at DESC')]

    def source_records(self, message_id):
        # Read definitions only. Use the last successful attempt for each Q label
        # in its original position, including when an earlier attempt failed.
        rows=self.db.all("""SELECT id,model_id,json_extract(result,'$.label') AS label,
                json_extract(result,'$.scope') AS scope,json_extract(result,'$.columns') AS columns_json,
                json_extract(result,'$.template_dax') AS template_dax,
                title AS executed_title,coalesce(json_extract(result,'$.template_title'),title) AS title,
                coalesce(json_extract(result,'$.template_dax'),dax) AS dax FROM analysis_queries
                WHERE message_id=? AND json_extract(result,'$.ok')=1 ORDER BY created_at,rowid""",(message_id,))
        return list({q['label'] or q['id']:q for q in rows}.values())

    def restore_queries(self, template):
        """Adopt existing evidence from an earlier app version or a resolved first-run clarification."""
        queries = self.source_records(template['source_message_id']) if template.get('source_message_id') else []
        samples = template['defaults']
        if not queries:
            source = self.db.one("""SELECT m.id,r.snapshot FROM analysis_template_runs r
                JOIN messages m ON m.conversation_id=r.conversation_id AND m.role='assistant' AND m.status='complete'
                WHERE r.template_id=? AND json_extract(r.snapshot,'$.revision')=?
                AND EXISTS(SELECT 1 FROM analysis_queries q WHERE q.message_id=m.id AND json_extract(q.result,'$.ok')=1)
                ORDER BY r.created_at DESC,r.rowid DESC,m.rowid LIMIT 1""",(template['id'],template['revision']))
            if source:
                queries = self.source_records(source['id'])
                samples = json.loads(source['snapshot'])['values']
        if queries:
            queries = prepare_queries(queries,template['prompt'],samples)
            self.db.execute('UPDATE analysis_templates SET queries_json=? WHERE id=? AND revision=? AND queries_json IS NULL',
                            (encode(queries),template['id'],template['revision']))
            template.update(self.get(template['id']))

    def scan(self, body):
        model = self.db.one("""SELECT fingerprint, (SELECT json_group_array(json_extract(m.value,'$.name'))
            FROM json_each(models.context,'$.measures') m) AS measures FROM models WHERE id=?""", (body.model_id,))
        if not model:
            raise HTTPException(404, '模型不存在')
        checked = self.db.all("""SELECT literal,column_name FROM scope_value_hints
            WHERE model_id=? AND fingerprint=? AND instr(?,literal)>0""",
            (body.model_id, model['fingerprint'], body.prompt.casefold()))
        source_values = []
        if body.source_message_id:
            # Clarifications can precede the final reply. Use only adopted values from
            # this model in the source conversation up through that reply.
            rows = self.db.all("""
                SELECT DISTINCT json_extract(f.result,'$.column') AS name,
                       coalesce(json_extract(f.result,'$.confirmed_value'),json_extract(f.result,'$.adopted_value')) AS value
                FROM messages source JOIN messages m ON m.conversation_id=source.conversation_id AND m.rowid<=source.rowid
                JOIN field_observations f ON f.message_id=m.id
                WHERE source.id=? AND json_extract(f.result,'$.model_id')=? AND value IS NOT NULL
            """, (body.source_message_id, body.model_id))
            source_values = [(row['value'], row['name']) for row in rows if isinstance(row['value'], str) and row['name']]
        source_terms = {value.casefold() for value, _ in source_values}
        field_values = source_values + [(row['literal'], row['column_name']) for row in checked if row['literal'] not in source_terms]
        try:
            queries = [q.model_dump() for q in body.queries]
            declared = set(query_parameter_names(queries))
            names = parameter_names(body.prompt, declared)
            samples = {name: body.samples[name] for name in names if body.samples.get(name)}
            columns = (self.chat.semantics.summary(body.model_id, model['fingerprint'])['search_names']
                       if self.chat.semantics else ())
            candidates = recognize_parameters(body.prompt, json.loads(model['measures']), field_values,
                                              columns=columns, query_fields=query_columns(queries), samples=samples)
            if queries and candidates:
                _, options, automatic = query_suggestions(queries,{c['name']:c['value'] for c in candidates})
                for candidate in candidates:
                    name = candidate['name']
                    roles = options.get(name, {})
                    bindable = name in automatic
                    field = candidate.get('field')
                    if field and roles:
                        key = binding_field(*field).casefold()
                        if key in roles:
                            candidate['binding'] = key
                            bindable = True
                        elif any(role.endswith(']') for role in roles):
                            bindable = False
                    candidate['bindable'] = bindable or name in declared
                    if roles and not candidate['bindable']:
                        candidate['options'] = roles
            return {'candidates':candidates}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def seed(self, message_id):
        message = self.db.one('SELECT rowid AS position,* FROM messages WHERE id=?', (message_id,))
        if not message:
            raise HTTPException(404, '分析回复不存在')
        conv = self.chat.conversation(message['conversation_id'])
        queries = self.source_records(message_id)
        if conv['mode'] != 'analysis' or message['role'] != 'assistant' or message['status'] != 'complete' or not queries:
            raise HTTPException(409, '请从已完成且取得真实查询结果的分析保存模板')
        user = self.db.one("""SELECT content FROM messages WHERE conversation_id=? AND role='user' AND rowid<?
                              ORDER BY rowid DESC LIMIT 1""", (conv['id'], message['position']))
        prompt = user['content'] if user else ''
        scopes = list(dict.fromkeys(q['scope'] for q in queries if q['scope'] and q['scope'] not in SAVED_QUERY_SCOPE))
        defaults = {}
        run = self.db.one("""SELECT r.snapshot,t.source_message_id FROM analysis_template_runs r
                             LEFT JOIN analysis_templates t ON t.id=r.template_id WHERE r.conversation_id=?""", (conv['id'],))
        if run and any(q['template_dax'] for q in queries):
            snapshot = json.loads(run['snapshot'])
            # Restore placeholders only for the turn that actually ran the saved task.
            if prompt == snapshot['prompt']:
                prompt = snapshot.get('task_template', prompt)
                if run['source_message_id']:
                    with self.db.connection() as db:
                        prompt = single_turn_legacy_prompt(db, run['source_message_id'], prompt, snapshot['values'])
            defaults = snapshot['values']
            scopes = [scope for scope in scopes if scope != snapshot['prompt']]
        name = suggested_template_name(queries, scopes)
        if scopes:
            heading = wording(*SCOPE_HEADINGS)
            prompt = '\n\n'.join(filter(None, (prompt, heading + '\n' + '\n'.join(scopes))))
        prompt = (prompt or name)[:12000]
        if defaults:
            used = set(query_parameter_names(queries))
            used.update(match[1].strip() for match in PARAMETER.finditer(prompt))
            defaults = {key:value for key,value in defaults.items() if key in used}
        return {'name': name, 'prompt': prompt, 'defaults': defaults,
                'queries':[{'title':q['title'],'dax':q['dax']} for q in queries], 'model_id':queries[0]['model_id'],
                'source_message_id': message_id}

    def save(self, body, template_id=None):
        with self.lock:
            queries = [q.model_dump() for q in body.queries] if body.queries is not None else []
            if template_id:
                old = self.get(template_id)
                if body.revision != old['revision']:
                    raise HTTPException(409, '模板已更新，请重新打开后编辑')
                if body.queries is None:
                    queries = old['queries']
                elif old['queries'] and not queries:
                    raise HTTPException(422, wording('请至少保留一条固定查询', 'Keep at least one saved query.'))
            else:
                model = self.powerbi.get(body.model_id)
                if model['source'] != 'desktop':
                    raise HTTPException(409, '分析模板需要关联真实 Desktop 模型')
                if body.source_message_id:
                    seed = self.seed(body.source_message_id)
                    if seed['model_id'] != model['id']:
                        raise HTTPException(409, '模板来源与所选模型不一致')
                    queries = queries or seed['queries']
            try:
                names = parameter_names(body.prompt, query_parameter_names(queries))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            if set(body.defaults) - set(names):
                raise HTTPException(422, wording('默认值包含模板中未使用的参数', 'A default value refers to an unused parameter.'))
            if queries:
                queries = prepare_queries(queries,body.prompt,{**body.defaults,**body.samples})
                compile_queries(queries,names)
            stored = encode(queries) if queries else None
            if template_id:
                changed=self.db.execute('UPDATE analysis_templates SET name=?,prompt=?,defaults_json=?,queries_json=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?',
                                        (body.name,body.prompt,encode(body.defaults),stored,now(),template_id,body.revision))
                if not changed:raise HTTPException(409,'模板已更新，请重新打开后编辑')
            else:
                template_id, stamp = uid(), now()
                self.db.execute('INSERT INTO analysis_templates (id,model_id,source_message_id,name,prompt,defaults_json,revision,created_at,updated_at,queries_json) VALUES (?,?,?,?,?,?,?,?,?,?)',
                                (template_id, model['id'], body.source_message_id, body.name, body.prompt,
                                 encode(body.defaults), 1, stamp, stamp,stored))
            return self.get(template_id)

    def delete(self, template_id, revision):
        with self.lock:
            if self.get(template_id)['revision'] != revision:
                raise HTTPException(409, '模板已更新，请刷新后再删除')
            if not self.db.execute('DELETE FROM analysis_templates WHERE id=? AND revision=?', (template_id,revision)):
                raise HTTPException(409,'模板已更新，请刷新后再删除')
        return {'ok': True}

    def existing_run(self, row, request):
        previous = json.loads(row['snapshot'])['request']
        previous.setdefault('language', 'zh-CN')
        if previous != request:
            raise HTTPException(409, '运行标识已用于另一次请求')
        return self.run_response(row)

    def pin_queries(self, snapshot, queries):
        request = snapshot['request']
        version = (request['template_id'], snapshot['revision'])
        with self.lock, self.db.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE analysis_templates SET queries_json=? WHERE id=? AND revision=? AND queries_json IS NULL',
                       (encode(queries), *version))
            current = db.execute('SELECT queries_json FROM analysis_templates WHERE id=? AND revision=?', version).fetchone()
            if current:
                queries = json.loads(current['queries_json'])
            snapshot['queries'] = queries
            db.execute('UPDATE analysis_template_runs SET snapshot=? WHERE id=?', (encode(snapshot), request['request_id']))
            return queries

    def execution(self, snapshot):
        return TemplateExecution(snapshot, lambda queries: self.pin_queries(snapshot, queries))

    def resume_analysis(self, conversation_id, body, saved):
        with self.lock:
            row = self.db.one('SELECT * FROM analysis_template_runs WHERE conversation_id=?', (conversation_id,))
            if not row:
                raise HTTPException(409, '原模板运行记录不可用，请重新运行模板')
            snapshot = json.loads(row['snapshot'])
            if not snapshot['queries']:
                current = self.db.one('SELECT queries_json FROM analysis_templates WHERE id=? AND revision=?',
                                      (row['template_id'], snapshot['revision']))
                if current and current['queries_json']:
                    self.pin_queries(snapshot,json.loads(current['queries_json']))
            source = self.chat.begin(conversation_id, body, self.settings, checkpoint=saved, template=self.execution(snapshot))
            self.db.execute("UPDATE analysis_template_runs SET status='running',error='' WHERE id=?", (row['id'],))
            return self.track(row, source)

    def continue_preparation(self, conversation_id, body):
        # Only an answer to an outstanding clarification can continue preparing
        # the first definition. Later, ordinary follow-ups are free-form analysis.
        run = self.db.one("""SELECT r.* FROM analysis_template_runs r
            JOIN message_clarifications c ON c.message_id=(
                SELECT id FROM messages WHERE conversation_id=r.conversation_id ORDER BY rowid DESC LIMIT 1)
            WHERE r.conversation_id=? AND json_array_length(r.snapshot,'$.queries')=0""", (conversation_id,))
        if not run:
            return None
        with self.lock:
            snapshot = json.loads(run['snapshot'])
            current = self.db.one('SELECT queries_json FROM analysis_templates WHERE id=? AND revision=?',
                                  (run['template_id'], snapshot['revision']))
            if current and current['queries_json']:
                self.pin_queries(snapshot, json.loads(current['queries_json']))
            source = self.chat.begin(conversation_id, body, self.settings, template=self.execution(snapshot))
            self.db.execute("UPDATE analysis_template_runs SET status='running',error='' WHERE id=?", (run['id'],))
            return self.track(run, source)

    def run(self, template_id, body):
        with self.lock:
            request = {'template_id': template_id, **body.model_dump()}
            existing = self.db.one('SELECT * FROM analysis_template_runs WHERE id=?', (body.request_id,))
            if existing:
                return self.existing_run(existing,request)
            template = self.get(template_id)
            if body.revision != template['revision']:
                raise HTTPException(409, '模板已更新，请重新打开后运行')
            source, target = self.powerbi.get(template['model_id']), self.powerbi.get(body.model_id)
            if target['source'] != 'desktop' or (source['id'] != target['id'] and not same_report(source['report'], target['report'])):
                raise HTTPException(409, f'请先连接模板所属报表“{source["name"]}”')
            prompt, values = expand(template, body.values)
            conversation_id, stamp = uid(), now()
            snapshot = {'request': request, 'name': template['name'], 'prompt': prompt,
                        'values': values, 'revision': template['revision'], 'model_id': target['id'],
                        'task_template':template['prompt'],'queries':template['queries']}
            with self.db.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                existing=db.execute('SELECT * FROM analysis_template_runs WHERE id=?',(body.request_id,)).fetchone()
                if existing:return self.existing_run(existing,request)
                current=db.execute('SELECT revision,queries_json FROM analysis_templates WHERE id=?',(template_id,)).fetchone()
                if not current or current['revision']!=body.revision:
                    raise HTTPException(409,'模板已更新，请重新打开后运行')
                if current['queries_json']:
                    snapshot['queries'] = json.loads(current['queries_json'])
                execution = self.execution(snapshot)
                if not execution.queries and db.execute("""SELECT 1 FROM analysis_template_runs WHERE template_id=?
                        AND status='running' AND json_extract(snapshot,'$.revision')=?""",(template_id,body.revision)).fetchone():
                    raise HTTPException(409,wording('正在为此模板准备固定查询，请稍后重跑', 'This template is preparing its saved queries. Try again shortly.'))
                db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?)',
                           (conversation_id, template['name'], 'analysis', target['id'], '', stamp, stamp))
                db.execute('INSERT INTO analysis_template_runs VALUES (?,?,?,?,?,?,?)',
                           (body.request_id, template_id, conversation_id, encode(snapshot), 'running', '', stamp))
            row = self.db.one('SELECT * FROM analysis_template_runs WHERE id=?', (body.request_id,))
            try:
                source_stream = self.chat.begin(conversation_id, MessageInput(content=prompt, language=body.language,
                    generation_id=body.request_id, llm_profile_id=body.llm_profile_id), self.settings,template=execution)
            except Exception as exc:
                self.failed(row, exc)
            else:
                task = BackgroundStream(self.track(row, source_stream))
                task.detached.set()  # The UI follows persisted snapshots, just like restored tasks.
            return self.run_response(row)

    def failed(self, row, error):
        detail = str(error.detail if isinstance(error, HTTPException) else '分析未能启动，请检查模型连接与服务设置')
        last = self.db.one('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1', (row['conversation_id'],))
        if not last:
            self.chat.images.save_user(row['conversation_id'], json.loads(row['snapshot'])['prompt'])
        if not last or last['role'] != 'assistant' or last['status'] not in ('error', 'cancelled'):
            self.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
                            (uid(), row['conversation_id'], 'assistant', detail, 'error', now()))
        # Pollers may open the conversation as soon as this status becomes terminal.
        # Publish it only after both the original task and visible failure are saved.
        self.db.execute("UPDATE analysis_template_runs SET status='error',error=? WHERE id=?", (detail, row['id']))

    def track(self, row, source):
        error = None
        status = 'finished'
        try:
            for line in source:
                event = json.loads(line)
                if event['type'] == 'error':
                    error = HTTPException(409, event['text'])
                if event['type'] == 'done' and event.get('partial'):
                    status = 'partial'
                yield line
            if error:
                self.failed(row, error)
            else:
                self.db.execute("UPDATE analysis_template_runs SET status=? WHERE id=?", (status,row['id']))
        except Exception as exc:
            self.failed(row, exc)
        finally:
            source.close()

    def run_response(self, row):
        return {'id': row['id'], 'conversation': self.chat.conversation(row['conversation_id'])}

    def runs(self, template_id):
        self.get(template_id)
        result = []
        for row in self.db.all('SELECT * FROM analysis_template_runs WHERE template_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100', (template_id,)):
            snapshot = json.loads(row.pop('snapshot'))
            messages = self.chat.messages(row['conversation_id'])
            last = messages[-1] if messages else {}
            if self.chat.generation_status(row['conversation_id'])['running']:
                status = 'running'
            elif last.get('status') in ('error', 'cancelled'):
                status = last['status']
            elif last.get('clarification') and not last['clarification']['answered']:
                status = 'clarification'
            elif last.get('analysis_resume'):
                status = 'partial'
            elif last.get('status') == 'complete':
                status = 'complete'
            else:
                status = 'error' if row['status'] == 'error' else 'running'
            result.append({**row, 'status': status, 'values': snapshot['values'], 'revision': snapshot['revision']})
        return result
