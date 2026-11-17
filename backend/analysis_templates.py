"""Reusable analysis instructions. Each run queries live data in its own conversation."""
import json
import re
import threading
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .database import encode, now, uid
from .reports import same_report
from .schemas import MessageInput
from .streaming import BackgroundStream


PARAMETER = re.compile(r'\{\{\s*([\w\u4e00-\u9fff][\w\u4e00-\u9fff -]{0,39}?)\s*\}\}')
Value = Annotated[str, Field(max_length=500)]


def parameter_names(prompt):
    names = list(dict.fromkeys(m.group(1).strip() for m in PARAMETER.finditer(prompt)))
    remainder = PARAMETER.sub('', prompt)
    if '{{' in remainder or '}}' in remainder or len(names) > 12:
        raise ValueError('参数请使用 {{月份}} 这样的名称，最多 12 项，每个名称最多 40 字')
    return names


class TemplateEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=12000)
    defaults: dict[str, Value] = Field(default_factory=dict, max_length=12)
    revision: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def valid_parameters(self):
        names = parameter_names(self.prompt)
        if set(self.defaults) - set(names):
            raise ValueError('默认值包含未在任务中使用的参数')
        return self


class TemplateCreate(TemplateEdit):
    model_id: str
    source_message_id: str | None = None


