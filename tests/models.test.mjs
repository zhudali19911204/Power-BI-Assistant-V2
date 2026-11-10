import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {mountModelManager} from '../frontend/models.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
const profile=(id,extra={})=>({id,name:id,model:id,provider:'openai_compat',base_url:'http://localhost:8080/v1',
  reasoning_level:'low',reasoning_control:{supported:true,mode:'effort',description:'原生 low / high 推理强度',
    effective_label:extra.reasoning_level==='high'?'高强度思考':'低强度思考'},
  has_key:true,ready:true,active:id==='qwen',...extra});

async function fixture(initial=[profile('qwen'),profile('deepseek')]) {
  const {document,window}=parseHTML('<html><body><section></section></body></html>');
  const root=document.querySelector('section'),calls=[],settings=[];
  let profiles=structuredClone(initial),active=profiles.find(p=>p.active)?.id||null,testError='',reasoningError='',reasoningWait;
  const current=()=>({...profiles.find(p=>p.id===active),active_profile_id:active});
  async function api(path,opts={}) {
    calls.push({path,...opts});
    if(path==='/settings')return current();
    if(path==='/llm/profiles'&&!opts.method)return structuredClone(profiles);
    if(path.endsWith('/reasoning')&&opts.method==='PATCH') {
      if(reasoningWait)await reasoningWait;
      if(reasoningError)throw new Error(reasoningError);
      const saved=profiles.find(p=>p.id===path.split('/')[3]);
      saved.reasoning_level=JSON.parse(opts.body).reasoning_level;
      saved.reasoning_control.effective_label=saved.reasoning_level==='high'?'高强度思考':'低强度思考';
      return structuredClone(saved);
    }
    if(path.endsWith('/activate')) {
      active=path.split('/')[3];profiles.forEach(p=>p.active=p.id===active);return current();
    }
    if(path.endsWith('/test')) {
      if(testError)throw new Error(testError);
      return {ok:true,detail:'模型服务连接成功'};
    }
    if(opts.method==='DELETE') {
      profiles=profiles.filter(p=>p.id!==path.split('/').at(-1));
      if(!profiles.some(p=>p.id===active))active=profiles[0]?.id||null;
      profiles.forEach(p=>p.active=p.id===active);return current();
    }
    if(opts.method==='POST'||opts.method==='PUT') {
      const data=JSON.parse(opts.body),id=opts.method==='POST'?'added':path.split('/').at(-1);
      const old=profiles.find(p=>p.id===id),saved=profile(id,{...old,...data,name:data.name||data.model,active:id===active,
        has_key:data.clear_key?false:!!(data.api_key||old?.has_key)});
      delete saved.api_key;delete saved.clear_key;
      if(old)profiles[profiles.indexOf(old)]=saved;else profiles.push(saved);
      return structuredClone(saved);
    }
    throw new Error('Unexpected request '+path);
  }
  await mountModelManager(root,{api,onSettings:s=>settings.push(s)});
  async function click(selector) {
    root.querySelectorAll('form').forEach(form=>{form.reportValidity=()=>true;});
    const button=root.querySelector(selector);assert.ok(button,selector);button.click();await tick();
  }
  function input(name,value) {root.querySelector(`[name="${name}"]`).value=value;}
  function choose(name,value) {
    const options=[...root.querySelectorAll(`[name="${name}"] option`)];
    options.forEach(option=>option.selected=false);options.find(option=>option.value===value).selected=true;
  }
  async function submit() {
    const form=root.querySelector('form');form.reportValidity=()=>true;
    form.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));await tick();
  }
  return {root,calls,settings,click,input,choose,submit,failTest:message=>{testError=message;},
    failReasoning:message=>{reasoningError=message;},
    deferReasoning:()=>{let release;reasoningWait=new Promise(resolve=>{release=resolve;});return release;}};
}

test('saved models list shows current selection and can switch without editing the other model',async()=>{
  const f=await fixture();
  assert.equal(f.root.querySelectorAll('.llm-profile').length,2);
  assert.equal(f.root.querySelector('.llm-profile.active strong').textContent,'qwen');
  await f.click('[data-use="deepseek"]');
  assert.equal(f.settings.at(-1).active_profile_id,'deepseek');
  assert.equal(f.root.querySelector('.llm-profile.active strong').textContent,'deepseek');
  assert.equal(f.calls.filter(call=>call.method==='PUT').length,0);
});

