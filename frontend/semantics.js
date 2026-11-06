import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';

const $=(selector,root)=>root.querySelector(selector);
const states={confirmed:'用户已确认',candidate:'候选含义 · 待确认',unresolved:'业务含义未知'};
const kinds={table:'表',column:'字段',measure:'度量值'};
export const semanticSummary=p=>({status:p.status,stage:p.stage,coverage:p.coverage,total:p.entries.length,
  confirmed:p.entries.filter(e=>e.state==='confirmed').length});

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
    <p class="semantic-meaning">${esc(entry.meaning||'业务含义尚未确定，需要结合模型描述和业务定义确认。')}</p>
    <p class="semantic-unit">单位：${esc(entry.unit||'未知 / 未确认')}${entry.aliases.length?' · 别名：'+entry.aliases.map(esc).join('、'):''}</p>${facts(entry)}
    <div class="semantic-entry-actions"><button class="secondary-button" data-edit-semantic="${esc(entry.id)}">编辑与确认</button>${entry.kind==='column'?`<button class="text-link" data-semantic-preview="${esc(entry.id)}">${icon('eye')}查看实际取值</button>`:''}</div></article>`;
}

function editMarkup(entry) {
  return `<form data-semantic-edit><button type="button" class="text-link" data-semantic-back>${icon('arrow-left')}返回词典</button><p class="notice">${esc(entry.table)}${entry.kind==='table'?'':'['+esc(entry.name)+']'}。保存为候选与确认定义分开处理；只有用户确认的定义才作为已确定的业务语义使用。</p>
    <label class="form-field">业务名称<input name="business_name" maxlength="160" value="${esc(entry.business_name)}"></label>
    <label class="form-field">业务含义与口径<textarea name="meaning" maxlength="1500" rows="5">${esc(entry.meaning)}</textarea></label>
    <div class="form-row"><label class="form-field">字段角色<input name="role" maxlength="120" placeholder="例如：成本中心编号、费用分类、业务日期" value="${esc(entry.role)}"></label><label class="form-field">单位 / 币种<input name="unit" maxlength="100" placeholder="不确定时留空" value="${esc(entry.unit)}"></label></div>
    <label class="form-field">业务别名（用逗号分隔）<input name="aliases" value="${esc(entry.aliases.join('，'))}" placeholder="例如：人员费用，人力费用"></label>
    ${facts(entry)}<div class="modal-actions"><button type="submit" class="secondary-button">保存为待确认</button><button type="button" class="primary-button" data-confirm-semantic>确认业务定义</button></div></form>`;
}

export async function mountSemanticWorkbench(root,{api,modelId,onUpdate=()=>{},onPreview=()=>{}}) {
  let profile,service,editing=null,filter='',onlyPending=false,busy=false,timer=null,disposed=false;
  function feedback(text,error=false) {const el=$('[data-semantic-feedback]',root);if(el){el.textContent=text;el.hidden=false;el.classList.toggle('error',error);}}
  function list() {
    const entries=profile.entries.filter(e=>(!onlyPending||e.state!=='confirmed')&&
      [e.table,e.name,e.business_name,e.meaning,...e.aliases].join(' ').toLowerCase().includes(filter.toLowerCase()));
    $('[data-semantic-entries]',root).innerHTML=entries.map(entryMarkup).join('')||'<p class="notice">没有匹配的条目。</p>';
  }
  function render() {
    const summary=semanticSummary(profile);
    root.innerHTML=`<p class="notice">先梳理模型，再进行 DAX 与数据分析。字段名称和取值属于模型事实；自动解释是候选建议，需要你审阅。编号出现在哪个字段与本次要筛选哪个字段分别确认。</p>
      <p data-semantic-feedback class="semantic-feedback" role="status" hidden></p>
      <div class="semantic-progress"><strong>${esc(profile.stage)}</strong><span>已梳理 ${profile.coverage}/${profile.entries.length} 项 · 用户确认 ${summary.confirmed} 项</span></div>
      ${profile.error?`<p class="semantic-error">${esc(profile.error)}</p>`:''}
      <div class="semantic-tools"><button type="button" class="primary-button" data-build-semantics ${profile.status==='building'?'disabled':''}>${icon('sparkles')}${profile.status==='building'?'正在后台梳理…':profile.status==='ready'?'重新梳理候选含义':profile.status==='error'&&profile.coverage>0?`继续梳理（已完成 ${profile.coverage} 项）`:'开始梳理模型语义'}</button><span>点击后将模型结构、公式和描述发送至 ${esc(service?.name||service?.model||'当前模型服务')}（${esc(service?.base_url||'Anthropic 官方服务')}）解释字段含义；不修改 Desktop。</span></div>
      <section data-semantic-content>${editing?editMarkup(editing):`<div class="semantic-filter"><input type="search" data-semantic-search value="${esc(filter)}" placeholder="搜索表、字段、业务名称或含义" aria-label="搜索语义词典"><label><input type="checkbox" data-semantic-pending ${onlyPending?'checked':''}>只看待确认项</label></div><div data-semantic-entries></div>`}</section>`;
    if(!editing)list();
  }
  function schedule() {
    clearTimeout(timer);
    if(!disposed&&root.isConnected&&profile?.status==='building')timer=setTimeout(()=>void refresh(),1800);
  }
  async function refresh() {
    try {
      profile=await api(`/models/${modelId}/semantics`);onUpdate(semanticSummary(profile));
      // Do not rebuild an editor the user is currently typing in.
      if(!editing)render();else {
        const el=$('.semantic-progress',root);el.innerHTML=`<strong>${esc(profile.stage)}</strong><span>已梳理 ${profile.coverage}/${profile.entries.length} 项</span>`;
      }
    } catch(error){if(!profile)root.innerHTML=`<p class="semantic-error">${esc(error.message)}</p>`;else feedback(error.message,true);}
    schedule();
  }
  async function save(confirmed) {
    const form=$('[data-semantic-edit]',root);if(!form.reportValidity())return;
    const value=name=>$(`[name="${name}"]`,form).value.trim();
    const body={business_name:value('business_name'),meaning:value('meaning'),role:value('role'),unit:value('unit'),
      aliases:value('aliases').split(/[,，;；]/).map(x=>x.trim()).filter(Boolean),confirmed};
    profile=await api(`/models/${modelId}/semantics/${editing.id}`,{method:'PUT',body:JSON.stringify(body)});
    editing=null;onUpdate(semanticSummary(profile));render();feedback(confirmed?'业务定义已确认，后续 DAX 和分析将共同使用':'已保存为待确认候选');schedule();
  }
  async function run(work) {
    if(busy)return;busy=true;
    root.querySelectorAll('button').forEach(button=>button.disabled=true);
    try {await work();}catch(error){feedback(error.message,true);}
    finally {busy=false;root.querySelectorAll('button').forEach(button=>button.disabled=false);if(profile?.status==='building')$('[data-build-semantics]',root).disabled=true;}
  }
  root.onclick=event=>{
    const button=event.target.closest('button');if(!button||busy)return;
    if(button.dataset.editSemantic){editing=profile.entries.find(e=>e.id===button.dataset.editSemantic);render();}
    if(button.hasAttribute('data-semantic-back')){editing=null;render();}
    if(button.hasAttribute('data-confirm-semantic'))void run(()=>save(true));
    if(button.hasAttribute('data-build-semantics'))void run(async()=>{
      profile=await api(`/models/${modelId}/semantics/build`,{method:'POST',body:JSON.stringify({llm_profile_id:service?.active_profile_id||null})});onUpdate(semanticSummary(profile));render();schedule();
    });
    if(button.dataset.semanticPreview){const entry=profile.entries.find(e=>e.id===button.dataset.semanticPreview);onPreview(entry.table,entry.name,modelId);}
  };
  root.oninput=event=>{if(event.target.matches('[data-semantic-search]')){filter=event.target.value;list();}};
  root.onchange=event=>{if(event.target.matches('[data-semantic-pending]')){onlyPending=event.target.checked;list();}};
  root.onsubmit=event=>{event.preventDefault();if(editing)void run(()=>save(false));};
  root.innerHTML='<p class="notice">正在读取模型语义词典…</p>';
  service=await api('/settings');
  await refresh();
  return {dispose(){disposed=true;clearTimeout(timer);}};
}
