import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {mountDictionaryLibrary} from '../frontend/dictionaries.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
function row(id,extra={}) {
  return {model_id:id,name:'费用分析',path:'E:\\Reports\\费用分析.pbix',group:'report-a',updated_at:'2026-11-12T08:00:00+00:00',
    coverage:6,total:8,confirmed:3,business_count:2,same_schema:false,shared_objects:6,
    dictionary_notice:{status:'needs_update',message:'有定义需要更新'},...extra};
}
async function fixture({modelId='current',rows:initial=[row('saved'),row('current',{is_current:true})],failDelete=false,associateError=false,failRename=false}={}) {
  const {document,window}=parseHTML('<html><body><dialog><section></section></dialog></body></html>');
  const root=document.querySelector('section'),dialog=document.querySelector('dialog'),calls=[],associated=[],deleted=[];
  let rows=structuredClone(initial),targetBusy=false;
  const preview={source_model_id:'saved',source_name:'费用分析',source_updated_at:'source-revision',metadata_status:'read',
    changes:{preserved:6,added:1,modified:1,removed:1,items:[{id:'changed',kind:'measure',table:'Sales',name:'Actual',change:'modified',reasons:['公式变化']}]},
    dictionary_notice:{status:'needs_update',message:'1 项对象、1 项业务记录待更新'},preserved_reviews:1,business_records:2};
  const api=async(path,options={})=>{
    calls.push({path,...options});
    if(path.includes('/reuse-preview?'))return structuredClone(preview);
    if(path.endsWith('/reuse')){
      if(associateError)throw new Error('所选历史词典已更新，请重新预览差异后关联');
      return {model_id:modelId,status:'inventory',dictionary_notice:preview.dictionary_notice};
    }
    if(options.method==='PUT'&&path.endsWith('/name')){
      if(failRename)throw new Error('词典刚刚更新，请刷新列表后再重命名');
      const id=path.split('/').at(-2),record=rows.find(r=>r.model_id===id),body=JSON.parse(options.body);
      record.report_name??=record.name;record.saved_at??=record.updated_at;record.name=body.name;record.updated_at='2026-11-12T10:00:00+00:00';
      return {model_id:id,name:record.name,updated_at:record.updated_at};
    }
    if(options.method==='DELETE'){
      if(failDelete)throw new Error('该词典刚刚更新，请刷新历史列表后再删除');
      const id=path.split('/').at(-1).split('?')[0];rows=rows.filter(r=>r.model_id!==id);
      return {deleted_model_id:id,detail:'历史版本已删除，已关联副本保留'};
    }
    return {dictionaries:structuredClone(rows),target_busy:targetBusy};
  };
  const manager=await mountDictionaryLibrary(root,{api,modelId,onAssociate:p=>associated.push(p),onDeleted:id=>deleted.push(id)});
  async function click(selector){const el=root.querySelector(selector);assert.ok(el,selector);el.click();await tick();}
  return {root,dialog,window,calls,associated,deleted,manager,click,setBusy(value){targetBusy=value;}};
}

test('library groups report versions and keeps current dictionary visible and protected',async()=>{
  const f=await fixture({rows:[row('saved'),row('current',{is_current:true}),row('other',{name:'另一报表',group:'report-b'})]});
  try {
    assert.equal(f.root.querySelectorAll('[data-dictionary-report]').length,2);
    assert.equal(f.root.querySelectorAll('[data-dictionary-group]').length,1);
    assert.equal(f.root.querySelectorAll('[data-dictionary-card]').length,3);
    assert.equal(f.root.querySelector('[data-dictionary-group]').hasAttribute('open'),false);
    assert.equal(f.root.querySelector('[data-dictionary-report]').firstElementChild.dataset.dictionaryCard,'current');
    assert.equal(f.root.querySelector('[data-dictionary-delete="current"]'),null);
    assert.equal(f.root.querySelector('[data-dictionary-preview="current"]'),null);
    assert.ok(f.root.querySelector('[data-dictionary-card="current"] .dictionary-in-use'));
    const time=f.root.querySelector('[data-dictionary-card="current"] time');
    assert.equal(time.getAttribute('datetime'),'2026-11-12T08:00:00+00:00');
    assert.ok(time.textContent.includes('2026-11-12')&&time.textContent.includes('16:00'));
    assert.equal(f.root.textContent.includes('T08:00:00'),false);
    assert.ok(f.root.textContent.includes('共有 6 个同名对象'));
    assert.equal(f.calls[0].path,'/semantic-dictionaries?current_model_id=current');
  } finally{f.manager.dispose();}
});

