import test from 'node:test';
import assert from 'node:assert/strict';
import {createWorkspaces,createGenerationManager} from '../frontend/workspaces.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
const model={id:'model',source:'desktop'};
const conversation=(id,mode='dax')=>({id,title:id,mode,model_id:'model',query_name:''});
function stream() {
  let controller;
  const body=new ReadableStream({start(value){controller=value;}});
  return {response:new Response(body),event:event=>controller.enqueue(new TextEncoder().encode(JSON.stringify(event)+'\n')),
    close:()=>controller.close()};
}

function fixture() {
  const workspace=createWorkspaces(),streams=new Map(),saved=new Map(),statuses=new Map(),sent=[],completed=[],errors=[],cancelled=[];
  let stopError='';
  workspace.current.model=model;
  const api=async(path,opts={})=>{
    const id=path.split('/')[2];
    if(path==='/conversations')return conversation('new-'+sent.length);
    if(path.endsWith('/cancel')){
      if(stopError)throw new Error(stopError);
      cancelled.push({id,body:JSON.parse(opts.body)});
      const view=workspace.find(id),answer=view.messages.at(-1);
      saved.set(id,[{...answer,status:'cancelled',content:answer.content+'\n\n[已中止]'}]);
      statuses.set(id,{running:false});
      streams.get(id)?.event({type:'cancelled',text:'已中止'});streams.get(id)?.close();
      return {running:false};
    }
    if(path.endsWith('/messages'))return structuredClone(saved.get(id)||[]);
    if(path.endsWith('/generation'))return statuses.get(id)||{running:false};
    throw new Error('Unexpected '+path);
  };
  const manager=createGenerationManager({workspace,api,fetchResponse:async(path,opts)=>{
    const id=path.split('/')[3];sent.push({id,body:JSON.parse(opts.body)});return streams.get(id).response;
  },onComplete:(view,failed)=>completed.push({view,failed}),onError:message=>errors.push(message),pause:tick});
  return {workspace,manager,streams,saved,statuses,sent,completed,errors,cancelled,failStop:message=>{stopError=message;}};
}

test('switching assistants preserves drafts, filters and the running page',()=>{
  const w=createWorkspaces(),dax=w.current;dax.model=model;dax.draft='未发送的 DAX';dax.points=[{expected:10}];dax.busy=true;
  const analysis=w.switchMode('analysis');analysis.draft='分析草稿';
  assert.equal(analysis.model,model);assert.equal(analysis.busy,false);
  assert.equal(w.switchMode('dax'),dax);assert.equal(dax.draft,'未发送的 DAX');
  assert.deepEqual(dax.points,[{expected:10}]);assert.equal(dax.busy,true);
  assert.equal(w.switchMode('analysis').draft,'分析草稿');
});

test('image attachments are pinned to the submitted view and only IDs reach the backend',async()=>{
  const f=fixture(),owner=f.workspace.open(conversation('A'),model),s=stream();f.streams.set('A',s);
  const image={id:'a'.repeat(32),name:'截图.png',url:'/api/images/'+'a'.repeat(32),status:'ready'};
  owner.images=[image];
  const pending=f.manager.send(owner,{content:'看图',image_ids:[image.id],images:[image]});
  assert.equal(owner.images.length,0);assert.equal(owner.messages[0].images[0].id,image.id);
  f.workspace.switchMode('analysis');await tick();
  assert.deepEqual(f.sent[0].body.image_ids,[image.id]);assert.equal(f.sent[0].body.images,undefined);
  assert.equal(f.workspace.current.images.length,0);
  f.saved.set('A',[{role:'user',content:'看图',images:[image]},{role:'assistant',content:'完成',status:'complete'}]);
  s.event({type:'done'});s.close();await pending;
  assert.equal(owner.messages[0].images[0].id,image.id);
});

