import {language, serviceText, tr} from './i18n.js';
import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';

const parameterPattern=()=>/\{\{\s*([\p{L}\p{N}_][\p{L}\p{N}_ -]{0,39}?)\s*\}\}/gu;
// Only executable DAX slots declare parameters. Skip strings, identifiers and comments.
const daxReferences=/\/\/[^\n]*|--[^\n]*|\/\*[\s\S]*?(?:\*\/|$)|"(?:[^"]|"")*(?:"|$)|'(?:[^']|'')*(?:'|$)|\[(?:[^\]]|\]\])*(?:\]|$)|\{\{\s*([\p{L}\p{N}_][\p{L}\p{N}_ -]{0,39}?)(?:\.(?:year|month|day))?\s*\}\}/giu;
const parameterLimit=12;
const parameterValue=(values,name)=>typeof values?.[name]==='string'?values[name]:'';
function templateValues(names,defaults,lastValues){
  return Object.fromEntries(names.map(name=>[name,parameterValue(defaults,name).trim()||parameterValue(lastValues,name).trim()]));
}
export function templateParameters(prompt,queries=[]) {
  const names=new Set([...prompt.matchAll(parameterPattern())].map(match=>match[1].trim()));
  for(const query of queries){
    if(!query.dax.includes('{{'))continue;
    for(const match of query.dax.matchAll(daxReferences)){
      if(match[1])names.add(match[1].trim());
    }
  }
  const rest=prompt.replace(parameterPattern(),'');
  if(rest.includes('{{')||rest.includes('}}')||names.size>parameterLimit)throw new Error(tr('参数请使用 {{月份}} 这样的名称，最多 12 项，每个名称最多 40 字'));
  return [...names];
}
export function templatePreview(prompt,values) {
  templateParameters(prompt);
  return prompt.replace(parameterPattern(),(match,name)=>parameterValue(values,name.trim()).trim()||match);
}
export function saveTemplateButton(message) {
  return message.id&&message.role==='assistant'&&message.status==='complete'&&message.analysis?.some(query=>query.ok)?
    `<div class="template-message-action"><button class="secondary-button" data-save-analysis-template="${esc(message.id)}">${icon('plus')}${tr('保存为分析模板')}</button></div>`:'';
}

export async function mountAnalysisTemplates(root,{api,getModel,getService,onStarted,onOpen,seed=null}) {
  const $=selector=>root.querySelector(selector);
  let rows=[],screen=seed?'edit':'list',selected=seed,busy=false,disposed=false,pendingRun=null;
  let draftDefaults=new Map(),sourceValues=new Map(),recognition=null;
  const bindingChoices=new Map(),bindingIssues=new Map();
  const statuses={running:tr('运行中'),complete:tr('已完成'),error:tr('未完成'),cancelled:tr('已中止'),clarification:tr('待确认口径')};
  const request=(path,method,body)=>api(path,{method,body:JSON.stringify(body)});
  function feedback(message,error=false) {
    const notice=$('[data-template-notice]');
    if(notice){notice.textContent=message;notice.classList.toggle('error',error);notice.hidden=!message;}
  }
  function setBusy(value) {
    busy=value;const fields=$('fieldset');if(fields)fields.disabled=value;
  }
  async function action(work) {
    if(busy||disposed)return;
    setBusy(true);feedback('');
    try{await work();}catch(error){if(!disposed)feedback(error.message,true);}
    finally{if(!disposed)setBusy(false);}
  }
  function runParameterField(name) {
    const value=selected.run_values[name];
    let hint='';
    if(!value)hint=tr('首次需填写');
    else if(!parameterValue(selected.defaults,name).trim())hint=tr('已带入上次取值');
    return `<label class="form-field"><span class="template-field-label">${esc(name)}${hint?`<small>${hint}</small>`:''}</span>
      <input data-template-value="${esc(name)}" value="${esc(value)}" maxlength="500" ${value?'':'required'} placeholder="${esc(value||tr('填写本次取值'))}"></label>`;
  }
  function parameterFeedback(message,error=false) {
    const notice=$('[data-template-parameter-notice]');
    notice.textContent=message;notice.hidden=!message;notice.classList.toggle('error',error);
  }
  function editorPreview() {
    const prompt=$('[name="prompt"]').value;
    try{
      const values=templateValues(templateParameters(prompt),Object.fromEntries(draftDefaults),selected.last_values);
      $('[data-template-editor-preview]').textContent=templatePreview(prompt,values)||tr('填写分析任务后，可在这里核对替换结果。');
    }
    catch{$('[data-template-editor-preview]').textContent=prompt;}
  }
  function bindingField(name,issue) {
    if(!issue)return '';
    const options=Object.entries(issue.options||{});
    if(!options.length)return `<small class="template-binding-help">${tr('查询中找不到对应取值。')} <button type="button" class="text-link" data-edit-binding>${tr('查看查询代码')}</button></small>`;
    const current=bindingChoices.get(name);
    return `<label class="template-binding-help">${issue.conflict?tr('此位置已被其他参数选用，请选择不同的字段。'):tr('对应的筛选字段')}
      <select data-binding-choice="${esc(name)}" aria-label="${esc(tr('{0}的筛选字段',name))}" required><option value="">${tr('请选择字段')}</option>${options.map(([key,label])=>`<option value="${esc(key)}" ${current===key?'selected':''}>${esc(label)}</option>`).join('')}</select></label>`;
  }
  function editRow(item,index) {
    const {name}=item,suggested=index!==null,active=!suggested||item.checked;
    const last=parameterValue(selected.last_values,name).trim();
    const label=suggested?`<label><input type="checkbox" data-template-candidate="${index}" ${active?'checked':''}>${esc(name)}</label>`:esc(name);
    const issue=bindingIssues.get(name)||(item.options?{options:item.options}:null);
    return `<tr data-parameter-row="${esc(name)}"><th scope="row">${label}</th><td>
      <input data-template-default="${esc(name)}" aria-label="${esc(tr('{0}的默认值',name))}" value="${esc(draftDefaults.get(name)??'')}"
        maxlength="500" placeholder="${esc(last?tr('留空沿用上次的 {0}',last):tr('可在首次运行时填写'))}" ${active?'':'disabled'}>
      ${active?bindingField(name,issue):''}</td></tr>`;
  }
  function renderParameters() {
    const input=$('[name="prompt"]'),container=$('[data-template-defaults]');
    try{
      const queries=editorQueries(),names=templateParameters(input.value,queries);
      const declared=recognition?templateParameters(recognition.prompt,queries):names;
      const candidates=recognition?.candidates||[];
      const signature=JSON.stringify([declared,candidates.map(({name,checked,options})=>[name,checked,options]),[...bindingIssues]]);
      if(container.dataset.parameters!==signature){
        container.innerHTML=declared.map(name=>editRow({name},null)).join('')+candidates.map(editRow).join('');
        // Also set the properties for DOM implementations that do not reflect checked.
        for(const box of root.querySelectorAll('[data-template-candidate]'))box.checked=candidates[Number(box.dataset.templateCandidate)].checked;
        container.dataset.parameters=signature;
      }
      const hasRows=declared.length+candidates.length>0;
      $('[data-parameter-table]').hidden=!hasRows;
      const help=$('#template-parameter-help');
      help.hidden=!hasRows;
      help.textContent=candidates.length?tr('勾选重跑时想改的项，右侧可调整默认值。'):tr('保存后，一键重跑会使用这里的默认值。');
      $('[data-template-parameter-empty]').hidden=hasRows;
      $('[data-template-parameter-count]').textContent=names.length?tr('{0} 个可填写参数',names.length):'';
      $('[data-template-recognize]').hidden=!!recognition||names.length>=parameterLimit;
      input.setCustomValidity?.('');input.removeAttribute('aria-invalid');
    }catch(error){
      input.setCustomValidity?.(error.message);input.setAttribute('aria-invalid','true');
      parameterFeedback(error.message,true);
    }
    editorPreview();
  }
  function clearRecognition() {
    recognition=null;bindingIssues.clear();bindingChoices.clear();parameterFeedback('');
  }
  function applySelection() {
    const chosen=recognition.candidates.filter(item=>item.checked);
    const edits=[...recognition.existing,...chosen].flatMap(item=>item.spans.map(([start,end])=>({start,end,name:item.name}))).sort((a,b)=>a.start-b.start);
    // Recognition offsets are Unicode code points, including emoji as one character.
    const chars=Array.from(recognition.prompt),parts=[];
    let cursor=0;
    for(const {start,end,name} of edits){parts.push(chars.slice(cursor,start).join(''),`{{${name}}}`);cursor=end;}
    const prompt=parts.join('')+chars.slice(cursor).join('');
    if(chosen.length>recognition.room)throw new Error(tr('最多还能添加 {0} 项参数，请调整勾选。',recognition.room));
    if(prompt.length>12000)throw new Error(tr('批量添加后任务超过 12000 字，请减少勾选或缩短任务。'));
    $('[name="prompt"]').value=prompt;
    bindingIssues.clear();parameterFeedback('');renderParameters();
  }
  async function recognize() {
    const prompt=$('[name="prompt"]').value,queries=editorQueries().filter(q=>q.title&&q.dax.trim());
    const declared=new Set(templateParameters(prompt,queries)),room=parameterLimit-declared.size;
    if(!prompt.trim())throw new Error(tr('请先填写分析任务。'));
    if(!room)return;
    const result=await request('/analysis-templates/parameters/recognize','POST',
      {prompt,model_id:selected.model_id,source_message_id:selected.source_message_id||null,...(queries.length?{queries}:{})});
    if(disposed||screen!=='edit'||$('[name="prompt"]').value!==prompt)return;
    // Offer values that can affect these queries. Measures and function names stay in the task.
    const existing=result.candidates.filter(item=>declared.has(item.name));
    for(const item of existing){
      if(!draftDefaults.has(item.name))draftDefaults.set(item.name,item.value);
      sourceValues.set(item.name,item.value);
    }
    const candidates=result.candidates.filter(item=>!declared.has(item.name)&&(item.bindable!==false||Object.keys(item.options||{}).length));
    let remaining=room;
    recognition={prompt,room,existing,candidates:candidates.map(item=>{
      if(!draftDefaults.has(item.name)||sourceValues.get(item.name)!==item.value)draftDefaults.set(item.name,item.value);
      sourceValues.set(item.name,item.value);
      const checked=item.bindable!==false&&remaining>0;
      if(checked)remaining--;
      return {...item,checked};
    })};
    try{applySelection();}catch(error){
      recognition.candidates.forEach(item=>item.checked=false);renderParameters();parameterFeedback(error.message,true);
    }
  }
  function insertParameter(rawName) {
    const name=rawName.trim(),input=$('[name="prompt"]'),placeholder=`{{${name}}}`;
    try {
      if(!/^[\p{L}\p{N}_][\p{L}\p{N}_ -]{0,39}$/u.test(name))throw new Error(tr('请填写参数名（最多 40 字），可用中文、字母、数字、下划线、空格或短横线，不需要输入花括号。'));
      const start=input.selectionStart??input.value.length,end=input.selectionEnd??start;
      const original=input.value.slice(start,end);
      if(original.includes('{{')||original.includes('}}'))throw new Error(tr('请选中具体的年份、月份或工厂等文字，不要选中已有参数标记。'));
      if(original.trim().length>500)throw new Error(tr('选中文字超过默认值的 500 字上限，请缩小选择范围。'));
      const candidate=input.value.slice(0,start)+placeholder+input.value.slice(end);
      if(candidate.length>12000)throw new Error(tr('任务已达到 12000 字上限，请缩短后再添加参数。'));
      templateParameters(candidate,editorQueries());
      const fillDefault=original.trim()&&!draftDefaults.get(name)?.trim();
      if(fillDefault)draftDefaults.set(name,original.trim());
      if(original.trim())sourceValues.set(name,original.trim());
      // Keep the selection and reading position in the task editor after using the toolbar.
      const scrollTop=input.scrollTop;
      input.value=candidate;clearRecognition();
      renderParameters();
      if(fillDefault) {
        const field=[...root.querySelectorAll('[data-template-default]')].find(field=>field.dataset.templateDefault===name);
        field.value=original.trim();draftDefaults.set(name,field.value);editorPreview();
      }
      input.focus();input.setSelectionRange?.(start+placeholder.length,start+placeholder.length);input.scrollTop=scrollTop;
      $('[data-template-parameter-name]').value='';
      parameterFeedback(tr('已添加“{0}”。',name));
    }catch(error){parameterFeedback(error.message,true);}
  }
  function effectiveValues() {
    const values={...selected.run_values};
    for(const input of root.querySelectorAll('[data-template-value]')){
      const name=input.dataset.templateValue;
      values[name]=input.value.trim()||selected.run_values[name];
    }
    return values;
  }
  function queryEditor(query) {
    return `<div class="template-query" data-template-query><label class="form-field">${tr('查询名称')}<input data-query-title required maxlength="160" value="${esc(query.title)}"></label>
      <label class="form-field">DAX<textarea data-query-dax required rows="5" maxlength="16000" spellcheck="false"></textarea></label>
      <button type="button" class="text-link" data-remove-query>${tr('移除此查询')}</button></div>`;
  }
  function showEditorQueries(queries){
    $('[data-query-list]').innerHTML=queries.map(queryEditor).join('');
    [...root.querySelectorAll('[data-query-dax]')].forEach((input,i)=>{input.value=queries[i].dax;});
  }
  function editorQueries() {
    return [...root.querySelectorAll('[data-template-query]')].map(row=>({
      title:row.querySelector('[data-query-title]').value.trim(),dax:row.querySelector('[data-query-dax]').value,
    }));
  }
  function preview() {
    const effective=effectiveValues(),missing=selected.parameters.filter(name=>!effective[name]);
    $('[data-template-preview]').textContent=templatePreview(selected.prompt,effective);
    const status=$('[data-template-run-status]');
    status.hidden=selected.parameters.length>0&&!missing.length;
    status.textContent=missing.length?tr('待填写：{0}',missing.join(language==='en'?', ':'、')):
      selected.parameters.length?'':tr('固定任务，无需填写参数。核对下方任务后可直接运行。');
    for(const input of root.querySelectorAll('[data-template-value]'))input.setCustomValidity?.(effective[input.dataset.templateValue]?'':tr('请填写该参数的首次取值'));
  }
  function render() {
    if(disposed)return;
    let content='';
    if(screen==='list') {
      content=`<div class="template-toolbar"><span>${tr('保存常用分析，按原任务或更换参数重跑')}</span><button class="primary-button" data-template-new>${tr('新建模板')}</button></div>
        <label class="form-field">${tr('搜索模板')}<input type="search" data-template-search placeholder="${tr('模板名称或报表名称')}"></label>
        <div class="template-list">${rows.map(row=>`<article class="template-card" data-template-card="${esc(row.id)}"><strong>${esc(row.name)}</strong><p>${tr('{0} · 版本 {1}', esc(row.model_name), row.revision)}</p><div class="template-parameter-summary"><span class="template-kind">${row.parameters.length?tr('{0} 个可填写参数', row.parameters.length):tr('固定任务')}</span>${row.parameters.length?row.parameters.map(name=>`<span class="template-parameter-chip">${esc(name)}</span>`).join(''):`<span>${tr('无需填写参数，按原任务重新取数')}</span>`}</div><div class="template-actions"><button class="primary-button" data-template-run="${esc(row.id)}">${icon('refresh')}${tr('一键重跑')}</button><button class="secondary-button" data-template-edit="${esc(row.id)}">${tr('编辑')}</button><button class="secondary-button" data-template-history="${esc(row.id)}">${tr('运行记录')}</button><button class="text-link" data-template-delete="${esc(row.id)}">${tr('删除')}</button></div><div data-template-delete-panel hidden><p>${tr('删除此模板？已有分析对话和结果会保留。')}</p><button class="secondary-button" data-template-delete-cancel>${tr('取消')}</button><button class="secondary-button" data-template-delete-confirm="${esc(row.id)}">${tr('确认删除模板')}</button></div></article>`).join('')}</div>
        <p class="notice" data-template-empty ${rows.length?'hidden':''}>${tr('还没有模板。可新建，或在已完成的分析回复下点击“保存为分析模板”。')}</p>`;
    } else if(screen==='edit') {
      draftDefaults=new Map(Object.entries(selected.defaults||{}));sourceValues=new Map(draftDefaults);recognition=null;
      bindingChoices.clear();bindingIssues.clear();
      content=`<button type="button" class="text-link" data-template-back>${tr('← 返回模板列表')}</button>
        <form data-template-editor><label class="form-field">${tr('模板名称')}<input name="name" required maxlength="120" value="${esc(selected.name||'')}"></label>
        <p class="field-help">${tr('所属报表：{0}', esc(selected.model_name||getModel()?.name||tr('未连接')))}</p>
        <label class="form-field">${tr('分析任务与口径')}<textarea name="prompt" required maxlength="12000" rows="6" aria-describedby="template-parameter-help template-parameter-notice" placeholder="${tr('例如：分析 0810 工厂在 2026 年各月的 MPV，找出表现最差的月份。')}">${esc(selected.prompt||'')}</textarea></label>
        <section class="template-parameter-builder" aria-labelledby="template-parameters-heading">
          <div class="template-section-heading"><h3 id="template-parameters-heading">${tr('运行参数')} <small data-template-parameter-count></small></h3>
            <button type="button" class="text-link" data-template-recognize>${tr('从任务识别')}</button></div>
          <p class="field-help" id="template-parameter-help"></p>
          <table class="template-parameter-table" data-parameter-table hidden>
            <thead><tr><th scope="col">${tr('参数')}</th><th scope="col">${tr('默认值（选填）')}</th></tr></thead><tbody data-template-defaults></tbody></table>
          <p class="field-help" data-template-parameter-empty>${tr('当前为固定任务，可直接保存。')}</p>
          <details class="template-manual"><summary>${tr('自定义参数')}</summary><p class="field-help">${tr('选中任务里要替换的文字，再给它起一个参数名。')}</p>
            <div class="template-parameter-shortcuts">${[tr('年份'),tr('月份'),tr('工厂'),tr('部门')].map(name=>`<button type="button" class="secondary-button" data-template-insert-parameter="${name}" title="${tr('将选中文字设为{0}参数',name)}">${name}</button>`).join('')}</div>
            <div class="template-parameter-custom"><label class="form-field">${tr('自定义参数名')}<input data-template-parameter-name maxlength="40" placeholder="${tr('例如：物料')}"></label>
              <button type="button" class="secondary-button" data-template-add-parameter>${tr('添加参数')}</button></div></details>
          <p class="template-parameter-notice" id="template-parameter-notice" data-template-parameter-notice role="status" hidden></p>
          <details class="template-editor-preview"><summary>${tr('任务预览')}</summary><pre class="template-preview" data-template-editor-preview></pre></details>
        </section>
        <details class="template-query-editor" data-template-query-editor><summary>${tr('查询代码（高级）')}</summary>
          <p class="field-help">${tr('保存时自动将参数关联到查询。手动编写 DAX 时可用 {{参数名}} 引用取值，数值可用 VALUE({{参数名}})。')}</p>
          <div data-query-list></div>
          <button type="button" class="secondary-button" data-add-query>${tr('添加 DAX 查询')}</button></details>
        <div class="modal-actions"><button type="button" class="secondary-button" data-template-back>${tr('取消')}</button><button type="submit" class="primary-button">${tr('保存模板')}</button></div></form>`;
    } else if(screen==='run') {
      content=`<button type="button" class="text-link" data-template-back>${tr('← 返回模板列表')}</button><h3>${esc(selected.name)}</h3>
        <p class="notice">${tr('将在当前报表“{0}”新建分析。模板所属报表：{1}。', esc(getModel()?.name||tr('尚未连接')), esc(selected.model_name))}</p>
        <form data-template-run-form><div class="template-section-heading"><h3>${selected.parameters.length?tr('本次运行参数'):tr('核对固定任务')}</h3></div>
        <p class="template-run-status" data-template-run-status role="status"></p><div class="template-parameters">${selected.parameters.map(runParameterField).join('')}</div>
        <details open><summary>${tr('本次分析任务')}</summary><pre class="template-preview" data-template-preview></pre></details>
        ${selected.queries?.length?`<details><summary>${tr('固定 DAX 查询')} (${selected.queries.length})</summary>${selected.queries.map(q=>`<h4>${esc(q.title)}</h4><pre class="template-preview">${esc(q.dax)}</pre>`).join('')}</details>
        <p class="field-help">${tr('只替换本次参数，按保存的查询从 Desktop 重新取数，再根据本次结果生成分析。')}</p>`:`<p class="field-help">${tr('首次运行会准备并保存固定查询，以后只替换参数。')}</p>`}
        <div class="modal-actions"><button type="submit" class="primary-button">${icon('refresh')}${tr('开始运行')}</button></div></form>`;
    } else {
      content=`<button type="button" class="text-link" data-template-back>${tr('← 返回模板列表')}</button><h3>${tr('{0} · 运行记录', esc(selected.name))}</h3><div data-template-runs></div>`;
    }
    root.innerHTML=`<fieldset class="template-fields" ${busy?'disabled':''}>${content}</fieldset><p class="notice template-notice" role="status" data-template-notice hidden></p>`;
    if(screen==='edit'){
      $('[name="prompt"]').value=selected.prompt||'';
      showEditorQueries(selected.queries||[]);renderParameters();
    }
    if(screen==='run')preview();
  }
  async function list() {rows=await api('/analysis-templates');screen='list';selected=null;render();}
  async function history() {
    const runs=await api(`/analysis-templates/${selected.id}/runs`);
    if(disposed)return;
    $('[data-template-runs]').innerHTML=runs.length?runs.map(run=>`<article class="template-card"><strong>${esc(statuses[run.status]||run.status)}</strong><p>${tr('{0} · 模板版本 {1}', esc(new Date(run.created_at).toLocaleString(language)), run.revision)}</p><p>${Object.entries(run.values).map(([name,value])=>`${esc(name)}：${esc(value)}`).join(' · ')||tr('无参数')}</p>${run.error?`<p>${esc(serviceText(run.error))}</p>`:''}<button class="secondary-button" data-template-open-run="${esc(run.conversation_id)}">${tr('查看分析')}</button></article>`).join(''):`<p class="notice">${tr('还没有运行记录。')}</p>`;
  }
  root.onclick=event=>{
    const button=event.target.closest('button');if(!button||busy||disposed)return;
    if(button.hasAttribute('data-template-back'))void action(list);
    if(button.hasAttribute('data-template-recognize'))void action(recognize);
    if(button.hasAttribute('data-edit-binding')){
      $('[data-template-query-editor]').open=true;$('[data-query-dax]')?.focus();
    }
    if(button.dataset.templateInsertParameter)insertParameter(button.dataset.templateInsertParameter);
    if(button.hasAttribute('data-template-add-parameter'))insertParameter($('[data-template-parameter-name]').value);
    if(button.hasAttribute('data-template-new')) {
      const model=getModel();if(model?.source!=='desktop'){feedback(tr('请先连接真实 Desktop 报表'),true);return;}
      selected={model_id:model.id,model_name:model.name,name:'',prompt:'',defaults:{}};screen='edit';render();
    }
    if(button.dataset.templateEdit)void action(async()=>{
      selected=await api(`/analysis-templates/${button.dataset.templateEdit}`);screen='edit';render();
    });
    if(button.hasAttribute('data-add-query')){
      const count=root.querySelectorAll('[data-template-query]').length;
      if(count>=12){feedback(tr('每个模板最多保存 12 条查询'),true);return;}
      $('[data-query-list]').insertAdjacentHTML('beforeend',queryEditor({title:'Q'+(count+1),dax:''}));
    }
    if(button.hasAttribute('data-remove-query')){
      button.closest('[data-template-query]').remove();clearRecognition();renderParameters();
    }
    if(button.dataset.templateRun)void action(async()=>{
      selected=await api(`/analysis-templates/${button.dataset.templateRun}`);
      selected.run_values??=templateValues(selected.parameters,selected.defaults,selected.last_values);
      screen='run';pendingRun=null;render();
    });
    if(button.dataset.templateHistory){selected=rows.find(row=>row.id===button.dataset.templateHistory);screen='history';render();void action(history);}
    if(button.dataset.templateDelete)button.closest('article').querySelector('[data-template-delete-panel]').hidden=false;
    if(button.hasAttribute('data-template-delete-cancel'))button.closest('[data-template-delete-panel]').hidden=true;
    if(button.dataset.templateDeleteConfirm)void action(async()=>{
      const row=rows.find(row=>row.id===button.dataset.templateDeleteConfirm);
      await api(`/analysis-templates/${row.id}?revision=${row.revision}`,{method:'DELETE'});await list();feedback(tr('模板已删除，已有分析结果保留'));
    });
    if(button.dataset.templateOpenRun)void action(()=>onOpen(button.dataset.templateOpenRun));
  };
  root.oninput=event=>{
    if(event.target.name==='prompt'||event.target.hasAttribute('data-query-dax')){
      clearRecognition();renderParameters();
    }
    if(event.target.hasAttribute('data-template-default')){draftDefaults.set(event.target.dataset.templateDefault,event.target.value);editorPreview();}
    if(event.target.hasAttribute('data-template-value'))preview();
    if(event.target.hasAttribute('data-template-search')) {
      const needle=event.target.value.toLowerCase();let count=0;
      for(const card of root.querySelectorAll('[data-template-card]')){
        const row=rows.find(row=>row.id===card.dataset.templateCard);
        card.hidden=!(row.name+' '+row.model_name).toLowerCase().includes(needle);if(!card.hidden)count++;
      }
      const empty=$('[data-template-empty]');empty.hidden=!!count;empty.textContent=rows.length?tr('没有匹配的模板'):tr('还没有模板。可新建，或从已完成的分析保存。');
    }
  };
  root.onchange=event=>{
    const field=event.target;
    if(recognition&&field.hasAttribute('data-template-candidate')){
      const index=Number(field.dataset.templateCandidate),item=recognition.candidates[index];
      item.checked=field.checked;
      try{
        applySelection();
        root.querySelectorAll('[data-template-candidate]')[index]?.focus();
      }catch(error){item.checked=!item.checked;field.checked=item.checked;parameterFeedback(error.message,true);}
    }
    if(field.hasAttribute('data-binding-choice'))bindingChoices.set(field.dataset.bindingChoice,field.value);
    if(field.hasAttribute('data-template-value')&&!field.value.trim()){
      field.value=selected.run_values[field.dataset.templateValue];preview();
    }
  };
  function revealInvalidQuery(event){
    const panel=event.target.closest('[data-template-query-editor]');
    if(panel)panel.open=true;
  }
  root.addEventListener('invalid',revealInvalidQuery,true);
  root.onkeydown=event=>{
    if(event.target.hasAttribute('data-template-parameter-name')&&event.key==='Enter') {
      event.preventDefault();
      if(!busy&&!disposed&&!event.isComposing&&event.keyCode!==229)insertParameter(event.target.value);
    }
  };
  root.onsubmit=event=>{
    event.preventDefault();if(busy||disposed||!event.target.reportValidity())return;
    if(event.target.hasAttribute('data-template-editor'))void action(async()=>{
      const prompt=$('[name="prompt"]').value,queries=editorQueries(),names=templateParameters(prompt,queries);
      const defaults=Object.fromEntries(names.map(name=>[name,draftDefaults.get(name)||'']));
      const body={name:$('[name="name"]').value,prompt,defaults};
      if(queries.length||selected.queries?.length)body.queries=queries;
      if(queries.length&&names.length){
        const samples=Object.fromEntries(names.map(name=>[name,sourceValues.get(name)||defaults[name]]));
        const review=await request('/analysis-templates/queries/preview','POST',{prompt,queries,samples,choices:Object.fromEntries(bindingChoices)});
        if(disposed)return;
        if(review.unresolved?.length){
          bindingIssues.clear();
          for(const issue of review.unresolved)bindingIssues.set(issue.name,issue);
          renderParameters();parameterFeedback(tr('请确认标出的参数后再保存。'),true);
          const row=[...root.querySelectorAll('[data-parameter-row]')].find(r=>bindingIssues.has(r.dataset.parameterRow));
          row?.scrollIntoView?.({block:'center'});return;
        }
        body.queries=review.queries;body.samples=samples;
      }
      if(selected.id)body.revision=selected.revision;
      else Object.assign(body,{model_id:selected.model_id,source_message_id:selected.source_message_id||null});
      await request(selected.id?`/analysis-templates/${selected.id}`:'/analysis-templates',selected.id?'PUT':'POST',body);
      await list();feedback(names.length?tr('模板已保存，可使用新参数重跑'):tr('模板已保存，可按固定任务重跑'));
    });
    if(event.target.hasAttribute('data-template-run-form'))void action(async()=>{
      const model=getModel(),service=getService();
      if(model?.source!=='desktop')throw new Error(tr('请先连接模板所属的 Desktop 报表'));
      if(!service.ready)throw new Error(tr('请先配置并选择可用的模型服务'));
      const effective=effectiveValues(),missing=[...root.querySelectorAll('[data-template-value]')].filter(input=>!effective[input.dataset.templateValue]);
      if(missing.length){missing[0].focus();throw new Error(tr('请填写参数：')+missing.map(input=>input.dataset.templateValue).join('、'));}
      // Pin the displayed task even if a different window runs this template before submission.
      const body={model_id:model.id,language,revision:selected.revision,values:effective,llm_profile_id:service.active_profile_id||null};
      const key=JSON.stringify({id:selected.id,...body});
      if(!pendingRun||pendingRun.key!==key)pendingRun={key,id:globalThis.crypto.randomUUID().replaceAll('-','')};
      const result=await request(`/analysis-templates/${selected.id}/runs`,'POST',{...body,request_id:pendingRun.id});
      await onStarted(result,model,!disposed);pendingRun=null;
      if(!disposed){
        selected.last_values=effective;
        selected.run_values=templateValues(selected.parameters,selected.defaults,effective);
        render();
      }
    });
  };
  if(seed){
    render();
    if(seed.source_message_id&&seed.prompt?.trim())await action(recognize);
  }else await list();
  return {dispose(){disposed=true;root.removeEventListener('invalid',revealInvalidQuery,true);root.onclick=null;root.oninput=null;root.onchange=null;root.onkeydown=null;root.onsubmit=null;}};
}
