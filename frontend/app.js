import {icon, hydrateIcons} from './icons.js';
import {createMessageRenderer, createRenderScheduler} from './messages.js';
import {escapeHtml as esc, markdown} from './markdown.js';
import {mountCalibrationEditor} from './calibration.js';
import {MODES} from './modes.js';
import {analysisEvidence} from './analysis.js';
import {mountModelManager} from './models.js';
import {createWorkspaces, createGenerationManager} from './workspaces.js';
import {clarificationCard, clarificationReply} from './clarification.js';
import {fieldEvidence, mountFieldPreview} from './fields.js';
import {mountSemanticWorkbench, semanticSummary} from './semantics.js';
import {mountDictionaryLibrary} from './dictionaries.js';
import {mountComposerImages, imageGallery} from './images.js';
import {createConnectionMonitor,connectionPresentation} from './connections.js';

const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const workspace=createWorkspaces();
const state = {models:[], settings:{}, conversations:[], contextTab:'tables', get view(){return workspace.current;}};
const connectionMonitor=createConnectionMonitor({api,
  getModels:()=>[state.view.model,...workspace.all().filter(view=>view.busy).map(view=>view.model)],
  onChange:result=>{
    const patch=model=>model?.id===result.model_id?{...model,live:result.live,port:result.port,connection:result.connection}:model;
    state.models=state.models.map(patch);
    for(const view of workspace.all())view.model=patch(view.model);
    if(state.view.model?.id===result.model_id)renderConnectionStatus();
  },
});
let modalCleanup=null,modalReturn=null,composerImages=null;
hydrateIcons();
const messageRenderer=createMessageRenderer({
  root:$('#messages'),scroll:$('#message-scroll'),
  label:m=>`<span class="message-avatar">${m.role==='user'?'你':icon('sparkles')}</span><span>${m.role==='user'?'你':'Power BI Assistant'}</span>${m.model_stale?'<small class="validation-badge" title="同步前的回复与结果作为历史保留，后续分析使用最新模型重新取数">同步前回复</small>':''}`,
  content:m=>m.role==='user'?imageGallery(m.images)+'<p>'+esc(m.content).replace(/\n/g,'<br>')+'</p>':m.clarification&&m.status!=='error'&&m.status!=='cancelled'?'':markdown(m.content),
  artifacts:m=>fieldEvidence(m.field_previews||[])+clarificationCard(m)+analysisEvidence(m.analysis||[])+(m.artifacts||[]).map(artifactCard).join(''),
  status:streamStatus,
});
const messagePaint=createRenderScheduler(renderMessages);
let taskSignature='';
const generation=createGenerationManager({
  workspace,api,
  onChange:view=>{
    if(state.view===view){renderBusy();messagePaint.schedule();}
    const signature=workspace.all().map(v=>`${v.key}:${v.busy}:${v.attention}:${v.conversation?.id||''}`).join('|');
    if(signature!==taskSignature){taskSignature=signature;renderHistory();renderBackgroundTasks();}
  },
  onCreated:conversation=>{state.conversations=[conversation,...state.conversations.filter(c=>c.id!==conversation.id)];renderHistory();},
  onModel:(view,event)=>{if(view.model?.id===event.model_id){view.model.live=event.live;if(event.live)view.model.connection={status:'connected'};if(state.view===view)renderConnectionStatus();}},
  onComplete:(view,failed)=>{
    if(state.view===view){renderControls();if(!$('#prompt').value&&view.draft)$('#prompt').value=view.draft;messagePaint.flush();}
    else toast(`“${view.conversation?.title||'后台会话'}”${view.cancelled?'已中止生成':failed?'生成未完成，请查看原因':view.messages.at(-1)?.clarification?'有一项口径待确认':'已生成完成'}`,failed);
    void reloadHistory().catch(error=>toast(error.message,true));
  },
  onError:(message,view)=>toast((state.view===view?'':`“${view.conversation?.title||'后台会话'}”：`)+message,true),
});

async function api(path, options={}) {
  const response = await fetch('/api'+path, {headers:{'Content-Type':'application/json'}, ...options});
  if(!response.ok) {
    const body = await response.json().catch(()=>({detail:'服务暂时不可用'}));
    throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail));
  }
  return response.json();
}
const post = (path, body={}) => api(path,{method:'POST', body:JSON.stringify(body)});
function toast(text,error=false) {
  const el=document.createElement('div');el.className='toast'+(error?' error':'');el.textContent=text;
  $('#toasts').append(el);setTimeout(()=>el.remove(),5500);
}
const guard = fn => async (...args) => {try {await fn(...args);} catch(e) {toast(e.message,true);}};
function available() {if(state.view.busy||state.view.loading||state.view.syncing){toast(state.view.syncing?'正在同步当前对话的模型，请稍候':state.view.loading?'正在读取此会话':'此会话正在生成，可切换到其他页面继续使用');return false;}return true;}
function renderBusy() {
  $('#send-button').disabled=state.view.busy||state.view.syncing||!!state.view.loading||state.view.images.some(img=>img.status==='uploading');
  $('#refresh-model').disabled=state.view.busy||state.view.syncing||!!state.view.loading;
  $('#refresh-model').title=state.view.syncing?'正在同步模型':state.view.busy?'当前对话结束或中止后可同步模型':'同步模型，继续当前对话';
  $('#stop-button').disabled=!state.view.busy||state.view.stopping;
  $('#stop-button').setAttribute('aria-label',state.view.stopping?'正在中止本次生成':'中止本次生成');
  $('#stop-button').title=state.view.stopping?'正在中止本次生成':'中止本次生成，保留已收到的内容';
  composerImages?.render();
  document.body.classList.toggle('busy',state.view.busy);
}
function showModal(title,eyebrow,html,{wide=false}={}) {
  modalReturn=null;
  modalCleanup?.();modalCleanup=null;
  $('#modal-close').setAttribute('aria-label','关闭弹窗');
  $('#modal').classList.toggle('wide-modal',wide);
  $('#modal-title').textContent=title;$('#modal-eyebrow').textContent=eyebrow;$('#modal-body').innerHTML=html;
  if(!$('#modal').open) $('#modal').showModal();
}
function closeModalLayer(){if(modalReturn)modalReturn();else $('#modal').close();}
$('#modal-close').onclick=closeModalLayer;
$('#modal').addEventListener('close',()=>{modalReturn=null;modalCleanup?.();modalCleanup=null;});
$('#modal').addEventListener('cancel',event=>{if(modalReturn){event.preventDefault();modalReturn();}});
$('#modal').addEventListener('click', e=>{if(e.target===$('#modal')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeModalLayer();}});

