import {icon} from './icons.js';
import {escapeHtml as esc} from './markdown.js';

const FILTER_LIMIT=10;
const $=(selector,root)=>root.querySelector(selector);
const rows=form=>[...form.querySelectorAll('[data-calibration-filter]')];

function numberValue(input,label) {
  const text=input.value.trim();
  if(!text||!Number.isFinite(Number(text)))throw new Error(`请填写有效的${label}`);
  return Number(text);
}

function valueKind(value) {
  return value===null?'null':typeof value==='number'?'number':typeof value==='boolean'?'boolean':'text';
}

function filterMarkup(context,filter={}) {
  const kind=valueKind(filter.value);
  const columns=context.tables[filter.table]||[];
  return `<div class="calibration-filter" data-calibration-filter>
    <div class="filter-heading"><span data-filter-label></span><button class="icon-button" type="button" data-remove-filter aria-label="删除筛选条件">${icon('trash')}</button></div>
    <div class="form-row">
      <label class="form-field">筛选表<select data-filter-table required><option value="">选择表</option>${Object.keys(context.tables).map(table=>`<option value="${esc(table)}" ${table===filter.table?'selected':''}>${esc(table)}</option>`).join('')}</select></label>
      <label class="form-field">筛选字段<select data-filter-column required><option value="">选择字段</option>${columns.map(column=>`<option value="${esc(column.name)}" ${column.name===filter.column?'selected':''}>${esc(column.name)}</option>`).join('')}</select></label>
    </div>
    <div class="form-row">
      <label class="form-field">筛选值<input data-filter-value value="${esc(filter.value??'')}" placeholder="例如：华东"></label>
      <label class="form-field">值类型<select data-filter-kind><option value="text" ${kind==='text'?'selected':''}>文本 / 日期</option><option value="number" ${kind==='number'?'selected':''}>数值</option><option value="boolean" ${kind==='boolean'?'selected':''}>布尔值 true/false</option><option value="null" ${kind==='null'?'selected':''}>空值</option></select></label>
    </div>
  </div>`;
}

function updateValueInput(row) {
  const kind=$('[data-filter-kind]',row).value;
  const input=$('[data-filter-value]',row);
  input.disabled=kind==='null';
  input.placeholder=kind==='boolean'?'true 或 false':kind==='number'?'例如：2026':kind==='null'?'空值 BLANK()':'例如：华东，或 2026-01-01';
}

export function mountCalibrationEditor(form,context,initial={},onLimit=()=>{}) {
  form.innerHTML=`<div class="calibration-section-heading"><strong>筛选条件</strong><span>所有条件同时满足（且）</span></div>
    <div data-filter-list></div>
    <p class="calibration-empty" data-no-filters>未添加筛选条件时，校准整个模型的结果。</p>
    <button type="button" class="secondary-button" data-add-filter>${icon('plus')}添加筛选条件</button>
    <div class="calibration-target">
      <strong>校准结果</strong><p class="field-help">以上条件共同对应一个已知正确结果。</p>
      <div class="form-row">
        <label class="form-field">已知正确结果<input type="number" step="any" required data-calibration-expected value="${esc(initial.expected??'')}"></label>
        <label class="form-field">允许的绝对误差<input type="number" min="0" step="any" required data-calibration-tolerance value="${esc(initial.tolerance??0.01)}"></label>
      </div>
    </div>
    <div class="modal-actions"><button type="button" class="secondary-button" data-clear-calibration>清除校准</button><button class="primary-button" type="submit">应用到后续生成</button></div>`;
  const list=$('[data-filter-list]',form);

  function renumber() {
    const filters=rows(form);
    filters.forEach((row,index)=>{$('[data-filter-label]',row).textContent=`条件 ${index+1}`;});
    $('[data-no-filters]',form).hidden=filters.length>0;
    $('[data-add-filter]',form).disabled=filters.length>=FILTER_LIMIT;
  }

  function addFilter(filter={}) {
    if(rows(form).length>=FILTER_LIMIT){onLimit(`最多添加 ${FILTER_LIMIT} 个筛选条件`);return;}
    list.insertAdjacentHTML('beforeend',filterMarkup(context,filter));
    updateValueInput(list.lastElementChild);renumber();
  }
  for(const filter of initial.filters||[])addFilter(filter);
  renumber();

  form.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button)return;
    if(button.hasAttribute('data-add-filter'))addFilter();
    if(button.hasAttribute('data-remove-filter')){button.closest('[data-calibration-filter]').remove();renumber();}
  });
  form.addEventListener('change',event=>{
    const row=event.target.closest('[data-calibration-filter]');if(!row)return;
    if(event.target.hasAttribute('data-filter-table')) {
      const columns=context.tables[event.target.value]||[];
      $('[data-filter-column]',row).innerHTML='<option value="">选择字段</option>'+columns.map(c=>`<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('');
    }
    if(event.target.hasAttribute('data-filter-kind'))updateValueInput(row);
  });

  return {
    read() {
      const filters=rows(form).map(row=>{
        const table=$('[data-filter-table]',row).value,column=$('[data-filter-column]',row).value;
        if(!table||!column||!context.tables[table]?.some(c=>c.name===column))throw new Error('请为每个条件选择有效的表和字段');
        const raw=$('[data-filter-value]',row).value,kind=$('[data-filter-kind]',row).value;
        let value=raw;
        if(kind==='number')value=numberValue($('[data-filter-value]',row),'数值筛选值');
        if(kind==='boolean') {
          if(!['true','false'].includes(raw.trim().toLowerCase()))throw new Error('布尔值请输入 true 或 false');
          value=raw.trim().toLowerCase()==='true';
        }
        if(kind==='null')value=null;
        return {table,column,value};
      });
      const expected=numberValue($('[data-calibration-expected]',form),'已知正确结果');
      const tolerance=numberValue($('[data-calibration-tolerance]',form),'绝对误差');
      if(tolerance<0)throw new Error('允许的绝对误差不能为负数');
      return {filters,expected,tolerance};
    },
  };
}
