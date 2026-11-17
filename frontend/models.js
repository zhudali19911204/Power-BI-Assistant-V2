import {icon} from './icons.js';
import {escapeHtml as esc} from './markdown.js';

const $=(selector,root)=>root.querySelector(selector);
const dataUseHelp='<p class="field-help model-data-note">当前服务会接收相关模型结构、对话及你发送的图片；数据分析时还会接收本轮查询结果。查询由本机 Desktop 执行。</p>';
const feedbackMarkup=attributes=>`<p class="model-feedback" ${attributes} role="status" aria-live="polite" hidden></p>`;

function editorMarkup(profile={}) {
  return `<form data-model-form novalidate>
    <div class="model-editor-scroll">
    <button type="button" class="text-link" data-back>${icon('arrow-left')}返回模型列表</button>
    <h3 class="model-form-heading">连接信息</h3>
    <label class="form-field">显示名称<input name="name" maxlength="160" value="${esc(profile.name||'')}" placeholder="例如：阿里云 DeepSeek / 内网 Qwen" autocomplete="off"></label>
    <label class="form-field">API 协议<select name="provider"><option value="openai_compat" ${profile.provider!=='claude'?'selected':''}>OpenAI 兼容</option><option value="claude" ${profile.provider==='claude'?'selected':''}>Anthropic Messages</option></select></label>
    <label class="form-field">服务器 API 地址<input name="base_url" type="url" maxlength="500" value="${esc(profile.base_url||'')}" placeholder="https://your-provider.example/v1" autocomplete="off"><span class="field-help">填写服务商提供的完整 API 根地址。Anthropic 官方服务可留空。</span></label>
    <label class="form-field">模型名称 / ID<input name="model" required maxlength="160" value="${esc(profile.model||'')}" placeholder="与服务器提供的模型 ID 一致" autocomplete="off"></label>
    <label class="form-field">API Key<input name="api_key" type="password" maxlength="2000" placeholder="${profile.has_key?'已保存，留空保持此模型的 Key':'无鉴权的本地服务可填写 local'}" autocomplete="new-password"><span class="field-help">每个模型的 Key 分别加密保存在本机，界面不回显。</span></label>
    <label class="check-label" data-clear-key ${profile.has_key?'':'hidden'}><input name="clear_key" type="checkbox">清除此模型已保存的 Key</label>
    <details class="model-advanced" data-model-advanced>
    <summary><span>高级设置<small>输出额度与等待时间</small></span>${icon('chevron-down')}</summary>
    <div class="model-advanced-fields">
    <label class="form-field">单次输出额度（token）<input name="max_tokens" type="number" min="256" max="131072" step="1" required value="${esc(String(profile.max_tokens??16000))}"><span class="field-help">默认 16000，范围 256～131072，须符合所选模型的限制。推理模型的思考可能占用输出额度；与输入上下文长度、生成等待时间分别设置。</span></label>
    <label class="form-field">无新数据等待时间（秒）<input name="idle_timeout" type="number" min="30" max="600" step="1" required value="${esc(String(profile.idle_timeout??90))}"><span class="field-help">默认 90 秒。正文或思考数据持续返回时会重新计时；暂时停滞时先提示继续等待。</span></label>
    <label class="form-field">单次生成总时限（秒）<input name="generation_timeout" type="number" min="180" max="7200" step="1" required value="${esc(String(profile.generation_timeout??900))}"><span class="field-help">默认 900 秒（15 分钟），可设为 180～7200 秒。超时保留已收到的文字；数据分析整轮任务也遵循此总时限。</span></label>
    </div></details>
    <p class="field-help">新增模型默认低强度。保存后可直接在模型卡片切换低／高强度；实际能力按接口和模型版本识别。</p>
    ${dataUseHelp}
    </div>
    <div class="model-editor-footer">
      ${feedbackMarkup('data-model-editor-feedback')}
      <p class="model-save-help">仅保存：保留当前选择。保存并使用：切换到此模型，下一次请求生效。</p>
      <div class="modal-actions model-editor-actions"><button type="submit" class="text-link" data-save-only title="保存当前配置，保留当前模型选择">仅保存</button><div class="model-editor-main-actions"><button type="button" class="secondary-button" data-save-test title="先保存配置，再测试连接">保存并测试</button><button type="button" class="primary-button" data-save-use>${icon('check')}保存并使用</button></div></div>
    </div>
  </form>`;
}

