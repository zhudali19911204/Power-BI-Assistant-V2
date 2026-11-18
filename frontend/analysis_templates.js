import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';

const parameterPattern=()=>/\{\{\s*([\p{L}\p{N}_][\p{L}\p{N}_ -]{0,39}?)\s*\}\}/gu;
const parameterLimit=12;
const parameterValue=(values,name)=>typeof values?.[name]==='string'?values[name]:'';
export function templateParameters(prompt) {
  const names=[...new Set([...prompt.matchAll(parameterPattern())].map(match=>match[1].trim()))];
  const rest=prompt.replace(parameterPattern(),'');
  if(rest.includes('{{')||rest.includes('}}')||names.length>parameterLimit)throw new Error('参数请使用 {{月份}} 这样的名称，最多 12 项，每个名称最多 40 字');
  return names;
}
export function templatePreview(prompt,values) {
  templateParameters(prompt);
  return prompt.replace(parameterPattern(),(match,name)=>parameterValue(values,name.trim()).trim()||match);
}
export function saveTemplateButton(message) {
  return message.id&&message.role==='assistant'&&message.status==='complete'&&message.analysis?.some(query=>query.ok)?
    `<div class="template-message-action"><button class="secondary-button" data-save-analysis-template="${esc(message.id)}">${icon('plus')}保存为分析模板</button></div>`:'';
}

