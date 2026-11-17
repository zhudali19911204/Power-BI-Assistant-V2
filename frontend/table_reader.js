import {escapeHtml as esc} from './html.js';

export function resultTable(columns,rows) {
  const cell=value=>value===null||value===undefined?'<span class="analysis-null">空值</span>':esc(value);
  return `<table><thead><tr>${columns.map(c=>`<th scope="col">${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${columns.map(c=>`<td>${cell(row[c])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

/** Read the saved query data, never execute another query just to enlarge its table. */
export function createTableTools(dialog,{getQuery,onNotice=()=>{}}) {
  const body=dialog.querySelector('[data-table-reader-body]');
  const summary=dialog.querySelector('[data-table-reader-summary]');
  const download=dialog.querySelector('[data-table-reader-download]');
  let opener=null;
  dialog.addEventListener('close',()=>{
    body.replaceChildren();summary.replaceChildren();download.removeAttribute('href');
    if(opener?.isConnected)opener.focus();
    opener=null;
  });
  dialog.addEventListener('cancel',event=>{event.preventDefault();dialog.close();});
  return {
    handleClick(button) {
      if(button.hasAttribute('data-table-reader-close')){dialog.close();return true;}
      if(!button.hasAttribute('data-table-expand'))return false;
      const query=getQuery(button.dataset.tableExpand);
      if(!query?.ok||!query.rows?.length){onNotice('本条结果暂不可用，请返回对话后重试');return true;}
      opener=button;
      dialog.querySelector('#table-reader-title').textContent=`${query.label||'分析结果'} · ${query.title||'结果表格'}`;
      summary.innerHTML=`<p>本次已保留 ${query.rows.length} 行 · ${(query.columns||[]).length} 列${query.truncated?' · 结果已截断，仅显示已保留部分':''}</p>
        ${query.created_at?`<p>查询时间：${esc(query.created_at)}</p>`:''}
        ${(query.warnings||[]).map(w=>`<p>${esc(w)}</p>`).join('')}`;
      body.innerHTML=resultTable(query.columns||[],query.rows);
      body.scrollTop=0;body.scrollLeft=0;
      download.href=`/api/analysis/${encodeURIComponent(query.id)}/csv`;
      download.textContent=`下载本次结果 CSV${query.truncated?'（已截断）':''}`;
      if(!dialog.open)dialog.showModal();
      return true;
    },
  };
}