function reasoningMarkup(profile) {
  const control=profile.reasoning_control;
  const supported=control?.supported===true,level=profile.reasoning_level||'low';
  return `<div class="llm-reasoning">
    <div class="llm-reasoning-heading"><span>思考强度</span>
      <div class="llm-reasoning-options" role="group" aria-label="${esc(profile.name||profile.model)}的思考强度">
        ${['low','high'].map(value=>`<button type="button" class="llm-reasoning-option" data-reasoning-profile="${esc(profile.id)}" data-reasoning-level="${value}" aria-pressed="${supported&&level===value}" ${supported?'':'disabled'}>${value==='low'?'低强度 · 快速':'高强度 · 深入'}</button>`).join('')}
      </div>
    </div>
    <p class="llm-reasoning-effective">${esc(control?.effective_label||'服务端默认 · 强度不可调')}</p>
    <p class="llm-reasoning-help">${esc(control?.description||'当前服务尚未提供强度能力信息，请更新后端后重试。')}</p>
  </div>`;
}

function listMarkup(profiles,confirmId) {
  return `<div class="model-list-heading"><span>${profiles.length} 个已保存模型</span><button type="button" class="primary-button" data-add>${icon('plus')}添加模型</button></div>
    <div class="llm-profile-list">${profiles.map(profile=>`<article class="llm-profile ${profile.active?'active':''}" data-model-profile="${esc(profile.id)}">
      <div class="llm-profile-heading"><strong>${esc(profile.name||profile.model)}</strong>${profile.active?'<span class="llm-badge">当前使用</span>':''}</div>
      <p class="llm-model-id">${esc(profile.model)}</p><p class="llm-endpoint">${esc(profile.base_url||'Anthropic 官方服务')}</p>
      <div class="llm-profile-meta">${profile.provider==='claude'?'Anthropic Messages':'OpenAI 兼容'} · ${profile.ready?'已配置':profile.has_key?'待补全地址或模型名称':'未设置 Key'}</div>
      ${reasoningMarkup(profile)}
      ${confirmId===profile.id?`<div class="llm-delete-confirm"><p>删除“${esc(profile.name||profile.model)}”的配置和 Key？${profile.active?'将切换到列表中的其他模型。':''}</p><button type="button" class="secondary-button" data-cancel-delete>取消</button><button type="button" class="secondary-button danger-button" data-confirm-delete="${esc(profile.id)}">确认删除</button></div>`:
        `<div class="llm-profile-actions">${!profile.active?`<button type="button" class="primary-button" data-use="${esc(profile.id)}">使用此模型</button>`:''}<button type="button" class="secondary-button" data-edit="${esc(profile.id)}">编辑</button><button type="button" class="secondary-button" data-test="${esc(profile.id)}">测试连接</button><button type="button" class="icon-button" data-delete="${esc(profile.id)}" aria-label="删除 ${esc(profile.name||profile.model)}">${icon('trash')}</button></div>`}
      ${feedbackMarkup(`data-model-feedback-for="${esc(profile.id)}"`)}
    </article>`).join('')||'<p class="notice">尚未添加模型。点击“添加模型”填写服务器地址、模型名称和 Key。</p>'}</div>`;
}

