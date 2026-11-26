import {serviceText, tr} from './i18n.js';
import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';
import {codeBlock} from './code_blocks.js';
import {resultTable} from './table_reader.js';

export function analysisEvidence(queries) {
  return queries.map(query=>{
    const rows=query.rows||[],columns=query.columns||[];
    const preview=rows.slice(0,20);
    const attempt=query.recovery?`${tr('替代 {0} ·', esc(query.recovery.for_query))} `:query.attempt>1?`${tr('重试 {0} ·', query.attempt-1)} `:'';
    return `<details class="analysis-evidence ${query.ok?'':'failed'}" data-evidence="${esc(query.id)}">
      <summary>${icon(query.ok?'database':'info')}<strong>${esc(query.label)} · ${esc(query.title)}</strong><span>${attempt}${query.ok?tr('{0} 行{1}', query.row_count, query.truncated?` ${tr('· 已截断')}`:''):query.run_verified?tr('查询未通过'):tr('查询未执行')}</span>${icon('chevron-down')}</summary>
      <div class="analysis-evidence-body">
        <div class="analysis-query-meta">${tr('{0}{1} · {2} 秒 · {3}', query.ok?tr('Desktop 真实查询'):tr('查询记录'), query.batch?` ${tr('· 第 {0} 批', esc(query.batch))}`:'', esc(query.seconds), esc(query.created_at))}</div>
        ${query.recovery?`<p class="analysis-result-note">${tr('需要补充的证据：{0}', esc(query.recovery.missing))}<br>${tr('替代查法：{0}', esc(query.recovery.basis))}</p>`:''}
        ${Object.values(query.convention_exceptions||{}).map(note=>`<p class="analysis-result-note"><strong>${esc(note.title)}</strong>　${esc(note.reason)}</p>`).join('')}
        ${query.ok?(rows.length?`<div class="analysis-table-toolbar"><span>${rows.length>preview.length?tr('预览前 {0} 行 · 已保留 {1} 行', preview.length, rows.length):tr('{0} 行 · {1} 列', rows.length, columns.length)}</span><button type="button" class="text-link" data-table-expand="${esc(query.id)}" aria-haspopup="dialog">${icon('expand')}${tr('放大查看')}</button></div><div class="analysis-result-table">${resultTable(columns,preview)}</div>`:`<p class="analysis-empty-result">${tr('查询成功，但没有符合条件的数据。不能据此将业务指标认定为零。')}</p>`):`<p class="analysis-query-error">${esc(serviceText(query.error))}</p>`}
        ${(query.warnings||[]).map(w=>`<p class="analysis-result-note">${esc(serviceText(w))}</p>`).join('')}
        ${rows.length>preview.length?`<p class="analysis-result-note">${tr('放大查看或下载可查看本次保留的全部结果。')}</p>`:''}
        ${Object.keys(query.parameters||{}).length?`<p class="analysis-result-note"><strong>${tr('本次查询参数')}</strong> ${Object.entries(query.parameters).map(([name,value])=>'@'+esc(name)+' = '+esc(JSON.stringify(value))).join(' · ')}</p>`:''}
        ${codeBlock(query.dax,'dax',{title:`DAX · ${query.label}`,copyLabel:tr('复制查询')})}
        ${query.ok?`<a class="analysis-download" href="/api/analysis/${esc(query.id)}/csv" download>${icon('download')}${tr('下载本次结果 CSV{0}', query.truncated?tr('（已截断）'):'')}</a>`:''}
      </div>
    </details>`;
  }).join('');
}
