import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {mountAnalysisTemplates,templateParameters,templatePreview,saveTemplateButton} from '../frontend/analysis_templates.js';
import {createMessageRenderer} from '../frontend/messages.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
const initial={id:'template',name:'月度销售',model_id:'model',model_name:'销售报表',prompt:'分析 {{月份}} 的 {{部门}}，比较 {{月份}} 环比。',
  parameters:['月份','部门'],defaults:{月份:'2026-10',部门:'销售部'},revision:1};

async function fixture({seed=null,templates=null,candidates=[],review}={}) {
  const {document,window}=parseHTML('<html><body><section></section></body></html>');
  const root=document.querySelector('section'),calls=[],started=[],opened=[];
  let rows=structuredClone(templates??[initial]),waitRun=null,failRun=false,model={id:'model',source:'desktop',name:'销售报表'};
  async function api(path,options={}) {
    const body=options.body?JSON.parse(options.body):null;calls.push({path,...options,body});
    if(path==='/analysis-templates'&&!options.method)return structuredClone(rows);
    if(path==='/analysis-templates/parameters/recognize')return {candidates};
    if(path==='/analysis-templates/queries/preview')return review?review(body):
      {queries:body.queries.map(q=>({...q,dax:q.dax.replace('"0087"','{{Plant}}')})),unresolved:[]};
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
    const row=rows.find(item=>path==='/analysis-templates/'+item.id);
    if(row&&!options.method)return structuredClone(row);
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
    remember(id,values){rows.find(row=>row.id===id).last_values=values;},
    defer(){let release;waitRun=new Promise(resolve=>{release=resolve;});return release;},
    fail(value){failRun=value;},setModel(value){model=value;}};
}

test('editing binds a source value and rerunning changes only the request parameters',async()=>{
  const original={...initial,prompt:'Sales for {{Plant}}',parameters:['Plant'],defaults:{Plant:'0087'},
    queries:[{title:'Sales',dax:'EVALUATE FILTER(T,T[Plant]="0087")'}]};
  const f=await fixture({templates:[original]});
  await f.click('[data-template-edit]');
  assert.equal(f.$('[data-query-dax]').value,original.queries[0].dax);
  assert.equal(f.$('[data-template-query-editor]').hasAttribute('open'),false);
  assert.equal(f.$('[data-preview-bindings]'),null);
  const code='EVALUATE FILTER(T,T[Plant]={{Plant}})';
  f.input('[data-template-default="Plant"]','0090');
  await f.submit();
  assert.equal(f.calls.find(c=>c.method==='PUT').body.queries[0].dax,code);
  const binding=f.calls.find(c=>c.path.endsWith('/queries/preview')).body;
  assert.deepEqual(binding.samples,{Plant:'0087'});
  assert.deepEqual(f.calls.find(c=>c.method==='PUT').body.defaults,{Plant:'0090'});
  await f.click('[data-template-run]');
  const savedPreview=[...f.root.querySelectorAll('pre')].find(p=>p.textContent===code);
  assert.ok(savedPreview);
  assert.equal(f.$('[data-template-value="Plant"]').value,'0090');
  f.input('[data-template-value="Plant"]','0110');await f.submit();
  const run=f.calls.find(c=>c.method==='POST'&&c.path.endsWith('/runs'));
  assert.deepEqual(run.body.values,{Plant:'0110'});assert.equal(run.body.queries,undefined);
  assert.equal(savedPreview.textContent,code);
});

test('parameter expansion is repeatable, literal and validates malformed placeholders',()=>{
  assert.deepEqual(templateParameters('分析 {{ 月份 }}、{{部门}} 和 {{月份}}'),['月份','部门']);
  assert.equal(templatePreview('{{月份}} {{月份}}',{月份:'$& {{部门}}'}),'$& {{部门}} $& {{部门}}');
  assert.throws(()=>templateParameters('{{missing'));
  assert.throws(()=>templateParameters(Array.from({length:13},(_,i)=>`{{p${i}}}`).join(' ')));
  const dax=`// {{comment}}
    EVALUATE ROW("{{alias}}",{{Month.year}},"filter",{{Material}},"copy",{{Month.month}},"text","{{literal}}","amount",'{{Table}}'[{{Column}}])`;
  assert.deepEqual(templateParameters('Report for {{Month}}',[{dax}]),['Month','Material']);
});