/** Model management stays independent of the chat, so switching never clears its history. */
export async function mountModelManager(root,{api,onSettings=()=>{},onNotice=()=>{}}) {
  let profiles=[],editing=null,confirmId=null,busy=false;
  root.classList.add('model-manager');

  function render() {
    root.innerHTML=`<p class="notice model-manager-notice">${editing?'填写连接信息后保存并使用；输出额度和等待时间可在“高级设置”中调整。':'模型默认低强度，可在卡片上按需切换高强度。修改从下一次请求生效，不打断正在生成的回答。'}</p>
      <fieldset class="model-manager-fields" ${busy?'disabled':''}>${editing?editorMarkup(editing):`<div class="model-list-scroll">${feedbackMarkup('data-model-list-feedback')}${listMarkup(profiles,confirmId)}${dataUseHelp}</div>`}</fieldset>`;
  }
  function feedback(message,{error=false,profileId=null,pending=false}={}) {
    const local=profileId?[...root.querySelectorAll('[data-model-feedback-for]')].find(el=>el.dataset.modelFeedbackFor===profileId):null;
    const el=local||$('[data-model-editor-feedback], [data-model-list-feedback]',root);
    if(!el)return;
    el.hidden=!message;el.textContent=message;el.classList.toggle('error',error);el.classList.toggle('pending',pending);
  }
  function validEditor() {
    const form=$('[data-model-form]',root);if(!form)return false;
    // Expand invalid advanced fields before native validation tries to focus them.
    for(const input of form.querySelectorAll('input,select')) {
      if(input.validity?.valid===false) {
        input.closest('[data-model-advanced]')?.setAttribute('open','');break;
      }
    }
    return form.reportValidity();
  }
  async function refresh() {
    const [list,settings]=await Promise.all([api('/llm/profiles'),api('/settings')]);
    profiles=list;onSettings(settings);
  }
  async function run(work,{profileId=null,pending=''}={}) {
    if(busy)return;
    busy=true;root.setAttribute('aria-busy','true');$('.model-manager-fields',root).disabled=true;
    feedback(pending,{profileId,pending:true});
    try {await work();} catch(error) {feedback(error.message,{error:true,profileId});}
    finally {busy=false;root.setAttribute('aria-busy','false');$('.model-manager-fields',root).disabled=false;}
  }
  async function save(action) {
    const form=$('[data-model-form]',root);
    // The surrounding fieldset is disabled while a request is pending, so read controls directly.
    const value=name=>$(`[name="${name}"]`,form)?.value?.trim()||'';
    const data={name:value('name'),provider:value('provider'),base_url:value('base_url'),model:value('model'),
      api_key:value('api_key'),clear_key:!!$('[name="clear_key"]',form)?.checked,
      max_tokens:Number(value('max_tokens')),
      idle_timeout:Number(value('idle_timeout')),generation_timeout:Number(value('generation_timeout'))};
    const profile=await api('/llm/profiles'+(editing.id?'/'+editing.id:''),{
      method:editing.id?'PUT':'POST',body:JSON.stringify(data),
    });
    editing=profile;
    // Remove the typed key after it is stored; future edits retain only this profile's key.
    $('[name="api_key"]',form).value='';
    $('[name="api_key"]',form).placeholder=profile.has_key?'已保存，留空保持此模型的 Key':'无鉴权的本地服务可填写 local';
    $('[name="clear_key"]',form).checked=false;
    $('[data-clear-key]',form).hidden=!profile.has_key;
    await refresh();
    if(action==='test') {
      // Keep the form, advanced-section state and scroll position mounted during the test.
      feedback('配置已保存，正在测试连接…',{pending:true});
      const result=await api(`/llm/profiles/${profile.id}/test`,{method:'POST',body:'{}'});
      feedback(result.detail,{error:result.ok===false});onNotice(result.detail);return;
    }
    if(action==='use') {
      const settings=await api(`/llm/profiles/${profile.id}/activate`,{method:'POST',body:'{}'});
      onSettings(settings);await refresh();
    }
    editing=null;render();feedback(action==='use'?'已保存并切换模型':'模型配置已保存');
  }

  root.onclick=event=>{
    const button=event.target.closest('button');if(!button||busy)return;
    const id=button.dataset.use||button.dataset.edit||button.dataset.test||button.dataset.delete;
    if(button.hasAttribute('data-add')){editing={};confirmId=null;render();$('[name="name"]',root).focus();}
    if(button.hasAttribute('data-back')){editing=null;render();}
    if(button.dataset.reasoningProfile&&!button.disabled&&button.getAttribute('aria-pressed')!=='true')void run(async()=>{
      const profileId=button.dataset.reasoningProfile,level=button.dataset.reasoningLevel;
      const updated=await api(`/llm/profiles/${profileId}/reasoning`,{
        method:'PATCH',body:JSON.stringify({reasoning_level:level}),
      });
      await refresh();render();
      feedback(`已切换为${level==='low'?'低':'高'}强度，下一次请求生效。${updated.reasoning_control.effective_label}`);
      [...root.querySelectorAll('[data-reasoning-profile]')].find(el=>
        el.dataset.reasoningProfile===profileId&&el.dataset.reasoningLevel===level)?.focus();
    });
    if(button.dataset.edit){editing=profiles.find(p=>p.id===id);render();}
    if(button.dataset.delete){confirmId=id;render();}
    if(button.hasAttribute('data-cancel-delete')){confirmId=null;render();}
    if(button.dataset.use)void run(async()=>{
      onSettings(await api(`/llm/profiles/${id}/activate`,{method:'POST',body:'{}'}));
      await refresh();render();feedback('已切换模型，下一次请求开始使用');
    });
    if(button.dataset.test)void run(async()=>{
      const result=await api(`/llm/profiles/${id}/test`,{method:'POST',body:'{}'});
      feedback(result.detail,{error:result.ok===false,profileId:id});
    },{profileId:id,pending:'正在测试连接…'});
    if(button.dataset.confirmDelete)void run(async()=>{
      onSettings(await api(`/llm/profiles/${button.dataset.confirmDelete}`,{method:'DELETE'}));
      confirmId=null;await refresh();render();feedback('模型配置已删除');
    });
    if(button.hasAttribute('data-save-test')||button.hasAttribute('data-save-use')) {
      if(validEditor())void run(()=>save(button.hasAttribute('data-save-test')?'test':'use'),{pending:'正在保存配置…'});
    }
  };
  root.onsubmit=event=>{
    event.preventDefault();if(busy)return;
    if(validEditor())void run(()=>save('save'),{pending:'正在保存配置…'});
  };
  root.innerHTML='<p class="notice">正在读取已保存模型…</p>';
  await refresh();render();
}
