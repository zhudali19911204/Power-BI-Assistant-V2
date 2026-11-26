import { $ } from './ui.js';
import { language, serviceText, tr } from './i18n.js';
import { icon } from './icons.js';
import { createMessageRenderer, createRenderScheduler } from './messages.js';
import { escapeHtml as esc, markdown } from './markdown.js';
import { modeFor } from './modes.js';
import { analysisEvidence } from './analysis.js';
import { fieldEvidence } from './fields.js';
import { clarificationCard } from './clarification.js';
import { saveTemplateButton } from './analysis_templates.js';
import { artifactCard } from './artifacts.js';
import { imageGallery } from './images.js';

export function createConversationView(getView) {
  const messageRenderer = createMessageRenderer({
    root: $('#messages'),
    scroll: $('#message-scroll'),
    label: (m) =>
      `<span class="message-avatar">${m.role === 'user' ? tr('你') : icon('sparkles')}</span><span>${m.role === 'user' ? tr('你') : 'Power BI Assistant'}</span>${m.model_stale ? `<small class="validation-badge" title="${tr('同步前的回复与结果作为历史保留，后续分析使用最新模型重新取数')}">${tr('同步前回复')}</small>` : ''}`,
    content: (m) =>
      m.role === 'user'
        ? imageGallery(m.images) + '<p>' + esc(m.content).replace(/\n/g, '<br>') + '</p>'
        : m.clarification && m.status !== 'error' && m.status !== 'cancelled'
          ? ''
          : markdown(m.content),
    artifacts: (m) =>
      fieldEvidence(m.field_previews || []) +
      clarificationCard(m) +
      analysisEvidence(m.analysis || []) +
      (m.artifacts || []).map(artifactCard).join('') +
      saveTemplateButton(m),
    status: streamStatus,
  });
  const messagePaint = createRenderScheduler(renderMessages);

  function welcome(view) {
    const mode = modeFor(view.mode);
    const model = view.model;
    const needsReport = !model || (view.mode === 'analysis' && model.source !== 'desktop');
    return `<section class="welcome" aria-labelledby="welcome-title">
    <div class="welcome-emblem">${icon(needsReport ? 'database' : mode.icon)}</div>
    <h1 id="welcome-title">${needsReport ? tr('从哪份报表开始？') : mode.headline}</h1>
    <p class="welcome-subtitle">${needsReport ? tr('先在 Power BI Desktop 中打开报表，连接后即可使用其中的字段和度量值。') : mode.subtitle}</p>
    ${
      needsReport
        ? `<div class="welcome-actions"><button type="button" class="primary-button welcome-connect" id="welcome-connect">${icon('monitor')}${tr('连接 Power BI 报表')}</button>
      <button type="button" class="text-link" data-open-guide aria-haspopup="dialog">${tr('快速上手')}${icon('arrow-right')}</button></div>`
        : ''
    }
  </section>`;
  }
  function renderMessages(forceFollow = false) {
    const view = getView();
    const showWelcome =
      !view.messages.length &&
      (!view.conversation || view.loaded) &&
      !view.loading &&
      !view.busy &&
      !view.preparing &&
      !view.syncing;
    $('#prompt-actions').hidden = !showWelcome;
    for (const message of view.messages)
      if (message.clarification) {
        message.clarificationDraft = view.clarificationDrafts[message.id] || {};
        message.clarificationLocked = !!(
          view.busy ||
          view.preparing ||
          view.loading ||
          view.syncing ||
          message.model_stale ||
          message.status !== 'complete' ||
          message.clarification.answered ||
          message !== view.messages.at(-1)
        );
      }
    messageRenderer.render(view.messages, {
      conversation: view.key,
      language,
      welcome: showWelcome ? welcome(view) : '',
      forceFollow,
    });
  }
  function streamStatus(message) {
    const view = getView();
    const elapsed = view.startedAt
      ? Math.floor((Date.now() - view.startedAt) / 1000)
      : message.progress?.elapsed || 0;
    return (
      serviceText(message.progress?.stage || view.status || '正在等待模型回复') +
      (elapsed > 0 ? ` ${tr('· 已用时 {0} 秒', elapsed)}` : '')
    );
  }
  setInterval(() => {
    messageRenderer.updateStatus(getView().messages);
  }, 1000);

  return { renderMessages, messagePaint };
}