test('DAX-only parameters show up once in the form and can be saved without editing the task',async()=>{
  const dax='EVALUATE ROW("material",{{Material}},"month",VALUE({{Month.month}}),"other",{{Material}})';
  const f=await fixture({seed:{name:'Prices',model_id:'model',prompt:'Review monthly prices.',defaults:{},
    queries:[{title:'Prices',dax}]}});
  assert.deepEqual([...f.root.querySelectorAll('[data-template-default]')].map(el=>el.dataset.templateDefault),['Material','Month']);
  f.input('[data-template-default="Material"]','XR40007');f.input('[data-template-default="Month"]','2026-10');
  await f.submit();
  const saved=f.calls.find(c=>c.path==='/analysis-templates'&&c.method==='POST').body;
  assert.equal(saved.prompt,'Review monthly prices.');assert.deepEqual(saved.defaults,{Material:'XR40007',Month:'2026-10'});
  assert.equal(saved.queries[0].dax,dax);
});

test('an ambiguous filter is chosen in its parameter row while the default and query edits are preserved',async()=>{
  const source={...initial,prompt:'Prices for {{Plant}}',parameters:['Plant'],defaults:{Plant:'0087'},
    queries:[{title:'Prices',dax:'EVALUATE FILTER(T,T[Plant]="0087" && T[Vendor]="0087")'}]};
  const options={'t[plant]':'T[Plant]','t[vendor]':'T[Vendor]'};
  const f=await fixture({templates:[source],review:({queries,choices})=>choices.Plant==='t[plant]'?
    {queries:queries.map(q=>({...q,dax:q.dax.replace('T[Plant]="0087"','T[Plant]={{Plant}}')})),unresolved:[]}:
    {queries,unresolved:[{name:'Plant',options}]}});
  await f.click('[data-template-edit]');
  f.input('[data-template-default="Plant"]','0001');await f.submit();
  assert.equal(f.calls.some(c=>c.method==='PUT'),false);
  const row=f.$('[data-parameter-row="Plant"]'),field=row.querySelector('select');
  assert.ok(field);assert.equal(row.querySelector('[data-template-default]').value,'0001');
  assert.equal(f.$('[data-template-query-editor]').hasAttribute('open'),false);
  field.querySelector('option[value="t[plant]"]').selected=true;
  field.dispatchEvent(new f.window.Event('change',{bubbles:true}));await f.submit();
  const saved=f.calls.find(c=>c.method==='PUT').body;
  assert.deepEqual(saved.defaults,{Plant:'0001'});
  assert.equal(saved.queries[0].dax,'EVALUATE FILTER(T,T[Plant]={{Plant}} && T[Vendor]="0087")');
});

