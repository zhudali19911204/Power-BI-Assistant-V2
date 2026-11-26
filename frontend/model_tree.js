import {tr} from './i18n.js';
import {escapeHtml as esc} from './html.js';
import {icon} from './icons.js';

function entryKey(kind,table,name) {
  return JSON.stringify([kind,table,name]);
}

function matchesName(technical,definition,needle) {
  return [technical,definition?.business_name,...(definition?.aliases||[])]
    .some(value=>value?.toLowerCase().includes(needle));
}

function resultLabel(name,definition,reference,needle) {
  if(!needle)return esc(name);
  const alias=definition?.aliases?.find(value=>value.toLowerCase().includes(needle));
  const label=definition?.business_name||alias||name;
  return `<span class="field-result-label"><strong>${esc(label)}</strong><small>${esc(reference)}</small></span>`;
}

export function modelTree(context,query,searchNames=[]) {
  const needle=query.trim().toLowerCase();
  const definitions=new Map();
  if(needle)for(const entry of searchNames)definitions.set(entryKey(entry.kind,entry.table,entry.name),entry);
  const measures=new Map();
  for(const measure of context.measures){
    if(!measures.has(measure.table))measures.set(measure.table,[]);
    measures.get(measure.table).push(measure);
  }

  const groups=[];
  for(const [table,columns] of Object.entries(context.tables)){
    const tableRef="'"+table.replaceAll("'","''")+"'";
    const lookup=(kind,name)=>definitions.size?definitions.get(entryKey(kind,table,name)):undefined;
    const tableName=lookup('table',table);
    const tableMatch=!!needle&&matchesName(table,tableName,needle);
    const rows=[];
    if(tableMatch){
      rows.push(`<button type="button" class="field-button" data-ref="${esc(tableRef)}">${icon('table')}${resultLabel(table,tableName,tableRef,needle)}</button>`);
    }
    for(const column of columns){
      const definition=lookup('column',column.name);
      if(needle&&!tableMatch&&!matchesName(column.name,definition,needle))continue;
      const reference=tableRef+'['+column.name.replaceAll(']',']]')+']';
      const type=/int|double|decimal/.test(column.dtype)?'#':column.dtype==='datetime'?'◷':'Aa';
      rows.push(`<div class="field-row"><button type="button" class="field-button" data-ref="${esc(reference)}" title="${esc(column.dtype)}"><span class="field-type">${type}</span>${resultLabel(column.name,definition,reference,needle)}</button><button type="button" class="icon-button field-preview-button" data-preview-table="${esc(table)}" data-preview-column="${esc(column.name)}" title="${tr('查看字段取值')}" aria-label="${tr('查看 {0}[{1}] 的取值', esc(table), esc(column.name))}">${icon('eye')}</button></div>`);
    }
    for(const measure of measures.get(table)||[]){
      const definition=lookup('measure',measure.name);
      if(needle&&!tableMatch&&!matchesName(measure.name,definition,needle))continue;
      const reference='['+measure.name.replaceAll(']',']]')+']';
      rows.push(`<button type="button" class="field-button" data-ref="${esc(reference)}">${icon('formula')}${resultLabel(measure.name,definition,tableRef+reference,needle)}</button>`);
    }
    if(needle){
      if(rows.length)groups.push(`<div class="field-search-results">${rows.join('')}</div>`);
    }else{
      groups.push(`<details class="table-group"><summary>${icon('chevron-right')}${icon('table')}${esc(table)}<small>${columns.length}</small></summary>${rows.join('')}</details>`);
    }
  }
  return groups.join('')||`<p class="history-empty muted">${tr('没有匹配的字段')}</p>`;
}
