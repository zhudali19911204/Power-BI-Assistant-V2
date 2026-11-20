"""Portable business definitions. Model facts and local connection state stay with the destination."""
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import business_dictionary as business
from .database import now
from .reconnection import RELEASED_DETAIL
from .reports import schema_signature
from .semantic_diff import DEFINITION_KEYS
from .semantics import DefinitionFields, object_id, pending_build_ids


MAX_IMPORT_BYTES = 64 * 1024 * 1024
State = Literal['confirmed', 'candidate', 'unresolved']
Name = Annotated[str, Field(min_length=1)]
Signature = Annotated[str, Field(pattern='^[0-9a-f]{64}$')]


class Definition(DefinitionFields):
    state: State
    source: str
    reviewed_at: str | None = None

    @model_validator(mode='after')
    def confirmed_meaning(self):
        if self.state == 'confirmed' and not self.meaning.strip():
            raise ValueError('已确认的对象定义必须填写业务含义')
        return self


class Change(BaseModel):
    kind: Literal['added', 'modified']
    reasons: list[str]


class ObjectEntry(Definition):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    kind: Literal['table', 'column', 'measure']
    table: Name
    name: Name
    previous_definition: Definition | None = None
    change: Change | None = None

    @property
    def key(self):
        return object_id(self.kind, self.table, self.name)


class RecordState(BaseModel):
    state: State
    source: str
    reviewed_at: str | None = None
    updated_at: str
    stale_reason: str | None = None
    affected_object_ids: list[str] | None = None


class Overview(business.OverviewEdit, RecordState):
    pass


class Item(business.BusinessEdit, RecordState):
    id: str = Field(pattern='^[A-Za-z0-9_-]{1,64}$')


class SemanticFile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal['power-bi-assistant.semantic-layer']
    version: Literal[1]
    model_name: str
    dictionary_name: str = Field(default='', max_length=160)
    schema_signature: Signature
    metadata_signature: Signature | None = None
    entries: list[ObjectEntry]
    overview: Overview | None
    overview_pending: bool = False
    items: list[Item] = Field(max_length=500)

    @model_validator(mode='after')
    def unique_ids(self):
        if len({e.key for e in self.entries}) != len(self.entries):
            raise ValueError('文件中有重复的模型对象')
        if len({r.id for r in self.items}) != len(self.items):
            raise ValueError('文件中有重复的业务词条 ID')
        return self


def read_file(content):
    try:
        return SemanticFile.model_validate_json(content.removeprefix(b'\xef\xbb\xbf'))
    except ValidationError as exc:
        issue = exc.errors(include_input=False, include_context=False, include_url=False)[0]
        path = '.'.join(map(str, issue['loc']))
        if issue['type'] == 'json_invalid':
            detail = '文件不是有效的 UTF-8 JSON'
        elif path in ('format', 'version'):
            detail = '请选择受支持版本的 Power BI Assistant 语义层文件'
        else:
            detail = f'请检查 {path or "文件内容"}（{issue["msg"]}）'
        raise HTTPException(422, '无法导入语义层：' + detail) from exc


def _metadata_signature(profile):
    if profile['metadata_status'] == 'read':
        return schema_signature({e['id']: e['facts'] for e in profile['entries']})
    return None


def export_file(service, model_id):
    with service.lock:
        model = service.powerbi.get(model_id)
        if model.get('snapshot_released'):
            raise HTTPException(409, RELEASED_DETAIL)
        profile = service.get(model_id)
        keys = (*DEFINITION_KEYS, 'table', 'kind', 'name', 'previous_definition', 'change')
        data = profile['business_dictionary']
        return {'format': 'power-bi-assistant.semantic-layer', 'version': 1,
                'model_name': model.get('report', {}).get('name') or model['name'],
                'dictionary_name': profile.get('dictionary_name', ''),
                'schema_signature': schema_signature(model['context']), 'metadata_signature': _metadata_signature(profile),
                'entries': [{k: e[k] for k in keys if k in e} for e in profile['entries']],
                'overview': data['overview'], 'overview_pending': bool(profile.get('overview_pending')),
                'items': data['items']}


