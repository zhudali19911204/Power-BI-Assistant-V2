import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {mountSemanticWorkbench,semanticSummary} from '../frontend/semantics.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
function entry(id,extra={}) {return {id,kind:'column',table:'成本中心',name:'CC Number',facts:{dtype:'string',
  description:'模型记录的文本编号',relationships:[]},business_name:'成本中心编号',meaning:'根据字段名推测，业务含义待确认',
  role:'编号候选',unit:'',aliases:[],state:'candidate',source:'模型建议',reviewed_at:null,...extra};}
function profile(extra={}) {return {model_id:'model',status:'ready',stage:'语义梳理完成',error:'',coverage:2,
  entries:[entry('one'),entry('two',{kind:'measure',table:'Fact',name:'Actual Amount',state:'unresolved',meaning:'',facts:{expression:'SUM(Fact[Amount])'}})],...extra};}

async function fixture(initial=profile()) {
  const {document,window}=parseHTML('<html><body><dialog><section></section></dialog></body></html>');
  const root=document.querySelector('section'),dialog=document.querySelector('dialog'),calls=[],updates=[],previews=[];let current=structuredClone(initial);
  // Model the browser clamping the dialog's scroll offset as its long list is
  // replaced with shorter content. LinkeDOM itself has no layout engine.
  let proto=root,html;
  while(proto&&!html){html=Object.getOwnPropertyDescriptor(proto,'innerHTML');proto=Object.getPrototypeOf(proto);}
  Object.defineProperty(root,'innerHTML',{configurable:true,get(){return html.get.call(this);},
    set(value){html.set.call(this,value);dialog.scrollTop=0;}});
  const api=async(path,opts={})=>{
    calls.push({path,...opts});
    if(path==='/settings')return {name:'模型服务',model:'test-model',base_url:'https://service.example/v1',active_profile_id:'selected'};
    if(opts.method==='PUT'){
      const body=JSON.parse(opts.body),id=path.split('/').at(-1);
      const e=current.entries.find(e=>e.id===id);Object.assign(e,body,{state:body.confirmed?'confirmed':'candidate'});
      return structuredClone(current);
    }
    if(path.endsWith('/build')){current={...current,status:'building',stage:'正在梳理'};return structuredClone(current);}
    if(path.endsWith('/reuse')){current={...current,status:'ready',stage:'已复用本地词典',coverage:current.entries.length};return structuredClone(current);}
    return structuredClone(current);
  };
  const manager=await mountSemanticWorkbench(root,{api,modelId:'model',onUpdate:p=>updates.push(p),onPreview:(...a)=>previews.push(a)});
  async function click(selector){root.querySelectorAll('form').forEach(f=>f.reportValidity=()=>true);root.querySelector(selector).click();await tick();}
  return {root,dialog,window,calls,updates,previews,manager,click};
}

test('dictionary distinguishes model facts, candidate meaning and confirmed business definitions',async()=>{
  const f=await fixture();
  try {
    assert.equal(f.root.querySelectorAll('[data-semantic-entry]').length,2);
    assert.ok(f.root.textContent.includes('候选含义 · 待确认'));
    assert.ok(f.root.textContent.includes('业务含义未知'));
    assert.ok(f.root.textContent.includes('模型记录的文本编号'));
    assert.ok(f.root.textContent.includes('单位：未知 / 未确认'));
    assert.ok(f.root.textContent.includes('https://service.example/v1'));
  } finally {f.manager.dispose();}
});

test('business meaning can be edited and explicitly confirmed without replacing other fields',async()=>{
  const f=await fixture();
  try {
    await f.click('[data-edit-semantic="one"]');
    f.root.querySelector('[name="meaning"]').value='业务定义：成本中心文本编号，保留前导零';
    f.root.querySelector('[name="aliases"]').value='成本中心，费用中心';
    await f.click('[data-confirm-semantic]');
    const call=f.calls.find(c=>c.method==='PUT'),body=JSON.parse(call.body);
    assert.equal(call.path,'/models/model/semantics/one');
    assert.equal(body.confirmed,true);assert.deepEqual(body.aliases,['成本中心','费用中心']);
    assert.ok(f.root.textContent.includes('用户已确认'));
    assert.equal(f.updates.at(-1).confirmed,1);
    assert.equal(f.root.querySelectorAll('[data-semantic-entry]').length,2);
  } finally {f.manager.dispose();}
});

