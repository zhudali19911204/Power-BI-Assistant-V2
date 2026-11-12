import {escapeHtml as esc} from './markdown.js';

const states={candidate:'候选 · 待确认',confirmed:'用户已确认',unresolved:'模型已变化 · 待复核'};
const kinds={table:'表',column:'字段',measure:'度量值'};
const label=e=>`${kinds[e.kind]} · ${e.table}${e.kind==='table'?'':'['+e.name+']'}`;
const lines=value=>value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
const aliases=value=>value.split(/[,，；\n]/).map(s=>s.trim()).filter(Boolean);
export const businessData=profile=>profile.business_dictionary||{revision:0,overview:null,items:[],generation:{status:'idle'}};
export const hasBusinessKnowledge=profile=>[businessData(profile).overview,...businessData(profile).items].some(r=>r&&['candidate','confirmed'].includes(r.state));

function status(record) {
  return `<span class="semantic-state ${esc(record.state)}">${states[record.state]||'待确认'}</span>
    ${record.stale_reason?`<p class="semantic-change-note">${esc(record.stale_reason)}</p>`:''}`;
}
function provenance(record) {
  return `<p class="notice">来源：${esc(record.source||'用户编辑')}${record.source_note?' · '+esc(record.source_note):''}${record.reviewed_at?' · 审阅于 '+esc(record.reviewed_at):''}</p>`;
}

export function businessMarkup(profile,tab) {
  const data=businessData(profile);
  if(tab==='overview') {
    const overview=data.overview;
    return `<div data-semantic-entries><article class="semantic-entry" data-semantic-entry="business-overview"><h3>模型业务总览</h3>
      <p class="notice">补充模型服务的业务、主题和重要联系。表结构、关系方向和度量值公式继续来自模型同步。</p>
      ${overview?`${status(overview)}<p class="semantic-meaning">${esc(overview.description)}</p><p>业务主题：${overview.topics.map(esc).join('、')||'未填写'}</p>${provenance(overview)}`:'<p>尚未填写业务总览。可以手动补充，或生成候选后审阅。</p>'}
      <button type="button" class="secondary-button" data-business-overview>编辑业务总览</button></article></div>`;
  }
  const ref=id=>{const entry=profile.entries.find(e=>e.id===id);return entry?label(entry):'已删除对象（请重新关联）';};
  return `<p class="notice">术语可关联多个指标、维度或筛选值；分析约定只在适用范围内补充未指定条件。计算标准由现有度量值公式决定。</p>
    <div class="semantic-tools"><button type="button" class="secondary-button" data-business-new="term">新增业务术语</button><button type="button" class="secondary-button" data-business-new="convention">新增分析约定</button></div>
    <div data-semantic-entries>${data.items.map(item=>`<article class="semantic-entry" data-semantic-entry="${esc(item.id)}">
      <div class="semantic-entry-heading"><strong>${esc(item.title)}</strong>${status(item)}</div>
      <p>${item.kind==='term'?'业务术语':'分析约定'} · ${item.scope==='model'?'整个模型':item.scope==='topic'?'业务主题：'+esc(item.topic):'指定对象：'+item.scope_object_ids.map(id=>esc(ref(id))).join('、')}</p>
      <p class="semantic-meaning">${esc(item.meaning)}</p>${item.aliases.length?`<p>别名：${item.aliases.map(esc).join('、')}</p>`:''}
      <details class="semantic-facts"><summary>关联对象与业务依据</summary>
        ${item.targets.map(t=>`<p><b>${esc(ref(t.object_id))}</b><span>${esc(t.purpose)}</span></p>`).join('')}
        ${item.filters.map(f=>`<p><b>${esc(ref(f.object_id))}</b><span>${f.operator==='eq'?'等于':'属于'} ${f.values.map(esc).join('、')}</span></p>`).join('')}${provenance(item)}</details>
      <div class="semantic-entry-actions"><button type="button" class="secondary-button" data-business-edit="${esc(item.id)}">编辑与确认</button><button type="button" class="text-link" data-business-delete="${esc(item.id)}">删除</button></div>
      </article>`).join('')||'<p class="notice">尚无业务术语或分析约定。</p>'}</div>`;
}

function options(entries,selected=[],columns=false) {
  const available=entries.filter(e=>!columns||e.kind==='column');
  return available.map(e=>`<option value="${esc(e.id)}" ${selected.includes(e.id)?'selected':''}>${esc(label(e))}</option>`).join('')+
    selected.filter(id=>!available.some(e=>e.id===id)).map(id=>`<option value="${esc(id)}" selected>已删除对象（请重新选择）</option>`).join('');
}

