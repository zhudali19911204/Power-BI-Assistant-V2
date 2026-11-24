import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {parseHTML} from 'linkedom';
import {mountChatContext} from '../frontend/chat_context.js';
import {createWorkspaces,createGenerationManager} from '../frontend/workspaces.js';

const html=await readFile(new URL('../frontend/index.html',import.meta.url),'utf8');
const usage={automatic:true,auto_percent:80,percent:80,messages:16,message_limit:20,chars:890,
  char_limit:60000,summary:[],folded_messages:0,omitted_messages:0,can_compress:true};
const compacted={...usage,percent:25,messages:5,folded_messages:12,can_compress:false,
  summary:[{'来源':'id','类别':'起始需求','内容':'<b>仅看 0257</b>'}]};

function fixture(api) {
  const {document}=parseHTML(html),root=document.querySelector('#history-context');
  const w=createWorkspaces(),errors=[];
  const panel=mountChatContext(root,{getView:()=>w.current,api,onChange:()=>panel.render(),onError:message=>errors.push(message)});
  panel.render();
  return {w,root,panel,errors,find:s=>root.querySelector(s)};
}

test('automatic preference on an unsent draft follows only that conversation into its first request',async()=>{
  const calls=[];
  const f=fixture(async()=>assert.fail('A draft has no stored context'));
  const draft=f.w.current;
  assert.equal(f.find('[data-percent]').textContent,'0%');
  const automatic=f.find('[data-automatic]');
  automatic.checked=false;await automatic.onchange();
  assert.equal(draft.autoCompress,false);
  f.w.switchMode('analysis');f.panel.render();assert.equal(automatic.checked,true);
  f.w.switchMode('dax');f.panel.render();assert.equal(automatic.checked,false);
  draft.model={id:'model'};
  const generation=createGenerationManager({workspace:f.w,api:async(path,options)=>{
    if(path==='/conversations'){
      calls.push(JSON.parse(options.body));
      return {id:'saved',mode:'dax',model_id:'model'};
    }
    if(path.endsWith('/messages'))return [{role:'assistant',content:'好',status:'complete'}];
    assert.fail(path);
  },fetchResponse:async()=>new Response([
    JSON.stringify({type:'start',message_id:'reply',context_usage:{...usage,automatic:false}}),
    JSON.stringify({type:'done'})].join('\n')+'\n')});
  await generation.send(draft,{content:'继续'});
  f.panel.render();
  assert.equal(calls.length,1);assert.equal(calls[0].auto_compress,false);
  assert.equal(automatic.checked,false);assert.equal(f.find('[data-percent]').textContent,'80%');
});

test('progress, manual compaction and readable summary reflect the result even after a stale read',async()=>{
  const calls=[];
  let finishOld,finishCompress;
  const api=async(path,opts={})=>{
    calls.push([path,opts.method||'GET']);
    if(opts.method==='POST')return new Promise(resolve=>{finishCompress=resolve;});
    if(opts.method==='PUT')return {...compacted,automatic:JSON.parse(opts.body).automatic};
    return new Promise(resolve=>{finishOld=resolve;});
  };
  const f=fixture(api),view=f.w.open({id:'one',mode:'dax'});
  view.contextUsage=usage;f.panel.render();
  const button=f.find('[data-compress]'),automatic=f.find('[data-automatic]');
  assert.equal(f.find('progress').getAttribute('value'),'80');
  assert.equal(button.disabled,false);assert.equal(f.root.hasAttribute('data-full'),true);
  const old=f.panel.load(view);
  const operation=button.onclick();
  assert.equal(button.disabled,true);assert.equal(automatic.disabled,true);
  await button.onclick();
  finishCompress(compacted);await operation;
  finishOld(usage);await old;
  assert.equal(f.find('progress').getAttribute('value'),'25');
  assert.match(f.find('[data-feedback]').textContent,/已压缩 12 条/);
  assert.equal(f.find('[data-notes]').hidden,false);
  assert.match(f.find('[data-notes-body]').textContent,/<b>仅看 0257<\/b>/);
  assert.equal(f.find('[data-notes-body] b'),null);
  automatic.checked=false;await automatic.onchange();
  assert.equal(automatic.checked,false);
  assert.deepEqual(calls,[['/conversations/one/context','GET'],['/conversations/one/context/compress','POST'],
    ['/conversations/one/context','PUT']]);
});

test('navigation and generation keep readings, failures, and controls scoped to their conversation',async()=>{
  let finish;
  const f=fixture((path,options)=>{
    if(options?.method==='PUT')throw new Error('正在同步');
    return new Promise(resolve=>{finish=resolve;});
  });
  const first=f.w.open({id:'first',mode:'dax'}),pending=f.panel.load(first);
  const second=f.w.open({id:'second',mode:'analysis'});
  second.contextUsage={...usage,percent:42};second.busy=true;f.panel.render();
  finish(usage);await pending;
  assert.equal(first.contextUsage,usage);assert.equal(f.find('[data-percent]').textContent,'42%');
  assert.equal(f.find('[data-automatic]').disabled,true);
  assert.equal(f.find('[data-compress]').disabled,true);
  f.root.open=true;f.w.open(first.conversation);f.panel.render();assert.equal(f.root.open,false);
  const automatic=f.find('[data-automatic]');
  automatic.checked=false;await automatic.onchange();
  assert.equal(automatic.checked,true);assert.deepEqual(f.errors,['正在同步']);
  assert.match(f.find('[data-feedback]').textContent,/正在同步/);
});