def import_file(service, model_id, document, *, expected=None):
    """Preview and apply use the same merge. Only apply persists, in one dictionary write."""
    with service.lock:
        model = service.powerbi.get(model_id)
        if model.get('snapshot_released'):
            raise HTTPException(409, RELEASED_DETAIL)
        if service.library.busy(model_id):
            raise HTTPException(409, '当前模型正在梳理或生成，请完成后再导入')
        profile = service.get(model_id)
        if expected is not None and expected != profile['updated_at']:
            raise HTTPException(409, '当前词典已更新，请重新选择文件预览后导入')
        metadata = _metadata_signature(profile)
        compatible = (document.schema_signature == schema_signature(model['context']) and
                      not (metadata and document.metadata_signature and metadata != document.metadata_signature))
        objects = {e['id']: e for e in profile['entries']}
        result = {'updated_at': profile['updated_at'], 'compatible': compatible, 'objects': 0, 'unmatched': 0,
                  'protected': 0, 'items': 0, 'overview': False, 'review': 0}
        imported = set()
        for entry in document.entries:
            current = objects.get(entry.key)
            if current is None:
                result['unmatched'] += 1
                continue
            if entry.state == 'unresolved' and not entry.previous_definition:
                continue
            if not compatible and (current.get('reviewed_at') or current['state'] == 'confirmed'):
                result['protected'] += 1
                continue
            definition = entry.model_dump(include=set(DEFINITION_KEYS))
            previous = entry.previous_definition.model_dump() if entry.previous_definition else None
            change = entry.change.model_dump() if entry.change else None
            if not compatible:
                previous = previous if entry.state == 'unresolved' else definition
                definition = Definition(state='unresolved', source='导入词典 · 待复核').model_dump()
                change = {'kind': 'modified', 'reasons': ['导入文件与当前模型结构或元数据不同']}
            current.update(definition)
            for key, value in [('previous_definition', previous), ('change', change)]:
                if value is None:
                    current.pop(key, None)
                else:
                    current[key] = value
            imported.add(entry.key)
            result['objects'] += 1
            result['review'] += current['state'] == 'unresolved'

        data = profile['business_dictionary']
        items = {r['id']: r for r in data['items']}
        restored = set()
        records = ([document.overview] if document.overview else []) + document.items
        for record in records:
            overview = isinstance(record, Overview)
            current = data['overview'] if overview else items.get(record.id)
            saved = record.model_dump(exclude={'revision', 'confirmed'}, exclude_none=True)
            saved['reviewed_at'] = record.reviewed_at
            refs = business.refs(saved)
            missing = refs - objects.keys()
            invalid = {f['object_id'] for f in saved.get('filters', [])
                       if f['object_id'] in objects and objects[f['object_id']]['kind'] != 'column'}
            if invalid:
                raise HTTPException(422, '词条的筛选映射只能引用字段')
            if not compatible or (missing and record.state != 'unresolved'):
                if current and (current.get('reviewed_at') or current['state'] == 'confirmed'):
                    result['protected'] += 1
                    continue
                saved.update(state='unresolved', stale_reason='导入文件与当前模型不一致，请核对后确认',
                             affected_object_ids=sorted(missing), updated_at=now())
            if overview:
                data['overview'] = saved
                profile['overview_pending'] = document.overview_pending or not compatible
                result['overview'] = True
            else:
                items[record.id] = saved
                restored.add(record.id)
                result['items'] += 1
            result['review'] += saved['state'] == 'unresolved'
        if len(items) > 500:
            raise HTTPException(422, '合并后业务词条超过 500 项，请先整理词条')
        if expected is None:
            return result
        if not (imported or result['overview'] or restored):
            return {'preview': result, 'profile': profile}
        data['items'] = list(items.values())
        data['deleted_ids'] = [key for key in data['deleted_ids'] if key not in restored]
        data['revision'] += 1
        if imported and not result['overview'] and not business.manual_overview(profile):
            profile['overview_pending'] = bool(data['overview']) or document.overview_pending
        if document.dictionary_name and not profile.get('dictionary_name'):
            profile.update(dictionary_name=document.dictionary_name, dictionary_name_source='inherited')
        if profile.get('rebuild_remaining'):
            profile['rebuild_remaining'] = [key for key in profile['rebuild_remaining'] if key not in imported]
        pending = pending_build_ids(profile)
        ready = not pending and (profile['metadata_status'] == 'read' or all(e['state'] == 'confirmed' for e in objects.values()))
        profile.update(pending_ids=pending, coverage=len(objects)-len(pending), status='ready' if ready else 'inventory',
                       stage=f'已导入语义层，{len(pending)} 项对象待梳理', error='',
                       has_history=bool(imported) or profile.get('has_history', False))
        service._store(model_id, profile, expected=expected)
        return {'preview': result, 'profile': profile}
