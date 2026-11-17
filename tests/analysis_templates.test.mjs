import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {mountAnalysisTemplates,templateParameters,templatePreview,saveTemplateButton} from '../frontend/analysis_templates.js';
import {createMessageRenderer} from '../frontend/messages.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
const initial={id:'template',name:'月度销售',model_id:'model',model_name:'销售报表',prompt:'分析 {{月份}} 的 {{部门}}，比较 {{月份}} 环比。',
  parameters:['月份','部门'],defaults:{月份:'2026-10',部门:'销售部'},revision:1};

async function fixture({seed=null,templates=null}={}) {
  const {document,window}=parseHTML('<html><body><section></section></body></html>');
  const root=document.querySelector('section'),calls=[],started=[],opened=[];
  let rows=structuredClone(templates??[initial]),waitRun=null,failRun=false,model={id:'model',source:'desktop',name:'销售报表'};
  async function api(path,options={}) {
    const body=options.body?JSON.parse(options.body):null;calls.push({path,...options,body});
    if(path==='/analysis-templates'&&!options.method)return structuredClone(rows);
    if(path==='/analysis-templates'&&options.method==='POST') {
      const row={...body,id:'new',revision:1,model_name:model.name,parameters:templateParameters(body.prompt)};rows.push(row);return row;
    }
    if(options.method==='PUT'){
      rows=rows.map(row=>row.id===path.split('/').at(-1)?{...row,...body,parameters:templateParameters(body.prompt),revision:row.revision+1}:row);return rows[0];
    }
    if(options.method==='DELETE'){rows=rows.filter(row=>row.id!==path.split('/').at(-1).split('?')[0]);return {ok:true};}
    if(path.endsWith('/runs')&&options.method==='POST') {
      if(waitRun)await waitRun;
      if(failRun)throw new Error('网络连接中断');
      return {id:body.request_id,conversation:{id:'run-conversation',mode:'analysis',model_id:body.model_id,title:'月度销售'}};
    }
    if(path.endsWith('/runs'))return [{conversation_id:'old-conversation',status:'complete',values:{月份:'2026-09'},revision:1,created_at:'2026-10-01T00:00:00Z'}];
    throw new Error('Unexpected request '+path);
  }
  const manager=await mountAnalysisTemplates(root,{api,seed,getModel:()=>model,getService:()=>({ready:true,active_profile_id:'llm'}),
    onStarted:(...args)=>started.push(args),onOpen:id=>opened.push(id)});
  const $=selector=>root.querySelector(selector);
  async function click(selector){$(selector).click();await tick();await tick();}
  function input(selector,value){$(selector).value=value;$(selector).dispatchEvent(new window.Event('input',{bubbles:true}));}
  function select(start,end=start){const text=$('[name="prompt"]');text.selectionStart=start;text.selectionEnd=end;text.setSelectionRange=(start,end)=>{text.selectionStart=start;text.selectionEnd=end;};}
  async function submit(){const form=$('form');form.reportValidity=()=>true;form.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();}
  return {root,window,$,calls,started,opened,manager,click,input,select,submit,
    defer(){let release;waitRun=new Promise(resolve=>{release=resolve;});return release;},
    fail(value){failRun=value;},setModel(value){model=value;}};
}

test('parameter expansion is repeatable, literal and validates malformed placeholders',()=>{
  assert.deepEqual(templateParameters('分析 {{ 月份 }}、{{部门}} 和 {{月份}}'),['月份','部门']);
  assert.equal(templatePreview('{{月份}} {{月份}}',{月份:'$& {{部门}}'}),'$& {{部门}} $& {{部门}}');
  assert.throws(()=>templateParameters('{{missing'));
  assert.throws(()=>templateParameters(Array.from({length:13},(_,i)=>`{{p${i}}}`).join(' ')));
});

