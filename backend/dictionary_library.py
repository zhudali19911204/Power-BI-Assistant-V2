"""Catalog and compare saved dictionary revisions without changing native models."""
import copy
import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import business_dictionary as business
from .reports import normalize_name, normalize_path, schema_signature
from .database import encode, now


class DictionaryNameEdit(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    name:str=Field(min_length=1,max_length=160)
    updated_at:str|None=Field(default=None,max_length=64)

    @field_validator('name')
    @classmethod
    def single_line(cls,value):
        if any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError('词典名称请使用单行文字')
        return value


def display_name(row):
    return row['profile'].get('dictionary_name') or row['report'].get('name') or row['name']


def notice(profile):
    pending=sum(e['state']=='unresolved' for e in profile['entries'])
    data=profile.get('business_dictionary',{})
    records=[data.get('overview'),*data.get('items',[])]
    business_pending=sum(bool(r and r.get('state')=='unresolved') for r in records)
    linked=bool(profile.get('reuse') or profile.get('has_history') or business.has_knowledge(profile)
                or any(e['state'] in ('candidate','confirmed') for e in profile['entries']))
    if not linked:
        status,message='unlinked','尚未关联业务词典，可选择历史词典或建立新词典'
    elif profile.get('metadata_status')!='read':
        status,message='unverified','已有业务词典可用，完整模型元数据尚未核对，请重新检查'
    elif pending or business_pending:
        status,message='needs_update',f'业务词典需更新：{pending} 项对象待补充或更新，{business_pending} 项业务记录待复核'
    else:
        status,message='current','业务词典已与当前模型核对，可继续审阅候选定义'
    return {'status':status,'message':message,'pending_objects':pending,'pending_business':business_pending}


class DictionaryLibrary:
    def __init__(self,owner):self.owner=owner

    def busy(self,model_id):
        return (model_id in self.owner.jobs or model_id in self.owner.business.jobs
                or self.owner.in_use(model_id))

    def catalog(self,current_model_id=None):
        with self.owner.lock:
            current=self.owner.get(current_model_id) if current_model_id else None
            current_model=self.owner.powerbi.get(current_model_id) if current_model_id else None
            current_ids={e['id'] for e in current['entries']} if current else set()
            signature=schema_signature(current_model['context']) if current_model else None
            rows=self.owner._history('')
            result=[]
            for row in rows:
                p=row['profile'];report=row['report'];data=p.get('business_dictionary',{})
                group=(('path:'+normalize_path(report['path'])) if report.get('path') else
                       ('name:'+normalize_name(report['name'])) if report.get('name') else
                       'engine:'+(p.get('connection_identity') or row['model_id']))
                result.append({'model_id':row['model_id'],'name':display_name(row),
                    'report_name':report.get('name') or row['name'],
                    'saved_at':p.get('dictionary_content_updated_at',row['updated_at']),
                    'path':report.get('path',''),'group':group,'updated_at':row['updated_at'],
                    'total':len(p['entries']),'coverage':sum(e['state'] in ('candidate','confirmed') for e in p['entries']),
                    'confirmed':sum(e['state']=='confirmed' for e in p['entries']),
                    'business_count':len(data.get('items',[]))+bool(data.get('overview')),
                    'status':p['status'],'dictionary_notice':notice(p),'busy':self.busy(row['model_id']),
                    'is_current':row['model_id']==current_model_id,
                    'same_schema':row['signature']==signature if current else None,
                    'shared_objects':len(current_ids & {e['id'] for e in p['entries']}) if current else None})
            return {'dictionaries':result,'current_model_id':current_model_id,
                    'target_busy':bool(current_model_id and self.busy(current_model_id))}

    def source(self,model_id,source_model_id,expected=None):
        source=next((r for r in self.owner._history(model_id) if r['model_id']==source_model_id),None)
        if not source:raise HTTPException(404,'所选历史词典不存在、已删除或没有可复用的业务定义')
        if expected is not None and source['updated_at']!=expected:
            raise HTTPException(409,'所选历史词典已更新，请重新预览差异后关联')
        return source

    def preview(self,model_id,source_model_id):
        with self.owner.lock:
            destination=self.owner.get(model_id)
            source=self.source(model_id,source_model_id)
            model=self.owner.powerbi.get(model_id)
            result=self.owner._reuse_from(model,copy.deepcopy(destination),copy.deepcopy(source),
                'user_selection',destination.get('metadata_status')=='read',persist=False)
            changes=result.get('changes',{})
            return {'source_model_id':source_model_id,'source_name':display_name(source),
                'source_updated_at':source['updated_at'],'metadata_status':destination.get('metadata_status','pending'),
                'same_schema':schema_signature(model['context'])==source['signature'],
                'changes':changes,'dictionary_notice':notice(result),
                'preserved_reviews':sum(bool(e.get('reviewed_at')) for e in destination['entries']),
                'business_records':len(result['business_dictionary']['items'])+bool(result['business_dictionary']['overview'])}

    def rename(self,model_id,body):
        with self.owner.lock:
            row=self.owner.db.one('SELECT payload,updated_at FROM model_semantics WHERE model_id=?',(model_id,))
            if not row:raise HTTPException(404,'词典已删除或不存在')
            if self.busy(model_id):raise HTTPException(409,'词典正在梳理或用于生成，请完成后再重命名')
            if body.updated_at is not None and row['updated_at']!=body.updated_at:
                raise HTTPException(409,'词典刚刚更新，请刷新列表后再重命名')
            profile=json.loads(row['payload']);changed_at=now()
            # A name edit must not make an old semantic revision the newest source.
            profile.setdefault('dictionary_content_updated_at',row['updated_at'])
            profile.update(dictionary_name=body.name,dictionary_name_source='user',
                           dictionary_name_edited_at=changed_at,updated_at=changed_at)
            updated=self.owner.db.execute('UPDATE model_semantics SET payload=?,updated_at=? WHERE model_id=? AND updated_at=?',
                (encode(profile),changed_at,model_id,row['updated_at']))
            if not updated:raise HTTPException(409,'词典刚刚更新或已删除，请刷新列表后重试')
            return {'model_id':model_id,'name':body.name,'updated_at':changed_at}

    def delete(self,model_id,*,current_model_id=None,updated_at=None):
        with self.owner.lock:
            row=self.owner.db.one('SELECT payload,updated_at FROM model_semantics WHERE model_id=?',(model_id,))
            if not row:raise HTTPException(404,'历史词典已删除或不存在')
            if model_id==current_model_id:raise HTTPException(409,'这是当前模型使用的词典，请选择其他历史版本')
            if self.busy(model_id):raise HTTPException(409,'该词典正在梳理或用于生成，请完成任务后再删除')
            if updated_at is not None and row['updated_at']!=updated_at:
                raise HTTPException(409,'该词典刚刚更新，请刷新历史列表后再删除')
            deleted=self.owner.db.execute('DELETE FROM model_semantics WHERE model_id=? AND updated_at=?',
                                          (model_id,row['updated_at']))
            if not deleted:raise HTTPException(409,'该词典刚刚更新或已被删除，请刷新历史列表后重试')
            return {'deleted_model_id':model_id,'detail':'历史词典版本已删除；模型快照、历史对话及其他模型已关联的副本保留'}