test('manual association requires preview and carries its revision token',async()=>{
  const f=await fixture();
  try {
    await f.click('[data-dictionary-preview="saved"]');
    assert.ok(f.root.textContent.includes('公式变化'));
    assert.ok(f.root.textContent.includes('当前已审阅的 1 项对象定义优先保留'));
    assert.equal(f.calls.filter(c=>c.method==='POST').length,0);
    await f.click('[data-dictionary-associate]');
    const call=f.calls.find(c=>c.method==='POST');
    assert.equal(call.path,'/models/current/semantics/reuse');
    assert.deepEqual(JSON.parse(call.body),{source_model_id:'saved',source_updated_at:'source-revision'});
    assert.equal(f.associated.length,1);
  } finally{f.manager.dispose();}
});

test('returning from preview retains search and list scroll position',async()=>{
  const f=await fixture();
  try {
    const search=f.root.querySelector('[data-dictionary-search]');search.value='费用';
    search.dispatchEvent(new f.window.Event('input',{bubbles:true}));f.dialog.scrollTop=560;
    f.root.querySelector('[data-dictionary-group]').setAttribute('open','');
    await f.click('[data-dictionary-preview="saved"]');
    assert.equal(f.dialog.scrollTop,0);
    await f.click('[data-dictionary-preview-back]');
    assert.equal(f.dialog.scrollTop,560);
    assert.equal(f.root.querySelector('[data-dictionary-search]').value,'费用');
    assert.ok(f.root.querySelector('[data-dictionary-group]').hasAttribute('open'));
  } finally{f.manager.dispose();}
});

test('deletion is confirmed explicitly and targets only the selected revision',async()=>{
  const f=await fixture();
  try {
    f.dialog.scrollTop=330;
    await f.click('[data-dictionary-delete="saved"]');
    assert.equal(f.calls.filter(c=>c.method==='DELETE').length,0);
    assert.ok(f.root.textContent.includes('其他模型已关联的副本会保留'));
    await f.click('[data-dictionary-delete-confirm="saved"]');
    const call=f.calls.find(c=>c.method==='DELETE');
    assert.ok(call.path.startsWith('/semantic-dictionaries/saved?'));
    const query=new URLSearchParams(call.path.split('?')[1]);
    assert.equal(query.get('current_model_id'),'current');
    assert.equal(query.get('updated_at'),'2026-11-12T08:00:00+00:00');
    assert.deepEqual(f.deleted,['saved']);
    assert.equal(f.root.querySelector('[data-dictionary-card="saved"]'),null);
    assert.ok(f.root.querySelector('[data-dictionary-card="current"]'));
    assert.equal(f.dialog.scrollTop,330);
  } finally{f.manager.dispose();}
});

test('canceling deletion performs no mutation',async()=>{
  const f=await fixture();
  try {
    await f.click('[data-dictionary-delete="saved"]');await f.click('[data-dictionary-delete-cancel]');
    assert.equal(f.calls.filter(c=>c.method==='DELETE').length,0);
    assert.ok(f.root.querySelector('[data-dictionary-delete="saved"]'));
  } finally{f.manager.dispose();}
});

test('revision conflicts preserve preview or deletion controls for review',async()=>{
  const f=await fixture({associateError:true,failDelete:true});
  try {
    await f.click('[data-dictionary-preview="saved"]');await f.click('[data-dictionary-associate]');
    assert.ok(f.root.textContent.includes('所选历史词典已更新'));
    assert.equal(f.associated.length,0);
    await f.click('[data-dictionary-preview-back]');
    await f.click('[data-dictionary-delete="saved"]');await f.click('[data-dictionary-delete-confirm="saved"]');
    assert.ok(f.root.textContent.includes('该词典刚刚更新'));
    assert.ok(f.root.querySelector('[data-dictionary-card="saved"]'));
    await f.click('[data-dictionary-refresh]');
    assert.equal(f.root.querySelector('[data-dictionary-delete-confirm]'),null);
  } finally{f.manager.dispose();}
});

test('library is available without a connected model and can search historical paths',async()=>{
  const f=await fixture({modelId:null});
  try {
    assert.equal(f.calls[0].path,'/semantic-dictionaries');
    assert.equal(f.root.querySelector('[data-dictionary-preview]'),null);
    const timeSearch=f.root.querySelector('[data-dictionary-search]');timeSearch.value='16:00';
    timeSearch.dispatchEvent(new f.window.Event('input',{bubbles:true}));
    assert.equal(f.root.querySelectorAll('[data-dictionary-card]').length,2);
    const search=f.root.querySelector('[data-dictionary-search]');search.value='does not exist';
    search.dispatchEvent(new f.window.Event('input',{bubbles:true}));
    assert.equal(f.root.querySelectorAll('[data-dictionary-card]').length,0);
    assert.ok(f.root.textContent.includes('没有匹配的历史业务词典'));
  } finally{f.manager.dispose();}
});

