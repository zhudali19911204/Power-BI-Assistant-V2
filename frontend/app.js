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

const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const workspace=createWorkspaces();
const state = {models:[], settings:{}, conversations:[], contextTab:'tables', get view(){return workspace.current;}};
let modalCleanup=null;
hydrateIcons();
const messageRenderer=createMessageRenderer({
  root:$('#messages'),scroll:$('#message-scroll'),
  label:m=>`<span class="message-avatar">${m.role==='user'?'你':icon('sparkles')}</span><span>${m.role==='user'?'你':'Power BI Assistant'}</span>`,
  content:m=>m.role==='user'?'<p>'+esc(m.content).replace(/\n/g,'<br>')+'</p>':m.clarification&&m.status!=='error'?'':markdown(m.content),
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
  onModel:(view,event)=>{if(view.model?.id===event.model_id){view.model.live=event.live;if(state.view===view)renderContext();}},
  onComplete:(view,failed)=>{
    if(state.view===view){renderControls();if(!$('#prompt').value&&view.draft)$('#prompt').value=view.draft;messagePaint.flush();}
    else toast(`“${view.conversation?.title||'后台会话'}”${failed?'生成未完成，请查看原因':view.messages.at(-1)?.clarification?'有一项口径待确认':'已生成完成'}`,failed);
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
function available() {if(state.view.busy||state.view.loading){toast(state.view.loading?'正在读取此会话':'此会话正在生成，可切换到其他页面继续使用');return false;}return true;}
function renderBusy() {
  $('#send-button').disabled=state.view.busy||!!state.view.loading;
  document.body.classList.toggle('busy',state.view.busy);
}
function showModal(title,eyebrow,html) {
  modalCleanup?.();modalCleanup=null;
  $('#modal-title').textContent=title;$('#modal-eyebrow').textContent=eyebrow;$('#modal-body').innerHTML=html;
  if(!$('#modal').open) $('#modal').showModal();
}
$('#modal-close').onclick=()=>$('#modal').close();
$('#modal').addEventListener('close',()=>{modalCleanup?.();modalCleanup=null;});
$('#modal').addEventListener('click', e=>{if(e.target===$('#modal')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});

function welcome() {
  const mode=MODES[state.view.mode]||MODES.dax;
  return `<section class="welcome"><div class="welcome-emblem">${icon(mode.icon)}</div><div class="eyebrow">YOUR POWER BI COPILOT</div><h1>${mode.headline}</h1><p class="welcome-subtitle">${mode.subtitle}</p><div class="quick-cards">${mode.cards.map((c,i)=>`<button class="quick-card" data-prompt="${i}"><span class="card-icon">${icon(c[0])}</span>${icon('arrow-right','card-arrow')}<strong>${c[1]}</strong><p>${c[2]}</p></button>`).join('')}</div><div class="workflow-line"><span><b>1</b>连接本机模型</span>${icon('arrow-right')}<span><b>2</b>描述业务需求</span>${icon('arrow-right')}<span><b>3</b>${mode.finalStep}</span></div></section>`;
}
function renderMessages(forceFollow=false) {
  for(const message of state.view.messages)if(message.clarification){
    message.clarificationDraft=state.view.clarificationDrafts[message.id]||{};
    message.clarificationLocked=!!(state.view.busy||state.view.loading||message.status!=='complete'||message.clarification.answered||message!==state.view.messages.at(-1));
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
  const retry=obj.kind!=='m_script'&&obj.message_id?`<button data-revalidate="${esc(obj.message_id)}">${icon('refresh')}重新实跑本组代码</button>`:'';
  const calibration=(v.calibration||[]).map((p,i)=>`<div class="validation-detail">${v.calibration.length===1?'校准结果':`历史校准点 ${i+1}`}：期望 ${esc(p.expected)} / 实际 ${esc(p.actual)} · ${p.matched?'匹配':'未匹配'}</div>`).join('');
  return `<div class="validation-card ${!v.ok?'failed':!live?'warning':''}"><div class="validation-top">${icon(v.ok?'check':'info')}<strong>${esc(obj.name)}</strong><span class="validation-badge">${live?'Desktop 实跑通过':v.run_verified?'Desktop 实跑未通过':v.ok?'静态检查通过 · 未实跑':'验证未通过'}</span></div>${v.sample!==null&&v.sample!==undefined?`<div class="validation-detail">实跑结果：${esc(typeof v.sample==='object'?JSON.stringify(v.sample):v.sample)}</div>`:''}${v.errors?.length?`<div class="validation-detail">${v.errors.map(esc).join('<br>')}</div>`:''}${v.warnings?.length?`<div class="validation-detail">${v.warnings.map(esc).join('<br>')}</div>`:''}${calibration}${obj.kind==='m_script'?'<div class="validation-detail">复制脚本至 Power Query 高级编辑器，在 Desktop 中应用验证。</div>':live?`<button data-apply="${esc(obj.id)}">${icon(obj.applied_at?'check':'database')}${obj.applied_at?'已写入模型':'审阅并写入模型'}</button>`:''}${retry}</div>`;
}
function renderHistory() {
  const needle=$('#history-search').value.toLowerCase();
  const rows=state.conversations.filter(c=>c.title.toLowerCase().includes(needle));
  $('#history-list').innerHTML=rows.length?rows.map(c=>{
    const view=workspace.find(c.id),busy=view?.busy||(c.generating&&!view?.loaded);
    const last=view?.messages.at(-1),pending=last?.status==='complete'&&last.clarification&&!last.clarification.answered;
    const badge=busy?'<small class="history-task running">生成中</small>':pending?'<small class="history-task clarification">待确认</small>':view?.attention?`<small class="history-task ${view.attention}">${view.attention==='error'?'查看错误':'已完成'}</small>`:'';
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
  $('#connection-label').textContent=model?(model.live?'Desktop 已连接':model.source==='demo'?'示例模型':'模型快照'):'连接 Power BI';
  $('.status-dot').classList.toggle('connected',!!model?.live);
  $('#composer-context').textContent=model?model.name+' · '+(model.live?'实时模型':model.source==='demo'?'仅示例结构，无法实跑':'离线快照，需重新连接才能实跑'):'连接模型，让回答基于真实字段';
  $('#model-summary').innerHTML=model?`<div class="model-name">${esc(model.name)}</div><div class="model-state">${model.live?'● 本机实时连接':model.source==='demo'?'○ 示例数据结构 · 仅供体验':'○ 离线模型快照'}</div><div class="model-counts"><span><b>${Object.keys(ctx.tables).length}</b>表</span><span><b>${Object.values(ctx.tables).flat().length}</b>字段</span><span><b>${ctx.measures.length}</b>度量值</span></div>`:'<div class="model-name">尚未连接模型</div><div class="model-state">在 Power BI Desktop 打开报表后连接</div>';
  if(model?.source==='desktop')$('#model-summary').insertAdjacentHTML('beforeend',`<button class="text-link semantic-open-button" data-open-semantics="${esc(model.id)}">${icon('book')}模型语义层 <small>${model.semantics?.status==='ready'?'已梳理 · 可审阅定义':model.semantics?.status==='building'?'正在后台梳理':'需要先梳理语义'}</small></button>`);
  if(!model) {$('#model-tree').innerHTML=`<div class="model-empty"><div class="empty-model-icon">${icon('database')}</div><strong>让模型成为对话的上下文</strong><p>表、字段、关系与度量值<br>连接后将显示在这里</p><button class="text-link" id="empty-connect">连接 Power BI ${icon('arrow-right')}</button></div>`;return;}
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
  $('#active-llm').textContent=state.settings.model||'配置大模型';
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
  if(state.view.scrollTop!==undefined&&!state.view.pinned)$('#message-scroll').scrollTo({top:state.view.scrollTop,behavior:'instant'});
}
function newChat(mode=state.view.mode) {saveDraft();workspace.newChat(mode);showView();$('#prompt').focus();}
function switchMode(mode) {saveDraft();workspace.switchMode(mode);showView();}
function useModel(model) {saveDraft();workspace.newChat(state.view.mode,model);$('#modal').close();showView();if(model.source==='desktop'&&model.semantics?.status!=='ready')void openSemantics(model.id).catch(error=>toast(error.message,true));}
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
  if(view.loading||generation.isRunning(view))return;
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
  $('#modal-body').innerHTML=`<p class="notice">先在本机 Power BI Desktop 打开报表。连接后读取表、字段、关系、度量值与 M 查询；不会在连接时修改报表。</p>${instances.length?instances.map(i=>`<button class="instance-button" data-port="${i.port}">${icon('monitor')}<span>Power BI Desktop · ${i.port}<small>本地实例 · PID ${i.pid}</small></span></button>`).join(''):'<p class="field-help">未找到已打开的实例。请打开报表后重新扫描。</p>'}<div class="modal-actions"><button class="secondary-button" id="demo-load">体验示例模型</button><button class="primary-button" id="rescan">${icon('refresh')}重新扫描</button></div>${models.length?`<div class="snapshot-section"><div class="section-label">本地保存的模型结构</div>${models.slice(0,8).map(m=>`<button class="snapshot-button" data-snapshot="${m.id}">${icon('database')}<span>${esc(m.name)}<small>${m.live?'实时连接':m.source==='demo'?'示例模型':'离线快照 · 无法实跑'}</small></span></button>`).join('')}</div>`:''}`;
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
  await mountFieldPreview($('#field-preview'),{api,modelId,table,column});
}

async function openSemantics(modelId=state.view.model?.id) {
  if(!modelId)return;
  showModal('模型语义层 · 业务词典','MODEL SEMANTICS','<section id="semantic-workbench"></section>');
  const root=$('#semantic-workbench');
  const manager=await mountSemanticWorkbench(root,{api,modelId,
    onUpdate:summary=>{
      for(const view of workspace.all())if(view.model?.id===modelId)view.model.semantics=summary;
      if(state.view.model?.id===modelId)renderContext();
    },onPreview:openFieldPreview});
  if(root.isConnected)modalCleanup=()=>manager.dispose();else manager.dispose();
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
  const content=$('#prompt').value.trim();if(!content)return;
  if(!state.view.model){await openConnections();return;}
  if(state.view.mode==='analysis'&&state.view.model.source!=='desktop'){toast('请先连接真实 Desktop 模型进行数据分析');await openConnections();return;}
  if(!state.settings.ready){await openSettings();return;}
  if(state.view.mode==='mquery'&&!$('#query-select').value){toast('此模型没有可用的 Power Query 查询',true);return;}
  const view=state.view;
  if(view.model.source==='desktop'&&view.mode!=='mquery'&&view.model.semantics?.status!=='ready'){
    try {
      const dictionary=await api(`/models/${view.model.id}/semantics`);view.model.semantics=semanticSummary(dictionary);
      if(state.view!==view)return;
      if(dictionary.status!=='ready'){await openSemantics(view.model.id);toast('先完成模型语义梳理，再继续这条需求');return;}
    } catch(error){toast(error.message,true);return;}
  }
  const payload={content,points:view.mode==='dax'?view.points:[],
    query_name:view.mode==='mquery'?$('#query-select').value:'',llm_profile_id:state.settings.active_profile_id||null};
  $('#prompt').value='';
  const pending=generation.send(view,payload);renderMessages(true);await pending;
}

async function reviewWrite(id) {
  if(!available())return;
  const view=state.view,obj=view.messages.flatMap(m=>m.artifacts||[]).find(o=>o.id===id);if(!obj)return;
  showModal('确认写入模型','REVIEW CHANGE',`<p class="notice gold">将${obj.kind==='dax_table'?'创建计算表':'写入度量值'} <strong>${esc(obj.name)}</strong>${obj.kind==='dax_measure'?'，存放于 '+esc(obj.home_table):''}。完成后请在 Desktop 按 Ctrl+S 保存报表。</p><div class="code-block"><pre><code>${esc(obj.expression)}</code></pre></div>${obj.kind==='dax_measure'?'<label class="check-label"><input type="checkbox" id="overwrite-measure">如果存在同名度量值，允许覆盖</label>':'<p class="field-help">仅支持新建计算表，不覆盖同名已有表。</p>'}<div class="modal-actions"><button class="secondary-button" id="cancel-write">取消</button><button class="primary-button" id="confirm-write">确认写入</button></div>`);
  $('#cancel-write').onclick=()=>$('#modal').close();
  $('#confirm-write').onclick=guard(async e=>{const b=e.currentTarget;b.disabled=true;b.textContent='重新验证并写入…';try {const r=await post(`/artifacts/${id}/apply`,{overwrite:!!$('#overwrite-measure')?.checked});obj.applied_at=new Date().toISOString();$('#modal').close();view.model=await api(`/models/${view.model.id}`);if(state.view===view)render();toast(r.detail);}finally {b.disabled=false;b.textContent='确认写入';}});
}

$('#composer-form').onsubmit=send;
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
$('#refresh-model').onclick=guard(async()=>{const view=state.view;if(!view.model)return openConnections();if(view.model.source==='demo'){toast('当前为示例模型，请连接 Desktop 以读取真实模型');return;}const model=await post(`/models/${view.model.id}/refresh`);state.models=[model,...state.models.filter(m=>m.id!==model.id)];if(state.view===view){useModel(model);toast('模型已同步，已开启新对话');}else toast('模型已同步，可重新连接后使用');});
$('#query-select').onchange=()=>{const query=$('#query-select').value;if(state.view.conversation)newChat('mquery');state.view.queryName=query;$('#query-select').value=query;};
$('#export-chat').onclick=()=>{if(!state.view.conversation){toast('先开始一段对话再导出');return;}const a=document.createElement('a');a.href=`/api/conversations/${state.view.conversation.id}/export`;a.download='conversation.md';document.body.append(a);a.click();a.remove();};
document.addEventListener('click',guard(async e=>{
  const el=e.target.closest('button');if(!el)return;
  if(el.dataset.openSemantics){await openSemantics(el.dataset.openSemantics);return;}
  if(el.dataset.previewColumn!==undefined){await openFieldPreview(el.dataset.previewTable,el.dataset.previewColumn,el.dataset.previewModel||state.view.model?.id);return;}
  if(el.dataset.confirmClarification){
    if(!available())return;
    const view=state.view,message=view.messages.find(m=>m.id===el.dataset.confirmClarification);
    if(!message?.clarification||message.clarification.answered||message!==view.messages.at(-1)){toast('请回复会话中的最新口径问题');return;}
    const reply=clarificationReply(message,view.clarificationDrafts[message.id]);
    view.draft=$('#prompt').value;
    const pending=generation.send(view,{...reply,points:view.mode==='dax'?view.points:[],
      llm_profile_id:state.settings.active_profile_id||null});
    renderMessages(true);await pending;return;
  }
  if(el.dataset.mode)switchMode(el.dataset.mode);
  if(el.dataset.contextTab){state.contextTab=el.dataset.contextTab;$$('[data-context-tab]').forEach(b=>{const active=b===el;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active));});renderContext();}
  if(el.dataset.prompt!==undefined){$('#prompt').value=MODES[state.view.mode].cards[Number(el.dataset.prompt)][3];$('#prompt').focus();}
  if(el.dataset.ref){const input=$('#prompt'),start=input.selectionStart;input.setRangeText(el.dataset.ref,start,input.selectionEnd,'end');input.focus();}
  if(el.id==='empty-connect'||el.id==='rescan')await openConnections();
  if(el.id==='demo-load')useModel(await post('/models/demo'));
  if(el.dataset.port){el.disabled=true;el.textContent='正在读取模型…';try{useModel(await post('/models/connect',{port:Number(el.dataset.port)}));toast('已读取 Desktop 模型');}catch(err){await openConnections();throw err;}}
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
    for(const conversation of conversations.filter(c=>c.generating)) {
      const view=workspace.register(conversation,models.find(m=>m.id===conversation.model_id)||null);
      void loadConversation(view).catch(error=>toast(error.message,true));
    }
  } catch(e) {render();toast('无法连接本地服务：'+e.message,true);}
}
init();