for(const action of ['back','save','confirm'])test(`returning from semantic edit via ${action} keeps the list position and expanded facts`,async()=>{
  const f=await fixture();
  try {
    const search=f.root.querySelector('[data-semantic-search]');search.value='成本中心';
    search.dispatchEvent(new f.window.Event('input',{bubbles:true}));
    const pending=f.root.querySelector('[data-semantic-pending]');pending.checked=action!=='confirm';
    pending.dispatchEvent(new f.window.Event('change',{bubbles:true}));
    f.root.querySelector('[data-semantic-entry="one"] details').setAttribute('open','');
    f.dialog.scrollTop=680;
    await f.click('[data-edit-semantic="one"]');
    assert.equal(f.dialog.scrollTop,0);
    f.dialog.scrollTop=125;
    if(action==='back')await f.click('[data-semantic-back]');
    else if(action==='confirm')await f.click('[data-confirm-semantic]');
    else {
      const form=f.root.querySelector('form');form.reportValidity=()=>true;
      form.dispatchEvent(new f.window.Event('submit',{bubbles:true,cancelable:true}));await tick();
    }
    assert.equal(f.dialog.scrollTop,680);
    assert.equal(f.root.querySelector('[data-semantic-search]').value,'成本中心');
    assert.equal(f.root.querySelector('[data-semantic-pending]').hasAttribute('checked'),action!=='confirm');
    assert.ok(f.root.querySelector('[data-semantic-entry="one"] details').hasAttribute('open'));
    assert.equal(f.calls.some(call=>call.method==='PUT'),action!=='back');
  } finally {f.manager.dispose();}
});

test('confirming a pending-only entry keeps the filter and returns to remaining entries',async()=>{
  const f=await fixture(profile({entries:[entry('one'),entry('two',{name:'Next Code'})]}));
  try {
    const pending=f.root.querySelector('[data-semantic-pending]');pending.checked=true;
    pending.dispatchEvent(new f.window.Event('change',{bubbles:true}));f.dialog.scrollTop=480;
    await f.click('[data-edit-semantic="one"]');await f.click('[data-confirm-semantic]');
    assert.equal(f.dialog.scrollTop,480);
    assert.equal(f.root.querySelector('[data-semantic-pending]').hasAttribute('checked'),true);
    assert.equal(f.root.querySelector('[data-semantic-entry="one"]'),null);
    assert.ok(f.root.querySelector('[data-semantic-entry="two"]'));
  } finally {f.manager.dispose();}
});

test('save feedback keeps the same visible entry anchored as the header grows',async()=>{
  const f=await fixture(),proto=f.window.HTMLElement.prototype;
  const original=Object.getOwnPropertyDescriptor(proto,'getBoundingClientRect');
  Object.defineProperty(proto,'getBoundingClientRect',{configurable:true,value(){
    if(this===f.dialog)return {top:0,bottom:600};
    if(this.hasAttribute('data-semantic-entry')){
      const index=[...this.parentElement.children].indexOf(this);
      const feedback=f.root.querySelector('[data-semantic-feedback]');
      const top=240+index*250+(feedback&&!feedback.hidden?40:0)-f.dialog.scrollTop;
      return {top,bottom:top+250};
    }
    return {top:0,bottom:0};
  }});
  try {
    f.dialog.scrollTop=360;
    const before=f.root.querySelector('[data-semantic-entry="one"]').getBoundingClientRect().top;
    await f.click('[data-edit-semantic="one"]');await f.click('[data-confirm-semantic]');
    assert.equal(f.root.querySelector('[data-semantic-entry="one"]').getBoundingClientRect().top,before);
    assert.equal(f.dialog.scrollTop,400);
  } finally {
    f.manager.dispose();
    if(original)Object.defineProperty(proto,'getBoundingClientRect',original);else delete proto.getBoundingClientRect;
  }
});

test('background semantic refresh keeps the list position and expanded facts',async()=>{
  const f=await fixture(profile({status:'building'}));
  try {
    f.dialog.scrollTop=720;
    f.root.querySelector('[data-semantic-entry="one"] details').setAttribute('open','');
    await new Promise(resolve=>setTimeout(resolve,1850));
    assert.ok(f.calls.filter(call=>call.path==='/models/model/semantics').length>=2);
    assert.equal(f.dialog.scrollTop,720);
    assert.ok(f.root.querySelector('[data-semantic-entry="one"] details').hasAttribute('open'));
  } finally {f.manager.dispose();}
});

test('search and pending-only filter operate on names, meanings and aliases',async()=>{
  const f=await fixture(profile({entries:[entry('one',{state:'confirmed'}),entry('two',{name:'Material Code',business_name:'物料编号',meaning:'物料文本标识'})]}));
  try {
    const input=f.root.querySelector('[data-semantic-search]');input.value='物料';input.dispatchEvent(new f.window.Event('input',{bubbles:true}));
    assert.equal(f.root.querySelectorAll('[data-semantic-entry]').length,1);
    assert.ok(f.root.querySelector('[data-semantic-entry]').textContent.includes('Material Code'));
    input.value='';input.dispatchEvent(new f.window.Event('input',{bubbles:true}));
    const pending=f.root.querySelector('[data-semantic-pending]');pending.checked=true;pending.dispatchEvent(new f.window.Event('change',{bubbles:true}));
    assert.equal(f.root.querySelectorAll('[data-semantic-entry]').length,1);
  } finally {f.manager.dispose();}
});

test('explicit build action pins the displayed destination model and exposes background status',async()=>{
  const f=await fixture(profile({status:'inventory',coverage:0}));
  try {
    assert.equal(f.calls.filter(c=>c.path.endsWith('/build')).length,0);
    await f.click('[data-build-semantics]');
    const call=f.calls.find(c=>c.path.endsWith('/build'));
    assert.deepEqual(JSON.parse(call.body),{llm_profile_id:'selected'});
    assert.equal(f.root.querySelector('[data-build-semantics]').disabled,true);
    assert.ok(f.root.textContent.includes('正在后台梳理'));
  } finally {f.manager.dispose();}
});