test('busy source and busy destination disable association and running source deletion',async()=>{
  const f=await fixture({rows:[row('running',{busy:true}),row('saved')]});
  try {
    assert.equal(f.root.querySelector('[data-dictionary-delete="running"]').disabled,true);
    assert.equal(f.root.querySelector('[data-dictionary-preview="running"]').disabled,true);
    f.setBusy(true);await f.click('[data-dictionary-refresh]');
    assert.equal(f.root.querySelector('[data-dictionary-preview="saved"]').disabled,true);
    assert.equal(f.root.querySelector('[data-dictionary-delete="saved"]').disabled,false);
  } finally{f.manager.dispose();}
});

test('saved report labels and difference text cannot create executable markup',async()=>{
  const f=await fixture({rows:[row('saved',{name:'<img src=x onerror=1>',path:'<script>alert(1)</script>'})]});
  try {
    assert.equal(f.root.querySelector('img'),null);assert.equal(f.root.querySelector('script'),null);
    assert.ok(f.root.textContent.includes('<img src=x onerror=1>'));
  } finally{f.manager.dispose();}
});

async function submitName(f,value) {
  const input=f.root.querySelector('[data-dictionary-name-input]');input.value=value;
  input.dispatchEvent(new f.window.Event('input',{bubbles:true}));
  const form=f.root.querySelector('[data-dictionary-rename-form]');form.reportValidity=()=>true;
  form.dispatchEvent(new f.window.Event('submit',{bubbles:true,cancelable:true}));await tick();
}

test('current dictionary can be renamed while its saved semantic time and native report label stay visible',async()=>{
  const f=await fixture();
  try {
    await f.click('[data-dictionary-rename="current"]');
    await submitName(f,'工厂费用词典');
    const call=f.calls.find(c=>c.method==='PUT');
    assert.equal(call.path,'/semantic-dictionaries/current/name');
    assert.deepEqual(JSON.parse(call.body),{name:'工厂费用词典',updated_at:'2026-11-12T08:00:00+00:00'});
    const card=f.root.querySelector('[data-dictionary-card="current"]');
    assert.equal(card.querySelector('.dictionary-name-line>strong').textContent,'工厂费用词典');
    assert.ok(card.textContent.includes('报表 费用分析'));
    assert.equal(card.querySelector('time').getAttribute('datetime'),'2026-11-12T08:00:00+00:00');
    assert.equal(card.querySelector('[data-dictionary-delete]'),null);
    const search=f.root.querySelector('[data-dictionary-search]');search.value='工厂费用词典';
    search.dispatchEvent(new f.window.Event('input',{bubbles:true}));
    assert.equal(f.root.querySelectorAll('[data-dictionary-card]').length,1);
  } finally{f.manager.dispose();}
});

test('name conflict preserves the draft and canceling the edit leaves the saved name intact',async()=>{
  const f=await fixture({failRename:true});
  try {
    await f.click('[data-dictionary-rename="current"]');await submitName(f,'仍需保留的命名');
    assert.ok(f.root.textContent.includes('词典刚刚更新'));
    assert.equal(f.root.querySelector('[data-dictionary-name-input]').value,'仍需保留的命名');
    await f.click('[data-dictionary-name-cancel]');
    assert.equal(f.root.querySelector('[data-dictionary-card="current"] .dictionary-name-line>strong').textContent,'费用分析');
    assert.equal(f.calls.filter(c=>c.method==='PUT').length,1);
  } finally{f.manager.dispose();}
});

test('empty names are rejected locally and Escape cancels inline editing',async()=>{
  const f=await fixture();
  try {
    await f.click('[data-dictionary-rename="current"]');await submitName(f,'   ');
    assert.ok(f.root.textContent.includes('请填写词典名称'));
    assert.equal(f.calls.filter(c=>c.method==='PUT').length,0);
    const escape=new f.window.Event('keydown',{bubbles:true,cancelable:true});Object.defineProperty(escape,'key',{value:'Escape'});
    f.root.querySelector('[data-dictionary-name-input]').dispatchEvent(escape);
    assert.ok(escape.defaultPrevented);
    assert.equal(f.root.querySelector('[data-dictionary-name-input]'),null);
  } finally{f.manager.dispose();}
});
