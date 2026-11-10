"""Compare stored model profiles using synthetic prompts only; never print credentials."""
import argparse
import hashlib
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

import httpx

from backend.analysis import plan_system, parse_plan, readonly_query
from backend.app import data_directory
from backend.cancellation import CancellationToken, cancellation_scope, close_cancelled_provider, close_on_cancel, check_cancelled
from backend.database import Database, encode, now, uid
from backend.engine.context.base import Column, Measure, ModelContext, Relationship
from backend.engine.llm.base import ChatMessage
from backend.engine.llm.diagnostics import GenerationReport, token_usage
from backend.settings import SettingsService
from backend.streaming import completion_events


def synthetic_analysis():
    ctx=ModelContext()
    facts=('Fact_Actual','Fact_Budget','Fact_Forecast')
    dimensions=('Dim_Date','Dim_Company','Dim_CostCenter','Dim_CostElement','Dim_Plant',
                'Dim_Material','Dim_Customer','Dim_Currency','Dim_Scenario','Dim_Vendor','Dim_Product')
    for table in facts:
        ctx.tables[table]=[Column(name,kind,table) for name,kind in (
            ('Date','datetime'),('CompanyCode','string'),('CostCenterCode','string'),('CostElementCode','string'),
            ('Amount','double'),('Quantity','double'),('Version','string'),('MaterialCode','string'))]
    for table in dimensions:
        fields=(('Date','datetime'),('Year','int64'),('YearMonth','string'),('Month','int64'),
                ('Quarter','string'),('MonthName','string'),('IsWorkingDay','boolean'),('Week','int64')) if table=='Dim_Date' else (
                ('Code','string'),('Name','string'),('Category','string'),('Group','string'),
                ('Region','string'),('Description','string'),('Status','string'),('ParentCode','string'))
        ctx.tables[table]=[Column(name,kind,table) for name,kind in fields]
    for table in facts:
        ctx.relationships += [Relationship(table,'Date','Dim_Date','Date'),
            Relationship(table,'CompanyCode','Dim_Company','Code'),
            Relationship(table,'CostCenterCode','Dim_CostCenter','Code'),
            Relationship(table,'CostElementCode','Dim_CostElement','Code')]
    ctx.measures=[Measure(name,table,f"SUM('{table}'[Amount])") for name,table in zip(
        ('Actual.Amount','Budget.Amount','Forecast.Amount'),facts)]
    ctx.date_tables=['Dim_Date']
    entries=[]
    for table,columns in ctx.tables.items():
        for column in columns:
            meaning=(f'{table}[{column.name}] 是本测试模型中真实声明的字段，类型为 {column.dtype}。'
                '所有字段名、公式和关系均为程序合成，只供接口性能测试，不代表任何真实报表。'
                '金额字段单位统一为人民币元，数量字段为件；日期字段表示业务发生日期。'
                '分类代码与名称用于描述业务归属，未读取分类实际值时不能虚构具体值。'
                '查询优先复用已有度量值，按维度聚合，不导出事实明细；无日期限制时覆盖测试模型全部数据。'
                '预算和预测具有独立版本，版本之间不得直接相加后与实际金额比较；需要时按版本分别展示。')
            entries.append({'table':table,'column':column.name,'meaning':meaning,'state':'confirmed'})
    system=plan_system(ctx)+'\n合成模型业务词典：\n'+encode(entries)
    prompt=('请对这个测试模型做整体分析的查询规划：先查实际、预算、预测的时间覆盖，'
            '再按年月对比三项已有金额指标，并查实际金额在成本要素类别和成本中心分组的主要贡献。'
            '不增加用户未指定的公司筛选，不合并预算或预测的不同版本。最多四条只读 DAX，'
            '只返回规定 JSON，暂不写分析结论。')
    return system,prompt


class Probe:
    def __init__(self,provider,tuning):
        self.provider=provider;self._client=provider._client;self.model=provider.model
        self.tuning=tuning;self.started=None;self.first_upstream=None;self.first_reasoning=None;self.first_content=None
        self.reasoning_chars=0;self.answer=''

    def stream_events(self,system,messages,**opts):
        self.started=time.monotonic()
        extra=self.provider._extra_body({},self.provider.enable_thinking,self.provider.base_url)
        kwargs={}
        if self.tuning=='thinking_off':extra=self.provider._extra_body({'enable_thinking':False},None,self.provider.base_url)
        elif self.tuning=='low_effort':kwargs['reasoning_effort']='low'
        stream=self._client.chat.completions.create(model=self.model,
            messages=self.provider._to_messages(system,messages),max_tokens=opts['max_tokens'],
            extra_body=extra or None,stream=True,stream_options={'include_usage':True},**kwargs)
        unregister=close_on_cancel(stream.close)
        try:
            yield {'type':'metadata','finish_reason_supported':True}
            for chunk in stream:
                check_cancelled();elapsed=time.monotonic()-self.started
                if self.first_upstream is None:self.first_upstream=elapsed
                yield {'type':'activity'}
                metadata={'type':'metadata','usage':token_usage(getattr(chunk,'usage',None)),
                          'request_id':getattr(chunk,'id',None)}
                if chunk.choices:
                    choice=chunk.choices[0];metadata['finish_reason']=choice.finish_reason
                    delta=choice.delta
                    reasoning=getattr(delta,'reasoning_content',None) or getattr(delta,'reasoning',None)
                    if isinstance(reasoning,str) and reasoning:
                        self.reasoning_chars+=len(reasoning);metadata['reasoning_received']=True
                        if self.first_reasoning is None:self.first_reasoning=elapsed
                    if delta.content:
                        if self.first_content is None:self.first_content=elapsed
                        self.answer+=delta.content
                        yield {'type':'delta','text':delta.content}
                yield metadata
        finally:unregister();stream.close()