class TemplateRun(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_id: str
    revision: int = Field(ge=1)
    values: dict[str, Value] = Field(default_factory=dict, max_length=12)
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    llm_profile_id: str | None = None


def expand(template, supplied):
    names = parameter_names(template['prompt'])
    if set(supplied) - set(names):
        raise HTTPException(422, '包含模板未定义的参数')
    values = {**template['defaults'], **supplied}
    missing = [name for name in names if not values.get(name, '').strip()]
    if missing:
        raise HTTPException(422, '请填写参数：' + '、'.join(missing))
    # A single substitution pass: values are plain text, never DAX/code interpolation.
    text = PARAMETER.sub(lambda match: values[match.group(1).strip()].strip(), template['prompt'])
    if len(text) > 24000:
        raise HTTPException(422, '替换参数后的任务过长，请缩短模板或参数值')
    return text, {name: values[name].strip() for name in names}


class AnalysisTemplates:
    def __init__(self, db, powerbi, chat, settings):
        self.db, self.powerbi, self.chat, self.settings = db, powerbi, chat, settings
        self.lock = threading.RLock()
        # A process restart cannot resume its old generation threads.
        db.execute("UPDATE analysis_template_runs SET status='error',error='上次运行已中断，请查看已保留的结果' WHERE status='running'")

    def unpack(self, row):
        row = dict(row)
        row['defaults'] = json.loads(row.pop('defaults_json'))
        row['parameters'] = parameter_names(row['prompt'])
        row['model_name'] = self.powerbi.get(row['model_id'])['name']
        return row

    def get(self, template_id):
        row = self.db.one('SELECT * FROM analysis_templates WHERE id=?', (template_id,))
        if not row:
            raise HTTPException(404, '分析模板不存在')
        return self.unpack(row)

    def catalog(self):
        return [self.unpack(row) for row in self.db.all('SELECT * FROM analysis_templates ORDER BY updated_at DESC')]

    def seed(self, message_id):
        message = self.db.one('SELECT rowid AS position,* FROM messages WHERE id=?', (message_id,))
        if not message:
            raise HTTPException(404, '分析回复不存在')
        conv = self.chat.conversation(message['conversation_id'])
        queries = self.chat.analysis.records(message_id)
        if conv['mode'] != 'analysis' or message['role'] != 'assistant' or message['status'] != 'complete' or not any(q['ok'] for q in queries):
            raise HTTPException(409, '请从已完成且取得真实查询结果的分析保存模板')
        # Keep the request and scope; never copy old result rows into a new analysis.
        users = self.db.all("SELECT content FROM messages WHERE conversation_id=? AND role='user' AND rowid<? ORDER BY rowid DESC LIMIT 8",
                            (conv['id'], message['position']))
        scopes = list(dict.fromkeys(q['scope'] for q in queries if q.get('scope') and q['ok']))
        prompt = '\n\n'.join(row['content'] for row in reversed(users))
        if scopes:
            prompt += '\n\n分析口径（保存前请核对）：\n' + '\n'.join(scopes)
        return {'name': conv['title'][:120], 'prompt': prompt[:12000], 'defaults': {},
                'model_id': self.db.one('SELECT model_id FROM analysis_queries WHERE message_id=? ORDER BY rowid LIMIT 1', (message_id,))['model_id'],
                'source_message_id': message_id}

    def save(self, body, template_id=None):
        with self.lock:
            if template_id:
                old = self.get(template_id)
                if body.revision != old['revision']:
                    raise HTTPException(409, '模板已更新，请重新打开后编辑')
                self.db.execute('UPDATE analysis_templates SET name=?,prompt=?,defaults_json=?,revision=revision+1,updated_at=? WHERE id=?',
                                (body.name, body.prompt, encode(body.defaults), now(), template_id))
            else:
                model = self.powerbi.get(body.model_id)
                if model['source'] != 'desktop':
                    raise HTTPException(409, '分析模板需要关联真实 Desktop 模型')
                if body.source_message_id and self.seed(body.source_message_id)['model_id'] != model['id']:
                    raise HTTPException(409, '模板来源与所选模型不一致')
                template_id, stamp = uid(), now()
                self.db.execute('INSERT INTO analysis_templates VALUES (?,?,?,?,?,?,?,?,?)',
                                (template_id, model['id'], body.source_message_id, body.name, body.prompt,
                                 encode(body.defaults), 1, stamp, stamp))
            return self.get(template_id)

    def delete(self, template_id, revision):
        with self.lock:
            if self.get(template_id)['revision'] != revision:
                raise HTTPException(409, '模板已更新，请刷新后再删除')
            self.db.execute('DELETE FROM analysis_templates WHERE id=?', (template_id,))
        return {'ok': True}

    def run(self, template_id, body):
        with self.lock:
            request = {'template_id': template_id, **body.model_dump()}
            existing = self.db.one('SELECT * FROM analysis_template_runs WHERE id=?', (body.request_id,))
            if existing:
                if json.loads(existing['snapshot'])['request'] != request:
                    raise HTTPException(409, '运行标识已用于另一次请求')
                return self.run_response(existing)
            template = self.get(template_id)
            if body.revision != template['revision']:
                raise HTTPException(409, '模板已更新，请重新打开后运行')
            source, target = self.powerbi.get(template['model_id']), self.powerbi.get(body.model_id)
            if target['source'] != 'desktop' or (source['id'] != target['id'] and not same_report(source['report'], target['report'])):
                raise HTTPException(409, f'请先连接模板所属报表“{source["name"]}”')
            prompt, values = expand(template, body.values)
            conversation_id, stamp = uid(), now()
            self.db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?)',
                            (conversation_id, template['name'], 'analysis', target['id'], '', stamp, stamp))
            snapshot = {'request': request, 'name': template['name'], 'prompt': prompt,
                        'values': values, 'revision': template['revision'], 'model_id': target['id']}
            self.db.execute('INSERT INTO analysis_template_runs VALUES (?,?,?,?,?,?,?)',
                            (body.request_id, template_id, conversation_id, encode(snapshot), 'running', '', stamp))
            row = self.db.one('SELECT * FROM analysis_template_runs WHERE id=?', (body.request_id,))
            try:
                source_stream = self.chat.begin(conversation_id, MessageInput(content=prompt,
                    generation_id=body.request_id, llm_profile_id=body.llm_profile_id), self.settings)
            except Exception as exc:
                self.failed(row, exc)
            else:
                task = BackgroundStream(self.track(row, source_stream))
                task.detached.set()  # The UI follows persisted snapshots, just like restored tasks.
            return self.run_response(row)

    def failed(self, row, error):
        detail = str(error.detail if isinstance(error, HTTPException) else '分析未能启动，请检查模型连接与服务设置')
        self.db.execute("UPDATE analysis_template_runs SET status='error',error=? WHERE id=?", (detail, row['id']))
        last = self.db.one('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1', (row['conversation_id'],))
        if not last:
            self.chat.images.save_user(row['conversation_id'], json.loads(row['snapshot'])['prompt'])
        if not last or last['role'] != 'assistant' or last['status'] not in ('error', 'cancelled'):
            self.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
                            (uid(), row['conversation_id'], 'assistant', detail, 'error', now()))

    def track(self, row, source):
        error = None
        try:
            for line in source:
                event = json.loads(line)
                if event['type'] == 'error':
                    error = HTTPException(409, event['text'])
                yield line
            if error:
                self.failed(row, error)
            else:
                self.db.execute("UPDATE analysis_template_runs SET status='finished' WHERE id=?", (row['id'],))
        except Exception as exc:
            self.failed(row, exc)
        finally:
            source.close()

    def run_response(self, row):
        return {'id': row['id'], 'conversation': self.chat.conversation(row['conversation_id'])}

    def runs(self, template_id):
        self.get(template_id)
        result = []
        for row in self.db.all('SELECT * FROM analysis_template_runs WHERE template_id=? ORDER BY created_at DESC LIMIT 100', (template_id,)):
            snapshot = json.loads(row.pop('snapshot'))
            messages = self.chat.messages(row['conversation_id'])
            last = messages[-1] if messages else {}
            if self.chat.generation_status(row['conversation_id'])['running']:
                status = 'running'
            elif last.get('status') in ('error', 'cancelled'):
                status = last['status']
            elif last.get('clarification') and not last['clarification']['answered']:
                status = 'clarification'
            elif last.get('status') == 'complete':
                status = 'complete'
            else:
                status = 'error' if row['status'] == 'error' else 'running'
            result.append({**row, 'status': status, 'values': snapshot['values'], 'revision': snapshot['revision']})
        return result
