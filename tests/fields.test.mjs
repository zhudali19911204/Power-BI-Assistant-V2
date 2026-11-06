import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {fieldEvidence,fieldProfile,mountFieldPreview} from '../frontend/fields.js';
import {createMessageRenderer} from '../frontend/messages.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
const element=html=>parseHTML('<html><body><section>'+html+'</section></body></html>').document.querySelector('section');
function profile(extra={}) {
  return {model_id:'model',table:'分类表',column:'类别',dtype:'string',
    statistics:{row_count:10,distinct_count:4,blank_count:2,empty_string_count:1,matched_distinct_count:4},
    values:[{value:'personnel',count:4,value_truncated:false},{value:'0257',count:3,value_truncated:false},
      {value:null,count:2,value_truncated:false},{value:'',count:1,value_truncated:false}],
    samples:[{value:'personnel',value_truncated:false},{value:'0257',value_truncated:false}],
    search_terms:[],truncated:false,limit:50,scope:'当前用户可读取的全模型范围，未应用报表页面筛选',
    checked_at:'2026-11-06T09:00:00Z',seconds:0.1,dax_queries:['EVALUATE ROW("count",10)'],run_verified:true,...extra};
}
function record(extra={}) {return {id:'field-1',model_id:'model',label:'F1',table:'分类表',column:'类别',
  reason:'核对人员费用对应的实际分类',ok:true,error:'',profile:profile(),...extra};}

test('field preview shows distinct actual values, counts, model blanks and empty strings separately',()=>{
  const root=element(fieldProfile(profile()));
  assert.equal(root.querySelectorAll('tbody tr').length,4);
  assert.ok(root.textContent.includes('0257'));
  assert.ok(root.textContent.includes('personnel'));
  assert.ok(root.textContent.includes('20.0%'));
  assert.ok(root.textContent.includes('空值 BLANK()'));
  assert.ok(root.textContent.includes('空字符串 ""'));
  assert.ok(root.textContent.includes('未应用报表页面筛选'));
  assert.ok(root.textContent.includes('40.00%'));
});

test('search matches and truncated samples stay distinct from full model statistics',()=>{
  const root=element(fieldProfile(profile({search_terms:['人员','personnel'],truncated:true,
    statistics:{row_count:10,distinct_count:4,blank_count:2,empty_string_count:null,matched_distinct_count:1}})));
  assert.ok(root.textContent.includes('匹配 1 个不同取值'));
  assert.ok(root.textContent.includes('统计卡片和样例仍为全模型范围'));
  assert.ok(root.textContent.includes('已截断'));
  assert.ok(root.textContent.includes('未统计'));
});

test('empty search is not presented as an empty column',()=>{
  const searched=element(fieldProfile(profile({search_terms:['不存在'],values:[],statistics:{
    row_count:10,distinct_count:4,blank_count:2,empty_string_count:1,matched_distinct_count:0}})));
  assert.ok(searched.textContent.includes('未找到匹配取值'));
  assert.ok(!searched.textContent.includes('此字段没有可显示的取值'));
  const empty=element(fieldProfile(profile({values:[],samples:[],statistics:{row_count:0,distinct_count:0,blank_count:0,
    empty_string_count:0,matched_distinct_count:0}})));
  assert.ok(empty.textContent.includes('此字段没有可显示的取值'));
  assert.ok(!empty.textContent.includes('NaN'));
});

test('model values, query source and error messages cannot create active markup',()=>{
  const p=profile({values:[{value:'<img src=x onerror=1>',count:1,value_truncated:true}],
    samples:[{value:'<script>bad()</script>',value_truncated:false}],dax_queries:['EVALUATE "</code><iframe>"']});
  const root=element(fieldEvidence([record({table:'<img src=x>',profile:p}),record({id:'failed',ok:false,error:'<script>error</script>'})]));
  assert.equal(root.querySelector('img,script,iframe'),null);
  assert.ok(root.textContent.includes('<img src=x onerror=1>'));
  assert.ok(root.textContent.includes('值已截断'));
  assert.ok(root.textContent.includes('<script>error</script>'));
  assert.equal(root.querySelector('[data-preview-column]').dataset.previewColumn,'类别');
});

test('preview dialog searches and clears values using the original model and column',async()=>{
  const {document,window}=parseHTML('<html><body><section></section></body></html>');
  const root=document.querySelector('section'),calls=[];
  const api=async(path,opts)=>{calls.push({path,body:JSON.parse(opts.body)});return profile({search_terms:calls.at(-1).body.search?[calls.at(-1).body.search]:[]});};
  await mountFieldPreview(root,{api,modelId:'original',table:'分类表',column:'类别'});
  assert.equal(calls.length,1);assert.equal(calls[0].body.search,'');
  const input=root.querySelector('input');input.value='personnel';
  root.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));await tick();
  assert.equal(calls[1].path,'/models/original/fields/preview');
  assert.deepEqual(calls[1].body,{table:'分类表',column:'类别',search:'personnel',limit:50});
  assert.equal(root.querySelector('input'),input);
  root.querySelector('[data-field-clear]').click();await tick();
  assert.equal(calls[2].body.search,'');assert.equal(input.value,'');
  assert.equal(root.querySelector('button').disabled,false);
});

test('failed reads offer retry without claiming that the column is empty',async()=>{
  const root=element('');let calls=0;
  const api=async()=>{if(++calls===1)throw new Error('Desktop 连接已断开');return profile();};
  await mountFieldPreview(root,{api,modelId:'model',table:'分类表',column:'类别'});
  assert.ok(root.textContent.includes('Desktop 连接已断开'));
  assert.ok(!root.querySelector('table'));
  root.querySelector('[data-field-retry]').click();await tick();
  assert.equal(calls,2);assert.equal(root.querySelectorAll('tbody tr').length,4);
});

test('dictionary previews can return immediately while field values are still loading',async()=>{
  const root=element('');let finish,returned=0;
  const mounting=mountFieldPreview(root,{api:()=>new Promise(resolve=>{finish=resolve;}),
    modelId:'model',table:'分类表',column:'类别',onBack:()=>{returned++;}});
  const back=root.querySelector('[data-field-back]');
  assert.ok(back);assert.equal(back.disabled,false);
  back.click();assert.equal(returned,1);
  finish(profile());const manager=await mounting;manager.dispose();
});

test('new field results keep the message, prior prose, and expanded evidence nodes',()=>{
  const {document}=parseHTML('<html><body><div id="scroll"><section></section></div></body></html>');
  const root=document.querySelector('section'),scroll=document.querySelector('#scroll');
  Object.assign(scroll,{scrollHeight:1000,scrollTop:600,clientHeight:400});scroll.scrollTo=()=>{};
  const renderer=createMessageRenderer({root,scroll,label:()=>'',content:m=>m.content,
    artifacts:m=>fieldEvidence(m.field_previews),status:()=>''});
  const message={role:'assistant',status:'streaming',content:'<p>保留原文</p>',field_previews:[]};
  renderer.render([message],{conversation:'c'});const article=root.firstChild,paragraph=root.querySelector('p');
  message.field_previews=[record()];renderer.render([message],{conversation:'c'});
  const details=root.querySelector('[data-field-evidence]');details.setAttribute('open','');
  message.field_previews.push(record({id:'field-2',label:'F2'}));renderer.render([message],{conversation:'c'});
  assert.equal(root.firstChild,article);assert.equal(root.querySelector('p'),paragraph);
  assert.equal(root.querySelector('[data-field-evidence]'),details);assert.ok(details.hasAttribute('open'));
  assert.equal(root.querySelectorAll('[data-field-evidence]').length,2);
});
