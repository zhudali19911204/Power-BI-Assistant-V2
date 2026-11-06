// Reconcile safe, application-generated markup while retaining existing message and text nodes.
// A streamed token must never replace the chat history or restart message entrance animations.
function sameNode(left, right) {
  return left.nodeType===right.nodeType && left.nodeName===right.nodeName;
}

function patchNode(current, next) {
  if(current.nodeType===3) {
    if(current.data!==next.data) {
      // Most stream updates extend a text node. Preserve it and append only the new suffix.
      if(next.data.startsWith(current.data))current.appendData(next.data.slice(current.data.length));
      else current.data=next.data;
    }
    return;
  }
  if(current.nodeType!==1)return;
  for(const name of current.getAttributeNames()) {
    // Expanded evidence is reader-controlled and should stay open as another query finishes.
    if(current.nodeName==='DETAILS'&&name==='open'&&!next.hasAttribute(name))continue;
    if(!next.hasAttribute(name))current.removeAttribute(name);
  }
  for(const name of next.getAttributeNames()) {
    const value=next.getAttribute(name);
    if(current.getAttribute(name)!==value)current.setAttribute(name,value);
  }
  patchChildren(current,next);
}

function patchChildren(current, next) {
  let index=0;
  for(const wanted of [...next.childNodes]) {
    const existing=current.childNodes[index];
    if(!existing)current.appendChild(wanted.cloneNode(true));
    else if(sameNode(existing,wanted))patchNode(existing,wanted);
    else current.replaceChild(wanted.cloneNode(true),existing);
    index++;
  }
  while(current.childNodes.length>index)current.lastChild.remove();
}

export function patchHtml(element, html) {
  const template=element.ownerDocument.createElement('template');
  // Callers provide escaped Markdown / application templates, never raw model HTML.
  template.innerHTML=html;
  patchChildren(element,template.content);
}

export function createMessageRenderer({root, scroll, label, content, artifacts, status}) {
  let scope=null, records=[], welcomeMarkup=null;
  const document=root.ownerDocument;
  const atBottom=()=>scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<100;
  const follow=()=>scroll.scrollTo({top:scroll.scrollHeight,behavior:'instant'});

  function clear(nextScope) {
    root.replaceChildren();records=[];welcomeMarkup=null;scope=nextScope;
  }

  function createRecord(message) {
    const article=document.createElement('article');
    article.className='message '+message.role;
    const heading=document.createElement('div');heading.className='message-label';
    heading.innerHTML=label(message);
    const body=document.createElement('div');body.className='message-content';
    const cards=document.createElement('div');cards.className='message-artifacts';
    const progress=document.createElement('div');progress.className='stream-status';progress.hidden=true;
    article.append(heading,body,cards,progress);root.appendChild(article);
    return {article,body,cards,progress,role:message.role,bodySource:null,artifactSource:null};
  }

  return {
    render(messages,{conversation, welcome='', forceFollow=false}={}) {
      const shouldFollow=forceFollow||scope!==conversation||atBottom();
      if(scope!==conversation || messages.length<records.length || records.some((r,i)=>r.role!==messages[i]?.role))clear(conversation);
      if(!messages.length) {
        if(welcomeMarkup!==welcome){patchHtml(root,welcome);welcomeMarkup=welcome;}
        return;
      }
      if(welcomeMarkup!==null){root.replaceChildren();welcomeMarkup=null;}
      messages.forEach((message,index)=>{
        const record=records[index]||(records[index]=createRecord(message));
        record.article.classList.toggle('error',message.status==='error');
        record.article.classList.toggle('streaming',message.status==='streaming');
        if(message.id)record.article.dataset.messageId=message.id;
        // Completed messages are untouched when a later message streams.
        const bodySource=JSON.stringify([message.content,!!message.clarification,message.status==='error']);
        if(record.bodySource!==bodySource) {
          patchHtml(record.body,content(message));record.bodySource=bodySource;
        }
        const artifactSource=JSON.stringify({artifacts:message.artifacts||[],analysis:message.analysis||[],
          clarification:message.clarification,clarificationDraft:message.clarificationDraft,clarificationLocked:message.clarificationLocked});
        if(record.artifactSource!==artifactSource) {
          patchHtml(record.cards,artifacts(message));record.artifactSource=artifactSource;
        }
        record.progress.hidden=message.status!=='streaming';
        if(!record.progress.hidden) {
          const text=status(message);
          if(record.progress.textContent!==text)record.progress.textContent=text;
        }
      });
      // Only follow when the reader was already at the bottom. Reading earlier text stays stable.
      if(shouldFollow)follow();
    },
    updateStatus(messages) {
      messages.forEach((message,index)=>{
        const record=records[index];
        if(record && message.status==='streaming') {
          const text=status(message);
          if(record.progress.textContent!==text)record.progress.textContent=text;
        }
      });
    },
  };
}

// Coalesce rapid token frames into one paint without delaying until the answer is complete.
export function createRenderScheduler(render,{request=requestAnimationFrame,cancel=cancelAnimationFrame}={}) {
  let pending=null;
  return {
    schedule() {
      if(pending!==null)return;
      pending=request(()=>{pending=null;render();});
    },
    flush() {
      if(pending!==null){cancel(pending);pending=null;}
      render();
    },
  };
}