test('new template saves parameter defaults, edits persist and list can be searched',async()=>{
  const f=await fixture();await f.click('[data-template-new]');
  f.input('[name="name"]','费用月报');
  f.input('[name="prompt"]','分析 {{月份}} 的费用，按 {{部门}} 汇总');
  f.input('[data-template-default="月份"]','2026-11');
  f.input('[data-template-default="部门"]','研发');
  f.input('[name="prompt"]','分析 {{月份}} 的费用，按 {{部门}} 汇总，比较环比');
  assert.equal(f.$('[data-template-default="部门"]').value,'研发');
  await f.submit();
  const saved=f.calls.find(call=>call.method==='POST').body;
  assert.deepEqual(saved.defaults,{月份:'2026-11',部门:'研发'});
  assert.equal(saved.model_id,'model');
  f.input('[data-template-search]','费用');
  assert.equal(f.$('[data-template-card="template"]').hidden,true);
  await f.click('[data-template-edit="new"]');
  f.input('[name="name"]','费用月报更新');await f.submit();
  assert.equal(f.calls.find(call=>call.method==='PUT').body.revision,1);
  assert.ok(f.root.textContent.includes('费用月报更新'));
});

test('run previews parameter changes and suppresses double submission',async()=>{
  const f=await fixture();await f.click('[data-template-run]');
  f.input('[data-template-value="月份"]','2026-11');
  f.input('[data-template-value="部门"]','<img src=x onerror=alert(1)>');
  assert.equal(f.$('[data-template-preview]').querySelector('img'),null);
  assert.ok(f.$('[data-template-preview]').textContent.includes('2026-11'));
  const release=f.defer();await f.submit();await f.submit();
  assert.equal(f.calls.filter(call=>call.method==='POST').length,1);
  f.setModel({id:'another',name:'另一个报表',source:'desktop'});
  release();await tick();await tick();
  assert.equal(f.started[0][1].id,'model');
  assert.equal(f.calls.find(call=>call.method==='POST').body.values.月份,'2026-11');
});

test('retry after uncertain transport failure reuses request ID and preserves parameters',async()=>{
  const f=await fixture();await f.click('[data-template-run]');f.fail(true);
  f.input('[data-template-value="月份"]','2026-12');await f.submit();
  assert.ok(f.root.textContent.includes('网络连接中断'));
  assert.equal(f.$('[data-template-value="月份"]').value,'2026-12');
  f.fail(false);await f.submit();
  const requests=f.calls.filter(call=>call.method==='POST');
  assert.equal(requests[0].body.request_id,requests[1].body.request_id);
  assert.equal(f.started.length,1);
});

test('closing manager during a run submission does not reopen it or lose the running task',async()=>{
  const f=await fixture();await f.click('[data-template-run]');
  const release=f.defer();await f.submit();f.manager.dispose();release();await tick();await tick();
  assert.equal(f.started.length,1);assert.equal(f.started[0][2],false);
});

test('history opens the saved conversation and deletion requires its explicit button',async()=>{
  const f=await fixture();await f.click('[data-template-history]');
  assert.ok(f.root.textContent.includes('2026-09'));await f.click('[data-template-open-run]');
  assert.deepEqual(f.opened,['old-conversation']);await f.click('[data-template-back]');
  await f.click('[data-template-delete]');assert.equal(f.calls.some(call=>call.method==='DELETE'),false);
  await f.click('[data-template-delete-confirm]');assert.equal(f.$('[data-template-card]'),null);
});

test('save action becomes visible when streamed analysis finishes without new query records',()=>{
  const {document}=parseHTML('<html><body><section></section></body></html>');
  const root=document.querySelector('section');root.scrollTo=()=>{};
  const renderer=createMessageRenderer({root,scroll:root,label:()=>'',content:m=>m.content,artifacts:saveTemplateButton,status:()=>''});
  const message={id:'answer',role:'assistant',content:'已查询',status:'streaming',analysis:[{ok:true}]};
  renderer.render([message],{conversation:'run'});
  assert.equal(root.querySelector('[data-save-analysis-template]'),null);
  message.status='complete';renderer.render([message],{conversation:'run'});
  assert.ok(root.querySelector('[data-save-analysis-template]'));
});

