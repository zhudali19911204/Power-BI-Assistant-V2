import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';

const parameterPattern=()=>/\{\{\s*([\p{L}\p{N}_][\p{L}\p{N}_ -]{0,39}?)\s*\}\}/gu;
export function templateParameters(prompt) {
  const names=[...new Set([...prompt.matchAll(parameterPattern())].map(match=>match[1].trim()))];
  const rest=prompt.replace(parameterPattern(),'');
  if(rest.includes('{{')||rest.includes('}}')||names.length>12)throw new Error('参数请使用 {{月份}} 这样的名称，最多 12 项，每个名称最多 40 字');
  return names;
}
export function templatePreview(prompt,values) {
  templateParameters(prompt);
  return prompt.replace(parameterPattern(),(match,name)=>values[name.trim()]?.trim()||match);
}
export function saveTemplateButton(message) {
  return message.id&&message.role==='assistant'&&message.status==='complete'&&message.analysis?.some(query=>query.ok)?
    `<div class="template-message-action"><button class="secondary-button" data-save-analysis-template="${esc(message.id)}">${icon('plus')}保存为分析模板</button></div>`:'';
}

export async function mountAnalysisTemplates(root,{api,getModel,getService,onStarted,onOpen,seed=null}) {
  const $=selector=>root.querySelector(selector);
  let rows=[],screen=seed?'edit':'list',selected=seed,busy=false,disposed=false,pendingRun=null;
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
    return names.map(name=>`<label class="form-field">${esc(name)}<input data-template-${kind}="${esc(name)}" value="${esc(values[name]||'')}" maxlength="500" ${kind==='value'?'required':''} placeholder="${kind==='value'?'填写本次分析参数':'默认值，可留空'}"></label>`).join('');
  }
  function values(kind) {
    return Object.fromEntries([...root.querySelectorAll(`[data-template-${kind}]`)].map(input=>[input.getAttribute(`data-template-${kind}`),input.value]));
  }
  function renderParameters() {
    const defaults=values('default');
    try{
      const names=templateParameters($('[name="prompt"]').value);
      $('[data-template-defaults]').innerHTML=parameterFields(names,defaults,'default');
      feedback('');
    }catch(error){feedback(error.message,true);}
  }
  function preview() {
    $('[data-template-preview]').textContent=templatePreview(selected.prompt,values('value'));
  }
  function render() {
    if(disposed)return;
    let content='';
    if(screen==='list') {
      content=`<div class="template-toolbar"><span>保存常用分析，下次换参数即可重跑</span><button class="primary-button" data-template-new>新建模板</button></div>
        <label class="form-field">搜索模板<input type="search" data-template-search placeholder="模板名称或报表名称"></label>
        <div class="template-list">${rows.map(row=>`<article class="template-card" data-template-card="${esc(row.id)}"><strong>${esc(row.name)}</strong><p>${esc(row.model_name)} · ${row.parameters.length} 个参数 · 版本 ${row.revision}</p><div class="template-actions"><button class="primary-button" data-template-run="${esc(row.id)}">${icon('refresh')}一键重跑</button><button class="secondary-button" data-template-edit="${esc(row.id)}">编辑</button><button class="secondary-button" data-template-history="${esc(row.id)}">运行记录</button><button class="text-link" data-template-delete="${esc(row.id)}">删除</button></div><div data-template-delete-panel hidden><p>删除此模板？已有分析对话和结果会保留。</p><button class="secondary-button" data-template-delete-cancel>取消</button><button class="secondary-button" data-template-delete-confirm="${esc(row.id)}">确认删除模板</button></div></article>`).join('')}</div>
        <p class="notice" data-template-empty ${rows.length?'hidden':''}>还没有模板。可新建，或在已完成的分析回复下点击“保存为分析模板”。</p>`;
    } else if(screen==='edit') {
      content=`<button type="button" class="text-link" data-template-back>← 返回模板列表</button>
        <form data-template-editor><label class="form-field">模板名称<input name="name" required maxlength="120" value="${esc(selected.name||'')}"></label>
        <p class="field-help">所属报表：${esc(selected.model_name||getModel()?.name||'未连接')}。请核对任务和口径；需要变化的内容用 {{月份}}、{{部门}} 等参数替换，同一参数可重复使用。</p>
        <label class="form-field">分析任务与口径<textarea name="prompt" required maxlength="12000" rows="9" placeholder="分析 {{月份}} 的 {{部门}} 费用，按费用类型比较环比，并解释主要变化。">${esc(selected.prompt||'')}</textarea></label>
        <p class="field-help">保存的是任务与口径。每次运行都会重新取数；依赖截图的信息请先补充为文字。</p>
        <div class="template-parameters" data-template-defaults>${parameterFields(templateParameters(selected.prompt||''),selected.defaults||{},'default')}</div>
        <div class="modal-actions"><button type="button" class="secondary-button" data-template-back>取消</button><button type="submit" class="primary-button">保存模板</button></div></form>`;
    } else if(screen==='run') {
      content=`<button type="button" class="text-link" data-template-back>← 返回模板列表</button><h3>${esc(selected.name)}</h3>
        <p class="notice">将在当前报表“${esc(getModel()?.name||'尚未连接')}”新建分析。模板所属报表：${esc(selected.model_name)}。</p>
        <form data-template-run-form><div class="template-parameters">${parameterFields(selected.parameters,selected.defaults,'value')}</div>
        <details open><summary>本次分析任务</summary><pre class="template-preview" data-template-preview></pre></details>
        <p class="field-help">将使用当前模型服务重新查询和分析，结果保存在独立对话中。数据有歧义时会继续向你确认。</p>
        <div class="modal-actions"><button type="submit" class="primary-button">${icon('refresh')}开始运行</button></div></form>`;
    } else {
      content=`<button type="button" class="text-link" data-template-back>← 返回模板列表</button><h3>${esc(selected.name)} · 运行记录</h3><div data-template-runs></div>`;
    }
    root.innerHTML=`<fieldset class="template-fields" ${busy?'disabled':''}>${content}</fieldset><p class="notice template-notice" role="status" data-template-notice hidden></p>`;
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
  root.onsubmit=event=>{
    event.preventDefault();if(busy||disposed||!event.target.reportValidity())return;
    if(event.target.hasAttribute('data-template-editor'))void action(async()=>{
      const prompt=$('[name="prompt"]').value,names=templateParameters(prompt),defaults=values('default');
      const body={name:$('[name="name"]').value,prompt,defaults:Object.fromEntries(names.map(name=>[name,defaults[name]||'']))};
      if(selected.id)body.revision=selected.revision;
      else Object.assign(body,{model_id:selected.model_id,source_message_id:selected.source_message_id||null});
      await request(selected.id?`/analysis-templates/${selected.id}`:'/analysis-templates',selected.id?'PUT':'POST',body);
      await list();feedback('模板已保存，可填写参数后运行');
    });
    if(event.target.hasAttribute('data-template-run-form'))void action(async()=>{
      const model=getModel(),service=getService();
      if(model?.source!=='desktop')throw new Error('请先连接模板所属的 Desktop 报表');
      if(!service.ready)throw new Error('请先配置并选择可用的模型服务');
      const body={model_id:model.id,revision:selected.revision,values:values('value'),llm_profile_id:service.active_profile_id||null};
      const key=JSON.stringify({id:selected.id,...body});
      if(!pendingRun||pendingRun.key!==key)pendingRun={key,id:globalThis.crypto.randomUUID().replaceAll('-','')};
      const result=await request(`/analysis-templates/${selected.id}/runs`,'POST',{...body,request_id:pendingRun.id});
      await onStarted(result,model,!disposed);pendingRun=null;
    });
  };
  if(seed)render();else await list();
  return {dispose(){disposed=true;root.onclick=null;root.oninput=null;root.onsubmit=null;}};
}
