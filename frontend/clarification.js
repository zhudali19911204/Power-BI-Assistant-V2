import { tr } from './i18n.js';
import { escapeHtml as esc } from './markdown.js';
import { icon } from './icons.js';

export function clarificationCard(message) {
  const question = message.clarification;
  if (!question) return '';
  const draft = message.clarificationDraft || {},
    locked = !!message.clarificationLocked;
  const custom = draft.optionId === 'custom';
  return `<section class="clarification-card" data-clarification="${esc(message.id)}" aria-label="${tr('确认{0}', esc(question.topic))}">
    <div class="clarification-heading"><span>${icon('target')}${tr('口径确认')}</span><small>${question.answered ? tr('已回复') : tr('一次确认一项')}</small></div>
    <h3>${esc(question.question)}</h3>
    ${question.context ? `<p class="clarification-context">${esc(question.context)}</p>` : ''}
    <fieldset ${locked ? 'disabled' : ''} class="clarification-fields"><legend class="sr-only">${tr('选择{0}的口径，或自定义', esc(question.topic))}</legend>
      <div class="clarification-options">${question.options
        .map(
          (option) => `<label class="clarification-option ${draft.optionId === option.id ? 'selected' : ''}">
        <input type="radio" name="clarification-${esc(message.id)}" value="${esc(option.id)}" data-clarification-choice ${draft.optionId === option.id ? 'checked' : ''}>
        <span><strong>${esc(option.label)}</strong><span class="clarification-option-value">${esc(option.value)}</span>${option.detail ? `<small>${esc(option.detail)}</small>` : ''}</span>
      </label>`
        )
        .join('')}
      <label class="clarification-option ${custom ? 'selected' : ''}"><input type="radio" name="clarification-${esc(message.id)}" value="custom" data-clarification-choice ${custom ? 'checked' : ''}><span><strong>${tr('自定义口径')}</strong><small>${tr('按下方补充说明继续讨论')}</small></span></label></div>
      <label class="clarification-supplement">${tr('补充说明{0}', custom ? tr('（必填）') : tr('（选填，也可只填写说明）'))}<textarea data-clarification-supplement maxlength="4000" rows="3" placeholder="${tr('补充筛选值、业务规则，或填写你自己的口径…')}">${esc(draft.supplement || '')}</textarea></label>
      <div class="clarification-actions"><span>${question.answered ? tr('本项已回复，后续确认见下面的对话。') : locked ? tr('正在处理本轮回复…') : tr('选择一项或补充说明后继续')}</span>${!question.answered ? `<button type="button" class="primary-button" data-confirm-clarification="${esc(message.id)}">${tr('确认并继续')}${icon('arrow-right')}</button>` : ''}</div>
    </fieldset>
  </section>`;
}

export function clarificationReply(message, draft = {}) {
  const question = message.clarification,
    supplement = (draft.supplement || '').trim();
  const option = question.options.find((option) => option.id === draft.optionId);
  if (draft.optionId === 'custom' && !supplement) throw new Error(tr('请填写自定义口径'));
  if (!option && !supplement) throw new Error(tr('请选择一个选项，或补充你的口径'));
  if (supplement.length > 4000) throw new Error(tr('补充说明最多 4000 字'));
  let content = tr('口径确认 · {0}\n{1}', question.topic, question.question);
  if (option)
    content += `
${tr('选择：{0}\n采用口径：{1}', option.label, option.value)}`;
  if (supplement)
    content +=
      `
${tr('用户补充：')}` + supplement;
  return {
    content,
    clarification_reply: { message_id: message.id, option_id: option?.id || null, supplement },
  };
}
