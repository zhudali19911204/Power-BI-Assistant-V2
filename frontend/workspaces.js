import {readEvents} from './stream.js';

export function createWorkspaces() {
  let nextId=0,current;
  const modes=new Map(),conversations=new Map(),views=new Set();
  function make(mode,model=null,conversation=null) {
    const view={key:`view-${++nextId}`,mode,model,conversation,messages:[],points:[],draft:'',images:[],clarificationDrafts:{},queryName:'',
      busy:false,stopping:false,status:'',startedAt:0,attention:''};
    views.add(view);if(conversation)conversations.set(conversation.id,view);return view;
  }
  function activate(view) {current=view;modes.set(view.mode,view);view.attention='';return view;}
  activate(make('dax'));
  return {
    get current(){return current;},
    all:()=>[...views],
    find:id=>conversations.get(id),
    newChat:(mode=current.mode,model=current.model)=>activate(make(mode,model)),
    switchMode:mode=>activate(modes.get(mode)||make(mode,current.model)),
    open:(conversation,model)=>activate(conversations.get(conversation.id)||make(conversation.mode,model,conversation)),
    register:(conversation,model)=>conversations.get(conversation.id)||make(conversation.mode,model,conversation),
    bind(view,conversation){view.conversation=conversation;view.loaded=true;conversations.set(conversation.id,view);},
    remove(id){const view=conversations.get(id);if(!view)return;conversations.delete(id);views.delete(view);
      if(modes.get(view.mode)===view)modes.delete(view.mode);if(current===view)activate(make(view.mode,view.model));},
  };
}

