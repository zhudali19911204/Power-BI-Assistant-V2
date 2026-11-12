import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';
import {businessData,hasBusinessKnowledge,businessMarkup,businessEditMarkup,businessBody,targetRow,filterRow} from './business_dictionary.js';

const $=(selector,root)=>root.querySelector(selector);
const states={confirmed:'用户已确认',candidate:'候选含义 · 待确认',unresolved:'业务含义未知'};
const kinds={table:'表',column:'字段',measure:'度量值'};
export const semanticSummary=p=>({status:p.status,stage:p.stage,coverage:p.coverage,total:p.entries.length,
  confirmed:p.entries.filter(e=>e.state==='confirmed').length,
  can_answer:hasBusinessKnowledge(p)||p.status==='ready'||!!p.has_history||p.entries.some(e=>['candidate','confirmed'].includes(e.state)||e.previous_definition),
  pending:p.entries.filter(e=>e.state==='unresolved').length,
  ...(p.dictionary_notice?{dictionary_notice:p.dictionary_notice}:{})});

function facts(entry) {
  const f=entry.facts;
  const items=[['模型描述',f.description],['实际数据类型',f.actual_dtype||f.dtype],['源字段',f.source_column],
    ['显示格式',f.format_string],['模型公式',f.expression],['表内字段',f.columns?.join('、')]];
  return `<details class="semantic-facts"><summary>模型事实与依据</summary>${items.filter(([,value])=>value).map(([label,value])=>`<p><b>${label}</b><span>${esc(value)}</span></p>`).join('')||'<p>模型没有提供额外描述，不能仅凭名称认定业务含义。</p>'}
    ${(f.relationships||[]).map(r=>`<p><b>关系</b><span>${esc(r.from_table)}[${esc(r.from_column)}] → ${esc(r.to_table)}[${esc(r.to_column)}] · ${r.is_active?'活动':'非活动'}</span></p>`).join('')}</details>`;
}

function entryMarkup(entry) {
  return `<article class="semantic-entry" data-semantic-entry="${esc(entry.id)}"><div class="semantic-entry-heading"><strong>${esc(entry.table)}${entry.kind==='table'?'':'['+esc(entry.name)+']'}</strong><span class="semantic-state ${entry.state}">${states[entry.state]||'待确认'}</span></div>
    <p class="semantic-business-name">${kinds[entry.kind]} · ${esc(entry.business_name||'尚无业务名称')}${entry.role?' · '+esc(entry.role):''}</p>
    ${entry.state==='unresolved'&&entry.change?`<p class="semantic-change-note">${entry.change.kind==='added'?'新增条目':'待更新条目'} · ${entry.change.reasons.map(esc).join('；')}</p>`:''}
    <p class="semantic-meaning">${esc(entry.meaning||'业务含义尚未确定，需要结合模型描述和业务定义确认。')}</p>
    ${entry.usage_notes?`<p class="semantic-meaning"><b>使用说明：</b>${esc(entry.usage_notes)}</p>`:''}
    ${entry.previous_definition?`<details class="semantic-facts"><summary>查看更新前定义（仅供核对，已不作为当前确认口径）</summary><p>${esc(entry.previous_definition.meaning||'此前未定义业务含义')}</p></details>`:''}
    <p class="semantic-unit">单位：${esc(entry.unit||'未知 / 未确认')}${entry.aliases.length?' · 别名：'+entry.aliases.map(esc).join('、'):''}</p>${facts(entry)}
    <div class="semantic-entry-actions"><button class="secondary-button" data-edit-semantic="${esc(entry.id)}">编辑与确认</button>${entry.kind==='column'?`<button class="text-link" data-semantic-preview="${esc(entry.id)}">${icon('eye')}查看实际取值</button>`:''}</div></article>`;
}

