import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';

const parameterPattern=()=>/\{\{\s*([\p{L}\p{N}_][\p{L}\p{N}_ -]{0,39}?)\s*\}\}/gu;
const parameterValue=(values,name)=>typeof values?.[name]==='string'?values[name]:'';
export function templateParameters(prompt) {
  const names=[...new Set([...prompt.matchAll(parameterPattern())].map(match=>match[1].trim()))];
  const rest=prompt.replace(parameterPattern(),'');
  if(rest.includes('{{')||rest.includes('}}')||names.length>12)throw new Error('参数请使用 {{月份}} 这样的名称，最多 12 项，每个名称最多 40 字');
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
  let draftDefaults=new Map();
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
  function parameterFields(names,values,kind) {
    return names.map(name=>`<label class="form-field"><span class="template-field-label">${esc(name)}<small>${kind==='value'?'必填':'默认值 · 选填'}</small></span><input data-template-${kind}="${esc(name)}" value="${esc(parameterValue(values,name))}" maxlength="500" ${kind==='value'?'required':''} placeholder="${kind==='value'?'填写本次分析参数':'留空则在每次运行前填写'}"></label>`).join('');
  }
  function values(kind) {
    return Object.fromEntries([...root.querySelectorAll(`[data-template-${kind}]`)].map(input=>[input.getAttribute(`data-template-${kind}`),input.value]));
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
      $('[data-template-parameter-count]').textContent=names.length?`${names.length} 个可填写参数`:'固定任务';
      $('[data-template-parameter-empty]').hidden=!!names.length;
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
      input.value=candidate;
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
  function preview() {
    const supplied=values('value'),missing=selected.parameters.filter(name=>!parameterValue(supplied,name).trim());
    $('[data-template-preview]').textContent=templatePreview(selected.prompt,supplied);
    $('[data-template-run-status]').textContent=selected.parameters.length?
      `已填写 ${selected.parameters.length-missing.length} / ${selected.parameters.length} 项${missing.length?` · 待填写：${missing.join('、')}`:' · 可开始运行'}`:
      '固定任务，无需填写参数。核对下方任务后可直接运行。';
    for(const input of root.querySelectorAll('[data-template-value]'))input.setCustomValidity?.(input.value.trim()?'':'请填写本次分析参数，不能只输入空格');
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
      draftDefaults=new Map(Object.entries(selected.defaults||{}));
      content=`<button type="button" class="text-link" data-template-back>← 返回模板列表</button>
        <form data-template-editor><label class="form-field">模板名称<input name="name" required maxlength="120" value="${esc(selected.name||'')}"></label>
        <p class="field-help">所属报表：${esc(selected.model_name||getModel()?.name||'未连接')}。先核对分析任务，再把每次需要变化的内容设为参数。</p>
        <label class="form-field">分析任务与口径<textarea name="prompt" required maxlength="12000" rows="6" aria-describedby="template-parameter-help template-parameter-notice" placeholder="例如：分析 0810 工厂在 2026 年各月的 MPV，找出表现最差的月份。">${esc(selected.prompt||'')}</textarea></label>
        <div class="template-parameter-builder"><p id="template-parameter-help">选中任务中的具体内容，再点击参数名称替换；未选中时插入到光标处。未设默认值时，会将选中文字填为默认值。</p>
          <div class="template-parameter-shortcuts"><span>常用参数</span>${['年份','月份','工厂','部门'].map(name=>`<button type="button" class="secondary-button" data-template-insert-parameter="${name}" title="将选中文字设为${name}参数">${name}</button>`).join('')}</div>
          <div class="template-parameter-custom"><label class="form-field">自定义参数名<input data-template-parameter-name maxlength="40" placeholder="例如：指标、产品"></label><button type="button" class="secondary-button" data-template-add-parameter>${icon('plus')}添加参数</button></div>
          <p class="field-help">也可直接填写 {{年份}} 等标记，同名参数只计一次，最多 12 项。例如：分析 {{工厂}} 工厂在 {{年份}} 年的 MPV。</p>
          <p class="template-parameter-notice" id="template-parameter-notice" data-template-parameter-notice role="status" hidden></p>
        </div>
        <p class="field-help">保存的是任务与口径。每次运行都会重新取数；依赖截图的信息请先补充为文字。</p>
        <div class="template-section-heading"><h3>运行参数与默认值</h3><span class="template-kind" data-template-parameter-count></span></div>
        <p class="field-help" data-template-parameter-empty>当前没有可变参数，会按固定任务重跑。可直接保存，也可在上方添加参数。</p>
        <div class="template-parameters" data-template-defaults></div>
        <details class="template-editor-preview"><summary>替换预览（使用默认值）</summary><p class="field-help">未填默认值的参数保留 {{参数名}}，运行前再填写。</p><pre class="template-preview" data-template-editor-preview></pre></details>
        <div class="modal-actions"><button type="button" class="secondary-button" data-template-back>取消</button><button type="submit" class="primary-button">保存模板</button></div></form>`;
    } else if(screen==='run') {
      content=`<button type="button" class="text-link" data-template-back>← 返回模板列表</button><h3>${esc(selected.name)}</h3>
        <p class="notice">将在当前报表“${esc(getModel()?.name||'尚未连接')}”新建分析。模板所属报表：${esc(selected.model_name)}。</p>
        <form data-template-run-form><div class="template-section-heading"><h3>${selected.parameters.length?'填写本次参数':'核对固定任务'}</h3>${selected.parameters.length?'<button type="button" class="text-link" data-template-reset-values>恢复模板默认值</button>':''}</div>
        ${selected.parameters.length?'<p class="field-help">有默认值的参数会预填，可修改为本次所需的值；这些修改只用于本次运行。</p>':''}
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
    if(button.dataset.templateRun){selected=rows.find(row=>row.id===button.dataset.templateRun);screen='run';pendingRun=null;render();}
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
    if(event.target.name==='prompt')renderParameters();
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
  root.onkeydown=event=>{
    if(event.target.hasAttribute('data-template-parameter-name')&&event.key==='Enter') {
      event.preventDefault();
      if(!busy&&!disposed&&!event.isComposing&&event.keyCode!==229)insertParameter(event.target.value);
    }
  };
  root.onsubmit=event=>{
    event.preventDefault();if(busy||disposed||!event.target.reportValidity())return;
    if(event.target.hasAttribute('data-template-editor'))void action(async()=>{
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
      const missing=[...root.querySelectorAll('[data-template-value]')].filter(input=>!input.value.trim());
      if(missing.length){missing[0].focus();throw new Error('请填写参数：'+missing.map(input=>input.dataset.templateValue).join('、'));}
      const body={model_id:model.id,revision:selected.revision,values:values('value'),llm_profile_id:service.active_profile_id||null};
      const key=JSON.stringify({id:selected.id,...body});
      if(!pendingRun||pendingRun.key!==key)pendingRun={key,id:globalThis.crypto.randomUUID().replaceAll('-','')};
      const result=await request(`/analysis-templates/${selected.id}/runs`,'POST',{...body,request_id:pendingRun.id});
      await onStarted(result,model,!disposed);pendingRun=null;
    });
  };
  if(seed)render();else await list();
  return {dispose(){disposed=true;root.onclick=null;root.oninput=null;root.onkeydown=null;root.onsubmit=null;}};
}