test('adding a model creates a separate profile and optionally activates it',async()=>{
  const f=await fixture();await f.click('[data-add]');
  assert.equal(f.root.querySelector('[name="api_key"]').value,'');
  f.input('name','新模型');f.input('model','custom-model');f.input('base_url','https://example.com/v1');f.input('api_key','test-new-secret');
  await f.click('[data-save-use]');
  const created=f.calls.find(call=>call.path==='/llm/profiles'&&call.method==='POST');
  assert.equal(JSON.parse(created.body).model,'custom-model');
  assert.equal(JSON.parse(created.body).enable_thinking,undefined);
  assert.equal(f.settings.at(-1).reasoning_level,'low');
  assert.equal(f.settings.at(-1).active_profile_id,'added');
  assert.equal(f.root.querySelectorAll('.llm-profile').length,3);
  assert.ok(!f.root.innerHTML.includes('test-new-secret'));
});

test('editing an inactive model preserves its key with a blank input and keeps the selected model',async()=>{
  const f=await fixture();await f.click('[data-edit="deepseek"]');
  assert.equal(f.root.querySelector('[name="api_key"]').type,'password');
  assert.equal(f.root.querySelector('[name="api_key"]').value,'');
  assert.equal(f.root.querySelector('[name="enable_thinking"]'),null);
  f.input('name','更名');await f.submit();
  const edited=f.calls.find(call=>call.method==='PUT');
  assert.equal(edited.path,'/llm/profiles/deepseek');
  assert.equal(JSON.parse(edited.body).api_key,'');
  assert.equal(JSON.parse(edited.body).enable_thinking,undefined);
  assert.equal(JSON.parse(edited.body).reasoning_level,undefined);
  assert.equal(f.settings.at(-1).active_profile_id,'qwen');
  assert.equal(f.calls.filter(call=>call.path.endsWith('/activate')).length,0);
});

test('connection tests use the chosen profile without switching the active model',async()=>{
  const f=await fixture();await f.click('[data-test="deepseek"]');
  assert.equal(f.calls.at(-1).path,'/llm/profiles/deepseek/test');
  assert.equal(f.settings.at(-1).active_profile_id,'qwen');
  assert.ok(f.root.querySelector('[role="status"]').textContent.includes('连接成功'));
});

test('save and test saves only the edited model, clears the typed secret, and retains edits on test failure',async()=>{
  const f=await fixture();f.failTest('连接失败，请检查配置');await f.click('[data-edit="deepseek"]');
  f.input('model','deepseek-updated');f.input('api_key','test-replacement-secret');
  await f.click('[data-save-test]');
  assert.ok(f.calls.some(call=>call.path==='/llm/profiles/deepseek'&&call.method==='PUT'));
  assert.ok(f.calls.some(call=>call.path==='/llm/profiles/deepseek/test'));
  assert.equal(f.root.querySelector('[name="api_key"]').value,'');
  assert.equal(f.root.querySelector('[name="model"]').value,'deepseek-updated');
  assert.ok(f.root.querySelector('.model-feedback.error').textContent.includes('连接失败'));
  assert.equal(f.root.getAttribute('aria-busy'),'false');
  assert.equal(f.root.querySelector('fieldset').disabled,false);
  assert.equal(f.settings.at(-1).active_profile_id,'qwen');
});

test('deletion needs an inline confirmation and updates active selection when confirmed',async()=>{
  const f=await fixture();await f.click('[data-delete="qwen"]');
  assert.equal(f.calls.filter(call=>call.method==='DELETE').length,0);
  await f.click('[data-cancel-delete]');
  assert.equal(f.root.querySelector('[data-confirm-delete]'),null);
  await f.click('[data-delete="qwen"]');await f.click('[data-confirm-delete="qwen"]');
  assert.equal(f.root.querySelectorAll('.llm-profile').length,1);
  assert.equal(f.settings.at(-1).active_profile_id,'deepseek');
});

test('model names, ids and addresses are escaped and never treated as HTML',async()=>{
  const f=await fixture([profile('qwen',{name:'<img src=x onerror=1>',model:'<script>alert(1)</script>',base_url:'https://example.com/" onload="x'})]);
  assert.equal(f.root.querySelector('img'),null);assert.equal(f.root.querySelector('script'),null);
  assert.ok(f.root.textContent.includes('<img src=x onerror=1>'));
  await f.click('[data-edit="qwen"]');
  assert.equal(f.root.querySelector('[name="model"]').value,'<script>alert(1)</script>');
  assert.equal(f.root.querySelector('[onload]'),null);
});

test('empty configurations can add a first model',async()=>{
  const f=await fixture([]);assert.ok(f.root.textContent.includes('尚未添加模型'));
  await f.click('[data-add]');assert.ok(f.root.querySelector('[data-model-form]'));
});

