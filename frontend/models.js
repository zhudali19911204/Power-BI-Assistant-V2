import {icon} from './icons.js';
import {escapeHtml as esc} from './markdown.js';

const $=(selector,root)=>root.querySelector(selector);

function editorMarkup(profile={}) {
  return `<form data-model-form>
    <button type="button" class="text-link" data-back>${icon('arrow-left')}返回模型列表</button>
    <label class="form-field">显示名称<input name="name" maxlength="160" value="${esc(profile.name||'')}" placeholder="例如：阿里云 DeepSeek / 内网 Qwen" autocomplete="off"></label>
    <label class="form-field">API 协议<select name="provider"><option value="openai_compat" ${profile.provider!=='claude'?'selected':''}>OpenAI 兼容</option><option value="claude" ${profile.provider==='claude'?'selected':''}>Anthropic Messages</option></select></label>
    <label class="form-field">服务器 API 地址<input name="base_url" type="url" maxlength="500" value="${esc(profile.base_url||'')}" placeholder="https://your-provider.example/v1" autocomplete="off"><span class="field-help">填写服务商提供的完整 API 根地址。Anthropic 官方服务可留空。</span></label>
    <label class="form-field">模型名称 / ID<input name="model" required maxlength="160" value="${esc(profile.model||'')}" placeholder="与服务器提供的模型 ID 一致" autocomplete="off"></label>
    <label class="form-field">API Key<input name="api_key" type="password" maxlength="2000" placeholder="${profile.has_key?'已保存，留空保持此模型的 Key':'无鉴权的本地服务可填写 local'}" autocomplete="new-password"><span class="field-help">每个模型的 Key 分别加密保存在本机，界面不回显。</span></label>
    ${profile.has_key?'<label class="check-label"><input name="clear_key" type="checkbox">清除此模型已保存的 Key</label>':''}
    <label class="form-field">回复方式<select name="enable_thinking"><option value="auto" ${profile.enable_thinking==null?'selected':''}>服务器默认</option><option value="off" ${profile.enable_thinking===false?'selected':''}>标准回复 · 关闭深度思考</option><option value="on" ${profile.enable_thinking===true?'selected':''}>深度思考</option></select><span class="field-help">模式切换参数适用于支持 chat_template_kwargs 的自建 Qwen 服务。其他服务选择“服务器默认”。</span></label>
    <label class="form-field">无新数据等待时间（秒）<input name="idle_timeout" type="number" min="30" max="600" step="1" required value="${esc(String(profile.idle_timeout??90))}"><span class="field-help">默认 90 秒。正文或思考数据持续返回时会重新计时；暂时停滞时先提示继续等待。</span></label>
    <label class="form-field">单次生成总时限（秒）<input name="generation_timeout" type="number" min="180" max="7200" step="1" required value="${esc(String(profile.generation_timeout??900))}"><span class="field-help">默认 900 秒（15 分钟），可设为 180～7200 秒。超时保留已收到的文字；数据分析整轮任务也遵循此总时限。</span></label>
    <div class="modal-actions model-editor-actions"><button type="button" class="secondary-button" data-save-test>保存并测试</button><button type="submit" class="secondary-button">保存配置</button><button type="button" class="primary-button" data-save-use>保存并使用</button></div>
  </form>`;
}

function listMarkup(profiles,confirmId) {
  return `<div class="model-list-heading"><span>${profiles.length} 个已保存模型</span><button type="button" class="primary-button" data-add>${icon('plus')}添加模型</button></div>
    <div class="llm-profile-list">${profiles.map(profile=>`<article class="llm-profile ${profile.active?'active':''}">
      <div class="llm-profile-heading"><strong>${esc(profile.name||profile.model)}</strong>${profile.active?'<span class="llm-badge">当前使用</span>':''}</div>
      <p class="llm-model-id">${esc(profile.model)}</p><p class="llm-endpoint">${esc(profile.base_url||'Anthropic 官方服务')}</p>
      <div class="llm-profile-meta">${profile.provider==='claude'?'Anthropic Messages':'OpenAI 兼容'} · ${profile.ready?'已配置':profile.has_key?'待补全地址或模型名称':'未设置 Key'}</div>
      ${confirmId===profile.id?`<div class="llm-delete-confirm"><p>删除“${esc(profile.name||profile.model)}”的配置和 Key？${profile.active?'将切换到列表中的其他模型。':''}</p><button type="button" class="secondary-button" data-cancel-delete>取消</button><button type="button" class="secondary-button danger-button" data-confirm-delete="${esc(profile.id)}">确认删除</button></div>`:
        `<div class="llm-profile-actions">${!profile.active?`<button type="button" class="primary-button" data-use="${esc(profile.id)}">使用此模型</button>`:''}<button type="button" class="secondary-button" data-edit="${esc(profile.id)}">编辑</button><button type="button" class="secondary-button" data-test="${esc(profile.id)}">测试连接</button><button type="button" class="icon-button" data-delete="${esc(profile.id)}" aria-label="删除 ${esc(profile.name||profile.model)}">${icon('trash')}</button></div>`}
    </article>`).join('')||'<p class="notice">尚未添加模型。点击“添加模型”填写服务器地址、模型名称和 Key。</p>'}</div>`;
}