export function targetRow(entries,target={}) {
  return `<div class="business-reference" data-business-target><label class="form-field">关联对象<select name="object_id" required><option value="">请选择模型对象</option>${options(entries,[target.object_id].filter(Boolean))}</select></label>
    <label class="form-field">该对象的业务用途<input name="purpose" maxlength="300" value="${esc(target.purpose||'')}" placeholder="例如：作为分析金额指标"></label><button type="button" class="text-link" data-business-remove>移除关联</button></div>`;
}
export function filterRow(entries,filter={}) {
  return `<div class="business-reference" data-business-filter><label class="form-field">筛选字段<select name="object_id" required><option value="">请选择真实字段</option>${options(entries,[filter.object_id].filter(Boolean),true)}</select></label>
    <label class="form-field">匹配方式<select name="operator"><option value="eq" ${filter.operator!=='in'?'selected':''}>等于一个值</option><option value="in" ${filter.operator==='in'?'selected':''}>属于多个值</option></select></label>
    <label class="form-field">实际取值（每行一个，保留前导零）<textarea name="values" rows="3" required>${esc((filter.values||[]).join('\n'))}</textarea></label><button type="button" class="text-link" data-business-remove>移除筛选</button></div>`;
}

export function businessEditMarkup(edit,entries) {
  const r=edit.record||{},isOverview=edit.kind==='overview';
  return `<form data-business-form><button type="button" class="text-link" data-business-back>返回词典</button>
    <p class="notice">${isOverview?'描述模型用途、主要业务联系和重要注意事项。':'说明业务含义与使用方式，引用现有对象。分析约定仅补充用户未指定的条件。'} 保存候选与确认分开处理。</p>
    ${isOverview?`<label class="form-field">业务总览<textarea name="description" rows="7" maxlength="4000" required>${esc(r.description||'')}</textarea></label>
      <label class="form-field">业务主题（每行一个）<textarea name="topics" rows="3">${esc((r.topics||[]).join('\n'))}</textarea></label>`:
      `<label class="form-field">${edit.kind==='term'?'业务术语':'分析约定'}名称<input name="title" maxlength="160" required value="${esc(r.title||'')}"></label>
      <label class="form-field">业务含义与使用说明<textarea name="meaning" maxlength="2000" rows="5" required>${esc(r.meaning||'')}</textarea></label>
      <label class="form-field">业务别名（用逗号分隔）<input name="aliases" value="${esc((r.aliases||[]).join('，'))}"></label>
      <label class="form-field">适用范围<select name="scope" data-business-scope>${[['model','整个模型'],['topic','指定业务主题'],['objects','指定模型对象']].map(([value,name])=>`<option value="${value}" ${(r.scope||'model')===value?'selected':''}>${name}</option>`).join('')}</select></label>
      <label class="form-field" data-business-topic>业务主题<input name="topic" maxlength="100" value="${esc(r.topic||'')}" placeholder="例如：采购、库存、费用"></label>
      <label class="form-field" data-business-scope-objects ${r.scope!=='objects'?'hidden':''}>适用对象（按住 Ctrl 或 Shift 多选）<select name="scope_object_ids" multiple size="5">${options(entries,r.scope_object_ids||[])}</select></label>
      <fieldset><legend>关联模型对象</legend><div data-business-targets>${(r.targets||[]).map(t=>targetRow(entries,t)).join('')}</div><button type="button" class="text-link" data-business-add-target>添加关联对象</button></fieldset>
      <fieldset><legend>业务筛选映射（可选）</legend><p class="notice">仅填写有业务依据的真实值；例如某业务分类对应的代码。候选筛选不会作为已确认条件。</p><div data-business-filters>${(r.filters||[]).map(f=>filterRow(entries,f)).join('')}</div><button type="button" class="text-link" data-business-add-filter>添加筛选映射</button></fieldset>`}
    <label class="form-field">业务依据 / 来源<textarea name="source_note" maxlength="1000" rows="2" placeholder="例如：业务负责人确认、报表使用说明">${esc(r.source_note||'')}</textarea></label>
    <div class="modal-actions"><button type="submit" class="secondary-button">保存为待确认</button><button type="button" class="primary-button" data-business-confirm>确认业务定义</button></div></form>`;
}

export function businessBody(form,edit,confirmed) {
  const value=(name,root=form)=>root.querySelector(`[name="${name}"]`).value.trim();
  const shared={confirmed,revision:edit.revision,source_note:value('source_note')};
  if(edit.kind==='overview')return {...shared,description:value('description'),topics:lines(value('topics'))};
  return {...shared,kind:edit.kind,title:value('title'),meaning:value('meaning'),aliases:aliases(value('aliases')),
    scope:value('scope'),topic:value('topic'),scope_object_ids:[...form.querySelectorAll('[name="scope_object_ids"] option')].filter(o=>o.selected).map(o=>o.value),
    targets:[...form.querySelectorAll('[data-business-target]')].map(row=>({object_id:value('object_id',row),purpose:value('purpose',row)})),
    filters:[...form.querySelectorAll('[data-business-filter]')].map(row=>({object_id:value('object_id',row),operator:value('operator',row),values:lines(value('values',row))}))};
}