test('real values remain accessible and dictionary text cannot create executable HTML',async()=>{
  const f=await fixture(profile({entries:[entry('one',{meaning:'<img src=x onerror=1>',business_name:'<script>name</script>',facts:{description:'<iframe src=evil>'}})]}));
  try {
    assert.equal(f.root.querySelector('img,script,iframe'),null);
    assert.ok(f.root.textContent.includes('<img src=x onerror=1>'));
    await f.click('[data-semantic-preview="one"]');
    assert.deepEqual(f.previews,[['成本中心','CC Number','model']]);
  } finally {f.manager.dispose();}
});

test('summary never conflates inferred entries with user-confirmed definitions',()=>{
  assert.deepEqual(semanticSummary(profile()),{status:'ready',stage:'语义梳理完成',coverage:2,total:2,confirmed:0,can_answer:true,pending:1});
});

test('a matched report displays its saved name and path without starting another build',async()=>{
  const f=await fixture(profile({report_identity:{name:'采购费用',path:'E:\\Reports\\采购费用.pbix'},
    stage:'已复用报表“采购费用”的语义词典 · 2 项'}));
  try {
    assert.ok(f.root.textContent.includes('采购费用.pbix'));
    assert.ok(f.root.textContent.includes('已复用报表'));
    assert.equal(f.root.querySelector('[data-semantic-history]'),null);
    assert.equal(f.calls.filter(c=>c.path.endsWith('/build')).length,0);
  } finally {f.manager.dispose();}
});

test('legacy dictionary selection reuses a concrete saved model without calling build',async()=>{
  const f=await fixture(profile({status:'inventory',coverage:0,reuse_candidates:[
    {model_id:'saved-dictionary',name:'采购费用',path:'E:\\Reports\\采购费用.pbix',coverage:2,total:2}]}));
  try {
    f.root.querySelector('[data-semantic-history] option').setAttribute('selected','');
    await f.click('[data-reuse-semantics]');
    const call=f.calls.find(c=>c.path.endsWith('/reuse'));
    assert.equal(call.path,'/models/model/semantics/reuse');
    assert.deepEqual(JSON.parse(call.body),{source_model_id:'saved-dictionary'});
    assert.equal(f.updates.at(-1).status,'ready');
    assert.ok(f.root.textContent.includes('已关联本地历史词典'));
    assert.equal(f.root.querySelector('[data-semantic-history]'),null);
    assert.equal(f.calls.filter(c=>c.path.endsWith('/build')).length,0);
  } finally {f.manager.dispose();}
});

test('incremental workbench lists changes and only offers to build pending objects',async()=>{
  const f=await fixture(profile({status:'inventory',coverage:1,has_history:true,entries:[entry('one'),entry('two',{
    name:'New Category',state:'unresolved',meaning:'',change:{kind:'added',reasons:['新增对象']}})],
    changes:{preserved:1,added:1,modified:0,removed:1,items:[
      {id:'two',kind:'column',table:'成本中心',name:'New Category',change:'added',reasons:['新增对象']},
      {id:'removed',kind:'column',table:'成本中心',name:'<Old>',change:'removed',reasons:['对象已删除']}]}}));
  try {
    assert.ok(f.root.textContent.includes('保留 1 项 · 新增 1 项 · 修改或受影响 0 项 · 删除 1 项'));
    assert.ok(f.root.textContent.includes('可继续提问'));
    assert.ok(f.root.querySelector('[data-build-semantics]').textContent.includes('仅梳理待更新 1 项'));
    assert.equal(f.root.querySelector('Old'),null);
    const filter=f.root.querySelector('[data-semantic-changes]');filter.checked=true;
    filter.dispatchEvent(new f.window.Event('change',{bubbles:true}));
    assert.equal(f.root.querySelectorAll('[data-semantic-entry]').length,1);
    assert.ok(f.root.querySelector('[data-semantic-entry]').textContent.includes('New Category'));
  } finally {f.manager.dispose();}
});

test('a fully current dictionary cannot accidentally trigger a full rebuild',async()=>{
  const f=await fixture(profile({entries:[entry('one')],coverage:1}));
  try {
    assert.equal(f.root.querySelector('[data-build-semantics]').disabled,true);
    assert.ok(f.root.querySelector('[data-build-semantics]').textContent.includes('语义层已是最新'));
    assert.equal(f.calls.filter(c=>c.path.endsWith('/build')).length,0);
  } finally {f.manager.dispose();}
});

test('history keeps chat available when all current entries await semantic updates',()=>{
  const summary=semanticSummary(profile({status:'error',has_history:true,coverage:0,
    entries:[entry('one',{state:'unresolved',previous_definition:{meaning:'历史解释',state:'confirmed'}})]}));
  assert.equal(summary.can_answer,true);assert.equal(summary.confirmed,0);assert.equal(summary.pending,1);
});