test('early rejection restores image attachments for retry without clearing another draft',async()=>{
  const workspace=createWorkspaces(),owner=workspace.open(conversation('A'),model);
  const image={id:'a'.repeat(32),name:'截图.png',status:'ready'};owner.images=[image];
  const manager=createGenerationManager({workspace,api:async()=>[],fetchResponse:async()=>
    new Response(JSON.stringify({detail:'请先检查图片'}),{status:422,headers:{'Content-Type':'application/json'}})});
  const pending=manager.send(owner,{content:'看图',image_ids:[image.id],images:[image]});
  const other=workspace.switchMode('analysis');other.images=[{id:'b'.repeat(32)}];
  await pending;
  assert.equal(owner.images[0].id,image.id);assert.equal(owner.draft,'看图');
  assert.equal(owner.messages.length,0);assert.equal(other.images[0].id,'b'.repeat(32));
});

test('creating a conversation after navigation binds only its owning view',async()=>{
  const f=fixture(),owner=f.workspace.current,s=stream();f.streams.set('new-0',s);
  const pending=f.manager.send(owner,{content:'原会话需求',home_table:'表',points:[],llm_profile_id:'old-profile'});
  const other=f.workspace.switchMode('analysis');other.draft='另一页';
  await tick();s.event({type:'start',message_id:'a'});s.event({type:'delta',text:'后台结果'});await tick();
  assert.equal(f.workspace.current,other);assert.equal(other.conversation,null);assert.equal(other.messages.length,0);
  assert.equal(owner.conversation.id,'new-0');assert.equal(owner.messages.at(-1).content,'后台结果');
  f.saved.set('new-0',[{id:'a',role:'assistant',content:'后台结果',status:'complete'}]);
  s.event({type:'done'});s.close();await pending;
  assert.equal(other.draft,'另一页');assert.equal(other.busy,false);
  assert.equal(owner.attention,'complete');assert.equal(f.sent[0].body.llm_profile_id,'old-profile');
});

test('two concurrent streams keep prose, artifacts and analysis in their own conversations',async()=>{
  const f=fixture(),a=f.workspace.open(conversation('A'),model),sa=stream();f.streams.set('A',sa);
  const pa=f.manager.send(a,{content:'A需求',points:[]});await tick();
  const b=f.workspace.open(conversation('B','analysis'),model),sb=stream();f.streams.set('B',sb);
  const pb=f.manager.send(b,{content:'B需求',points:[]});await tick();
  sa.event({type:'delta',text:'A正文'});sa.event({type:'artifacts',artifacts:[{id:'artifact-A'}]});
  sb.event({type:'delta',text:'B正文'});sb.event({type:'analysis',queries:[{id:'query-B'}]});await tick();
  assert.equal(a.messages.at(-1).content,'A正文');assert.equal(b.messages.at(-1).content,'B正文');
  assert.deepEqual(a.messages.at(-1).artifacts,[{id:'artifact-A'}]);assert.equal(a.messages.at(-1).analysis.length,0);
  assert.deepEqual(b.messages.at(-1).analysis,[{id:'query-B'}]);assert.equal(b.messages.at(-1).artifacts.length,0);
  f.saved.set('A',[{role:'assistant',status:'complete',content:'A正文'}]);sa.event({type:'done'});sa.close();await pa;
  assert.equal(b.busy,true);assert.equal(f.workspace.current,b);assert.equal(a.attention,'complete');
  f.saved.set('B',[{role:'assistant',status:'complete',content:'B正文'}]);sb.event({type:'done'});sb.close();await pb;
  assert.equal(b.busy,false);assert.equal(b.attention,'');
});

test('the same conversation cannot accidentally submit a second request while navigating',async()=>{
  const f=fixture(),a=f.workspace.open(conversation('A'),model),s=stream();f.streams.set('A',s);
  const first=f.manager.send(a,{content:'第一次',points:[]});
  const duplicate=f.manager.send(a,{content:'第二次',points:[]});
  assert.equal(first,duplicate);assert.equal(a.messages.length,2);await tick();assert.equal(f.sent.length,1);
  f.saved.set('A',[{role:'assistant',content:'完成',status:'complete'}]);s.event({type:'done'});s.close();await first;
});

