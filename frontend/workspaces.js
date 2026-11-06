import {readEvents} from './stream.js';

export function createWorkspaces() {
  let nextId=0,current;
  const modes=new Map(),conversations=new Map(),views=new Set();
  function make(mode,model=null,conversation=null) {
    const view={key:`view-${++nextId}`,mode,model,conversation,messages:[],points:[],draft:'',clarificationDrafts:{},queryName:'',
      busy:false,status:'',startedAt:0,attention:''};
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
  const changed=view=>onChange(view);
  function reconcile(view,messages) {
    if(!messages.length&&view.messages.some(m=>m.status==='streaming'))return;
    view.messages=messages.map(message=>{
      const prior=view.messages.find(m=>m.id&&m.id===message.id);
      if(message.status==='streaming'&&prior?.content.startsWith(message.content)) {
        return {...message,content:prior.content,artifacts:prior.artifacts?.length?prior.artifacts:message.artifacts,
          analysis:prior.analysis?.length>message.analysis?.length?prior.analysis:message.analysis};
      }
      return message;
    });
  }
  async function snapshot(view) {
    const id=view.conversation.id;
    const [messages,status]=await Promise.all([api(`/conversations/${id}/messages`),api(`/conversations/${id}/generation`)]);
    reconcile(view,messages);
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
    view.busy=false;view.startedAt=0;
    const failed=view.failed||view.messages.at(-1)?.status==='error';
    const last=view.messages.at(-1),pending=last?.status==='complete'&&last.clarification&&!last.clarification.answered;
    view.attention=workspace.current===view?'':failed?'error':pending?'clarification':'complete';
    changed(view);onComplete(view,failed);
  }
  function watch(view,work) {
    if(running.has(view))return running.get(view);
    // Register before any async work, including creation of a new conversation.
    view.busy=true;view.failed=false;view.startedAt=view.startedAt||now();view.attention='';changed(view);
    const promise=Promise.resolve().then(work).catch(error=>{
      view.failed=true;
      const last=view.messages.at(-1);
      if(last?.status==='streaming'){last.status='error';last.content+='\n\n'+error.message;}
      onError(error.message,view);
    }).finally(()=>{running.delete(view);finish(view);});
    running.set(view,promise);return promise;
  }
  return {
    isRunning:view=>running.has(view),
    follow(view){
      const pending=view.messages.find(m=>m.status==='streaming');
      if(!view.startedAt&&pending?.progress?.elapsed)view.startedAt=now()-pending.progress.elapsed*1000;
      return watch(view,()=>poll(view));
    },
    send(view,request) {
      if(running.has(view))return running.get(view);
      // Copy filters and service selection before the user navigates or edits another page.
      const body=structuredClone(request),previous=view.messages.slice();
      const answer={role:'assistant',content:'',status:'streaming',artifacts:[],analysis:[]};
      view.messages.push({role:'user',content:body.content,artifacts:[]},answer);
      if(!body.clarification_reply)view.draft='';
      view.status='正在等待模型回复';
      return watch(view,async()=>{
        let rejected=false,needsPolling=false;
        try {
          if(!view.conversation) {
            const conversation=await api('/conversations',{method:'POST',body:JSON.stringify({
              model_id:view.model.id,mode:view.mode,query_name:body.query_name||'',title:body.content.slice(0,36),
            })});
            workspace.bind(view,conversation);onCreated(conversation,view);changed(view);
          }
          const id=view.conversation.id;
          delete body.query_name;
          const response=await fetchResponse(`/api/conversations/${id}/messages`,{
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),
          });
          if(!response.ok) {
            rejected=true;
            const result=await response.json();
            throw new Error(typeof result.detail==='string'?result.detail:JSON.stringify(result.detail));
          }
          await readEvents(response,event=>{
            if(event.type==='start')answer.id=event.message_id;
            if(event.type==='model')onModel(view,event);
            if(event.type==='delta'){answer.content+=event.text;view.status='模型正在生成回复';}
            if(event.type==='reset')answer.content+='\n\n---\n\n### 验证修订\n\n';
            if(event.type==='status')view.status=event.text;
            if(event.type==='artifacts')answer.artifacts=event.artifacts;
            if(event.type==='analysis')answer.analysis=event.queries;
            if(event.type==='clarification')answer.clarification=event.clarification;
            if(event.type==='done')answer.status='complete';
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
          if(rejected||!view.conversation||view.messages.length<=previous.length){view.messages=previous;view.draft=view.draft||(body.clarification_reply?'':body.content);}
          throw error;
        }
      });
    },
  };
}