function welcome() {
  const mode=MODES[state.view.mode]||MODES.dax;
  return `<section class="welcome"><div class="welcome-emblem">${icon(mode.icon)}</div><div class="eyebrow">YOUR POWER BI COPILOT</div><h1>${mode.headline}</h1><p class="welcome-subtitle">${mode.subtitle}</p><div class="quick-cards">${mode.cards.map((c,i)=>`<button class="quick-card" data-prompt="${i}"><span class="card-icon">${icon(c[0])}</span>${icon('arrow-right','card-arrow')}<strong>${c[1]}</strong><p>${c[2]}</p></button>`).join('')}</div><div class="workflow-line"><span><b>1</b>连接本机模型</span>${icon('arrow-right')}<span><b>2</b>描述业务需求</span>${icon('arrow-right')}<span><b>3</b>${mode.finalStep}</span></div></section>`;
}
function renderMessages(forceFollow=false) {
  for(const message of state.view.messages)if(message.clarification){
    message.clarificationDraft=state.view.clarificationDrafts[message.id]||{};
    message.clarificationLocked=!!(state.view.busy||state.view.loading||state.view.syncing||message.model_stale||message.status!=='complete'||message.clarification.answered||message!==state.view.messages.at(-1));
  }
  messageRenderer.render(state.view.messages,{
    conversation:state.view.key,
    welcome:state.view.messages.length?'':welcome(),forceFollow,
  });
}
function streamStatus(message) {
  const elapsed=state.view.startedAt?Math.floor((Date.now()-state.view.startedAt)/1000):(message.progress?.elapsed||0);
  return (message.progress?.stage||state.view.status||'正在等待模型回复')+(elapsed>0?` · 已用时 ${elapsed} 秒`:'');
}
setInterval(()=>{
  messageRenderer.updateStatus(state.view.messages);
},1000);

