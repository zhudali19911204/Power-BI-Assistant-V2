import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';

const number=value=>value==null?'未统计':Number(value).toLocaleString('zh-CN');
export function fieldValue(entry) {
  if(entry.nonfinite)return '<span class="field-value-null">非有限数值</span>';
  if(entry.value===null)return '<span class="field-value-null">空值 BLANK()</span>';
  if(entry.value==='')return '<span class="field-value-null">空字符串 ""</span>';
  return esc(String(entry.value))+(entry.value_truncated?'<span class="field-value-null">…（值已截断）</span>':'');
}

function statistics(profile) {
  const stats=profile.statistics;
  const pct=stats.row_count?` · ${(100*stats.blank_count/stats.row_count).toFixed(1)}%`:'';
  return `<div class="field-statistics"><div><span>总行数</span><strong>${number(stats.row_count)}</strong></div>
    <div><span>不同取值（含空值）</span><strong>${number(stats.distinct_count)}</strong></div>
    <div><span>模型空值${pct}</span><strong>${number(stats.blank_count)}</strong></div>
    <div><span>空字符串</span><strong>${number(stats.empty_string_count)}</strong></div></div>`;
}

export function fieldProfile(profile,{compact=false}={}) {
  const values=compact?profile.values.slice(0,8):profile.values,stats=profile.statistics;
  return `<div class="field-profile">
    <p class="field-preview-scope">${esc(profile.scope)}</p>${statistics(profile)}
    <div class="field-samples"><span>取值样例</span>${profile.samples.map(entry=>`<span class="field-sample">${fieldValue(entry)}</span>`).join('')||'<small>此字段没有可显示的取值</small>'}</div>
    ${profile.search_terms.length?`<p class="field-preview-note">搜索：${profile.search_terms.map(esc).join(' / ')} · 匹配 ${number(stats.matched_distinct_count)} 个不同取值。统计卡片和样例仍为全模型范围。</p>`:''}
    ${values.length?`<div class="field-value-table"><table><thead><tr><th>取值</th><th>出现行数</th><th>占总行数</th></tr></thead><tbody>${values.map(entry=>`<tr><td>${fieldValue(entry)}</td><td>${number(entry.count)}</td><td>${stats.row_count?(100*entry.count/stats.row_count).toFixed(2)+'%':'—'}</td></tr>`).join('')}</tbody></table></div>`:
      `<p class="field-preview-note">${profile.search_terms.length?'未找到匹配取值，可清空搜索或尝试其他同义词。':'此字段没有可显示的取值。'}</p>`}
    <p class="field-preview-note">按出现次数排序。${profile.truncated?'结果已截断，不代表全部取值。':`本次返回 ${profile.values.length} 个${profile.search_terms.length?'匹配的':''}不同取值。`}${values.length<profile.values.length?` 此处预览前 ${values.length} 项。`:''}</p>
    <p class="field-preview-time">本机实际读取 · ${esc(profile.checked_at)} · ${esc(profile.seconds)} 秒</p>
  </div>`;
}

export function fieldEvidence(records=[]) {
  return records.map(record=>`<details class="field-evidence ${record.ok?'':'failed'}" data-field-evidence="${esc(record.id)}">
    <summary>${icon('search')}<strong>${esc(record.label)} · ${esc(record.table)}[${esc(record.column)}]</strong><span>${record.ok?'已核对取值':'检查未完成'}</span>${icon('chevron-down')}</summary>
    <div class="field-evidence-body">${record.reason?`<p class="field-preview-note">${esc(record.reason)}</p>`:''}
      ${record.ok?fieldProfile(record.profile,{compact:true}):`<p class="field-preview-error">${esc(record.error)}</p>`}
      <button class="text-link" data-preview-model="${esc(record.model_id)}" data-preview-table="${esc(record.table)}" data-preview-column="${esc(record.column)}">${icon('table')}打开字段预览</button>
      ${record.ok?`<details class="field-query-details"><summary>查看取值查询依据</summary>${record.profile.dax_queries.map(query=>`<div class="code-block"><pre><code>${esc(query)}</code></pre></div>`).join('')}</details>`:''}
    </div></details>`).join('');
}

export async function mountFieldPreview(root,{api,modelId,table,column,onBack=null}) {
  root.innerHTML=`${onBack?`<button type="button" class="text-link field-preview-back" data-field-back>${icon('arrow-left')}返回业务字典</button>`:''}<p class="notice">读取当前模型中的字段实际内容。空值按模型中的 BLANK() 统计，空字符串单独列出可识别的数量。</p>
    <form class="field-preview-search" data-field-search><input name="search" type="search" maxlength="120" placeholder="搜索取值，可尝试人员、personnel 等同义词" aria-label="搜索字段取值"><button class="primary-button" type="submit">搜索取值</button><button class="secondary-button" type="button" data-field-clear>清空</button></form>
    <div data-field-result aria-live="polite"></div>`;
  let sequence=0,disposed=false;
  const form=root.querySelector('form'),result=root.querySelector('[data-field-result]');
  async function load() {
    if(disposed)return;
    const request=++sequence;
    const search=form.querySelector('input').value.trim();
    form.querySelectorAll('button').forEach(button=>button.disabled=true);
    result.innerHTML='<p class="field-preview-note">正在读取字段取值和空值统计…</p>';
    try {
      const profile=await api(`/models/${encodeURIComponent(modelId)}/fields/preview`,{
        method:'POST',body:JSON.stringify({table,column,search,limit:50}),
      });
      if(request===sequence)result.innerHTML=fieldProfile(profile);
    } catch(error) {
      if(request===sequence)result.innerHTML=`<p class="field-preview-error">${esc(error.message)}</p><button type="button" class="secondary-button" data-field-retry>重新读取</button>`;
    } finally {if(request===sequence)form.querySelectorAll('button').forEach(button=>button.disabled=false);}
  }
  form.onsubmit=event=>{event.preventDefault();void load();};
  root.onclick=event=>{
    if(event.target.closest('[data-field-back]')){onBack?.();return;}
    if(event.target.closest('[data-field-clear]')){form.querySelector('input').value='';void load();}
    if(event.target.closest('[data-field-retry]'))void load();
  };
  await load();
  return {dispose(){disposed=true;sequence++;root.onclick=null;form.onsubmit=null;}};
}
