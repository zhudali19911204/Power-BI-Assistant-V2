import { language, tr } from './i18n.js';
export function mountChatContext(root, { getView, api, onChange, onError }) {
  const find = (selector) => root.querySelector(selector);
  const bar = find('progress'),
    percent = find('[data-percent]'),
    automatic = find('[data-automatic]');
  const compress = find('[data-compress]'),
    notes = find('[data-notes]'),
    feedback = find('[data-feedback]');
  const details = find('[data-usage]'),
    notesBody = find('[data-notes-body]'),
    threshold = find('[data-threshold]');
  let owner, lastUsage, lastBusy, lastError, lastMode, lastId, lastLanguage;

  function isBusy(view) {
    return !!(view.changingContext || view.busy || view.preparing || view.loading || view.syncing);
  }

  function render() {
    const view = getView(),
      usage = view.contextUsage,
      id = view.conversation?.id;
    const busy = isBusy(view);
    if (
      owner === view &&
      lastUsage === usage &&
      lastBusy === busy &&
      lastError === view.contextError &&
      lastMode === view.autoCompress &&
      lastId === id &&
      lastLanguage === language
    )
      return;
    lastLanguage = language;
    if (owner !== view) {
      root.open = false;
      notes.open = false;
    }
    owner = view;
    lastUsage = usage;
    lastBusy = busy;
    lastError = view.contextError;
    lastMode = view.autoCompress;
    lastId = id;

    const value = usage?.percent ?? (id ? null : 0);
    if (value === null) bar.removeAttribute('value');
    else bar.setAttribute('value', String(value));
    percent.textContent = value === null ? '—' : value + '%';
    bar.setAttribute(
      'aria-valuetext',
      value === null ? (view.contextError ? tr('历史占用读取失败') : tr('正在读取历史占用')) : value + '%'
    );
    const trigger = usage?.auto_percent ?? 80;
    root.toggleAttribute('data-full', value !== null && value >= trigger);
    threshold.textContent = trigger + '%';
    if (usage) {
      const counts = tr('{0} / {1} 条消息', usage.messages, usage.message_limit);
      const chars = tr(
        '{0} / {1} 字符',
        usage.chars.toLocaleString(language),
        usage.char_limit.toLocaleString(language)
      );
      details.textContent = counts + ' · ' + chars + (usage.summary?.length ? tr('（含摘要）') : '');
    } else if (id) {
      details.textContent = view.contextError ? tr('历史占用读取失败') : tr('正在读取…');
    } else {
      details.textContent = tr('新对话 · 0 条历史消息');
    }
    automatic.checked = usage?.automatic ?? view.autoCompress ?? true;
    automatic.disabled = busy || !!(id && !usage);
    compress.disabled = busy || !usage?.can_compress;
    let message = '';
    if (view.changingContext) message = tr('正在更新上下文…');
    else if (view.contextError) message = view.contextError;
    else if (usage?.omitted_messages)
      message = tr('{0} 条原文超出历史窗口，可通过压缩保留要点。', usage.omitted_messages);
    else if (usage?.folded_messages)
      message = tr('已压缩 {0} 条历史消息，原文可在会话中查看。', usage.folded_messages);
    else if (!usage?.can_compress) message = tr('积累较早的对话后可手动压缩。');
    feedback.textContent = message;
    notes.hidden = !usage?.summary?.length;
    notesBody.textContent = notes.hidden
      ? ''
      : usage.summary.map((n) => tr(n['类别']) + '\n' + n['内容']).join('\n\n');
  }

  async function load(view) {
    if (!view.conversation || view.changingContext) return;
    const request = (view.contextRequest = (view.contextRequest || 0) + 1);
    try {
      const usage = await api(`/conversations/${view.conversation.id}/context`);
      if (view.contextRequest === request) {
        view.contextUsage = usage;
        view.contextError = '';
      }
    } catch (error) {
      if (view.contextRequest === request) view.contextError = error.message;
    } finally {
      if (getView() === view) render();
    }
  }

  async function change(view, preference) {
    if (isBusy(view)) return;
    if (!view.conversation) {
      if (preference !== undefined) view.autoCompress = preference;
      render();
      return;
    }
    view.contextRequest = (view.contextRequest || 0) + 1;
    view.changingContext = true;
    view.contextError = '';
    onChange(view);
    const path = `/conversations/${view.conversation.id}/context`;
    try {
      view.contextUsage =
        preference === undefined
          ? await api(path + '/compress', { method: 'POST', body: '{}' })
          : await api(path, { method: 'PUT', body: JSON.stringify({ automatic: preference }) });
    } catch (error) {
      view.contextError = error.message;
      onError(error.message, view);
    } finally {
      view.changingContext = false;
      onChange(view);
    }
  }

  automatic.onchange = () => change(getView(), automatic.checked);
  compress.onclick = () => change(getView());
  root.addEventListener('toggle', () => {
    if (root.open && getView().conversation) void load(getView());
  });
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && root.open) {
      root.open = false;
      root.querySelector('summary').focus();
      event.stopPropagation();
    }
  });
  root.ownerDocument.addEventListener('click', (event) => {
    if (root.open && !root.contains(event.target)) root.open = false;
  });
  return { render, load };
}