test('legacy models show new timeout defaults and save independent edits',async()=>{
  const f=await fixture();await f.click('[data-edit="deepseek"]');
  assert.equal(f.root.querySelector('[name="idle_timeout"]').value,'90');
  assert.equal(f.root.querySelector('[name="generation_timeout"]').value,'900');
  f.input('idle_timeout','180');f.input('generation_timeout','1800');await f.submit();
  const body=JSON.parse(f.calls.find(call=>call.method==='PUT').body);
  assert.equal(body.idle_timeout,180);assert.equal(body.generation_timeout,1800);
  await f.click('[data-edit="deepseek"]');
  assert.equal(f.root.querySelector('[name="generation_timeout"]').value,'1800');
  await f.click('[data-back]');await f.click('[data-edit="qwen"]');
  assert.equal(f.root.querySelector('[name="generation_timeout"]').value,'900');
});

test('output budget defaults for legacy profiles and persists per model',async()=>{
  const f=await fixture();await f.click('[data-edit="deepseek"]');
  assert.equal(f.root.querySelector('[name="max_tokens"]').value,'16000');
  f.input('max_tokens','32000');await f.submit();
  assert.equal(JSON.parse(f.calls.find(call=>call.method==='PUT').body).max_tokens,32000);
  await f.click('[data-edit="deepseek"]');
  assert.equal(f.root.querySelector('[name="max_tokens"]').value,'32000');
  await f.click('[data-back]');await f.click('[data-edit="qwen"]');
  assert.equal(f.root.querySelector('[name="max_tokens"]').value,'16000');
  assert.ok(f.root.textContent.includes('默认低强度'));
});

test('strength is visible on each card and changes without editing or activating it',async()=>{
  const f=await fixture();
  const low='[data-reasoning-profile="deepseek"][data-reasoning-level="low"]';
  const high='[data-reasoning-profile="deepseek"][data-reasoning-level="high"]';
  assert.equal(f.root.querySelector(low).getAttribute('aria-pressed'),'true');
  await f.click(high);
  const changed=f.calls.find(call=>call.method==='PATCH');
  assert.equal(changed.path,'/llm/profiles/deepseek/reasoning');
  assert.deepEqual(JSON.parse(changed.body),{reasoning_level:'high'});
  assert.equal(f.root.querySelector(high).getAttribute('aria-pressed'),'true');
  assert.equal(f.root.querySelector(low).getAttribute('aria-pressed'),'false');
  assert.equal(f.settings.at(-1).active_profile_id,'qwen');
  assert.equal(f.root.querySelector('[data-model-form]'),null);
  await f.click(high);
  assert.equal(f.calls.filter(call=>call.method==='PATCH').length,1);
  await f.click('[data-edit="deepseek"]');f.input('name','保留高强度');await f.submit();
  assert.equal(f.root.querySelector(high).getAttribute('aria-pressed'),'true');
});

test('failed strength updates keep the saved state and show an explicit error',async()=>{
  const f=await fixture();f.failReasoning('强度保存失败');
  await f.click('[data-reasoning-profile="qwen"][data-reasoning-level="high"]');
  assert.equal(f.root.querySelector('[data-reasoning-profile="qwen"][data-reasoning-level="low"]').getAttribute('aria-pressed'),'true');
  assert.ok(f.root.querySelector('.model-feedback.error').textContent.includes('强度保存失败'));
  assert.equal(f.root.getAttribute('aria-busy'),'false');
  assert.equal(f.root.querySelector('fieldset').disabled,false);
});

test('pending strength changes disable controls and prevent competing requests',async()=>{
  const f=await fixture(),release=f.deferReasoning();
  await f.click('[data-reasoning-profile="qwen"][data-reasoning-level="high"]');
  assert.equal(f.root.querySelector('fieldset').disabled,true);
  await f.click('[data-reasoning-profile="deepseek"][data-reasoning-level="high"]');
  assert.equal(f.calls.filter(call=>call.method==='PATCH').length,1);
  release();await tick();
  assert.equal(f.settings.at(-1).reasoning_level,'high');
  assert.equal(f.root.querySelector('fieldset').disabled,false);
});

test('unsupported models never display a working low-strength selection',async()=>{
  const f=await fixture([profile('qwen',{reasoning_control:{supported:false,mode:'unsupported',
    description:'此模型固定思考，不支持降低强度',effective_label:'服务端默认 · 强度不可调'}})]);
  const buttons=[...f.root.querySelectorAll('[data-reasoning-level]')];
  assert.ok(buttons.every(button=>button.disabled&&button.getAttribute('aria-pressed')==='false'));
  assert.ok(f.root.textContent.includes('不支持降低强度'));
  await f.click('[data-reasoning-level="high"]');
  assert.equal(f.calls.filter(call=>call.method==='PATCH').length,0);
});
