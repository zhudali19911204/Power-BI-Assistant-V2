import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {mountModelManager} from '../frontend/models.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
const profile=(id,extra={})=>({id,name:id,model:id,provider:'openai_compat',base_url:'http://localhost:8080/v1',
  enable_thinking:null,has_key:true,ready:true,active:id==='qwen',...extra});

async function fixture(initial=[profile('qwen'),profile('deepseek')]) {
  const {document,window}=parseHTML('<html><body><section></section></body></html>');
  const root=document.querySelector('section'),calls=[],settings=[];
  let profiles=structuredClone(initial),active=profiles.find(p=>p.active)?.id||null,testError='';
  const current=()=>({...profiles.find(p=>p.id===active),active_profile_id:active});
  async function api(path,opts={}) {
    calls.push({path,...opts});
    if(path==='/settings')return current();
    if(path==='/llm/profiles'&&!opts.method)return structuredClone(profiles);
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
      const old=profiles.find(p=>p.id===id),saved=profile(id,{...data,name:data.name||data.model,active:id===active,
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
  return {root,calls,settings,click,input,choose,submit,failTest:message=>{testError=message;}};
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
  assert.equal(JSON.parse(created.body).enable_thinking,null);
  assert.equal(f.settings.at(-1).active_profile_id,'added');
  assert.equal(f.root.querySelectorAll('.llm-profile').length,3);
  assert.ok(!f.root.innerHTML.includes('test-new-secret'));
});

test('editing an inactive model preserves its key with a blank input and keeps the selected model',async()=>{
  const f=await fixture();await f.click('[data-edit="deepseek"]');
  assert.equal(f.root.querySelector('[name="api_key"]').type,'password');
  assert.equal(f.root.querySelector('[name="api_key"]').value,'');
  f.input('name','更名');f.choose('enable_thinking','off');await f.submit();
  const edited=f.calls.find(call=>call.method==='PUT');
  assert.equal(edited.path,'/llm/profiles/deepseek');
  assert.equal(JSON.parse(edited.body).api_key,'');
  assert.equal(JSON.parse(edited.body).enable_thinking,false);
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