def run(settings,profile,scenario,tuning,system,prompt,limit,timeout):
    provider=settings.provider(profile['id'])
    provider._client=provider._client.with_options(timeout=httpx.Timeout(connect=8,read=45,write=30,pool=8),max_retries=0)
    probe=Probe(provider,tuning);report=GenerationReport();token=CancellationToken(uid());error=None
    started=time.monotonic()
    try:
        with cancellation_scope(token):
            for _ in completion_events(probe,system,[ChatMessage('user',prompt)],max_tokens=limit,
                    report=report,timeout=timeout,idle_timeout=45):pass
    except Exception as exc:
        error=type(exc).__name__
    finally:
        token.cancel();close_cancelled_provider(probe)
    valid=bool(probe.answer.strip());query_count=None
    if scenario=='analysis' and valid:
        try:
            parsed=parse_plan(probe.answer)
            query_count=len(parsed.queries)
            for query in parsed.queries:readonly_query(query.dax)
        except Exception:valid=False
    elif scenario=='short':valid=probe.answer.strip()=='125'
    def rounded(number):return round(number,3) if number is not None else None
    return {'model':provider.model,'profile_id':profile['id'],'scenario':scenario,'tuning':tuning,
            'max_tokens':limit,'timeout':timeout,'input_chars':len(system)+len(prompt),
            'input_sha256':hashlib.sha256((system+'\n'+prompt).encode()).hexdigest(),
            'first_upstream_seconds':rounded(probe.first_upstream),
            'first_reasoning_seconds':rounded(probe.first_reasoning),'first_content_seconds':rounded(probe.first_content),
            'elapsed_seconds':rounded(time.monotonic()-started),'reasoning_char_count':probe.reasoning_chars,
            'valid_answer_or_plan':valid,'query_count':query_count,'error_type':error,'diagnostic':report.as_dict()}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--stage',choices=['short','default','tuned','all'],default='all')
    parser.add_argument('--timeout',type=int,default=120)
    args=parser.parse_args()
    logging.getLogger('httpx').setLevel(logging.WARNING)
    directory=data_directory();settings=SettingsService(Database(directory/'assistant.sqlite3'),directory)
    profiles=[p for p in settings.profiles() if p['ready'] and p['provider']=='openai_compat']
    # No parallel requests: avoid turning our own concurrency into a queueing confound.
    profiles.sort(key=lambda p:('10.61.' in p['base_url'], 'deepseek' not in p['model'],p['model']))
    system,prompt=synthetic_analysis()
    target=Path(__file__).resolve().parents[1]/'artifacts'/'model-benchmark.json'
    rows=json.loads(target.read_text(encoding='utf-8'))['results'] if target.exists() and args.stage!='all' else []
    stages=['short','default','tuned'] if args.stage=='all' else [args.stage]
    for stage in stages:
        for profile in profiles:
            scenario='short' if stage=='short' else 'analysis'
            tuning='current_profile' if stage!='tuned' else ('low_effort' if profile['model'].lower().startswith('glm-5.3') else 'thinking_off')
            print(json.dumps({'event':'start','model':profile['model'],'scenario':scenario,'tuning':tuning},ensure_ascii=True),flush=True)
            row=run(settings,profile,scenario,tuning,system if scenario=='analysis' else '只回答数字，不附加解释。',
                    prompt if scenario=='analysis' else '100+25等于多少？',4096 if scenario=='analysis' else 512,args.timeout)
            rows=[r for r in rows if (r['profile_id'],r['scenario'],r['tuning'])!=(row['profile_id'],scenario,tuning)]+[row]
            target.parent.mkdir(exist_ok=True)
            target.write_text(encode({'time':now(),'synthetic_only':True,'model_writes':0,'desktop_reads':0,
                                     'settings_changed':False,'concurrency':1,'results':rows}),encoding='utf-8')
            print(json.dumps({'event':'result',**row},ensure_ascii=True),flush=True)


if __name__=='__main__':main()