test('a detached HTTP stream follows the server task without issuing another generation',async()=>{
  const f=fixture(),a=f.workspace.open(conversation('A'),model),s=stream();f.streams.set('A',s);
  const pending=f.manager.send(a,{content:'分析',points:[]});await tick();
  s.event({type:'start',message_id:'answer-A'});s.event({type:'delta',text:'已显示原文'});
  f.saved.set('A',[{id:'answer-A',role:'assistant',content:'已显示原文及后台结果',status:'complete'}]);
  s.event({type:'background'});s.close();await pending;
  assert.equal(f.sent.length,1);assert.equal(a.messages.at(-1).content,'已显示原文及后台结果');
  assert.equal(a.busy,false);assert.deepEqual(f.errors,[]);
});

test('polling a recovered task preserves longer displayed text until the server snapshot catches up',async()=>{
  const w=createWorkspaces(),a=w.open(conversation('A'),model);
  a.messages=[{id:'answer',role:'assistant',status:'streaming',content:'已经显示的完整段落'}];
  let polls=0;const seen=[];
  const manager=createGenerationManager({workspace:w,api:async path=>path.endsWith('/messages')?
    [{id:'answer',role:'assistant',status:polls?'complete':'streaming',content:polls?'已经显示的完整段落及结果':'已经显示'}]:
    {running:polls++===0},pause:tick,onChange:view=>seen.push(view.messages.at(-1)?.content)});
  const pending=manager.follow(a);w.switchMode('analysis');await pending;
  assert.ok(!seen.includes('已经显示'));assert.equal(a.messages.at(-1).content,'已经显示的完整段落及结果');
  assert.equal(w.current.mode,'analysis');assert.equal(a.attention,'complete');
});

test('a background error does not change the current page or unlock another running conversation',async()=>{
  const f=fixture(),a=f.workspace.open(conversation('A'),model),s=stream();f.streams.set('A',s);
  const pending=f.manager.send(a,{content:'分析',points:[]});await tick();
  const b=f.workspace.newChat('analysis',model);b.draft='保留草稿';b.busy=true;
  s.event({type:'error',text:'测试失败'});s.close();
  f.saved.set('A',[{role:'assistant',content:'[测试失败]',status:'error'}]);await pending;
  assert.equal(f.workspace.current,b);assert.equal(b.busy,true);assert.equal(b.draft,'保留草稿');
  assert.equal(a.attention,'error');assert.deepEqual(f.errors,['测试失败']);
});

test('stop targets only its own generation and keeps prose, drafts and another running page',async()=>{
  const f=fixture(),a=f.workspace.open(conversation('A'),model),sa=stream();f.streams.set('A',sa);
  const pa=f.manager.send(a,{content:'A请求'});await tick();
  sa.event({type:'start',message_id:'answer-A'});sa.event({type:'delta',text:'A已输出文字'});await tick();
  a.draft='新草稿';
  const b=f.workspace.open(conversation('B','analysis'),model),sb=stream();f.streams.set('B',sb);
  const pb=f.manager.send(b,{content:'B请求'});await tick();
  await Promise.all([f.manager.stop(a),f.manager.stop(a)]);await pa;
  assert.equal(f.cancelled.length,1);assert.equal(f.cancelled[0].id,'A');
  assert.equal(f.cancelled[0].body.generation_id,f.sent[0].body.generation_id);
  assert.equal(a.messages.at(-1).status,'cancelled');assert.ok(a.messages.at(-1).content.includes('A已输出文字'));
  assert.equal(a.draft,'新草稿');assert.equal(a.busy,false);assert.equal(a.attention,'cancelled');
  assert.equal(b.busy,true);assert.equal(f.workspace.current,b);assert.deepEqual(f.errors,[]);
  f.saved.set('B',[{role:'assistant',content:'B完成',status:'complete'}]);sb.event({type:'done'});sb.close();await pb;
});

