import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';

function cell(value) {
  return value===null||value===undefined?'<span class="analysis-null">空值</span>':esc(value);
}

export function analysisEvidence(queries) {
  return queries.map(query=>{
    const rows=query.rows||[],columns=query.columns||[];
    const preview=rows.slice(0,20);
    return `<details class="analysis-evidence ${query.ok?'':'failed'}" data-evidence="${esc(query.id)}">
      <summary>${icon(query.ok?'database':'info')}<strong>${esc(query.label)} · ${esc(query.title)}</strong><span>${query.attempt>1?`重试 ${query.attempt-1} · `:''}${query.ok?`${query.row_count} 行${query.truncated?' · 已截断':''}`:query.run_verified?'查询未通过':'查询未执行'}</span>${icon('chevron-down')}</summary>
      <div class="analysis-evidence-body">
        <div class="analysis-query-meta">${query.ok?'Desktop 真实查询':'查询记录'} · ${esc(query.seconds)} 秒 · ${esc(query.created_at)}</div>
        ${query.ok?(rows.length?`<div class="analysis-result-table"><table><thead><tr>${columns.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${preview.map(row=>`<tr>${columns.map(c=>`<td>${cell(row[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`:'<p class="analysis-empty-result">查询成功，但没有符合条件的数据。不能据此将业务指标认定为零。</p>'):`<p class="analysis-query-error">${esc(query.error)}</p>`}
        ${(query.warnings||[]).map(w=>`<p class="analysis-result-note">${esc(w)}</p>`).join('')}
        ${rows.length>preview.length?`<p class="analysis-result-note">预览前 ${preview.length} 行，已保留 ${rows.length} 行。下载可查看本次保留的全部结果。</p>`:''}
        <div class="code-block"><div class="code-heading"><span>DAX · ${esc(query.label)}</span><button class="copy-code">${icon('copy')}复制查询</button></div><pre><code>${esc(query.dax)}</code></pre></div>
        ${query.ok?`<a class="analysis-download" href="/api/analysis/${esc(query.id)}/csv" download>${icon('download')}下载本次结果 CSV${query.truncated?'（已截断）':''}</a>`:''}
      </div>
    </details>`;
  }).join('');
}
