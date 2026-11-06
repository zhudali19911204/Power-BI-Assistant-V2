import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {parseHTML} from 'linkedom';

const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('actual app navigation keeps background streams, drafts and independent send controls',async()=>{
  const html=await readFile(new URL('../frontend/index.html',import.meta.url),'utf8');
  const {window,document}=parseHTML(html),savedGlobals={};
  const selectProto=window.HTMLSelectElement.prototype;
  const valueDescriptor=Object.getOwnPropertyDescriptor(selectProto,'value');
  Object.defineProperty(selectProto,'value',{configurable:true,get(){return [...this.options].find(o=>o.selected)?.value||this.options[0]?.value||'';},
    set(value){for(const option of this.options)option.selected=option.value===value;}});
  const scroll=document.querySelector('#message-scroll');
  Object.assign(scroll,{scrollHeight:1000,clientHeight:400,scrollTop:600,scrollTo({top}){this.scrollTop=top;}});
  const modal=document.querySelector('#modal');modal.showModal=()=>{modal.open=true;};modal.close=()=>{modal.open=false;};
  const settings={ready:true,model:'test-service',active_profile_id:'profile',name:'测试服务'};
  const model={id:'model',name:'测试模型',source:'desktop',live:true,
    context:{tables:{事实表:[{name:'金额',dtype:'double'}]},measures:[],relationships:[],table_queries:{事实表:'let x=1 in x'},shared_expressions:{}}};
  const conversations=[],messages=new Map(),streams=new Map();let nextId=0;
  const json=value=>new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
  async function fetchMock(path,opts={}) {
    if(path==='/api/settings')return json(settings);
    if(path==='/api/models')return json([model]);
    if(path==='/api/models/model')return json(model);
    if(path==='/api/conversations'&&opts.method==='POST') {
      const body=JSON.parse(opts.body),conversation={...body,id:'c'+(++nextId),generating:false};
      conversations.unshift(conversation);return json(conversation);
    }
    if(path==='/api/conversations')return json(conversations);
    const id=path.split('/')[3];
    if(path.endsWith('/generation'))return json({running:false});
    if(path.endsWith('/messages')&&opts.method==='POST') {
      const body=JSON.parse(opts.body);let controller;
      const response=new Response(new ReadableStream({start(value){controller=value;}}));
      const record={body,text:'',controller,emit(event){
        if(event.type==='start')this.messageId=event.message_id;
        if(event.type==='clarification')this.clarification=event.clarification;
        if(event.type==='delta')this.text+=event.text;
        if(event.type==='done'){
          const previous=messages.get(id)||[];
          previous.forEach(m=>{if(m.clarification)m.clarification.answered=true;});
          messages.set(id,[...previous,{role:'user',status:'complete',content:body.content,artifacts:[]},
            {id:this.messageId||'answer-'+id,role:'assistant',status:'complete',content:this.text,artifacts:[],analysis:[],clarification:this.clarification||null}]);
        }
        controller.enqueue(new TextEncoder().encode(JSON.stringify(event)+'\n'));
      }};
      streams.set(id,record);return response;
    }
    if(path.endsWith('/messages'))return json(messages.get(id)||[]);
    throw new Error('Unexpected '+path);
  }
  const replacements={document,window,fetch:fetchMock,requestAnimationFrame:cb=>setImmediate(cb),cancelAnimationFrame:clearImmediate,
    setInterval:()=>0,innerWidth:1440};
  for(const [key,value] of Object.entries(replacements)){savedGlobals[key]=Object.getOwnPropertyDescriptor(globalThis,key);Object.defineProperty(globalThis,key,{value,writable:true,configurable:true});}
  const $=s=>document.querySelector(s);
  async function click(selector){$(selector).click();await tick();await tick();}
  function draft(text){$('#prompt').value=text;$('#prompt').dispatchEvent(new window.Event('input',{bubbles:true}));}
  async function send(text){draft(text);$('#composer-form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();}
  try {
    await import('../frontend/app.js?navigation-test');await tick();await tick();
    await click('[data-mode="analysis"]');assert.equal($('#page-title').textContent,'数据分析助手');
    await send('分析请求');const analysis=streams.get('c1');assert.ok(analysis);
    analysis.emit({type:'start',message_id:'answer-c1'});analysis.emit({type:'delta',text:'第一段分析。'});await tick();await tick();
    assert.equal($('#send-button').disabled,true);
    await click('[data-mode="dax"]');draft('DAX 页面草稿');
    assert.equal($('#page-title').textContent,'DAX 助手');assert.equal($('#send-button').disabled,false);
    assert.equal($('#background-generations').hidden,false);
    analysis.emit({type:'delta',text:'后台续写。'});await tick();await tick();
    assert.ok(!$('#messages').textContent.includes('后台续写'));
    await click('[data-mode="analysis"]');
    assert.ok($('#messages').textContent.includes('第一段分析。后台续写。'));
    assert.equal($('#send-button').disabled,true);
    await click('[data-mode="dax"]');assert.equal($('#prompt').value,'DAX 页面草稿');
    await send('DAX 请求');const dax=streams.get('c2');assert.ok(dax);assert.equal($('#send-button').disabled,true);
    analysis.emit({type:'done'});analysis.controller.close();await tick();await tick();await tick();
    assert.equal($('#page-title').textContent,'DAX 助手');assert.equal($('#send-button').disabled,true);
    assert.ok(!$('#messages').textContent.includes('第一段分析'));
    assert.ok(document.querySelector('.history-task.complete'));
    dax.emit({type:'start',message_id:'answer-c2'});dax.emit({type:'delta',text:'DAX 回复。'});
    dax.emit({type:'done'});dax.controller.close();await tick();await tick();await tick();
    assert.equal($('#send-button').disabled,false);assert.ok($('#messages').textContent.includes('DAX 回复。'));
    await click('[data-conversation="c1"]');
    assert.equal($('#page-title').textContent,'数据分析助手');
    assert.ok($('#messages').textContent.includes('第一段分析。后台续写。'));
    assert.equal(analysis.body.llm_profile_id,'profile');
    assert.ok(!$('#toasts').textContent.includes('请等待本次响应完成'));

    await send('先讨论费用口径');
    const firstQuestion=streams.get('c1');
    const card={topic:'费用分类',question:'personnel 费用采用哪个分类字段？',context:'成本中心：0257',answered:false,
      options:[{id:'A',label:'费用类别',value:'按 CE Category 筛选',detail:'费用类别字段'},
        {id:'B',label:'一级分类',value:'按 Class 1 筛选',detail:'一级分类字段'}]};
    firstQuestion.emit({type:'start',message_id:'card-1'});
    firstQuestion.emit({type:'clarification',clarification:card});
    firstQuestion.emit({type:'delta',text:'存储用的问题及选项，不应在卡片上方重复显示。'});
    firstQuestion.emit({type:'done'});firstQuestion.controller.close();await tick();await tick();await tick();
    assert.equal(document.querySelectorAll('[data-clarification] h3').length,1);
    assert.ok(!$('#messages').textContent.includes('存储用的问题'));
    const choice=$('[data-clarification-choice][value="B"]');choice.checked=true;
    choice.dispatchEvent(new window.Event('change',{bubbles:true}));await tick();await tick();
    $('[data-clarification-supplement]').value='personnel，保留 0257';
    $('[data-clarification-supplement]').dispatchEvent(new window.Event('input',{bubbles:true}));
    draft('独立的聊天草稿');await click('[data-mode="dax"]');await click('[data-mode="analysis"]');
    assert.ok($('[data-clarification-choice][value="B"]').hasAttribute('checked'));
    assert.equal($('[data-clarification-supplement]').value,'personnel，保留 0257');
    await click('[data-confirm-clarification="card-1"]');
    const continuation=streams.get('c1');assert.notEqual(continuation,firstQuestion);
    assert.deepEqual(continuation.body.clarification_reply,{message_id:'card-1',option_id:'B',supplement:'personnel，保留 0257'});
    assert.equal($('#prompt').value,'独立的聊天草稿');
    assert.equal($('#send-button').disabled,true);
    continuation.emit({type:'start',message_id:'card-2'});
    continuation.emit({type:'clarification',clarification:{...card,topic:'时间范围',question:'采用哪个时间范围？'}});
    continuation.emit({type:'delta',text:'下一项待确认。'});
    continuation.emit({type:'done'});continuation.controller.close();await tick();await tick();await tick();
    assert.equal(document.querySelectorAll('[data-confirm-clarification]').length,1);
    assert.equal($('[data-confirm-clarification]').dataset.confirmClarification,'card-2');
    assert.equal($('#prompt').value,'独立的聊天草稿');
  } finally {
    Object.defineProperty(selectProto,'value',valueDescriptor);
    for(const [key,descriptor] of Object.entries(savedGlobals)){
      if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];
    }
  }
});