test('stop before submission restores text and attachments without starting a generation',async()=>{
  const f=fixture(),a=f.workspace.current;
  const image={id:'a'.repeat(32),status:'ready'};a.images=[image];
  const pending=f.manager.send(a,{content:'尚未提交',image_ids:[image.id],images:[image]});
  await f.manager.stop(a);await pending;
  assert.equal(f.sent.length,0);assert.equal(f.cancelled.length,0);
  assert.equal(a.draft,'尚未提交');assert.deepEqual(a.images,[image]);assert.deepEqual(a.messages,[]);
  assert.equal(a.conversation,null);
  assert.equal(a.busy,false);assert.equal(a.cancelled,true);
});

test('stop during submission waits for acceptance before cancelling and does not resubmit',async()=>{
  const workspace=createWorkspaces(),view=workspace.open(conversation('A'),model),s=stream(),calls=[];
  let accept;
  const response=new Promise(resolve=>{accept=resolve;});
  const manager=createGenerationManager({workspace,fetchResponse:()=>response,api:async(path,opts)=>{
    calls.push(path);
    if(path.endsWith('/cancel')){s.event({type:'cancelled',text:'已中止'});s.close();return {};}
    if(path.endsWith('/messages'))return [{role:'assistant',content:'[已中止]',status:'cancelled'}];
    throw new Error('Unexpected '+path);
  }});
  const pending=manager.send(view,{content:'请求'});await tick();await manager.stop(view);
  assert.equal(calls.length,0);assert.equal(view.stopping,true);
  accept(s.response);await pending;
  assert.deepEqual(calls,['/conversations/A/cancel','/conversations/A/messages']);
  assert.equal(view.messages.at(-1).status,'cancelled');assert.equal(view.busy,false);
});

test('a failed stop request leaves generation active and allows another stop attempt',async()=>{
  const f=fixture(),a=f.workspace.open(conversation('A'),model),s=stream();f.streams.set('A',s);
  const pending=f.manager.send(a,{content:'需求'});await tick();
  f.failStop('中止请求暂时失败');await f.manager.stop(a);
  assert.equal(a.stopping,false);assert.equal(a.busy,true);assert.deepEqual(f.errors,['中止请求暂时失败']);
  f.failStop('');await f.manager.stop(a);await pending;
  assert.equal(a.messages.at(-1).status,'cancelled');
});

test('a recovered background generation can be stopped with the server generation ID',async()=>{
  const workspace=createWorkspaces(),view=workspace.open(conversation('A'),model),calls=[];
  view.messages=[{id:'answer',role:'assistant',content:'之前的文字',status:'streaming'}];
  let cancelled=false;
  const manager=createGenerationManager({workspace,pause:tick,api:async(path,opts)=>{
    if(path.endsWith('/generation'))return {running:!cancelled,generation_id:'b'.repeat(32)};
    if(path.endsWith('/messages'))return [{id:'answer',role:'assistant',content:cancelled?'之前的文字[已中止]':'之前的文字',status:cancelled?'cancelled':'streaming'}];
    if(path.endsWith('/cancel')){calls.push(JSON.parse(opts.body));cancelled=true;return {};}
    throw new Error('Unexpected '+path);
  }});
  const pending=manager.follow(view);await manager.stop(view);await pending;
  assert.deepEqual(calls,[{generation_id:'b'.repeat(32)}]);assert.equal(view.cancelled,true);assert.equal(view.busy,false);
});

test('polling reloads a stale streaming snapshot when cancellation has just finished',async()=>{
  const workspace=createWorkspaces(),view=workspace.open(conversation('A'),model);
  let reads=0;
  const manager=createGenerationManager({workspace,api:async path=>{
    if(path.endsWith('/generation'))return {running:false};
    reads++;
    return [{id:'answer',role:'assistant',content:reads===1?'保留文字':'保留文字[已中止]',status:reads===1?'streaming':'cancelled'}];
  }});
  await manager.follow(view);
  assert.equal(reads,2);assert.equal(view.messages.at(-1).status,'cancelled');
  assert.equal(view.busy,false);assert.equal(view.cancelled,true);
});