function editMarkup(entry) {
  return `<form data-semantic-edit><button type="button" class="text-link" data-semantic-back>${icon('arrow-left')}返回词典</button><p class="notice">${esc(entry.table)}${entry.kind==='table'?'':'['+esc(entry.name)+']'}。保存为候选与确认定义分开处理；只有用户确认的定义才作为已确定的业务语义使用。</p>
    <label class="form-field">业务名称<input name="business_name" maxlength="160" value="${esc(entry.business_name)}"></label>
    <label class="form-field">业务含义与口径<textarea name="meaning" maxlength="1500" rows="5">${esc(entry.meaning)}</textarea></label>
    <label class="form-field">使用说明 / 适用边界<textarea name="usage_notes" maxlength="1500" rows="3" placeholder="补充表的业务粒度、指标的适用场景或限制；无需重复 DAX 公式">${esc(entry.usage_notes||'')}</textarea></label>
    <div class="form-row"><label class="form-field">字段角色<input name="role" maxlength="120" placeholder="例如：成本中心编号、费用分类、业务日期" value="${esc(entry.role)}"></label><label class="form-field">单位 / 币种<input name="unit" maxlength="100" placeholder="不确定时留空" value="${esc(entry.unit)}"></label></div>
    <label class="form-field">业务别名（用逗号分隔）<input name="aliases" value="${esc(entry.aliases.join('，'))}" placeholder="例如：人员费用，人力费用"></label>
    ${facts(entry)}<div class="modal-actions"><button type="submit" class="secondary-button">保存为待确认</button><button type="button" class="primary-button" data-confirm-semantic>确认业务定义</button></div></form>`;
}