export async function mountAnalysisTemplates(root,{api,getModel,getService,onStarted,onOpen,seed=null}) {
  const $=selector=>root.querySelector(selector);
  let rows=[],screen=seed?'edit':'list',selected=seed,busy=false,disposed=false,pendingRun=null;
  let draftDefaults=new Map(),recognition=null;
  const statuses={running:'运行中',complete:'已完成',error:'未完成',cancelled:'已中止',clarification:'待确认口径'};
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
  function inheritedValue(name) {
    return parameterValue(selected.last_values,name).trim()||parameterValue(selected.defaults,name).trim();
  }
  function parameterFields(names,defaults,kind) {
    return names.map(name=>{
      const editing=kind==='default',fallback=editing?'':inheritedValue(name);
      const hint=editing?'默认值 · 选填':parameterValue(selected.last_values,name).trim()?'留空沿用上次':fallback?'留空使用默认值':'首次需填写';
      const placeholder=editing?'可在首次运行时填写':fallback||'填写本次取值';
      return `<label class="form-field"><span class="template-field-label">${esc(name)}<small>${hint}</small></span><input data-template-${kind}="${esc(name)}" value="${esc(editing?parameterValue(defaults,name):'')}" maxlength="500" ${!editing&&!fallback?'required':''} placeholder="${esc(placeholder)}"></label>`;
    }).join('');
  }
  function values(kind) {
    return Object.fromEntries([...root.querySelectorAll(`[data-template-${kind}]`)].map(input=>[input.getAttribute(`data-template-${kind}`),input.value]));
  }
  function showParameterCount(stored,pending=0) {
    const total=stored+pending;
    $('[data-template-parameter-count]').textContent=total?`${total} 个可填写参数${pending?`（待应用 ${pending} 项）`:''}`:'固定任务';
    $('[data-template-parameter-empty]').hidden=!!total;
  }
  function renderParameters() {
    for(const [name,value] of Object.entries(values('default')))draftDefaults.set(name,value);
    const prompt=$('[name="prompt"]'),container=$('[data-template-defaults]');
    try{
      const names=templateParameters(prompt.value),signature=JSON.stringify(names);
      if(container.dataset.parameters!==signature) {
        container.innerHTML=parameterFields(names,Object.fromEntries(draftDefaults),'default');
        container.dataset.parameters=signature;
      }
      showParameterCount(names.length);
      prompt.setCustomValidity?.('');prompt.removeAttribute('aria-invalid');
      parameterFeedback('');
    }catch(error){
      prompt.setCustomValidity?.(error.message);prompt.setAttribute('aria-invalid','true');
      $('[data-template-parameter-count]').textContent='参数格式待完善';
      $('[data-template-parameter-empty]').hidden=true;
      parameterFeedback(error.message,true);
    }
    editorPreview();
  }
  function parameterFeedback(message,error=false) {
    const notice=$('[data-template-parameter-notice]');
    notice.textContent=message;notice.hidden=!message;notice.classList.toggle('error',error);
  }
  function editorPreview() {
    const prompt=$('[name="prompt"]').value;
    try{$('[data-template-editor-preview]').textContent=templatePreview(prompt,values('default'))||'填写分析任务后，可在这里核对替换结果。';}
    catch{$('[data-template-editor-preview]').textContent=prompt;}
  }
  function clearCandidates() {
    recognition=null;
    $('[data-template-candidates]').replaceChildren();
    $('[data-template-candidate-panel]').hidden=true;
  }
  function updateSelection() {
    const boxes=[...root.querySelectorAll('[data-template-candidate]')],count=boxes.filter(box=>box.checked).length;
    showParameterCount(parameterLimit-recognition.room,count);
    $('[data-template-selection]').textContent=`已选 ${count} 项${boxes.length>recognition.room?`，还可添加 ${recognition.room} 项参数`:''}`;
    $('[data-template-toggle-candidates]').textContent=count?'清空勾选':boxes.length>recognition.room?`勾选前 ${recognition.room} 项`:'勾选全部';
    $('[data-template-apply-candidates]').disabled=!count||count>recognition.room;
  }
  async function recognize() {
    const prompt=$('[name="prompt"]').value,room=parameterLimit-templateParameters(prompt).length;
    if(!prompt.trim())throw new Error('请先填写分析任务。');
    if(!room)throw new Error('模板已有 12 项参数，请先移除不需要的参数。');
    const result=await request('/analysis-templates/parameters/recognize','POST',
      {prompt,model_id:selected.model_id,source_message_id:selected.source_message_id||null});
    if(disposed||screen!=='edit'||$('[name="prompt"]').value!==prompt)return;
    clearCandidates();
    if(!result.candidates.length){showParameterCount(parameterLimit-room);parameterFeedback('没有识别出可替换的值，可直接保存或手动添加参数。');return;}
    recognition={prompt,room,candidates:result.candidates};
    $('[data-template-candidates]').innerHTML=result.candidates.map((item,i)=>
      `<label class="template-candidate"><input type="checkbox" data-template-candidate="${i}"><span><strong>${esc(item.name)}</strong><span>${esc(item.value)}</span></span>${item.spans.length>1?`<small>${item.spans.length} 处</small>`:''}</label>`).join('');
    for(const box of root.querySelectorAll('[data-template-candidate]'))box.checked=Number(box.dataset.templateCandidate)<room;
    $('[data-template-candidate-panel]').hidden=false;parameterFeedback('');updateSelection();
  }
  function applyCandidates() {
    if(!recognition)return;
    const input=$('[name="prompt"]');
    if(input.value!==recognition.prompt)throw new Error('分析任务已修改，请重新识别参数。');
    const chosen=[...root.querySelectorAll('[data-template-candidate]')].filter(box=>box.checked)
      .map(box=>recognition.candidates[Number(box.dataset.templateCandidate)]);
    if(!chosen.length)return;
    if(chosen.length>recognition.room)throw new Error(`最多还能添加 ${recognition.room} 项参数，请调整勾选。`);
    const edits=chosen.flatMap(item=>item.spans.map(([start,end])=>({start,end,name:item.name}))).sort((a,b)=>a.start-b.start);
    // Server offsets count Unicode code points, including emoji as one character.
    const chars=Array.from(input.value),parts=[];
    let cursor=0;
    for(const {start,end,name} of edits){parts.push(chars.slice(cursor,start).join(''),`{{${name}}}`);cursor=end;}
    const prompt=parts.join('')+chars.slice(cursor).join('');
    if(prompt.length>12000)throw new Error('批量添加后任务超过 12000 字，请减少勾选或缩短任务。');
    templateParameters(prompt);
    for(const {name,value} of chosen)draftDefaults.set(name,value);
    input.value=prompt;clearCandidates();renderParameters();
    parameterFeedback(`已添加 ${chosen.length} 项参数，原值已填入默认值。`);
  }
  function insertParameter(rawName) {
    const name=rawName.trim(),input=$('[name="prompt"]'),placeholder=`{{${name}}}`;
    try {
      if(!/^[\p{L}\p{N}_][\p{L}\p{N}_ -]{0,39}$/u.test(name))throw new Error('请填写参数名（最多 40 字），可用中文、字母、数字、下划线、空格或短横线，不需要输入花括号。');
      const start=input.selectionStart??input.value.length,end=input.selectionEnd??start;
      const original=input.value.slice(start,end);
      if(original.includes('{{')||original.includes('}}'))throw new Error('请选中具体的年份、月份或工厂等文字，不要选中已有参数标记。');
      if(original.trim().length>500)throw new Error('选中文字超过默认值的 500 字上限，请缩小选择范围。');
      const candidate=input.value.slice(0,start)+placeholder+input.value.slice(end);
      if(candidate.length>12000)throw new Error('任务已达到 12000 字上限，请缩短后再添加参数。');
      templateParameters(candidate);
      for(const [key,value] of Object.entries(values('default')))draftDefaults.set(key,value);
      const fillDefault=original.trim()&&!draftDefaults.get(name)?.trim();
      if(fillDefault)draftDefaults.set(name,original.trim());
      // Keep the selection and reading position in the task editor after using the toolbar.
      const scrollTop=input.scrollTop;
      input.value=candidate;clearCandidates();
      renderParameters();
      if(fillDefault) {
        const field=[...root.querySelectorAll('[data-template-default]')].find(field=>field.dataset.templateDefault===name);
        field.value=original.trim();draftDefaults.set(name,field.value);editorPreview();
      }
      input.focus();input.setSelectionRange?.(start+placeholder.length,start+placeholder.length);input.scrollTop=scrollTop;
      $('[data-template-parameter-name]').value='';
      parameterFeedback(fillDefault?`已添加“${name}”，选中文字已填入默认值。`:`已添加“${name}”，可在下方设置默认值。`);
    }catch(error){parameterFeedback(error.message,true);}
  }
  function effectiveValues() {
    return Object.fromEntries([...root.querySelectorAll('[data-template-value]')].map(input=>
      [input.dataset.templateValue,input.value.trim()||inheritedValue(input.dataset.templateValue)]));
  }
  function preview() {
    const effective=effectiveValues(),missing=selected.parameters.filter(name=>!effective[name]);
    const filled=Object.values(values('value')).filter(value=>value.trim()).length;
    $('[data-template-preview]').textContent=templatePreview(selected.prompt,effective);
    $('[data-template-run-status]').textContent=selected.parameters.length?
      `本次填写 ${filled} 项 · 沿用 ${selected.parameters.length-missing.length-filled} 项${missing.length?` · 待填写：${missing.join('、')}`:' · 可开始运行'}`:
      '固定任务，无需填写参数。核对下方任务后可直接运行。';
    for(const input of root.querySelectorAll('[data-template-value]'))input.setCustomValidity?.(effective[input.dataset.templateValue]?'':'请填写该参数的首次取值');
  }
  function render() {
    if(disposed)return;
    let content='';
    if(screen==='list') {
      content=`<div class="template-toolbar"><span>保存常用分析，按原任务或更换参数重跑</span><button class="primary-button" data-template-new>新建模板</button></div>
        <label class="form-field">搜索模板<input type="search" data-template-search placeholder="模板名称或报表名称"></label>
        <div class="template-list">${rows.map(row=>`<article class="template-card" data-template-card="${esc(row.id)}"><strong>${esc(row.name)}</strong><p>${esc(row.model_name)} · 版本 ${row.revision}</p><div class="template-parameter-summary"><span class="template-kind">${row.parameters.length?`${row.parameters.length} 个可填写参数`:'固定任务'}</span>${row.parameters.length?row.parameters.map(name=>`<span class="template-parameter-chip">${esc(name)}</span>`).join(''):'<span>无需填写参数，按原任务重新取数</span>'}</div><div class="template-actions"><button class="primary-button" data-template-run="${esc(row.id)}">${icon('refresh')}一键重跑</button><button class="secondary-button" data-template-edit="${esc(row.id)}">编辑</button><button class="secondary-button" data-template-history="${esc(row.id)}">运行记录</button><button class="text-link" data-template-delete="${esc(row.id)}">删除</button></div><div data-template-delete-panel hidden><p>删除此模板？已有分析对话和结果会保留。</p><button class="secondary-button" data-template-delete-cancel>取消</button><button class="secondary-button" data-template-delete-confirm="${esc(row.id)}">确认删除模板</button></div></article>`).join('')}</div>
        <p class="notice" data-template-empty ${rows.length?'hidden':''}>还没有模板。可新建，或在已完成的分析回复下点击“保存为分析模板”。</p>`;
    } else if(screen==='edit') {
      draftDefaults=new Map(Object.entries(selected.defaults||{}));recognition=null;
      content=`<button type="button" class="text-link" data-template-back>← 返回模板列表</button>
        <form data-template-editor><label class="form-field">模板名称<input name="name" required maxlength="120" value="${esc(selected.name||'')}"></label>
        <p class="field-help">所属报表：${esc(selected.model_name||getModel()?.name||'未连接')}</p>
        <label class="form-field">分析任务与口径<textarea name="prompt" required maxlength="12000" rows="6" aria-describedby="template-parameter-help template-parameter-notice" placeholder="例如：分析 0810 工厂在 2026 年各月的 MPV，找出表现最差的月份。">${esc(selected.prompt||'')}</textarea></label>
        <div class="template-parameter-builder"><div class="template-bulk-heading"><button type="button" class="secondary-button" data-template-recognize>批量识别参数</button><span id="template-parameter-help">从任务和口径中找出可替换的取值。</span></div>
          <div data-template-candidate-panel hidden>
            <p class="field-help">勾选的取值会在保存时转为参数。也可以先应用，再调整默认值。</p>
            <div class="template-bulk-heading"><span data-template-selection aria-live="polite"></span><button type="button" class="text-link" data-template-toggle-candidates>清空勾选</button></div>
            <div class="template-candidates" data-template-candidates></div>
            <button type="button" class="secondary-button" data-template-apply-candidates>应用所选参数</button>
          </div>
          <details class="template-manual"><summary>手动添加参数</summary><p class="field-help">选中原文再点击参数名，可把选中文字设为默认值。也可在光标处插入参数。</p>
          <div class="template-parameter-shortcuts"><span>常用参数</span>${['年份','月份','工厂','部门'].map(name=>`<button type="button" class="secondary-button" data-template-insert-parameter="${name}" title="将选中文字设为${name}参数">${name}</button>`).join('')}</div>
          <div class="template-parameter-custom"><label class="form-field">自定义参数名<input data-template-parameter-name maxlength="40" placeholder="例如：指标、产品"></label><button type="button" class="secondary-button" data-template-add-parameter>${icon('plus')}添加参数</button></div>
          <p class="field-help">也可直接填写 {{年份}} 等标记，同名参数只计一次，最多 12 项。</p></details>
          <p class="template-parameter-notice" id="template-parameter-notice" data-template-parameter-notice role="status" hidden></p>
        </div>
        <p class="field-help">保存的是任务与口径。每次运行都会重新取数；依赖截图的信息请先补充为文字。</p>
        <div class="template-section-heading"><h3>运行参数与默认值</h3><span class="template-kind" data-template-parameter-count></span></div>
        <p class="field-help" data-template-parameter-empty>可按固定任务保存，也可批量识别参数。</p>
        <div class="template-parameters" data-template-defaults></div>
        <details class="template-editor-preview"><summary>替换预览（使用默认值）</summary><p class="field-help">未设默认值的参数保留 {{参数名}}，首次使用时填写。</p><pre class="template-preview" data-template-editor-preview></pre></details>
        <div class="modal-actions"><button type="button" class="secondary-button" data-template-back>取消</button><button type="submit" class="primary-button">保存模板</button></div></form>`;
    } else if(screen==='run') {
      content=`<button type="button" class="text-link" data-template-back>← 返回模板列表</button><h3>${esc(selected.name)}</h3>
        <p class="notice">将在当前报表“${esc(getModel()?.name||'尚未连接')}”新建分析。模板所属报表：${esc(selected.model_name)}。</p>
        <form data-template-run-form><div class="template-section-heading"><h3>${selected.parameters.length?'填写本次参数':'核对固定任务'}</h3>${selected.parameters.length?'<button type="button" class="text-link" data-template-reset-values>恢复模板默认值</button>':''}</div>
        ${selected.parameters.length?'<p class="field-help">只填本次要改的项。留空沿用上次运行的值，首次使用时取模板默认值。</p>':''}
        <p class="template-run-status" data-template-run-status role="status"></p><div class="template-parameters">${parameterFields(selected.parameters,selected.defaults,'value')}</div>
        <details open><summary>本次分析任务</summary><pre class="template-preview" data-template-preview></pre></details>
        <p class="field-help">将使用当前模型服务重新查询和分析，结果保存在独立对话中。数据有歧义时会继续向你确认。</p>
        <div class="modal-actions"><button type="submit" class="primary-button">${icon('refresh')}开始运行</button></div></form>`;
    } else {
      content=`<button type="button" class="text-link" data-template-back>← 返回模板列表</button><h3>${esc(selected.name)} · 运行记录</h3><div data-template-runs></div>`;
    }
    root.innerHTML=`<fieldset class="template-fields" ${busy?'disabled':''}>${content}</fieldset><p class="notice template-notice" role="status" data-template-notice hidden></p>`;
    if(screen==='edit')renderParameters();
    if(screen==='run')preview();
  }
  async function list() {rows=await api('/analysis-templates');screen='list';selected=null;render();}
  async function history() {
    const runs=await api(`/analysis-templates/${selected.id}/runs`);
    if(disposed)return;
    $('[data-template-runs]').innerHTML=runs.length?runs.map(run=>`<article class="template-card"><strong>${esc(statuses[run.status]||run.status)}</strong><p>${esc(new Date(run.created_at).toLocaleString('zh-CN'))} · 模板版本 ${run.revision}</p><p>${Object.entries(run.values).map(([name,value])=>`${esc(name)}：${esc(value)}`).join(' · ')||'无参数'}</p>${run.error?`<p>${esc(run.error)}</p>`:''}<button class="secondary-button" data-template-open-run="${esc(run.conversation_id)}">查看分析</button></article>`).join(''):'<p class="notice">还没有运行记录。</p>';
  }
  root.onclick=event=>{
    const button=event.target.closest('button');if(!button||busy||disposed)return;
    if(button.hasAttribute('data-template-back'))void action(list);
    if(button.hasAttribute('data-template-recognize'))void action(recognize);
    if(button.hasAttribute('data-template-apply-candidates'))void action(applyCandidates);
    if(button.hasAttribute('data-template-toggle-candidates')&&recognition){
      const boxes=[...root.querySelectorAll('[data-template-candidate]')],select=!boxes.some(box=>box.checked);
      boxes.forEach((box,i)=>{box.checked=select&&i<recognition.room;});updateSelection();
    }
    if(button.dataset.templateInsertParameter)insertParameter(button.dataset.templateInsertParameter);
    if(button.hasAttribute('data-template-add-parameter'))insertParameter($('[data-template-parameter-name]').value);
    if(button.hasAttribute('data-template-reset-values')) {
      for(const input of root.querySelectorAll('[data-template-value]'))input.value=parameterValue(selected.defaults,input.dataset.templateValue);
      feedback('');preview();
    }
    if(button.hasAttribute('data-template-new')) {
      const model=getModel();if(model?.source!=='desktop'){feedback('请先连接真实 Desktop 报表',true);return;}
      selected={model_id:model.id,model_name:model.name,name:'',prompt:'',defaults:{}};screen='edit';render();
    }
    if(button.dataset.templateEdit){selected=rows.find(row=>row.id===button.dataset.templateEdit);screen='edit';render();}
    if(button.dataset.templateRun)void action(async()=>{
      selected=await api(`/analysis-templates/${button.dataset.templateRun}`);
      screen='run';pendingRun=null;render();
    });
    if(button.dataset.templateHistory){selected=rows.find(row=>row.id===button.dataset.templateHistory);screen='history';render();void action(history);}
    if(button.dataset.templateDelete)button.closest('article').querySelector('[data-template-delete-panel]').hidden=false;
    if(button.hasAttribute('data-template-delete-cancel'))button.closest('[data-template-delete-panel]').hidden=true;
    if(button.dataset.templateDeleteConfirm)void action(async()=>{
      const row=rows.find(row=>row.id===button.dataset.templateDeleteConfirm);
      await api(`/analysis-templates/${row.id}?revision=${row.revision}`,{method:'DELETE'});await list();feedback('模板已删除，已有分析结果保留');
    });
    if(button.dataset.templateOpenRun)void action(()=>onOpen(button.dataset.templateOpenRun));
  };
  root.oninput=event=>{
    if(event.target.name==='prompt'){
      const scanned=!!recognition;
      clearCandidates();renderParameters();
      if(scanned&&!event.target.hasAttribute('aria-invalid'))parameterFeedback('任务已修改，可重新识别参数。');
    }
    if(event.target.hasAttribute('data-template-default')){draftDefaults.set(event.target.dataset.templateDefault,event.target.value);editorPreview();}
    if(event.target.hasAttribute('data-template-value'))preview();
    if(event.target.hasAttribute('data-template-search')) {
      const needle=event.target.value.toLowerCase();let count=0;
      for(const card of root.querySelectorAll('[data-template-card]')){
        const row=rows.find(row=>row.id===card.dataset.templateCard);
        card.hidden=!(row.name+' '+row.model_name).toLowerCase().includes(needle);if(!card.hidden)count++;
      }
      const empty=$('[data-template-empty]');empty.hidden=!!count;empty.textContent=rows.length?'没有匹配的模板':'还没有模板。可新建，或从已完成的分析保存。';
    }
  };
  root.onchange=event=>{
    if(recognition&&event.target.hasAttribute('data-template-candidate'))updateSelection();
  };
  root.onkeydown=event=>{
    if(event.target.hasAttribute('data-template-parameter-name')&&event.key==='Enter') {
      event.preventDefault();
      if(!busy&&!disposed&&!event.isComposing&&event.keyCode!==229)insertParameter(event.target.value);
    }
  };
  root.onsubmit=event=>{
    event.preventDefault();if(busy||disposed||!event.target.reportValidity())return;
    if(event.target.hasAttribute('data-template-editor'))void action(async()=>{
      applyCandidates();
      const prompt=$('[name="prompt"]').value,names=templateParameters(prompt),defaults=values('default');
      const body={name:$('[name="name"]').value,prompt,defaults:Object.fromEntries(names.map(name=>[name,defaults[name]||'']))};
      if(selected.id)body.revision=selected.revision;
      else Object.assign(body,{model_id:selected.model_id,source_message_id:selected.source_message_id||null});
      await request(selected.id?`/analysis-templates/${selected.id}`:'/analysis-templates',selected.id?'PUT':'POST',body);
      await list();feedback(names.length?'模板已保存，可填写参数后运行':'模板已保存，可按固定任务重跑');
    });
    if(event.target.hasAttribute('data-template-run-form'))void action(async()=>{
      const model=getModel(),service=getService();
      if(model?.source!=='desktop')throw new Error('请先连接模板所属的 Desktop 报表');
      if(!service.ready)throw new Error('请先配置并选择可用的模型服务');
      const effective=effectiveValues(),missing=[...root.querySelectorAll('[data-template-value]')].filter(input=>!effective[input.dataset.templateValue]);
      if(missing.length){missing[0].focus();throw new Error('请填写参数：'+missing.map(input=>input.dataset.templateValue).join('、'));}
      // Pin the displayed task even if a different window runs this template before submission.
      const body={model_id:model.id,revision:selected.revision,values:effective,llm_profile_id:service.active_profile_id||null};
      const key=JSON.stringify({id:selected.id,...body});
      if(!pendingRun||pendingRun.key!==key)pendingRun={key,id:globalThis.crypto.randomUUID().replaceAll('-','')};
      const result=await request(`/analysis-templates/${selected.id}/runs`,'POST',{...body,request_id:pendingRun.id});
      await onStarted(result,model,!disposed);pendingRun=null;
      selected.last_values=effective;if(!disposed)render();
    });
  };
  if(seed){
    render();
    if(seed.source_message_id&&seed.prompt?.trim())await action(recognize);
  }else await list();
  return {dispose(){disposed=true;root.onclick=null;root.oninput=null;root.onchange=null;root.onkeydown=null;root.onsubmit=null;}};
}
