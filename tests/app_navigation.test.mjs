import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {parseHTML} from 'linkedom';

const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('actual app navigation keeps background streams, drafts and independent send controls',async()=>{
  const html=await readFile(new URL('../frontend/index.html',import.meta.url),'utf8');
  const {window,document}=parseHTML(html),savedGlobals={};
  assert.deepEqual([...document.querySelectorAll('.workspace-nav [data-mode]')].map(button=>button.dataset.mode),
    ['mquery','dax','analysis']);
  assert.equal(document.querySelector('.workspace-nav .active').dataset.mode,'dax');
  const selectProto=window.HTMLSelectElement.prototype;
  const valueDescriptor=Object.getOwnPropertyDescriptor(selectProto,'value');
  Object.defineProperty(selectProto,'value',{configurable:true,get(){return [...this.options].find(o=>o.selected)?.value||this.options[0]?.value||'';},
    set(value){for(const option of this.options)option.selected=option.value===value;}});
  const scroll=document.querySelector('#message-scroll');
  Object.assign(scroll,{scrollHeight:1000,clientHeight:400,scrollTop:600,scrollTo({top}){this.scrollTop=top;}});
  const modal=document.querySelector('#modal');modal.showModal=()=>{modal.open=true;};modal.close=()=>{modal.open=false;modal.dispatchEvent(new window.Event('close'));};
  const settings={ready:true,model:'test-service',active_profile_id:'profile',name:'测试服务',
    reasoning_level:'low',reasoning_control:{supported:true}};
  const model={id:'model',name:'测试模型',source:'desktop',live:true,semantics:{status:'inventory',coverage:0,total:1,confirmed:0},
    context:{tables:{事实表:[{name:'金额',dtype:'double'}],度量表:[]},measures:[],relationships:[],table_queries:{事实表:'let x=1 in x'},shared_expressions:{}}};
  const knownModels=new Map([[model.id,model]]),syncRequests=[];let pendingSync=null,failSync=false;
  function refreshedModel(previous){
    const fresh={...previous,id:'synced-'+(knownModels.size),semantics:{status:'inventory',can_answer:true,coverage:1,total:2,confirmed:0},
      context:{...previous.context,tables:{...previous.context.tables,事实表:[...previous.context.tables.事实表,{name:'同步新增字段'+knownModels.size,dtype:'string'}]}}};
    knownModels.set(fresh.id,fresh);return fresh;
  }
  const conversations=[],messages=new Map(),streams=new Map(),previewCalls=[];let nextId=0,pendingPreview=null;
  const imageMeta={id:'a'.repeat(32),name:'截图.png',url:'/api/images/'+'a'.repeat(32),mime_type:'image/png',width:80,height:40,size:100};
  const uploads=[],cancellations=[],writes=[];let failWrite=true;
  const intervals=[];let connectionStatus='connected',connectionPort=51413;
  let semanticProfile={model_id:'model',status:'inventory',stage:'本地事实已整理',error:'',coverage:0,entries:[{
    id:'amount',kind:'column',table:'事实表',name:'金额',facts:{dtype:'double'},state:'unresolved',business_name:'金额',
    meaning:'业务口径待确认',role:'数值',unit:'',aliases:[]}]};
  const fieldProfile={model_id:'model',table:'事实表',column:'金额',dtype:'double',scope:'全模型范围，未应用报表筛选',
    statistics:{row_count:10,distinct_count:3,blank_count:1,empty_string_count:null,matched_distinct_count:3},
    values:[{value:125,count:7,value_truncated:false},{value:500,count:2,value_truncated:false},{value:null,count:1,value_truncated:false}],
    samples:[{value:125,value_truncated:false}],search_terms:[],truncated:false,limit:50,run_verified:true,
    checked_at:'2026-11-06T08:00:00Z',seconds:0.1,dax_queries:['EVALUATE ROW("x",1)']};
  const fieldRecord={id:'field-check',model_id:'model',table:'事实表',column:'金额',label:'F1',reason:'识别口径',
    ok:true,error:'',profile:fieldProfile};
  const json=value=>new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
  async function fetchMock(path,opts={}) {
    if(path==='/api/images'&&opts.method==='POST'){uploads.push(opts);return json(imageMeta);}
    if(path==='/api/settings')return json(settings);
    if(path==='/api/models')return json([...knownModels.values()]);
    const modelPath=path.match(/^\/api\/models\/([^/]+)(?:\/(.*))?$/),modelId=modelPath?.[1];
    if(modelPath&&!modelPath[2])return json(knownModels.get(modelId));
    if(modelPath?.[2]==='connection')return json({model_id:modelId,live:connectionStatus==='connected',
      port:connectionPort,connection:{status:connectionStatus,detail:'连接状态测试'}});
    if(modelPath?.[2]==='semantics')return json(semanticProfile);
    if(modelPath?.[2]==='refresh'){syncRequests.push({modelId});return json(refreshedModel(knownModels.get(modelId)));}
    if(path==='/api/models/model/semantics/build'){
      semanticProfile={...semanticProfile,status:'ready',stage:'测试语义完成',coverage:1,
        entries:semanticProfile.entries.map(e=>({...e,state:'candidate'}))};
      model.semantics={...model.semantics,status:'ready',coverage:1};return json(semanticProfile);
    }
    if(path==='/api/models/model/fields/preview'){
      previewCalls.push(JSON.parse(opts.body));
      if(pendingPreview){const waiting=pendingPreview;pendingPreview=null;await waiting;}
      return json(fieldProfile);
    }
    if(path==='/api/conversations'&&opts.method==='POST') {
      const body=JSON.parse(opts.body),conversation={...body,id:'c'+(++nextId),generating:false};
      conversations.unshift(conversation);return json(conversation);
    }
    if(path==='/api/conversations')return json(conversations);
    if(path==='/api/artifacts/measure-test/apply'||path==='/api/artifacts/column-test/apply'){
      const body=JSON.parse(opts.body);writes.push(body);
      if(failWrite){failWrite=false;return new Response(JSON.stringify({detail:'测试：目标度量验证失败'}),{status:409});}
      const result={ok:true,detail:'已写入所选表',name:body.name,home_table:body.home_table,applied_at:'2026-11-10T08:00:00Z',
        validation:{ok:true,run_verified:true,errors:[],warnings:[]}};
      for(const rows of messages.values())for(const message of rows)for(const artifact of message.artifacts||[])
        if(path===`/api/artifacts/${artifact.id}/apply`)Object.assign(artifact,result);
      return json(result);
    }
    const id=path.split('/')[3];
    if(path.endsWith('/refresh-model')){
      syncRequests.push({conversationId:id});
      if(pendingSync){const waiting=pendingSync;pendingSync=null;await waiting;}
      if(failSync){failSync=false;return new Response(JSON.stringify({detail:'测试：模型暂不可读取'}),{status:409});}
      const conversation=conversations.find(c=>c.id===id),fresh=refreshedModel(knownModels.get(conversation.model_id));
      conversation.model_id=fresh.id;
      for(const message of messages.get(id)||[])if(message.role==='assistant')message.model_stale=true;
      return json({conversation,model:fresh});
    }
    if(path.endsWith('/cancel')){
      cancellations.push({id,...JSON.parse(opts.body)});
      const record=streams.get(id);
      messages.set(id,[...(messages.get(id)||[]),{role:'user',content:record.body.content,status:'complete'},
        {id:record.messageId,role:'assistant',content:record.text+'\n\n[已中止]',status:'cancelled',artifacts:[]}]);
      record.emit({type:'cancelled',text:'已中止'});record.controller.close();return json({running:false});
    }
    if(path.endsWith('/generation'))return json({running:false});
    if(path.endsWith('/messages')&&opts.method==='POST') {
      const body=JSON.parse(opts.body);let controller;
      const response=new Response(new ReadableStream({start(value){controller=value;}}));
      const record={body,text:'',controller,emit(event){
        if(event.type==='start')this.messageId=event.message_id;
        if(event.type==='clarification')this.clarification=event.clarification;
        if(event.type==='field_previews')this.field_previews=event.records;
        if(event.type==='artifacts')this.artifacts=event.artifacts;
        if(event.type==='delta')this.text+=event.text;
        if(event.type==='done'){
          const previous=messages.get(id)||[];
          previous.forEach(m=>{if(m.clarification)m.clarification.answered=true;});
          messages.set(id,[...previous,{role:'user',status:'complete',content:body.content,images:body.image_ids?.length?[imageMeta]:[],artifacts:[]},
            {id:this.messageId||'answer-'+id,role:'assistant',status:'complete',content:this.text,artifacts:this.artifacts||[],analysis:[],
              clarification:this.clarification||null,field_previews:this.field_previews||[]}]);
        }
        controller.enqueue(new TextEncoder().encode(JSON.stringify(event)+'\n'));
      }};
      streams.set(id,record);return response;
    }
    if(path.endsWith('/messages'))return json(messages.get(id)||[]);
    throw new Error('Unexpected '+path);
  }
  const replacements={document,window,fetch:fetchMock,requestAnimationFrame:cb=>setImmediate(cb),cancelAnimationFrame:clearImmediate,
    setInterval:(callback,ms)=>{intervals.push({callback,ms});return intervals.length;},innerWidth:1440};
  for(const [key,value] of Object.entries(replacements)){savedGlobals[key]=Object.getOwnPropertyDescriptor(globalThis,key);Object.defineProperty(globalThis,key,{value,writable:true,configurable:true});}
  const $=s=>document.querySelector(s);
  async function click(selector){$(selector).click();await tick();await tick();}
  function draft(text){$('#prompt').value=text;$('#prompt').dispatchEvent(new window.Event('input',{bubbles:true}));}
  async function send(text){draft(text);$('#composer-form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();}
  try {
    await import('../frontend/app.js?navigation-test');await tick();await tick();
    assert.equal($('#connection-label').textContent,'Desktop 已连接');
    assert.equal($('#active-llm').textContent,'test-service · 低强度');
    const treeNode=$('#model-tree').firstChild;draft('重连期间保留的草稿');
    connectionStatus='reconnecting';intervals.find(timer=>timer.ms===3000).callback();await tick();await tick();
    assert.equal($('#connection-label').textContent,'自动重连中');assert.equal($('#prompt').value,'重连期间保留的草稿');
    assert.equal($('#model-tree').firstChild,treeNode);assert.equal(document.querySelector('.status-dot').classList.contains('connected'),false);
    connectionStatus='connected';connectionPort=60001;intervals.find(timer=>timer.ms===3000).callback();await tick();await tick();
    assert.equal($('#connection-label').textContent,'Desktop 已连接');assert.equal($('#model-tree').firstChild,treeNode);
    assert.equal($('#stop-button').disabled,true);assert.equal($('#stop-button').type,'button');
    await send('先建立词典再生成');
    assert.equal(streams.size,0);assert.equal($('#prompt').value,'先建立词典再生成');
    assert.ok($('#semantic-workbench'));
    assert.ok($('#semantic-workbench').textContent.includes('业务口径待确认'));
    await click('[data-build-semantics]');
    assert.ok($('#semantic-workbench').textContent.includes('测试语义完成'));
    const dictionary=$('#semantic-workbench'),dictionarySearch=$('[data-semantic-search]'),pendingOnly=$('[data-semantic-pending]');
    dictionarySearch.value='金额';dictionarySearch.dispatchEvent(new window.Event('input',{bubbles:true}));
    pendingOnly.checked=true;pendingOnly.dispatchEvent(new window.Event('change',{bubbles:true}));
    modal.scrollTop=275;
    await click('[data-semantic-preview="amount"]');
    assert.ok(dictionary.hidden);assert.ok($('#field-preview').textContent.includes('返回业务字典'));
    assert.equal($('#modal-title').textContent,'事实表[金额]');
    await click('[data-field-back]');
    assert.equal($('#semantic-workbench'),dictionary);assert.equal(dictionary.hidden,false);
    assert.equal($('[data-semantic-search]'),dictionarySearch);assert.equal(dictionarySearch.value,'金额');
    assert.equal($('[data-semantic-pending]'),pendingOnly);assert.equal(pendingOnly.checked,true);
    assert.equal(modal.scrollTop,275);assert.equal($('#modal-title').textContent,'模型语义层 · 业务词典');
    assert.equal($('#field-preview'),null);assert.ok(modal.open);
    await click('[data-semantic-preview="amount"]');
    await click('#modal-close');
    assert.ok(modal.open);assert.equal(dictionary.hidden,false);assert.equal(modal.scrollTop,275);
    let finishPreview;pendingPreview=new Promise(resolve=>{finishPreview=resolve;});
    await click('[data-semantic-preview="amount"]');
    assert.equal($('[data-field-back]').disabled,false);
    const cancel=new window.Event('cancel',{cancelable:true});modal.dispatchEvent(cancel);
    assert.ok(cancel.defaultPrevented);assert.ok(modal.open);assert.equal(dictionary.hidden,false);
    finishPreview();await tick();await tick();
    assert.equal($('#semantic-workbench'),dictionary);assert.equal($('#field-preview'),null);
    assert.equal($('#modal-title').textContent,'模型语义层 · 业务词典');
    const dictionaryPreviewCount=previewCalls.length;
    await click('#modal-close');
    assert.equal(modal.open,false);
    draft('预览不改动这段草稿');
    await click('[data-preview-column="金额"]');
    assert.equal(previewCalls.length,dictionaryPreviewCount+1);
    assert.deepEqual(previewCalls.at(-1),{table:'事实表',column:'金额',search:'',limit:50});
    assert.equal($('[data-field-back]'),null);
    assert.ok($('#modal-body').textContent.includes('125'));
    assert.ok($('#modal-title').textContent.includes('事实表[金额]'));
    assert.equal($('#prompt').value,'预览不改动这段草稿');
    await click('#modal-close');
    await click('[data-mode="analysis"]');assert.equal($('#page-title').textContent,'数据分析助手');
    const imageFile=Object.assign(new Blob(['screenshot'],{type:'image/png'}),{name:'截图.png'});
    const paste=new window.Event('paste',{bubbles:true,cancelable:true});
    Object.defineProperty(paste,'clipboardData',{value:{items:[{kind:'file',type:'image/png',getAsFile:()=>imageFile}]}});
    $('#prompt').dispatchEvent(paste);await tick();await tick();
    assert.equal(uploads.length,1);assert.equal($('[data-image-drafts]').querySelectorAll('.image-draft').length,1);
    await click('[data-mode="dax"]');assert.equal($('[data-image-drafts]').hidden,true);
    await click('[data-mode="analysis"]');assert.equal($('[data-image-drafts]').querySelectorAll('.image-draft').length,1);
    await send('分析请求');const analysis=streams.get('c1');assert.ok(analysis);
    assert.deepEqual(analysis.body.image_ids,[imageMeta.id]);assert.equal(analysis.body.images,undefined);
    assert.equal($('[data-image-drafts]').hidden,true);
    assert.ok($('#messages').querySelector('.message-image img'));
    await click('[data-open-image]');assert.ok($('#modal-body').querySelector('.image-full-preview'));await click('#modal-close');
    analysis.emit({type:'start',message_id:'answer-c1'});analysis.emit({type:'delta',text:'第一段分析。'});await tick();await tick();
    assert.equal($('#send-button').disabled,true);
    assert.equal($('#stop-button').disabled,false);assert.equal($('#send-button').getAttribute('aria-label'),'发送消息');
    await click('[data-mode="dax"]');draft('DAX 页面草稿');
    assert.equal($('#page-title').textContent,'DAX 助手');assert.equal($('#send-button').disabled,false);
    assert.equal($('#stop-button').disabled,true);
    assert.equal($('#background-generations').hidden,false);
    analysis.emit({type:'delta',text:'后台续写。'});await tick();await tick();
    assert.ok(!$('#messages').textContent.includes('后台续写'));
    await click('[data-mode="analysis"]');
    assert.ok($('#messages').textContent.includes('第一段分析。后台续写。'));
    assert.equal($('#send-button').disabled,true);
    await click('[data-mode="dax"]');assert.equal($('#prompt').value,'DAX 页面草稿');
    await send('DAX 请求');const dax=streams.get('c2');assert.ok(dax);assert.equal($('#send-button').disabled,true);
    assert.equal($('#refresh-model').disabled,true);
    analysis.emit({type:'done'});analysis.controller.close();await tick();await tick();await tick();
    assert.equal($('#page-title').textContent,'DAX 助手');assert.equal($('#send-button').disabled,true);
    assert.ok(!$('#messages').textContent.includes('第一段分析'));
    assert.ok(document.querySelector('.history-task.complete'));
    dax.emit({type:'start',message_id:'answer-c2'});dax.emit({type:'delta',text:'DAX 回复。'});
    dax.emit({type:'artifacts',artifacts:[{id:'measure-test',kind:'dax_measure',name:'候选金额',home_table:'事实表',
      expression:'SUM(事实表[金额])',validation:{ok:true,run_verified:true,errors:[],warnings:[]}},
      {id:'column-test',kind:'dax_column',name:'新金额列',home_table:'事实表',expression:'事实表[金额] * 2',
        validation:{ok:true,run_verified:true,verification_scope:'row_sample',sample:'50 行',errors:[],warnings:['仅抽样检查']}}]});
    dax.emit({type:'done'});dax.controller.close();await tick();await tick();await tick();
    assert.equal($('#send-button').disabled,false);assert.ok($('#messages').textContent.includes('DAX 回复。'));
    await click('[data-apply="measure-test"]');
    assert.equal($('#write-measure-name').value,'候选金额');assert.equal($('#write-home-table').value,'事实表');
    $('#write-measure-name').value='   ';await click('#confirm-write');assert.equal(writes.length,0);
    $('#write-measure-name').value='自定义金额';$('#write-measure-name').dispatchEvent(new window.Event('input'));
    $('#write-home-table').value='度量表';$('#write-home-table').dispatchEvent(new window.Event('change'));
    $('#overwrite-measure').checked=true;
    assert.ok($('#write-target-notice').textContent.includes('自定义金额，存放于 度量表'));
    await click('#confirm-write');
    assert.equal(modal.open,true);assert.equal($('#confirm-write').disabled,false);
    assert.equal($('#write-measure-name').value,'自定义金额');assert.equal($('#write-home-table').value,'度量表');
    await click('#confirm-write');
    assert.deepEqual(writes.at(-1),{name:'自定义金额',home_table:'度量表',overwrite:true});
    assert.equal(modal.open,false);assert.ok($('#messages').textContent.includes('自定义金额'));
    assert.ok($('[data-apply="measure-test"]').textContent.includes('已写入模型'));
    assert.ok($('#messages').textContent.includes('计算列 · 事实表[新金额列]'));
    assert.ok($('#messages').textContent.includes('Desktop 行上下文抽样通过'));
    await click('[data-apply="column-test"]');
    assert.ok($('#modal-body').textContent.includes('计算列名称')&&$('#modal-body').textContent.includes('所属表'));
    assert.ok($('#modal-body').textContent.includes('执行完整计算'));
    assert.equal($('#overwrite-measure'),null);
    assert.equal($('#write-measure-name').value,'新金额列');assert.equal($('#write-home-table').value,'事实表');
    $('#write-measure-name').value='实际计算列';$('#write-measure-name').dispatchEvent(new window.Event('input'));
    await click('#confirm-write');
    assert.deepEqual(writes.at(-1),{name:'实际计算列',home_table:'事实表',overwrite:false});
    assert.ok($('#messages').textContent.includes('计算列 · 事实表[实际计算列]'));
    assert.ok($('[data-apply="column-test"]').textContent.includes('已写入模型'));
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
    firstQuestion.emit({type:'field_previews',records:[fieldRecord]});await tick();await tick();
    assert.ok($('#messages').querySelector('[data-field-evidence]'));
    firstQuestion.emit({type:'clarification',clarification:card});
    firstQuestion.emit({type:'delta',text:'存储用的问题及选项，不应在卡片上方重复显示。'});
    firstQuestion.emit({type:'done'});firstQuestion.controller.close();await tick();await tick();await tick();
    assert.equal(document.querySelectorAll('[data-clarification] h3').length,1);
    assert.ok(!$('#messages').textContent.includes('存储用的问题'));
    assert.ok($('#messages').querySelector('[data-field-evidence]'));
    await click('[data-field-evidence] [data-preview-column]');assert.equal(previewCalls.length,dictionaryPreviewCount+2);
    await click('#modal-close');
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
    await send('中止测试');const stopped=streams.get('c1');
    stopped.emit({type:'start',message_id:'stopped-answer'});stopped.emit({type:'delta',text:'这段文字需要保留。'});
    await tick();await tick();
    assert.equal($('#stop-button').disabled,false);assert.equal($('#send-button').disabled,true);
    await click('#stop-button');await tick();await tick();
    assert.equal(cancellations.at(-1).generation_id,stopped.body.generation_id);
    assert.equal($('#stop-button').disabled,true);assert.equal($('#send-button').disabled,false);
    assert.ok($('#messages').textContent.includes('这段文字需要保留。'));
    assert.ok($('#messages').textContent.includes('已中止'));

    // A stale/unfinished delta must never block sending when a historical dictionary exists.
    for(const status of ['inventory','building','error']){
      semanticProfile={...semanticProfile,status,coverage:0,has_history:true,entries:semanticProfile.entries.map(e=>({
        ...e,state:'unresolved',meaning:'',previous_definition:{meaning:'原有金额定义',state:'confirmed'}}))};
      await click('[data-open-semantics]');await click('#modal-close');
      const previous=streams.get('c1');await send('使用历史语义继续提问 '+status);
      const current=streams.get('c1');assert.notEqual(current,previous);
      assert.equal(current.body.content,'使用历史语义继续提问 '+status);
      assert.equal(modal.open,false);assert.equal($('#prompt').value,'');
      current.emit({type:'start',message_id:'delta-'+status});current.emit({type:'delta',text:'按可用语义回答。'});
      current.emit({type:'done'});current.controller.close();await tick();await tick();await tick();
    }

    // Synchronization keeps the conversation, history and edits, even after navigation.
    const conversationCount=conversations.length;
    const unsentImage=new window.Event('paste',{bubbles:true,cancelable:true});
    Object.defineProperty(unsentImage,'clipboardData',{value:{items:[{kind:'file',type:'image/png',getAsFile:()=>imageFile}]}});
    $('#prompt').dispatchEvent(unsentImage);await tick();await tick();
    assert.equal($('[data-image-drafts]').querySelectorAll('.image-draft').length,1);
    draft('同步前草稿');let releaseSync;
    pendingSync=new Promise(resolve=>{releaseSync=resolve;});
    await click('#refresh-model');assert.equal($('#send-button').disabled,true);
    assert.equal($('#refresh-model').disabled,true);
    draft('同步期间继续输入的草稿');await click('[data-mode="dax"]');draft('另一个页面的草稿');
    const foreignTree=$('#model-tree').firstChild;
    releaseSync();await tick();await tick();await tick();
    assert.equal($('#page-title').textContent,'DAX 助手');assert.equal($('#prompt').value,'另一个页面的草稿');
    assert.equal($('#model-tree').firstChild,foreignTree);
    await click('[data-conversation="c1"]');
    assert.equal($('#prompt').value,'同步期间继续输入的草稿');assert.equal(conversations.length,conversationCount);
    assert.equal(syncRequests.at(-1).conversationId,'c1');assert.ok($('#model-tree').textContent.includes('同步新增字段'));
    assert.ok($('#messages').textContent.includes('第一段分析。后台续写。'));
    assert.ok($('#messages').textContent.includes('同步前回复'));
    assert.equal($('[data-image-drafts]').querySelectorAll('.image-draft').length,1);
    const imageDraft=$('[data-image-drafts]').querySelector('.image-draft');
    const imageKey=imageDraft.dataset.imageKey,imageSource=imageDraft.querySelector('img').getAttribute('src');
    const article=$('#messages').querySelector('article.message.assistant');
    const contentNode=article.querySelector('.message-content'),caption=$('#conversation-caption').textContent;
    await click('#refresh-model');
    assert.equal($('#messages').querySelector('article.message.assistant'),article);
    assert.equal(article.querySelector('.message-content'),contentNode);
    assert.equal($('[data-image-drafts]').querySelector('.image-draft').dataset.imageKey,imageKey);
    assert.equal($('[data-image-drafts]').querySelector('img').getAttribute('src'),imageSource);
    assert.equal($('#conversation-caption').textContent,caption);
    assert.equal($('#prompt').value,'同步期间继续输入的草稿');assert.equal(conversations.length,conversationCount);
    failSync=true;const successfulModel=conversations.find(c=>c.id==='c1').model_id;
    await click('#refresh-model');
    assert.equal(conversations.find(c=>c.id==='c1').model_id,successfulModel);
    assert.equal($('#send-button').disabled,false);assert.equal($('#refresh-model').disabled,false);
    assert.equal($('#prompt').value,'同步期间继续输入的草稿');
    await send('同步后在原对话继续');const followup=streams.get('c1');
    assert.equal(conversations.length,conversationCount);assert.equal(followup.body.content,'同步后在原对话继续');
    followup.emit({type:'start',message_id:'after-sync'});followup.emit({type:'delta',text:'使用新结构继续回答。'});
    followup.emit({type:'done'});followup.controller.close();await tick();await tick();await tick();
    assert.ok(!$('#messages').lastElementChild.querySelector('.message-label').textContent.includes('同步前回复'));
    await click('#new-chat');draft('尚未发送的草稿');await click('#refresh-model');
    assert.equal(conversations.length,conversationCount);assert.equal($('#prompt').value,'尚未发送的草稿');
    assert.ok(syncRequests.at(-1).modelId);
  } finally {
    Object.defineProperty(selectProto,'value',valueDescriptor);
    for(const [key,descriptor] of Object.entries(savedGlobals)){
      if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];
    }
  }
});
