import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';

export function clarificationCard(message) {
  const question=message.clarification;if(!question)return '';
  const draft=message.clarificationDraft||{},locked=!!message.clarificationLocked;
  const custom=draft.optionId==='custom';
  return `<section class="clarification-card" data-clarification="${esc(message.id)}" aria-label="确认${esc(question.topic)}">
    <div class="clarification-heading"><span>${icon('target')}口径确认</span><small>${question.answered?'已回复':'一次确认一项'}</small></div>
    <h3>${esc(question.question)}</h3>
    ${question.context?`<p class="clarification-context">${esc(question.context)}</p>`:''}
    <fieldset ${locked?'disabled':''} class="clarification-fields"><legend class="sr-only">选择${esc(question.topic)}的口径，或自定义</legend>
      <div class="clarification-options">${question.options.map(option=>`<label class="clarification-option ${draft.optionId===option.id?'selected':''}">
        <input type="radio" name="clarification-${esc(message.id)}" value="${esc(option.id)}" data-clarification-choice ${draft.optionId===option.id?'checked':''}>
        <span><strong>${esc(option.label)}</strong><span class="clarification-option-value">${esc(option.value)}</span>${option.detail?`<small>${esc(option.detail)}</small>`:''}</span>
      </label>`).join('')}
      <label class="clarification-option ${custom?'selected':''}"><input type="radio" name="clarification-${esc(message.id)}" value="custom" data-clarification-choice ${custom?'checked':''}><span><strong>自定义口径</strong><small>按下方补充说明继续讨论</small></span></label></div>
      <label class="clarification-supplement">补充说明${custom?'（必填）':'（选填，也可只填写说明）'}<textarea data-clarification-supplement maxlength="4000" rows="3" placeholder="补充筛选值、业务规则，或填写你自己的口径…">${esc(draft.supplement||'')}</textarea></label>
      <div class="clarification-actions"><span>${question.answered?'本项已回复，后续确认见下面的对话。':locked?'正在处理本轮回复…':'选择一项或补充说明后继续'}</span>${!question.answered?`<button type="button" class="primary-button" data-confirm-clarification="${esc(message.id)}">确认并继续${icon('arrow-right')}</button>`:''}</div>
    </fieldset>
  </section>`;
}

export function clarificationReply(message,draft={}) {
  const question=message.clarification,supplement=(draft.supplement||'').trim();
  const option=question.options.find(option=>option.id===draft.optionId);
  if(draft.optionId==='custom'&&!supplement)throw new Error('请填写自定义口径');
  if(!option&&!supplement)throw new Error('请选择一个选项，或补充你的口径');
  if(supplement.length>4000)throw new Error('补充说明最多 4000 字');
  let content=`口径确认 · ${question.topic}\n${question.question}`;
  if(option)content+=`\n选择：${option.label}\n采用口径：${option.value}`;
  if(supplement)content+='\n用户补充：'+supplement;
  return {content,clarification_reply:{message_id:message.id,option_id:option?.id||null,supplement}};
}