test('fixed tasks are explained instead of showing a misleading zero count and can run without parameters',async()=>{
  const fixed={...initial,prompt:'分析本年度的销售趋势',parameters:[],defaults:{}};
  const f=await fixture({templates:[fixed]});
  assert.ok(f.$('.template-card').textContent.includes('固定任务'));
  assert.equal(f.$('.template-card').textContent.includes('0 个参数'),false);
  await f.click('[data-template-run]');assert.equal(f.$('[data-template-value]'),null);
  assert.ok(f.$('[data-template-run-status]').textContent.includes('无需填写参数'));
  assert.equal(f.$('[data-template-preview]').textContent,fixed.prompt);
  await f.submit();assert.deepEqual(f.calls.find(call=>call.method==='POST').body.values,{});
  await f.click('[data-template-back]');await f.click('[data-template-edit]');
  assert.equal(f.$('[data-template-parameter-empty]').hidden,false);
  await f.submit();assert.ok(f.$('[data-template-notice]').textContent.includes('按固定任务重跑'));
});

test('turning selected values into parameters preserves the literal default and original source metadata',async()=>{
  const prompt='0810 工厂在 2026 年哪个月 MPV 最差？';
  const f=await fixture({seed:{name:'工厂分析',model_id:'model',source_message_id:'answer',prompt,defaults:{}}});
  f.select(0,4);await f.click('[data-template-insert-parameter="工厂"]');
  assert.equal(f.$('[name="prompt"]').value,'{{工厂}} 工厂在 2026 年哪个月 MPV 最差？');
  assert.equal(f.$('[data-template-default="工厂"]').value,'0810');
  const start=f.$('[name="prompt"]').value.indexOf('2026');f.select(start,start+4);
  await f.click('[data-template-insert-parameter="年份"]');
  assert.equal(f.$('[data-template-parameter-count]').textContent,'2 个可填写参数');
  assert.equal(f.$('[data-template-editor-preview]').textContent,prompt);
  await f.submit();const saved=f.calls.find(call=>call.method==='POST').body;
  assert.deepEqual(saved.defaults,{工厂:'0810',年份:'2026'});assert.equal(saved.source_message_id,'answer');
});

test('custom parameters insert at the caret, Enter does not save the form and repeated parameters keep their defaults',async()=>{
  const f=await fixture({seed:{name:'指标分析',model_id:'model',prompt:'比较 MPV 与 MPV',defaults:{}}});
  f.select(3,6);f.input('[data-template-parameter-name]','指标');
  const event=new f.window.Event('keydown',{bubbles:true,cancelable:true});Object.assign(event,{key:'Enter'});
  f.$('[data-template-parameter-name]').dispatchEvent(event);
  assert.equal(event.defaultPrevented,true);assert.equal(f.$('[name="prompt"]').value,'比较 {{指标}} 与 MPV');
  f.input('[data-template-default="指标"]','销量');
  const start=f.$('[name="prompt"]').value.lastIndexOf('MPV');f.select(start,start+3);
  f.input('[data-template-parameter-name]','指标');await f.click('[data-template-add-parameter]');
  assert.equal(f.$('[name="prompt"]').value,'比较 {{指标}} 与 {{指标}}');
  assert.equal(f.$('[data-template-default="指标"]').value,'销量');
  assert.equal(f.root.querySelectorAll('[data-template-default]').length,1);
  f.select(0);await f.click('[data-template-insert-parameter="年份"]');
  assert.equal(f.$('[name="prompt"]').value,'{{年份}}比较 {{指标}} 与 {{指标}}');
  assert.equal(f.$('[data-template-default="年份"]').value,'');
  assert.equal(f.calls.length,0);
});

test('invalid names, overlapping placeholders and parameter limits leave the task unchanged',async()=>{
  const f=await fixture({seed:{name:'分析',model_id:'model',prompt:'分析 {{年份}}',defaults:{年份:'2026'}}});
  f.select(3,9);await f.click('[data-template-insert-parameter="月份"]');
  assert.equal(f.$('[name="prompt"]').value,'分析 {{年份}}');
  assert.ok(f.$('[data-template-parameter-notice]').textContent.includes('不要选中已有参数'));
  f.input('[data-template-parameter-name]','<img>');await f.click('[data-template-add-parameter]');
  assert.equal(f.$('[name="prompt"]').value,'分析 {{年份}}');assert.equal(f.root.querySelector('img'),null);
  const full=Array.from({length:12},(_,i)=>`{{参数${i}}}`).join(' ');f.input('[name="prompt"]',full);f.select(full.length);
  await f.click('[data-template-insert-parameter="年份"]');
  assert.equal(f.$('[name="prompt"]').value,full);
  assert.ok(f.$('[data-template-parameter-notice]').textContent.includes('最多 12 项'));
});

