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
  const {document,window}=parseHTML('<html><body><section></section></body></html>');
  const root=document.querySelector('section'),calls=[],updates=[],previews=[];let current=structuredClone(initial);
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
  return {root,window,calls,updates,previews,manager,click};
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
  assert.deepEqual(semanticSummary(profile()),{status:'ready',stage:'语义梳理完成',coverage:2,total:2,confirmed:0});
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