export async function mountSemanticWorkbench(root,{api,modelId,onUpdate=()=>{},onPreview=()=>{},onManageDictionaries=()=>{}}) {
  let profile,service,editing=null,filter='',onlyPending=false,onlyChanges=false,busy=false,timer=null,disposed=false,paused=false;
  let tab='objects',renderedTab='objects',businessEditing=null;
  const scroll=root.closest('dialog')||root;
  const listViews={};
  function captureListView() {
    if(!$('[data-semantic-entries]',root))return null;
    const entries=[...root.querySelectorAll('[data-semantic-entry]')];
    const expanded=new Map(entries.map(entry=>[entry.dataset.semanticEntry,
      [...entry.querySelectorAll('details')].flatMap((details,index)=>details.hasAttribute('open')?[index]:[])]));
    const viewport=scroll.getBoundingClientRect?.();
    const anchor=viewport&&entries.find(entry=>{const rect=entry.getBoundingClientRect?.();return rect&&rect.bottom>viewport.top&&rect.top<viewport.bottom;});
    return {top:scroll.scrollTop||0,expanded,deltaOpen:!!$('.semantic-delta details[open]',root),
      anchor:anchor?{id:anchor.dataset.semanticEntry,offset:anchor.getBoundingClientRect().top-viewport.top}:null};
  }
  function restoreListView(view) {
    if(!view)return;
    const entries=[...root.querySelectorAll('[data-semantic-entry]')];
    for(const entry of entries)for(const [index,details] of [...entry.querySelectorAll('details')].entries())
      details.toggleAttribute('open',view.expanded.get(entry.dataset.semanticEntry)?.includes(index)||false);
    $('.semantic-delta details',root)?.toggleAttribute('open',view.deltaOpen);
    scroll.scrollTop=view.top;
    const anchor=view.anchor&&entries.find(entry=>entry.dataset.semanticEntry===view.anchor.id);
    if(anchor)scroll.scrollTop+=anchor.getBoundingClientRect().top-scroll.getBoundingClientRect().top-view.anchor.offset;
  }
  function feedback(text,error=false) {
    const el=$('[data-semantic-feedback]',root),view=captureListView();
    if(el){el.textContent=text;el.hidden=false;el.classList.toggle('error',error);restoreListView(view);}
  }
  function list() {
    const changed=new Set((profile.changes?.items||[]).map(e=>e.id));
    const entries=profile.entries.filter(e=>(!onlyPending||e.state!=='confirmed')&&(!onlyChanges||changed.has(e.id))&&
      [e.table,e.name,e.business_name,e.meaning,...e.aliases].join(' ').toLowerCase().includes(filter.toLowerCase()));
    $('[data-semantic-entries]',root).innerHTML=entries.map(entryMarkup).join('')||'<p class="notice">没有匹配的条目。</p>';
  }
  function render() {
    listViews[renderedTab]=captureListView()||listViews[renderedTab];
    const summary=semanticSummary(profile);
    const delta=profile.changes;
    const buildDisabled=profile.status==='building'||(summary.pending===0&&profile.status==='ready');
    const dictionaryName=profile.dictionary_name||profile.reuse?.source_name||profile.reuse?.report_name;
    root.innerHTML=`<header class="semantic-model-heading"><div><span class="semantic-section-label">当前模型</span><h3>${esc(profile.report_identity?.name||'模型业务语义')}</h3>${profile.report_identity?.path?`<p class="semantic-model-path">${esc(profile.report_identity.path)}</p>`:''}</div><button type="button" class="secondary-button semantic-library-action" data-manage-dictionaries>${icon('book')}词典管理</button></header>
      <div class="semantic-status-grid ${delta?'has-delta':''}"><div class="semantic-progress"><strong>${esc(profile.stage)}</strong><span>已梳理 ${profile.coverage}/${profile.entries.length} 项 · 用户确认 ${summary.confirmed} 项</span>${dictionaryName?`<p class="semantic-binding">词典：${esc(dictionaryName)}${profile.reuse?` · ${profile.reuse.matched_by==='user_selection'?'手动关联':'自动关联'}${profile.reuse.source_deleted?' · 来源历史版本已删除，当前副本保留':''}`:''}</p>`:''}</div>
      ${delta?`<div class="semantic-delta"><strong>本次模型同步</strong><p>保留 ${delta.preserved} 项 · 新增 ${delta.added} 项 · 修改或受影响 ${delta.modified} 项 · 删除 ${delta.removed} 项</p><p>${summary.can_answer?'可继续提问；未知业务口径按需确认。':'已有定义保留，只梳理待更新条目。'}</p>${delta.items.length?`<details><summary>查看变更清单</summary><ul>${delta.items.map(item=>`<li><b>${({added:'新增',modified:'修改或受影响',removed:'删除'})[item.change]}</b> ${esc(item.table)}${item.kind==='table'?'':'['+esc(item.name)+']'} · ${item.reasons.map(esc).join('；')}</li>`).join('')}</ul></details>`:''}</div>`:''}</div>
      ${profile.error?`<p class="semantic-error">${esc(profile.error)}</p>`:''}
      ${profile.dictionary_notice?`<div class="semantic-model-notice ${['needs_update','unverified'].includes(profile.dictionary_notice.status)?'semantic-change-note':'semantic-current-note'}"><span>${esc(profile.dictionary_notice.message)}</span>${profile.dictionary_notice.pending_business?'<button type="button" class="text-link" data-review-business>复核受影响的业务记录</button>':''}</div>`:''}
      <div class="semantic-model-tools"><button type="button" class="primary-button" data-build-semantics ${buildDisabled?'disabled':''}>${icon('sparkles')}${profile.status==='building'?'正在后台梳理…':buildDisabled?'对象语义已是最新':delta?`仅梳理待更新 ${summary.pending} 项`:profile.coverage>0?`继续梳理（剩余 ${summary.pending} 项）`:'开始梳理模型语义'}</button><span>模型服务：${esc(service?.name||service?.model||'当前模型服务')} · 生成建议后需审阅</span>
      <details class="semantic-help"><summary>服务与使用说明</summary><p>将待梳理对象的结构、公式、描述及相关关系发送至 ${esc(service?.base_url||'Anthropic 官方服务')} 解释含义；不修改 Desktop。生成业务候选还会使用当前对象说明。</p><p>语义词典保存在本机，重新打开同一报表时自动匹配并核对模型。字段名称和取值属于模型事实；自动解释是候选建议，需要你审阅。编号归属以实际查值为依据，有歧义时再确认。</p></details></div>
      <p data-semantic-feedback class="semantic-feedback" role="status" hidden></p>
      <nav class="semantic-tabs" aria-label="当前模型维护视图">${[['overview','业务总览'],['business','业务术语与分析约定'],['objects','对象词典']].map(([value,name])=>`<button type="button" class="${tab===value?'primary-button':'secondary-button'}" data-semantic-tab="${value}" aria-pressed="${tab===value}">${name}</button>`).join('')}</nav>
      ${['overview','business'].includes(tab)?`<div class="semantic-tools"><button type="button" class="secondary-button" data-business-suggest ${businessData(profile).generation.status==='building'?'disabled':''}>${businessData(profile).generation.status==='building'?'正在生成业务候选…':'生成业务候选'}</button><span>基于当前模型生成业务建议 · ${esc(service?.name||service?.model||'当前模型服务')} · 已有定义保留</span></div><p class="notice" data-business-progress ${businessData(profile).generation.stage||businessData(profile).generation.error?'':'hidden'}>${esc(businessData(profile).generation.stage||'')}${esc(businessData(profile).generation.error||'')}</p>`:''}
      <section data-semantic-content>${businessEditing?businessEditMarkup(businessEditing,profile.entries):tab!=='objects'?businessMarkup(profile,tab):editing?editMarkup(editing):`<div class="semantic-filter"><input type="search" data-semantic-search value="${esc(filter)}" placeholder="搜索表、字段、业务名称或含义" aria-label="搜索语义词典"><label><input type="checkbox" data-semantic-pending ${onlyPending?'checked':''}>只看待确认项</label>${delta?`<label><input type="checkbox" data-semantic-changes ${onlyChanges?'checked':''}>只看本次变更</label>`:''}</div><div class="semantic-entry-grid" data-semantic-entries></div>`}</section>`;
    renderedTab=tab;
    if(!editing&&!businessEditing){if(tab==='objects')list();restoreListView(listViews[tab]);}else scroll.scrollTop=0;
  }
  function schedule() {
    clearTimeout(timer);
    if(!disposed&&!paused&&root.isConnected&&(profile?.status==='building'||(profile&&businessData(profile).generation.status==='building')))timer=setTimeout(()=>void refresh(),1800);
  }
  async function refresh() {
    try {
      const next=await api(`/models/${modelId}/semantics`);
      if(disposed||paused)return;
      profile=next;onUpdate(semanticSummary(profile));
      // Do not rebuild an editor the user is currently typing in.
      if(!editing&&!businessEditing)render();else {
        const el=$('.semantic-progress',root);el.innerHTML=`<strong>${esc(profile.stage)}</strong><span>已梳理 ${profile.coverage}/${profile.entries.length} 项</span>`;
        const progress=$('[data-business-progress]',root);if(progress){progress.textContent=(businessData(profile).generation.stage||'')+(businessData(profile).generation.error||'');progress.hidden=!progress.textContent;}
        const suggest=$('[data-business-suggest]',root);if(suggest)suggest.disabled=businessData(profile).generation.status==='building';
      }
    } catch(error){if(disposed||paused)return;if(!profile)root.innerHTML=`<p class="semantic-error">${esc(error.message)}</p>`;else feedback(error.message,true);}
    schedule();
  }
  async function save(confirmed) {
    const form=$('[data-semantic-edit]',root);if(!form.reportValidity())return;
    const value=name=>$(`[name="${name}"]`,form).value.trim();
    const body={business_name:value('business_name'),meaning:value('meaning'),usage_notes:value('usage_notes'),role:value('role'),unit:value('unit'),
      aliases:value('aliases').split(/[,，;；]/).map(x=>x.trim()).filter(Boolean),confirmed};
    profile=await api(`/models/${modelId}/semantics/${editing.id}`,{method:'PUT',body:JSON.stringify(body)});
    editing=null;onUpdate(semanticSummary(profile));render();feedback(confirmed?'业务定义已确认，后续 DAX 和分析将共同使用':'已保存为待确认候选');schedule();
  }
  async function run(work) {
    if(busy)return;busy=true;
    root.querySelectorAll('button').forEach(button=>button.disabled=true);
    try {await work();}catch(error){feedback(error.message,true);}
    finally {busy=false;root.querySelectorAll('button').forEach(button=>button.disabled=false);if(profile&&(profile.status==='building'||(profile.status==='ready'&&semanticSummary(profile).pending===0)))$('[data-build-semantics]',root).disabled=true;
      const suggest=$('[data-business-suggest]',root);if(suggest)suggest.disabled=businessData(profile).generation.status==='building';}
  }
  async function saveBusiness(confirmed) {
    const form=$('[data-business-form]',root);if(!form.reportValidity())return;
    const path=businessEditing.kind==='overview'?'overview':'items'+(businessEditing.record?.id?'/'+businessEditing.record.id:'');
    const method=businessEditing.kind==='overview'||businessEditing.record?.id?'PUT':'POST';
    profile=await api(`/models/${modelId}/semantics/business/${path}`,{method,body:JSON.stringify(businessBody(form,businessEditing,confirmed))});
    businessEditing=null;onUpdate(semanticSummary(profile));render();feedback(confirmed?'业务定义已确认，将用于后续查询和回答':'已保存为候选，尚不作为已确认业务条件');schedule();
  }
  function editBusiness(kind,record=null) {businessEditing={kind,record,revision:businessData(profile).revision};render();}
  root.onclick=event=>{
    const button=event.target.closest('button');if(!button||busy)return;
    if(button.hasAttribute('data-manage-dictionaries'))onManageDictionaries(modelId);
    if(button.dataset.semanticTab){tab=button.dataset.semanticTab;editing=null;businessEditing=null;render();}
    if(button.hasAttribute('data-review-business')){tab=businessData(profile).overview?.state==='unresolved'?'overview':'business';editing=null;businessEditing=null;render();}
    if(button.hasAttribute('data-business-overview'))editBusiness('overview',businessData(profile).overview);
    if(button.dataset.businessNew)editBusiness(button.dataset.businessNew);
    if(button.dataset.businessEdit){const record=businessData(profile).items.find(e=>e.id===button.dataset.businessEdit);if(record)editBusiness(record.kind,record);}
    if(button.hasAttribute('data-business-back')){businessEditing=null;render();void refresh();}
    if(button.hasAttribute('data-business-confirm'))void run(()=>saveBusiness(true));
    if(button.hasAttribute('data-business-add-target'))$('[data-business-targets]',root).insertAdjacentHTML('beforeend',targetRow(profile.entries));
    if(button.hasAttribute('data-business-add-filter'))$('[data-business-filters]',root).insertAdjacentHTML('beforeend',filterRow(profile.entries));
    if(button.hasAttribute('data-business-remove'))button.closest('.business-reference').remove();
    if(button.dataset.businessDelete){button.textContent='确认删除此词条';button.dataset.businessDeleteConfirm=button.dataset.businessDelete;delete button.dataset.businessDelete;}
    else if(button.dataset.businessDeleteConfirm)void run(async()=>{
      profile=await api(`/models/${modelId}/semantics/business/items/${button.dataset.businessDeleteConfirm}?revision=${businessData(profile).revision}`,{method:'DELETE'});
      onUpdate(semanticSummary(profile));render();feedback('业务词条已删除');
    });
    if(button.hasAttribute('data-business-suggest'))void run(async()=>{
      profile=await api(`/models/${modelId}/semantics/business/suggest`,{method:'POST',body:JSON.stringify({llm_profile_id:service?.active_profile_id||null})});
      onUpdate(semanticSummary(profile));if(!businessEditing)render();schedule();
    });
    if(button.dataset.editSemantic){editing=profile.entries.find(e=>e.id===button.dataset.editSemantic);render();}
    if(button.hasAttribute('data-semantic-back')){editing=null;render();}
    if(button.hasAttribute('data-confirm-semantic'))void run(()=>save(true));
    if(button.hasAttribute('data-build-semantics'))void run(async()=>{
      profile=await api(`/models/${modelId}/semantics/build`,{method:'POST',body:JSON.stringify({llm_profile_id:service?.active_profile_id||null})});onUpdate(semanticSummary(profile));if(!editing&&!businessEditing)render();schedule();
    });
    if(button.dataset.semanticPreview){const entry=profile.entries.find(e=>e.id===button.dataset.semanticPreview);onPreview(entry.table,entry.name,modelId);}
  };
  root.oninput=event=>{if(event.target.matches('[data-semantic-search]')){filter=event.target.value;list();}};
  root.onchange=event=>{if(event.target.matches('[data-semantic-pending]')){onlyPending=event.target.checked;list();}if(event.target.matches('[data-semantic-changes]')){onlyChanges=event.target.checked;list();}
    if(event.target.matches('[data-business-scope]'))$('[data-business-scope-objects]',root).hidden=event.target.value!=='objects';};
  root.onsubmit=event=>{event.preventDefault();if(businessEditing)void run(()=>saveBusiness(false));else if(editing)void run(()=>save(false));};
  root.innerHTML='<p class="notice">正在读取模型语义词典…</p>';
  service=await api('/settings');
  await refresh();
  return {pause(){paused=true;clearTimeout(timer);},resume(){if(!disposed){paused=false;schedule();}},refresh,
    dispose(){disposed=true;clearTimeout(timer);}};
}