// Each request owns its view. Navigation only selects a view; it never cancels a reader.
export function createGenerationManager({workspace,api,fetchResponse=fetch,onChange=()=>{},onCreated=()=>{},
  onModel=()=>{},onComplete=()=>{},onError=()=>{},pause=ms=>new Promise(resolve=>setTimeout(resolve,ms)),now=Date.now}) {
  const running=new Map();
  const stopping=new Map();
  const changed=view=>onChange(view);
  function reconcile(view,messages) {
    if(!messages.length&&view.messages.some(m=>m.status==='streaming'))return;
    view.messages=messages.map(message=>{
      const prior=view.messages.find(m=>m.id&&m.id===message.id);
      if(message.status==='streaming'&&prior?.content.startsWith(message.content)) {
        return {...message,content:prior.content,artifacts:prior.artifacts?.length?prior.artifacts:message.artifacts,
          field_previews:prior.field_previews?.length>message.field_previews?.length?prior.field_previews:message.field_previews,
          analysis:prior.analysis?.length>message.analysis?.length?prior.analysis:message.analysis};
      }
      return message;
    });
  }
  async function snapshot(view) {
    const id=view.conversation.id;
    let [messages,status]=await Promise.all([api(`/conversations/${id}/messages`),api(`/conversations/${id}/generation`)]);
    if(!status.running&&messages.some(message=>message.status==='streaming'))
      messages=await api(`/conversations/${id}/messages`);
    reconcile(view,messages);
    if(status.generation_id)view.generationId=status.generation_id;
    if(status.stopping)view.stopping=true;
    if(messages.at(-1)?.status==='cancelled')view.cancelled=true;
    if(status.progress?.stage)view.status=status.progress.stage;
    if(status.progress?.elapsed&&!view.startedAt)view.startedAt=now()-status.progress.elapsed*1000;
    changed(view);return status;
  }
  async function poll(view) {
    const deadline=now()+(view.mode==='analysis'?1200000:600000);
    let failures=0;
    while(true) {
      if(now()>deadline)throw new Error('后台任务长时间未完成，请检查服务后重新打开此会话');
      try {
        const status=await snapshot(view);failures=0;
        if(!status.running)return;
      } catch(error) {if(++failures>=3)throw error;}
      await pause(1000);
    }
  }
  function finish(view) {
    view.busy=false;view.stopping=false;view.stopRequested=false;view.requestReady=false;view.startedAt=0;
    if(view.messages.at(-1)?.status==='cancelled')view.cancelled=true;
    const failed=view.failed||view.messages.at(-1)?.status==='error';
    const last=view.messages.at(-1),pending=last?.status==='complete'&&last.clarification&&!last.clarification.answered;
    view.attention=workspace.current===view?'':view.cancelled?'cancelled':failed?'error':pending?'clarification':'complete';
    changed(view);onComplete(view,failed);
  }
  function watch(view,work) {
    if(running.has(view))return running.get(view);
    // Register before any async work, including creation of a new conversation.
    view.busy=true;view.failed=false;view.cancelled=false;view.stopping=false;view.stopRequested=false;
    view.runId={};view.requestReady=false;view.startedAt=view.startedAt||now();view.attention='';changed(view);
    const promise=Promise.resolve().then(work).catch(error=>{
      view.failed=true;
      const last=view.messages.at(-1);
      if(last?.status==='streaming'){last.status='error';last.content+='\n\n'+error.message;}
      onError(error.message,view);
    }).finally(()=>{running.delete(view);finish(view);});
    running.set(view,promise);return promise;
  }
  async function cancelRemote(view) {
    const runId=view.runId;
    if(stopping.has(runId))return stopping.get(runId);
    const work=(async()=>{
      try {
        let generationId=view.generationId;
        if(!generationId) {
          const status=await api(`/conversations/${view.conversation.id}/generation`);
          generationId=status.generation_id;
          if(view.runId!==runId||!view.busy)return;
          if(!status.running)return;
          if(!generationId)throw new Error('未取得本次生成标识，请刷新会话状态后重试中止');
        }
        await api(`/conversations/${view.conversation.id}/cancel`,{
          method:'POST',body:JSON.stringify({generation_id:generationId}),
        });
        if(view.runId!==runId||!view.busy)return;
        view.status='正在中止本次生成…';changed(view);
      } catch(error) {
        if(view.runId!==runId||!view.busy)return;
        view.stopping=false;view.stopRequested=false;changed(view);onError(error.message,view);
      }
    })().finally(()=>stopping.delete(runId));
    stopping.set(runId,work);return work;
  }
  return {
    isRunning:view=>running.has(view),
    stop(view) {
      if(!running.has(view)||view.stopping)return stopping.get(view.runId)||Promise.resolve();
      view.stopping=true;view.stopRequested=true;view.status='正在中止本次生成…';changed(view);
      // A request that has not reached the server is stopped locally. An in-flight
      // submission waits for its response headers, so cancellation cannot race registration.
      if(view.requestReady&&view.conversation)return cancelRemote(view);
      return Promise.resolve();
    },
    follow(view){
      const pending=view.messages.find(m=>m.status==='streaming');
      if(!view.startedAt&&pending?.progress?.elapsed)view.startedAt=now()-pending.progress.elapsed*1000;
      view.generationId=null;
      return watch(view,async()=>{view.requestReady=true;if(view.stopRequested)await cancelRemote(view);return poll(view);});
    },
    send(view,request) {
      if(running.has(view))return running.get(view);
      // Copy filters and service selection before the user navigates or edits another page.
      const body=structuredClone(request),previous=view.messages.slice();
      body.generation_id=globalThis.crypto.randomUUID().replaceAll('-','');
      view.generationId=body.generation_id;
      const submittedImages=body.images||[];delete body.images;
      const answer={role:'assistant',content:'',status:'streaming',artifacts:[],analysis:[]};
      view.messages.push({role:'user',content:body.content,images:submittedImages,artifacts:[]},answer);
      view.images=view.images.filter(img=>!body.image_ids?.includes(img.id));
      if(!body.clarification_reply)view.draft='';
      view.status='正在等待模型回复';
      function restoreUnsent() {
        view.cancelled=true;view.messages=previous;view.draft=view.draft||(body.clarification_reply?'':body.content);
        view.images=[...submittedImages,...view.images.filter(img=>!submittedImages.some(old=>old.id===img.id))];
      }
      return watch(view,async()=>{
        let rejected=false,needsPolling=false;
        try {
          if(view.stopRequested){restoreUnsent();return;}
          if(!view.conversation) {
            const conversation=await api('/conversations',{method:'POST',body:JSON.stringify({
              model_id:view.model.id,mode:view.mode,query_name:body.query_name||'',title:body.content.slice(0,36),
            })});
            workspace.bind(view,conversation);onCreated(conversation,view);changed(view);
          }
          const id=view.conversation.id;
          if(view.stopRequested){restoreUnsent();return;}
          delete body.query_name;
          const response=await fetchResponse(`/api/conversations/${id}/messages`,{
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),
          });
          if(!response.ok) {
            rejected=true;
            const result=await response.json();
            throw new Error(typeof result.detail==='string'?result.detail:JSON.stringify(result.detail));
          }
          view.requestReady=true;
          if(view.stopRequested)await cancelRemote(view);
          await readEvents(response,event=>{
            if(event.type==='start')answer.id=event.message_id;
            if(event.type==='model')onModel(view,event);
            if(event.type==='delta'){answer.content+=event.text;view.status='模型正在生成回复';}
            if(event.type==='reset')answer.content+='\n\n---\n\n### 验证修订\n\n';
            if(event.type==='status')view.status=event.text;
            if(event.type==='artifacts')answer.artifacts=event.artifacts;
            if(event.type==='analysis')answer.analysis=event.queries;
            if(event.type==='field_previews')answer.field_previews=event.records;
            if(event.type==='clarification')answer.clarification=event.clarification;
            if(event.type==='done')answer.status='complete';
            if(event.type==='cancelled'){
              view.cancelled=true;answer.status='cancelled';answer.content+='\n\n['+event.text+']';
            }
            if(event.type==='error'){view.failed=true;answer.status='error';answer.content+='\n\n'+event.text;onError(event.text,view);}
            if(event.type==='background')needsPolling=true;
            if(event.type!=='heartbeat')changed(view);
          });
          if(needsPolling)await poll(view);
          else reconcile(view,await api(`/conversations/${id}/messages`));
        } catch(error) {
          if(!rejected&&view.conversation) {
            // A disconnected viewer does not imply a failed server task. Never resubmit it.
            try {
              await poll(view);
              if(view.messages.length>previous.length&&!view.messages.some(m=>m.status==='streaming'))return;
            }
            catch { /* Preserve received content and report the original transport error. */ }
          }
          if(rejected||!view.conversation||view.messages.length<=previous.length){view.messages=previous;view.draft=view.draft||(body.clarification_reply?'':body.content);
            view.images=[...submittedImages,...view.images.filter(img=>!submittedImages.some(old=>old.id===img.id))];
          }
          throw error;
        }
      });
    },
  };
}