function artifactCard(obj) {
  const v=obj.validation;const live=v.run_verified&&v.ok;
  const column=obj.kind==='dax_column';
  const retry=obj.kind!=='m_script'&&obj.message_id?`<button data-revalidate="${esc(obj.message_id)}">${icon('refresh')}重新实跑本组代码</button>`:'';
  const calibration=(v.calibration||[]).map((p,i)=>`<div class="validation-detail">${v.calibration.length===1?'校准结果':`历史校准点 ${i+1}`}：期望 ${esc(p.expected)} / 实际 ${esc(p.actual)} · ${p.matched?'匹配':'未匹配'}</div>`).join('');
  return `<div class="validation-card ${!v.ok?'failed':!live?'warning':''}"><div class="validation-top">${icon(v.ok?'check':'info')}<strong>${column?'计算列 · '+esc(obj.home_table)+'['+esc(obj.name)+']':esc(obj.name)}</strong><span class="validation-badge">${live?(column?'Desktop 行上下文抽样通过':'Desktop 实跑通过'):v.run_verified?'Desktop 实跑未通过':v.ok?'静态检查通过 · 未实跑':'验证未通过'}</span></div>${v.sample!==null&&v.sample!==undefined?`<div class="validation-detail">实跑结果：${esc(typeof v.sample==='object'?JSON.stringify(v.sample):v.sample)}</div>`:''}${v.errors?.length?`<div class="validation-detail">${v.errors.map(esc).join('<br>')}</div>`:''}${v.warnings?.length?`<div class="validation-detail">${v.warnings.map(esc).join('<br>')}</div>`:''}${calibration}${obj.kind==='m_script'?'<div class="validation-detail">复制脚本至 Power Query 高级编辑器，在 Desktop 中应用验证。</div>':live?`<button data-apply="${esc(obj.id)}">${icon(obj.applied_at?'check':'database')}${obj.applied_at?'已写入模型':'审阅并写入模型'}</button>`:''}${retry}</div>`;
}
function renderHistory() {
  const needle=$('#history-search').value.toLowerCase();
  const rows=state.conversations.filter(c=>c.title.toLowerCase().includes(needle));
  $('#history-list').innerHTML=rows.length?rows.map(c=>{
    const view=workspace.find(c.id),busy=view?.busy||(c.generating&&!view?.loaded);
    const last=view?.messages.at(-1),pending=last?.status==='complete'&&last.clarification&&!last.clarification.answered;
    const badge=busy?'<small class="history-task running">生成中</small>':pending?'<small class="history-task clarification">待确认</small>':view?.attention?`<small class="history-task ${view.attention}">${view.attention==='error'?'查看错误':view.attention==='cancelled'?'已中止':'已完成'}</small>`:'';
    return `<div class="history-item ${state.view.conversation?.id===c.id?'active':''}"><button class="history-name" data-conversation="${c.id}" title="${esc(c.title)}">${icon(MODES[c.mode]?.historyIcon||'chat')}<span>${esc(c.title)}</span>${badge}</button><button class="icon-button" data-delete-conversation="${c.id}" aria-label="删除 ${esc(c.title)}" ${busy?'disabled title="生成完成后可删除"':''}>${icon('trash')}</button></div>`;
  }).join(''):'<p class="muted history-empty">'+(needle?'没有匹配的对话':'还没有对话<br>从一个数据问题开始吧')+'</p>';
}
function renderBackgroundTasks() {
  const tasks=workspace.all().filter(view=>view!==state.view&&view.busy);
  const banner=$('#background-generations');banner.hidden=!tasks.length;
  banner.innerHTML=tasks.length?`<span class="background-dot"></span><span>${tasks.length} 个会话正在后台生成</span>${tasks.filter(v=>v.conversation).slice(0,2).map(view=>`<button data-conversation="${view.conversation.id}" title="${esc(view.conversation.title)}">查看：${esc(view.conversation.title)}</button>`).join('')}`:'';
}
function renderContext() {
  const model=state.view.model, ctx=model?.context;
  $('#model-summary').innerHTML=model?`<div class="model-name">${esc(model.name)}</div><div class="model-state">${model.live?'● 本机实时连接':model.source==='demo'?'○ 示例数据结构 · 仅供体验':'○ 离线模型快照'}</div><div class="model-counts"><span><b>${Object.keys(ctx.tables).length}</b>表</span><span><b>${Object.values(ctx.tables).flat().length}</b>字段</span><span><b>${ctx.measures.length}</b>度量值</span></div>`:'<div class="model-name">尚未连接模型</div><div class="model-state">在 Power BI Desktop 打开报表后连接</div>';
  if(model?.source==='desktop')$('#model-summary').insertAdjacentHTML('beforeend',`<button class="text-link semantic-open-button" data-open-semantics="${esc(model.id)}">${icon('book')}模型语义层 <small>${['needs_update','unverified'].includes(model.semantics?.dictionary_notice?.status)?esc(model.semantics.dictionary_notice.message):model.semantics?.status==='ready'?'已梳理 · 可审阅定义':model.semantics?.can_answer?'已有语义可用 · 更新可稍后处理':model.semantics?.status==='building'?'正在后台梳理':'需要先梳理语义'}</small></button>`);
  if(!model) {$('#model-tree').innerHTML=`<div class="model-empty"><div class="empty-model-icon">${icon('database')}</div><strong>让模型成为对话的上下文</strong><p>表、字段、关系与度量值<br>连接后将显示在这里</p><button class="text-link" id="empty-connect">连接 Power BI ${icon('arrow-right')}</button></div>`;return;}
  renderConnectionStatus();
  const needle=$('#field-search').value.toLowerCase();
  if(state.contextTab==='tables') {
    $('#model-tree').innerHTML=Object.entries(ctx.tables).map(([name,cols])=>{
      const measures=ctx.measures.filter(m=>m.table===name);const match=name.toLowerCase().includes(needle);
      const fields=cols.filter(c=>match||c.name.toLowerCase().includes(needle));const ms=measures.filter(m=>match||m.name.toLowerCase().includes(needle));
      if(!fields.length&&!ms.length&&!match)return '';
      return `<details class="table-group" ${needle?'open':''}><summary>${icon('chevron-right')}${icon('table')}${esc(name)}<small>${cols.length}</small></summary>${fields.map(c=>`<div class="field-row"><button class="field-button" data-ref="${esc("'"+name.replaceAll("'","''")+"'["+c.name.replaceAll(']',']]')+"]")}" title="${esc(c.dtype)}"><span class="field-type">${/int|double|decimal/.test(c.dtype)?'#':c.dtype==='datetime'?'◷':'Aa'}</span>${esc(c.name)}</button><button class="icon-button field-preview-button" data-preview-table="${esc(name)}" data-preview-column="${esc(c.name)}" title="查看字段取值" aria-label="查看 ${esc(name)}[${esc(c.name)}] 的取值">${icon('eye')}</button></div>`).join('')}${ms.map(m=>`<button class="field-button" data-ref="${esc('['+m.name.replaceAll(']',']]')+']')}">${icon('formula')}${esc(m.name)}</button>`).join('')}</details>`;
    }).join('')||'<p class="history-empty muted">没有匹配的字段</p>';
  } else if(state.contextTab==='relations') {
    $('#model-tree').innerHTML=ctx.relationships.filter(r=>(r.from_table+r.to_table+r.from_column+r.to_column).toLowerCase().includes(needle)).map(r=>`<div class="relation-item">${esc(r.from_table)}[${esc(r.from_column)}]${icon('arrow-right')}${esc(r.to_table)}[${esc(r.to_column)}]<span>${r.cross_filter==='both'?'双向筛选':'单向筛选'} · ${r.is_active?'活动关系':'非活动关系'}</span></div>`).join('')||'<p class="history-empty muted">暂无匹配的关系</p>';
  } else {
    const queries={...ctx.table_queries,...ctx.shared_expressions};
    $('#model-tree').innerHTML=Object.keys(queries).filter(n=>n.toLowerCase().includes(needle)).map(name=>`<button class="query-button" data-query="${esc(name)}">${icon('code')}${esc(name)}</button>`).join('')||'<p class="history-empty muted">模型中暂无可用 M 查询</p>';
  }
}
function renderConnectionStatus() {
  const model=state.view.model,view=connectionPresentation(model);
  $('#connection-label').textContent=view.label;$('#connect-open').title=view.detail;
  $('.status-dot').classList.toggle('connected',view.kind==='connected');
  $('.status-dot').classList.toggle('reconnecting',view.kind==='reconnecting');
  $('.status-dot').classList.toggle('warning',view.kind==='warning');
  $('#composer-context').textContent=model?model.name+' · '+view.context:view.context;
  const status=$('.model-state');if(status)status.textContent=(view.kind==='connected'?'● ':'○ ')+(view.state||view.detail);
}
function renderControls() {
  const query=state.view.queryName;
  $('#query-select').hidden=state.view.mode!=='mquery';
  const ctx=state.view.model?.context;
  $('#query-select').innerHTML=Object.keys({...ctx?.table_queries,...ctx?.shared_expressions}).map(t=>`<option value="${esc(t)}">${esc(t)}</option>`).join('')||'<option value="">没有可用查询</option>';
  if(state.view.conversation?.query_name)$('#query-select').value=state.view.conversation.query_name;
  else if([...$('#query-select').options].some(o=>o.value===query))$('#query-select').value=query;
  state.view.queryName=$('#query-select').value;
  $('#calibration-open').hidden=state.view.mode!=='dax';
  $('#calibration-label').textContent=state.view.points.length?(state.view.points[0].filters.length?`校准 · ${state.view.points[0].filters.length} 个条件`:'校准 · 全模型'):'校准';
  const strength=state.settings.reasoning_control?.supported?(state.settings.reasoning_level==='high'?'高强度':'低强度'):
    state.settings.reasoning_control?'强度不可调':'';
  $('#active-llm').textContent=state.settings.model?(state.settings.model+(strength?` · ${strength}`:'')):'配置大模型';
  $('#model-settings-chip').title=state.settings.name?`${state.settings.name} · 点击切换或添加模型`:'点击添加模型';
  $('#sidebar-model').textContent=state.settings.model||'连接你的大模型服务器';
  const mode=MODES[state.view.mode]||MODES.dax;
  $('#page-title').textContent=mode.title;
  $('#prompt').placeholder=mode.placeholder;
  $('#conversation-caption').textContent=state.view.conversation?.title||mode.caption;
  $('.composer-note').innerHTML='<span class="tiny-dot"></span>'+(state.view.mode==='analysis'?'只读查询 · 查询结果用于 AI 分析 · 依据和结果本地留存':'模型在本机读取 · AI 在服务器推理 · 写入前由你确认');
  $$('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.mode===state.view.mode));
  renderBusy();
}
function render() {renderControls();renderHistory();renderContext();renderMessages();renderBackgroundTasks();}
function saveDraft() {
  Object.assign(state.view,{draft:$('#prompt').value,queryName:$('#query-select').value,
    scrollTop:$('#message-scroll').scrollTop,pinned:$('#message-scroll').scrollHeight-$('#message-scroll').scrollTop-$('#message-scroll').clientHeight<100});
}
function showView() {
  $('#prompt').value=state.view.draft;render();
  renderConnectionStatus();void connectionMonitor.refresh();
  if(state.view.scrollTop!==undefined&&!state.view.pinned)$('#message-scroll').scrollTo({top:state.view.scrollTop,behavior:'instant'});
}
function newChat(mode=state.view.mode) {saveDraft();workspace.newChat(mode);showView();$('#prompt').focus();}
function switchMode(mode) {saveDraft();workspace.switchMode(mode);showView();}
function useModel(model) {saveDraft();workspace.newChat(state.view.mode,model);$('#modal').close();showView();if(model.source==='desktop'&&model.semantics?.status!=='ready'&&!model.semantics?.can_answer)void openSemantics(model.id).catch(error=>toast(error.message,true));}
async function syncCurrentModel() {
  const view=state.view;
  if(!view.model)return openConnections();
  if(view.model.source==='demo'){toast('当前为示例模型，请连接 Desktop 以读取真实模型');return;}
  if(!available())return;
  saveDraft();view.syncing=true;historyRequest++;renderBusy();renderMessages();
  try {
    const result=view.conversation?await post(`/conversations/${view.conversation.id}/refresh-model`):
      {model:await post(`/models/${view.model.id}/refresh`)};
    // Capture edits typed while the request was running. Navigation saves the
    // owning draft separately; a late response must never activate another view.
    if(state.view===view)saveDraft();
    historyRequest++;
    view.model=result.model;
    state.models=[result.model,...state.models.filter(m=>m.id!==result.model.id)];
    if(result.conversation){
      workspace.bind(view,result.conversation);
      state.conversations=state.conversations.map(c=>c.id===result.conversation.id?result.conversation:c);
      for(const message of view.messages)if(message.role==='assistant')message.model_stale=true;
    }
    if(state.view===view)render();else renderHistory();
    toast('模型已同步，可在原对话继续；历史回复已保留');
  } finally {
    view.syncing=false;renderBusy();
    if(state.view===view)renderMessages();
  }
}
let historyRequest=0;
async function reloadHistory() {
  const request=++historyRequest,conversations=await api('/conversations');if(request!==historyRequest)return;
  const missing=workspace.all().filter(view=>view.busy&&view.conversation&&!conversations.some(c=>c.id===view.conversation.id));
  state.conversations=[...missing.map(view=>view.conversation),...conversations];
  for(const view of workspace.all()){
    const current=conversations.find(c=>c.id===view.conversation?.id);if(current)view.conversation=current;
  }
  renderHistory();renderBackgroundTasks();
}
async function loadConversation(view) {
  if(view.loading||view.syncing||generation.isRunning(view))return;
  view.loading=true;if(state.view===view)renderBusy();
  try {
    const [model,messages,status]=await Promise.all([api(`/models/${view.conversation.model_id}`),
      api(`/conversations/${view.conversation.id}/messages`),api(`/conversations/${view.conversation.id}/generation`)]);
    Object.assign(view,{model,messages,loaded:true});
    if(status.running||messages.some(m=>m.status==='streaming'))void generation.follow(view);
  } finally {view.loading=false;if(state.view===view)showView();}
}
async function openConversation(id) {
  const conversation=state.conversations.find(c=>c.id===id);if(!conversation)return;
  saveDraft();const view=workspace.open(conversation,state.models.find(m=>m.id===conversation.model_id)||null);showView();
  if(!generation.isRunning(view))await loadConversation(view);
}

async function openConnections() {
  showModal('连接 Power BI','LOCAL MODEL','<p class="notice">正在查找本机 Power BI Desktop 实例…</p>');
  const [instances,models]=await Promise.all([api('/desktop/instances'),api('/models')]);state.models=models;
  $('#modal-body').innerHTML=`<p class="notice">先在本机 Power BI Desktop 打开报表。连接后按报表名称与文件路径匹配本地语义词典，并核对字段、关系和公式；不会修改报表。</p>${instances.length?instances.map(i=>`<button class="instance-button" data-port="${i.port}">${icon('monitor')}<span>${esc(i.report?.name||'Power BI Desktop')} · ${i.port}<small>${esc(i.report?.path||'本地实例 · PID '+i.pid)}</small></span></button>`).join(''):'<p class="field-help">未找到已打开的实例。请打开报表后重新扫描。</p>'}${instances.some(i=>!i.report?.name)?'<label class="form-field">未识别报表名称时，可填写用于后续匹配的名称<input id="connection-report-name" maxlength="260" placeholder="例如：采购费用分析；以后打开同一报表填写相同名称"></label>':''}<div class="modal-actions"><button class="secondary-button" id="demo-load">体验示例模型</button><button class="primary-button" id="rescan">${icon('refresh')}重新扫描</button></div>${models.length?`<div class="snapshot-section"><div class="section-label">本地保存的模型结构</div>${models.slice(0,8).map(m=>`<button class="snapshot-button" data-snapshot="${m.id}">${icon('database')}<span>${esc(m.name)}<small>${m.live?'实时连接':m.source==='demo'?'示例模型':'离线快照 · 无法实跑'}</small></span></button>`).join('')}</div>`:''}`;
  $('#modal-body').insertAdjacentHTML('afterbegin','<div class="semantic-tools"><button type="button" class="secondary-button" id="manage-dictionaries">管理历史业务词典</button></div>');
}

async function openDictionaryLibrary() {
  const modelId=state.view.model?.source==='desktop'?state.view.model.id:null;
  showModal('业务词典管理','DICTIONARY LIBRARY','<section id="dictionary-library"></section>',{wide:true});
  const root=$('#dictionary-library');
  const manager=await mountDictionaryLibrary(root,{api,modelId,onAssociate:async profile=>{
    const summary=semanticSummary(profile);
    for(const view of workspace.all())if(view.model?.id===modelId)view.model.semantics=summary;
    if(state.view.model?.id===modelId)renderContext();
    toast(profile.dictionary_notice?.message||'业务词典已关联');await openSemantics(modelId);
  },onDeleted:deletedId=>{
    for(const view of workspace.all())if(view.model?.id===deletedId)view.model.semantics=null;
    if(state.view.model?.id===deletedId)renderContext();
  }});
  if(root.isConnected&&$('#modal').open)modalCleanup=()=>manager.dispose();else manager.dispose();
}

async function openSettings() {
  showModal('模型服务设置','MODEL SERVICES','<section id="model-manager"></section>');
  await mountModelManager($('#model-manager'),{
    api,onSettings:settings=>{state.settings=settings;renderControls();},onNotice:toast,
  });
}

async function openFieldPreview(table,column,modelId=state.view.model?.id) {
  if(!modelId){toast('请先连接本机 Power BI Desktop');return;}
  showModal(`${table}[${column}]`,'FIELD VALUES','<section id="field-preview"></section>');
  const root=$('#field-preview'),manager=await mountFieldPreview(root,{api,modelId,table,column});
  if(root.isConnected&&$('#modal').open)modalCleanup=()=>manager.dispose();else manager.dispose();
}

async function openSemantics(modelId=state.view.model?.id) {
  if(!modelId)return;
  showModal('模型语义层','MODEL SEMANTICS','<section id="semantic-workbench"></section>',{wide:true});
  const root=$('#semantic-workbench');
  let manager,previewCleanup=null;
  async function childPanel({id,title:childTitle,eyebrow:childEyebrow,mount,onReturn=()=>{}}) {
    if(root.hidden||!root.isConnected||!$('#modal').open)return;
    manager?.pause();
    const dialog=$('#modal'),title=$('#modal-title').textContent,eyebrow=$('#modal-eyebrow').textContent;
    const scrollTop=dialog.scrollTop;
    const child=document.createElement('section');child.id=id;
    let active=true,childManager;
    function cleanup(){active=false;childManager?.dispose();child.remove();if(modalReturn===back)modalReturn=null;}
    function back(){
      if(!active)return;
      cleanup();previewCleanup=null;root.hidden=false;
      $('#modal-title').textContent=title;$('#modal-eyebrow').textContent=eyebrow;
      $('#modal-close').setAttribute('aria-label','关闭弹窗');dialog.scrollTop=scrollTop;
      manager?.resume();
      onReturn();
    }
    previewCleanup=cleanup;modalReturn=back;root.hidden=true;root.after(child);
    $('#modal-title').textContent=childTitle;$('#modal-eyebrow').textContent=childEyebrow;
    $('#modal-close').setAttribute('aria-label','返回模型语义层');dialog.scrollTop=0;
    const mounted=await mount(child,back);
    if(active&&child.isConnected)childManager=mounted;else mounted.dispose();
  }
  function preview(table,column,sourceModelId) {
    return childPanel({id:'field-preview',title:`${table}[${column}]`,eyebrow:'FIELD VALUES',
      mount:(child,back)=>mountFieldPreview(child,{api,modelId:sourceModelId,table,column,onBack:back})});
  }
  function dictionaries() {
    return childPanel({id:'dictionary-library',title:'业务词典管理',eyebrow:'DICTIONARY LIBRARY',
      onReturn:()=>void manager?.refresh(),mount:(child,back)=>mountDictionaryLibrary(child,{api,modelId,onBack:back,
        onAssociate:profile=>{toast(profile.dictionary_notice?.message||'业务词典已关联');back();},
        onDeleted:deletedId=>{for(const view of workspace.all())if(view.model?.id===deletedId)view.model.semantics=null;}
      })});
  }
  manager=await mountSemanticWorkbench(root,{api,modelId,
    onUpdate:summary=>{
      for(const view of workspace.all())if(view.model?.id===modelId)view.model.semantics=summary;
      if(state.view.model?.id===modelId)renderContext();
    },onPreview:(...args)=>void preview(...args),onManageDictionaries:()=>void dictionaries()});
  if(root.isConnected&&$('#modal').open)modalCleanup=()=>{previewCleanup?.();manager.dispose();};
  else {previewCleanup?.();manager.dispose();}
}

function openCalibration() {
  if(!available())return;if(!state.view.model?.live){toast('校准生成需要先连接本机 Desktop 的实时模型',true);return;}
  showModal('用已知结果校准','CALIBRATION','<p class="notice">可添加多个筛选条件，所有条件同时生效，共同对应一个已知正确结果。将对回复中的最后一个度量值实跑校准，失败时最多自动修复两轮。</p><form id="calibration-form"></form>');
  const form=$('#calibration-form');
  const editor=mountCalibrationEditor(form,state.view.model.context,state.view.points[0]||{},message=>toast(message));
  $('[data-clear-calibration]',form).onclick=()=>{state.view.points=[];renderControls();$('#modal').close();};
  form.onsubmit=guard(async e=>{
    e.preventDefault();state.view.points=[editor.read()];renderControls();$('#modal').close();
    toast(`已设置 ${state.view.points[0].filters.length} 个筛选条件和 1 个校准结果`);
  });
}

async function send(e) {
  e.preventDefault();if(!available())return;
  if(state.view.images.some(img=>img.status==='uploading')){toast('请等待图片添加完成');return;}
  if(state.view.images.length>4){toast('每条消息最多 4 张图片，请移除多余图片');return;}
  const images=state.view.images.filter(img=>img.id),content=$('#prompt').value.trim()||(images.length?'请结合图片和当前上下文，帮我分析并处理。':'');if(!content)return;
  if(!state.view.model){await openConnections();return;}
  if(state.view.mode==='analysis'&&state.view.model.source!=='desktop'){toast('请先连接真实 Desktop 模型进行数据分析');await openConnections();return;}
  if(!state.settings.ready){await openSettings();return;}
  if(state.view.mode==='mquery'&&!$('#query-select').value){toast('此模型没有可用的 Power Query 查询',true);return;}
  const view=state.view;
  if(view.model.source==='desktop'&&view.mode!=='mquery'&&view.model.semantics?.status!=='ready'){
    try {
      const dictionary=await api(`/models/${view.model.id}/semantics`);view.model.semantics=semanticSummary(dictionary);
      if(state.view!==view)return;
      if(!view.model.semantics.can_answer){await openSemantics(view.model.id);toast('先建立初始语义词典，再继续这条需求');return;}
    } catch(error){toast(error.message,true);return;}
  }
  const payload={content,image_ids:images.map(img=>img.id),images,points:view.mode==='dax'?view.points:[],
    query_name:view.mode==='mquery'?$('#query-select').value:'',llm_profile_id:state.settings.active_profile_id||null};
  $('#prompt').value='';
  const pending=generation.send(view,payload);renderMessages(true);await pending;
}

async function reviewWrite(id) {
  if(!available())return;
  const view=state.view,obj=view.messages.flatMap(m=>m.artifacts||[]).find(o=>o.id===id);if(!obj)return;
  const measure=obj.kind==='dax_measure',column=obj.kind==='dax_column',hasHome=measure||column,
    objectLabel=column?'计算列':'度量值',tables=Object.keys(view.model.context.tables);
  showModal('确认写入模型','REVIEW CHANGE',`<p class="notice gold" id="write-target-notice">将${hasHome?'写入'+objectLabel:'创建计算表'} <strong>${esc(obj.name)}</strong>${hasHome?'，存放于 '+esc(obj.home_table):''}。完成后请在 Desktop 按 Ctrl+S 保存报表。</p>${hasHome?`<div class="form-row"><label class="form-field">${column?'计算列名称':'度量值名称'}<input id="write-measure-name" required maxlength="300" value="${esc(obj.name)}" autocomplete="off"></label><label class="form-field">${column?'所属表':'存放表'}<select id="write-home-table" required><option value="" ${!tables.includes(obj.home_table)?'selected':''} disabled>请选择${column?'所属表':'存放表'}</option>${tables.map(table=>`<option value="${esc(table)}" ${table===obj.home_table?'selected':''}>${esc(table)}</option>`).join('')}</select></label></div><p class="field-help">${column?'确认后按所选表的行上下文重新抽样验证，再创建计算列并执行完整计算。数据类型由引擎推断，计算耗时取决于表大小和表达式。':'可修改名称，并选择模型中已有的表。确认后将按所选名称和存放表重新验证，再写入模型。'}</p>`:''}<div class="code-block"><pre><code>${esc(obj.expression)}</code></pre></div>${measure?'<label class="check-label"><input type="checkbox" id="overwrite-measure">如果所选表中存在同名度量值，允许覆盖</label>':`<p class="field-help">${column?'仅支持新建计算列，不覆盖已有字段；同名时请修改列名。':'仅支持新建计算表，不覆盖同名已有表。'}</p>`}<div class="modal-actions"><button class="secondary-button" id="cancel-write">取消</button><button class="primary-button" id="confirm-write">确认写入</button></div>`);
  if(hasHome){
    const preview=()=>{$('#write-target-notice').textContent=`将写入${objectLabel} ${$('#write-measure-name').value.trim()||'（未填写名称）'}，存放于 ${$('#write-home-table').value||'（未选择表）'}。完成后请在 Desktop 按 Ctrl+S 保存报表。`;};
    $('#write-measure-name').oninput=preview;$('#write-home-table').onchange=preview;
  }
  $('#cancel-write').onclick=()=>$('#modal').close();
  $('#confirm-write').onclick=guard(async e=>{
    const body={overwrite:!!$('#overwrite-measure')?.checked};
    if(hasHome){
      body.name=$('#write-measure-name').value.trim();body.home_table=$('#write-home-table').value;
      if(!body.name||body.name.length>300){toast(`请填写 1～300 字的${objectLabel}名称`,true);$('#write-measure-name').focus();return;}
      if(!tables.includes(body.home_table)){toast('请选择模型中已有的存放表',true);return;}
    }
    const b=e.currentTarget,fields=$$('input,select',$('#modal-body'));b.disabled=true;b.textContent='重新验证并写入…';fields.forEach(el=>el.disabled=true);
    try {
      const r=await post(`/artifacts/${id}/apply`,body);
      Object.assign(obj,{name:r.name??obj.name,home_table:r.home_table??obj.home_table,
        validation:r.validation??obj.validation,applied_at:r.applied_at||new Date().toISOString()});
      $('#modal').close();
      try {view.model=await api(`/models/${view.model.id}`);}catch(error){toast('已写入，模型信息读取失败，请同步模型：'+error.message,true);}
      if(state.view===view)render();toast(r.detail);
    }finally {b.disabled=false;b.textContent='确认写入';fields.forEach(el=>el.disabled=false);}
  });
}

$('#composer-form').onsubmit=send;
$('#stop-button').onclick=()=>{void generation.stop(state.view);};
composerImages=mountComposerImages($('#composer-form'),{getView:()=>state.view,onNotice:toast,
  onChange:view=>{if(state.view===view)renderBusy();}});
function updateClarificationDraft(event) {
  const card=event.target.closest('[data-clarification]');if(!card)return;
  const id=card.dataset.clarification,draft=state.view.clarificationDrafts[id]||{};
  if(event.target.matches('[data-clarification-choice]'))draft.optionId=event.target.value;
  if(event.target.matches('[data-clarification-supplement]'))draft.supplement=event.target.value;
  state.view.clarificationDrafts[id]=draft;
  if(event.type==='change')messagePaint.schedule();
}
$('#messages').addEventListener('change',updateClarificationDraft);
$('#messages').addEventListener('input',updateClarificationDraft);
$('#prompt').addEventListener('input',()=>{state.view.draft=$('#prompt').value;});
$('#prompt').addEventListener('keydown',e=>{
  if(e.key!=='Enter'||e.shiftKey||e.isComposing||e.keyCode===229)return;
  e.preventDefault();
  if(e.repeat||$('#send-button').disabled)return;
  $('#composer-form').requestSubmit();
});
document.addEventListener('keydown',e=>{if(e.key.toLowerCase()==='k'&&(e.ctrlKey||e.metaKey)){e.preventDefault();if($('#modal').open)$('#modal').close();newChat();}});
$('#new-chat').onclick=()=>newChat();
$('#settings-open').onclick=guard(openSettings);$('#model-settings-chip').onclick=guard(openSettings);
$('#connect-open').onclick=guard(openConnections);$('#calibration-open').onclick=openCalibration;
$('#search-toggle').onclick=()=>{const input=$('#history-search');input.hidden=!input.hidden;if(!input.hidden)input.focus();else{input.value='';renderHistory();}};
$('#history-search').oninput=renderHistory;$('#field-search').oninput=renderContext;
$('#toggle-context').onclick=()=>{const panel=$('#context-panel');if(innerWidth<=1050)panel.classList.toggle('mobile-open');else panel.classList.toggle('collapsed');};
$('#refresh-model').onclick=guard(syncCurrentModel);
$('#query-select').onchange=()=>{const query=$('#query-select').value;if(state.view.conversation)newChat('mquery');state.view.queryName=query;$('#query-select').value=query;};
$('#export-chat').onclick=()=>{if(!state.view.conversation){toast('先开始一段对话再导出');return;}const a=document.createElement('a');a.href=`/api/conversations/${state.view.conversation.id}/export`;a.download='conversation.md';document.body.append(a);a.click();a.remove();};
document.addEventListener('click',guard(async e=>{
  const el=e.target.closest('button');if(!el)return;
  if(el.dataset.openImage){
    const id=el.dataset.openImage;if(!/^[a-f0-9]{32}$/.test(id))return;
    showModal('查看图片','IMAGE',`<img class="image-full-preview" src="/api/images/${id}" alt="消息附图"><p class="field-help">图片保存在本机；发送消息时会提供给所选模型服务。</p>`);return;
  }
  if(el.dataset.openSemantics){await openSemantics(el.dataset.openSemantics);return;}
  if(el.dataset.previewColumn!==undefined){await openFieldPreview(el.dataset.previewTable,el.dataset.previewColumn,el.dataset.previewModel||state.view.model?.id);return;}
  if(el.dataset.confirmClarification){
    if(!available())return;
    const view=state.view,message=view.messages.find(m=>m.id===el.dataset.confirmClarification);
    if(view.images.some(img=>img.status==='uploading')){toast('请等待图片添加完成');return;}
    if(view.images.length>4){toast('每条消息最多 4 张图片，请移除多余图片');return;}
    if(!message?.clarification||message.clarification.answered||message!==view.messages.at(-1)){toast('请回复会话中的最新口径问题');return;}
    const reply=clarificationReply(message,view.clarificationDrafts[message.id]);
    view.draft=$('#prompt').value;
    const pending=generation.send(view,{...reply,points:view.mode==='dax'?view.points:[],
      image_ids:view.images.filter(img=>img.id).map(img=>img.id),images:view.images.filter(img=>img.id),
      llm_profile_id:state.settings.active_profile_id||null});
    renderMessages(true);await pending;return;
  }
  if(el.dataset.mode)switchMode(el.dataset.mode);
  if(el.dataset.contextTab){state.contextTab=el.dataset.contextTab;$$('[data-context-tab]').forEach(b=>{const active=b===el;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active));});renderContext();}
  if(el.dataset.prompt!==undefined){$('#prompt').value=MODES[state.view.mode].cards[Number(el.dataset.prompt)][3];$('#prompt').focus();}
  if(el.dataset.ref){const input=$('#prompt'),start=input.selectionStart;input.setRangeText(el.dataset.ref,start,input.selectionEnd,'end');input.focus();}
  if(el.id==='empty-connect'||el.id==='rescan')await openConnections();
  if(el.id==='manage-dictionaries')await openDictionaryLibrary();
  if(el.id==='demo-load')useModel(await post('/models/demo'));
  if(el.dataset.port){const reportName=$('#connection-report-name')?.value.trim()||'';el.disabled=true;el.textContent='正在读取模型…';try{const model=await post('/models/connect',{port:Number(el.dataset.port),report_name:reportName});useModel(model);toast(model.semantics?.dictionary_notice?.message||(model.semantics?.status==='ready'?model.semantics.stage:'已读取 Desktop 模型'));}catch(err){await openConnections();throw err;}}
  if(el.dataset.snapshot)useModel(await api(`/models/${el.dataset.snapshot}`));
  if(el.dataset.query){const ctx=state.view.model.context,code=ctx.table_queries[el.dataset.query]??ctx.shared_expressions[el.dataset.query];showModal(el.dataset.query,'POWER QUERY',`<div class="code-block"><div class="code-heading"><span>POWERQUERY</span><button class="copy-code">${icon('copy')}复制</button></div><pre><code>${esc(code)}</code></pre></div><div class="modal-actions"><button class="primary-button" data-use-query="${esc(el.dataset.query)}">在清洗助手中使用</button></div>`);}
  if(el.dataset.useQuery){newChat('mquery');state.view.queryName=el.dataset.useQuery;$('#query-select').value=el.dataset.useQuery;$('#modal').close();}
  if(el.dataset.conversation)await openConversation(el.dataset.conversation);
  if(el.dataset.deleteConversation){const id=el.dataset.deleteConversation;if(workspace.find(id)?.busy){toast('此会话仍在后台生成，完成后可删除');return;}showModal('删除此对话？','LOCAL HISTORY','<p class="notice">会删除此对话及关联代码记录，已写入 Desktop 的内容不受影响。</p><div class="modal-actions"><button class="primary-button" id="delete-confirm">删除对话</button></div>');$('#delete-confirm').onclick=guard(async()=>{await api(`/conversations/${id}`,{method:'DELETE'});workspace.remove(id);showView();$('#modal').close();await reloadHistory();});}
  if(el.classList.contains('copy-code')){const text=$('code',el.closest('.code-block')).textContent;try{await navigator.clipboard.writeText(text);}catch{const input=document.createElement('textarea');input.value=text;document.body.append(input);input.select();document.execCommand('copy');input.remove();}toast('代码已复制');}
  if(el.dataset.apply)await reviewWrite(el.dataset.apply);
  if(el.dataset.revalidate){
    if(!available())return;
    const messageId=el.dataset.revalidate,view=state.view;
    el.disabled=true;el.textContent='正在 Desktop 实跑验证…';
    try {
      const result=await post(`/messages/${messageId}/validate`);
      const message=view.messages.find(m=>m.id===messageId);
      if(message)message.artifacts=result.artifacts;
      if(view.model?.id===result.model.id)view.model=result.model;
      if(state.view===view){renderContext();renderMessages();}
      toast(result.artifacts.every(a=>a.validation.ok&&a.validation.run_verified)?'本组代码已通过 Desktop 实跑验证':'验证已完成，请查看每段代码的结果');
    } finally {
      // Reconciliation may reuse this node for the newly available write button.
      if(el.dataset.revalidate===messageId){el.disabled=false;el.textContent='重新实跑本组代码';}
    }
  }
}));

async function init() {
  try {
    const [settings,models,conversations]=await Promise.all([api('/settings'),api('/models'),api('/conversations')]);
    Object.assign(state,{settings,models,conversations});state.view.model=models[0]||null;render();
    renderConnectionStatus();void connectionMonitor.refresh();
    for(const conversation of conversations.filter(c=>c.generating)) {
      const view=workspace.register(conversation,models.find(m=>m.id===conversation.model_id)||null);
      void loadConversation(view).catch(error=>toast(error.message,true));
    }
  } catch(e) {render();toast('无法连接本地服务：'+e.message,true);}
}
setInterval(()=>{void connectionMonitor.refresh();},3000);
init();