/** Model management stays independent of the chat, so switching never clears its history. */
export async function mountModelManager(root,{api,onSettings=()=>{},onNotice=()=>{}}) {
  let profiles=[],editing=null,confirmId=null,busy=false;
  root.classList.add('model-manager');

  function render() {
    root.innerHTML=`<p class="notice">可保存多个模型，切换后从下一次请求开始使用。配置分别保存，已有会话继续保留。</p>
      <p class="model-feedback" role="status" aria-live="polite" hidden></p>
      <fieldset class="model-manager-fields">${editing?editorMarkup(editing):listMarkup(profiles,confirmId)}</fieldset>
      <p class="field-help">当前服务会接收相关模型结构、对话及你发送的图片；数据分析时还会接收本轮查询结果。查询由本机 Desktop 执行。</p>`;
  }
  function feedback(message,error=false) {
    const el=$('.model-feedback',root);el.hidden=false;el.textContent=message;el.classList.toggle('error',error);
  }
  async function refresh() {
    const [list,settings]=await Promise.all([api('/llm/profiles'),api('/settings')]);
    profiles=list;onSettings(settings);
  }
  async function run(work) {
    if(busy)return;
    busy=true;root.setAttribute('aria-busy','true');$('.model-manager-fields',root).disabled=true;
    try {await work();} catch(error) {feedback(error.message,true);}
    finally {busy=false;root.setAttribute('aria-busy','false');$('.model-manager-fields',root).disabled=false;}
  }
  async function save(action) {
    const form=$('[data-model-form]',root);
    // The surrounding fieldset is disabled while a request is pending, so read controls directly.
    const value=name=>$(`[name="${name}"]`,form)?.value?.trim()||'';
    const thinking=value('enable_thinking');
    const data={name:value('name'),provider:value('provider'),base_url:value('base_url'),model:value('model'),
      api_key:value('api_key'),clear_key:!!$('[name="clear_key"]',form)?.checked,
      enable_thinking:thinking==='auto'?null:thinking==='on',
      idle_timeout:Number(value('idle_timeout')),generation_timeout:Number(value('generation_timeout'))};
    const profile=await api('/llm/profiles'+(editing.id?'/'+editing.id:''),{
      method:editing.id?'PUT':'POST',body:JSON.stringify(data),
    });
    editing=profile;
    // Remove the typed key after it is stored; future edits retain only this profile's key.
    $('[name="api_key"]',form).value='';
    await refresh();
    if(action==='test') {
      render();$('.model-manager-fields',root).disabled=true;
      feedback('配置已保存，正在测试连接…');
      const result=await api(`/llm/profiles/${profile.id}/test`,{method:'POST',body:'{}'});
      feedback(result.detail);onNotice(result.detail);return;
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
    if(button.dataset.edit){editing=profiles.find(p=>p.id===id);render();}
    if(button.dataset.delete){confirmId=id;render();}
    if(button.hasAttribute('data-cancel-delete')){confirmId=null;render();}
    if(button.dataset.use)void run(async()=>{
      onSettings(await api(`/llm/profiles/${id}/activate`,{method:'POST',body:'{}'}));
      await refresh();render();feedback('已切换模型，下一次请求开始使用');
    });
    if(button.dataset.test)void run(async()=>{
      feedback('正在测试连接…');
      const result=await api(`/llm/profiles/${id}/test`,{method:'POST',body:'{}'});
      feedback(result.detail);
    });
    if(button.dataset.confirmDelete)void run(async()=>{
      onSettings(await api(`/llm/profiles/${button.dataset.confirmDelete}`,{method:'DELETE'}));
      confirmId=null;await refresh();render();feedback('模型配置已删除');
    });
    if(button.hasAttribute('data-save-test')||button.hasAttribute('data-save-use')) {
      if($('[data-model-form]',root).reportValidity())void run(()=>save(button.hasAttribute('data-save-test')?'test':'use'));
    }
  };
  root.onsubmit=event=>{
    event.preventDefault();if(busy)return;
    if($('[data-model-form]',root)?.reportValidity())void run(()=>save('save'));
  };
  root.innerHTML='<p class="notice">正在读取已保存模型…</p>';
  await refresh();render();
}