test('a saved analysis offers filter values in one editable row each and leaves fixed identifiers out',async()=>{
  const prompt='物料 XR40007 的 Mov_Price';
  const seed={name:'物料价格',model_id:'model',source_message_id:'answer',prompt,defaults:{},
    queries:[{title:'价格',dax:'EVALUATE FILTER(T,T[Item]="XR40007" && [Mov_Price]>0)'}]};
  const f=await fixture({seed,candidates:[
    {name:'物料',value:'XR40007',spans:[[3,10]],bindable:true},
    {name:'指标',value:'Mov_Price',spans:[[13,22]],bindable:false},
  ]});
  assert.equal(f.root.querySelectorAll('[data-parameter-row]').length,1);
  const row=f.$('[data-parameter-row="物料"]');
  assert.ok(row.querySelector('input[type="checkbox"]').checked);
  assert.equal(row.querySelector('[data-template-default]').value,'XR40007');
  f.input('[data-template-default="物料"]','XR40099');await f.submit();
  const review=f.calls.find(c=>c.path.endsWith('/queries/preview')).body;
  const saved=f.calls.find(c=>c.path==='/analysis-templates'&&c.method==='POST').body;
  assert.deepEqual(review.samples,{物料:'XR40007'});assert.deepEqual(saved.defaults,{物料:'XR40099'});
  assert.equal(saved.prompt,'物料 {{物料}} 的 Mov_Price');
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
  await f.submit();const saved=f.calls.find(call=>call.path==='/analysis-templates'&&call.method==='POST').body;
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

test('run fields are editable on opening and whitespace uses the prefilled default',async()=>{
  const f=await fixture({templates:[{...initial,run_values:initial.defaults}]});await f.click('[data-template-run]');
  const fields=[...f.root.querySelectorAll('[data-template-value]')];
  assert.deepEqual(fields.map(field=>field.value),['2026-10','销售部']);
  assert.ok(fields.every(field=>!field.disabled&&!field.hasAttribute('readonly')));
  assert.equal(f.$('[data-template-adjust-values]'),null);
  f.input('[data-template-value="月份"]','   ');
  assert.equal(f.$('[data-template-preview]').textContent,'分析 2026-10 的 销售部，比较 2026-10 环比。');
  assert.equal(f.$('[data-template-run-status]').hidden,true);
  await f.submit();assert.deepEqual(f.calls.find(call=>call.method==='POST').body.values,initial.defaults);
});

test('rerun uses saved defaults and falls back only for blank defaults, even if another window runs it',async()=>{
  const f=await fixture({templates:[{...initial,defaults:{部门:'销售部'}}]});f.remember('template',{月份:'2026-12',部门:'物流部'});
  await f.click('[data-template-run]');
  const month=f.$('[data-template-value="月份"]'),department=f.$('[data-template-value="部门"]');
  assert.equal(month.value,'2026-12');assert.equal(department.value,'销售部');
  assert.ok(month.closest('label').textContent.includes('已带入上次取值'));
  assert.equal(month.hasAttribute('required'),false);
  f.input('[data-template-value="部门"]','研发');
  assert.equal(f.$('[data-template-preview]').textContent,'分析 2026-12 的 研发，比较 2026-12 环比。');
  f.remember('template',{月份:'2027-01',部门:'另一个窗口的修改'});
  await f.submit();assert.deepEqual(f.calls.find(call=>call.method==='POST').body.values,{月份:'2026-12',部门:'研发'});
  assert.equal(f.$('[data-template-value="月份"]').value,'2026-12');
  assert.equal(f.$('[data-template-value="部门"]').value,'销售部');
});

test('saving XR40010 prefills the run field and submits it unchanged despite a previous XR40009 run',async()=>{
  const f=await fixture({templates:[{...initial,prompt:'查看 {{物料}}。按 {{物料}} 筛选。',
    parameters:['物料'],defaults:{物料:'XR40009'},last_values:{物料:'XR40009'}}]});
  await f.click('[data-template-edit]');
  f.input('[data-template-default="物料"]','XR40010');
  const preview=f.$('[data-template-editor-preview]').textContent;
  assert.equal(preview,'查看 XR40010。按 XR40010 筛选。');
  await f.submit();await f.click('[data-template-run]');
  assert.equal(f.$('[data-template-value="物料"]').value,'XR40010');
  assert.equal(f.$('[data-template-preview]').textContent,preview);
  await f.submit();
  assert.deepEqual(f.calls.find(call=>call.path.endsWith('/runs')).body.values,{物料:'XR40010'});
});

test('a manual parameter without a default or previous value is required only on first use',async()=>{
  const f=await fixture({templates:[{...initial,defaults:{},last_values:{月份:'2026-12'}}]});await f.click('[data-template-run]');
  assert.equal(f.$('[data-template-value="月份"]').value,'2026-12');
  assert.equal(f.$('[data-template-value="部门"]').hasAttribute('required'),true);
  f.input('[data-template-value="部门"]','  ');await f.submit();
  assert.ok(f.$('[data-template-run-status]').textContent.includes('待填写：部门'));
  assert.ok(f.$('[data-template-notice]').textContent.includes('请填写参数：部门'));
  assert.equal(f.started.length,0);
  f.input('[data-template-value="部门"]','0');
  await f.submit();
  assert.deepEqual(f.calls.find(call=>call.method==='POST').body.values,{月份:'2026-12',部门:'0'});
  assert.equal(f.$('[data-template-value="部门"]').hasAttribute('required'),false);
});

test('bulk recognition on a saved analysis applies checked literals at their Unicode offsets on save',async()=>{
  const prompt='💡工厂0810在2026-5的费用，口径0810。按{{指标}}汇总';
  const candidates=[{name:'工厂',value:'0810',spans:[[3,7],[20,24]]},{name:'月份',value:'2026-5',spans:[[8,14]]}];
  const f=await fixture({candidates,seed:{model_id:'model',source_message_id:'answer',name:'费用',prompt,defaults:{指标:'实际费用'}}});
  assert.equal(f.$('[data-template-parameter-count]').textContent,'3 个可填写参数');
  assert.equal(f.$('[data-template-default="月份"]').value,'2026-5');
  assert.equal(f.$('[data-template-apply-candidates]'),null);
  const month=f.$('[data-template-candidate="1"]');month.checked=false;month.dispatchEvent(new f.window.Event('change',{bubbles:true}));
  assert.equal(f.$('[data-template-default="月份"]').disabled,true);
  assert.equal(f.$('[data-template-parameter-count]').textContent,'2 个可填写参数');
  await f.submit();
  const scan=f.calls[0],saved=f.calls.find(call=>call.path==='/analysis-templates'&&call.method==='POST').body;
  assert.deepEqual(scan.body,{prompt,model_id:'model',source_message_id:'answer',samples:{指标:'实际费用'}});
  assert.equal(saved.prompt,'💡工厂{{工厂}}在2026-5的费用，口径{{工厂}}。按{{指标}}汇总');
  assert.deepEqual(saved.defaults,{工厂:'0810',指标:'实际费用'});
});

test('recognized year aliases share one field from the editor through rerun', async()=>{
  const prompt='分析 {{Year}}。口径 Fact_MB51[Year]=2026。';
  const start=prompt.indexOf('2026');
  const seed={name:'发料',model_id:'model',source_message_id:'answer',prompt,defaults:{Year:'2026'},
    queries:[{title:'发料',dax:'EVALUATE FILTER(Fact_MB51,Fact_MB51[Year]=2026)'}]};
  const f=await fixture({seed,candidates:[{name:'Year',value:'2026',spans:[[start,start+4]],
    bindable:true,binding:'fact_mb51[year]'}],
    review:({queries})=>({queries:queries.map(q=>({...q,dax:q.dax.replace('2026','VALUE({{Year}})')})),unresolved:[]})});
  assert.equal(f.root.querySelectorAll('[data-parameter-row]').length,1);
  assert.deepEqual(f.calls[0].body.samples,{Year:'2026'});
  f.input('[data-template-default="Year"]','2027');
  assert.equal(f.$('[data-template-editor-preview]').textContent,'分析 2027。口径 Fact_MB51[Year]=2027。');
  await f.submit();
  const preview=f.calls.find(c=>c.path.endsWith('/queries/preview')).body;
  assert.deepEqual(preview.samples,{Year:'2026'});assert.deepEqual(preview.choices,{Year:'fact_mb51[year]'});
  await f.click('[data-template-run="new"]');
  assert.equal(f.root.querySelectorAll('[data-template-value]').length,1);
  f.input('[data-template-value="Year"]','2028');
  assert.equal(f.$('[data-template-preview]').textContent,'分析 2028。口径 Fact_MB51[Year]=2028。');
  await f.submit();
  assert.deepEqual(f.calls.find(c=>c.path.endsWith('/runs')).body.values,{Year:'2028'});
});

test('recognition can reuse a parameter at the limit using its last run value',async()=>{
  const marked=Array.from({length:11},(_,i)=>'{{p'+i+'}}').join(' ');
  const prompt=marked+' {{Year}}，口径 [Year]=2026';
  const f=await fixture({seed:{name:'已有模板',model_id:'model',source_message_id:'answer',prompt,
    defaults:{},last_values:{Year:'2026'}},
    candidates:[{name:'Year',value:'2026',spans:[[prompt.length-4,prompt.length]]},
      {name:'额外参数',value:'2026',spans:[]}]});
  assert.deepEqual(f.calls[0].body.samples,{Year:'2026'});
  assert.equal(f.$('[name="prompt"]').value,marked+' {{Year}}，口径 [Year]={{Year}}');
  assert.equal(f.$('[data-template-default="Year"]').value,'');
  assert.equal(f.root.querySelectorAll('[data-parameter-row]').length,12);
  assert.equal(f.$('[data-template-default="额外参数"]'),null);
});

test('changing text invalidates bulk suggestions and an existing fixed template can adopt all in one edit',async()=>{
  const prompt='费用 0887',f=await fixture({templates:[{...initial,prompt,parameters:[],defaults:{}}],
    candidates:[{name:'编号',value:'0887',spans:[[3,7]]}]});
  await f.click('[data-template-edit]');await f.click('[data-template-recognize]');
  f.input('[name="prompt"]','其他任务');assert.equal(f.$('[data-template-candidate]'),null);
  f.input('[name="prompt"]',prompt);await f.click('[data-template-recognize]');
  assert.equal(f.$('[data-template-default="编号"]').value,'0887');assert.equal(f.$('[data-template-editor-preview]').textContent,prompt);
  f.input('[data-template-default="编号"]','0257');await f.submit();
  const updated=f.calls.find(call=>call.method==='PUT').body;
  assert.equal(updated.prompt,'费用 {{编号}}');assert.deepEqual(updated.defaults,{编号:'0257'});
});

test('a new template offers all candidates beyond the remaining slots and saves only the selected subset',async()=>{
  const marked=Array.from({length:11},(_,i)=>`{{p${i}}}`).join(' '),prompt=marked+' 工厂0810在2026年';
  const candidates=[{name:'工厂',value:'0810'},{name:'年份',value:'2026'}].map(item=>{
    const start=prompt.indexOf(item.value);return {...item,spans:[[start,start+item.value.length]]};
  });
  const f=await fixture({candidates});await f.click('[data-template-new]');
  f.input('[name="name"]','新任务');f.input('[name="prompt"]',prompt);await f.click('[data-template-recognize]');
  const [factory,year]=f.root.querySelectorAll('[data-template-candidate]');
  assert.equal(factory.checked,true);assert.equal(year.checked,false);
  year.checked=true;year.dispatchEvent(new f.window.Event('change',{bubbles:true}));
  assert.ok(f.$('[data-template-parameter-notice]').textContent.includes('最多还能添加 1 项'));
  assert.equal(year.checked,false);
  factory.checked=false;factory.dispatchEvent(new f.window.Event('change',{bubbles:true}));
  const available=f.$('[data-template-candidate="1"]');available.checked=true;
  available.dispatchEvent(new f.window.Event('change',{bubbles:true}));await f.submit();
  const saved=f.calls.find(call=>call.path==='/analysis-templates'&&call.method==='POST').body;
  assert.equal(saved.prompt,marked+' 工厂0810在{{年份}}年');assert.equal(saved.defaults.年份,'2026');
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