test('temporary task edits preserve defaults and only active parameters are saved',async()=>{
  const f=await fixture();await f.click('[data-template-edit]');
  const month=f.$('[data-template-default="月份"]');
  f.input('[name="prompt"]',initial.prompt+' 请解释主要变化');
  assert.equal(f.$('[data-template-default="月份"]'),month);
  f.input('[name="prompt"]','分析 {{月份} 的销售');
  assert.equal(f.$('[name="prompt"]').getAttribute('aria-invalid'),'true');
  await f.submit();assert.equal(f.calls.some(call=>call.method==='PUT'),false);
  f.input('[name="prompt"]','分析最新销售');assert.equal(f.$('[data-template-default]'),null);
  f.input('[name="prompt"]','分析 {{月份}} 的销售');
  assert.equal(f.$('[data-template-default="月份"]').value,'2026-10');
  assert.equal(f.$('[name="prompt"]').hasAttribute('aria-invalid'),false);
  await f.submit();assert.deepEqual(f.calls.find(call=>call.method==='PUT').body.defaults,{月份:'2026-10'});
});

test('run guidance counts missing values, blocks whitespace and restores template defaults',async()=>{
  const f=await fixture();await f.click('[data-template-run]');
  assert.ok(f.$('[data-template-run-status]').textContent.includes('2 / 2'));
  f.input('[data-template-value="月份"]','   ');
  assert.ok(f.$('[data-template-run-status]').textContent.includes('待填写：月份'));
  await f.submit();assert.equal(f.calls.some(call=>call.method==='POST'),false);
  assert.ok(f.$('[data-template-notice]').textContent.includes('请填写参数：月份'));
  f.input('[data-template-value="部门"]','其他部门');await f.click('[data-template-reset-values]');
  assert.equal(f.$('[data-template-value="月份"]').value,'2026-10');
  assert.equal(f.$('[data-template-value="部门"]').value,'销售部');
  assert.ok(f.$('[data-template-run-status]').textContent.includes('2 / 2'));
  await f.submit();assert.deepEqual(f.calls.find(call=>call.method==='POST').body.values,initial.defaults);
});

test('editor previews and parameter chips treat values as text and ignore inherited object properties',async()=>{
  assert.equal(templatePreview('{{constructor}}',{}),'{{constructor}}');
  const f=await fixture({seed:{name:'分析',model_id:'model',prompt:'分析 {{指标}}',defaults:{指标:'<img src=x onerror=1>'}}});
  assert.equal(f.$('[data-template-editor-preview]').textContent,'分析 <img src=x onerror=1>');
  assert.equal(f.root.querySelector('img,[onerror]'),null);
  f.input('[data-template-default="指标"]','实际金额');assert.equal(f.$('[data-template-editor-preview]').textContent,'分析 实际金额');
  const g=await fixture();assert.deepEqual([...g.root.querySelectorAll('.template-parameter-chip')].map(node=>node.textContent),['月份','部门']);
});

test('IME Enter in the parameter name does not insert or submit',async()=>{
  const f=await fixture({seed:{name:'分析',model_id:'model',prompt:'固定任务',defaults:{}}});
  f.input('[data-template-parameter-name]','年份');
  const event=new f.window.Event('keydown',{bubbles:true,cancelable:true});Object.assign(event,{key:'Enter',isComposing:true,keyCode:229});
  f.$('[data-template-parameter-name]').dispatchEvent(event);
  assert.equal(event.defaultPrevented,true);assert.equal(f.$('[name="prompt"]').value,'固定任务');assert.equal(f.calls.length,0);
});
