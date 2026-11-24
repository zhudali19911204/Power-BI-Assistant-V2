import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';
import {codeBlock} from './code_blocks.js';
import {resultTable} from './table_reader.js';

export function analysisEvidence(queries) {
  return queries.map(query=>{
    const rows=query.rows||[],columns=query.columns||[];
    const preview=rows.slice(0,20);
    const attempt=query.recovery?`替代 ${esc(query.recovery.for_query)} · `:query.attempt>1?`重试 ${query.attempt-1} · `:'';
    return `<details class="analysis-evidence ${query.ok?'':'failed'}" data-evidence="${esc(query.id)}">
      <summary>${icon(query.ok?'database':'info')}<strong>${esc(query.label)} · ${esc(query.title)}</strong><span>${attempt}${query.ok?`${query.row_count} 行${query.truncated?' · 已截断':''}`:query.run_verified?'查询未通过':'查询未执行'}</span>${icon('chevron-down')}</summary>
      <div class="analysis-evidence-body">
        <div class="analysis-query-meta">${query.ok?'Desktop 真实查询':'查询记录'}${query.batch?` · 第 ${esc(query.batch)} 批`:''} · ${esc(query.seconds)} 秒 · ${esc(query.created_at)}</div>
        ${query.recovery?`<p class="analysis-result-note">需要补充的证据：${esc(query.recovery.missing)}<br>替代查法：${esc(query.recovery.basis)}</p>`:''}
        ${Object.values(query.convention_exceptions||{}).map(note=>`<p class="analysis-result-note"><strong>${esc(note.title)}</strong>　${esc(note.reason)}</p>`).join('')}
        ${query.ok?(rows.length?`<div class="analysis-table-toolbar"><span>${rows.length>preview.length?`预览前 ${preview.length} 行 · 已保留 ${rows.length} 行`:`${rows.length} 行 · ${columns.length} 列`}</span><button type="button" class="text-link" data-table-expand="${esc(query.id)}" aria-haspopup="dialog">${icon('expand')}放大查看</button></div><div class="analysis-result-table">${resultTable(columns,preview)}</div>`:'<p class="analysis-empty-result">查询成功，但没有符合条件的数据。不能据此将业务指标认定为零。</p>'):`<p class="analysis-query-error">${esc(query.error)}</p>`}
        ${(query.warnings||[]).map(w=>`<p class="analysis-result-note">${esc(w)}</p>`).join('')}
        ${rows.length>preview.length?'<p class="analysis-result-note">放大查看或下载可查看本次保留的全部结果。</p>':''}
        ${codeBlock(query.dax,'dax',{title:`DAX · ${query.label}`,copyLabel:'复制查询'})}
        ${query.ok?`<a class="analysis-download" href="/api/analysis/${esc(query.id)}/csv" download>${icon('download')}下载本次结果 CSV${query.truncated?'（已截断）':''}</a>`:''}
      </div>
    </details>`;
  }).join('');
}
