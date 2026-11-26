import { language, mountLanguage, serviceText, tr } from './i18n.js';
import { icon, hydrateIcons } from './icons.js';
import { createConversationView } from './conversation_view.js';
import { escapeHtml as esc } from './html.js';
import { codeBlock, createCodeTools } from './code_blocks.js';
import { reviewWrite } from './artifacts.js';
import { MODES, modeFor } from './modes.js';
import { createWorkspaceDialogs } from './workspace_dialogs.js';
import { createWorkspaces, createGenerationManager } from './workspaces.js';
import { clarificationReply } from './clarification.js';
import { createWorkspacePanels } from './workspace_panels.js';
import { semanticSummary } from './semantics.js';
import { mountComposerImages } from './images.js';
import { createConnectionMonitor, releaseSnapshot } from './connections.js';
import { mountAnalysisTemplates } from './analysis_templates.js';
import { createWorkspaceLayout } from './layout.js';
import { createTableTools } from './table_reader.js';
import { mountChatContext } from './chat_context.js';
import { $, $$, api, post, toast, guard } from './ui.js';
import { mountDesktopUpdates } from './updates.js';

mountLanguage(document, {
  save: (language) =>
    api('/ui/language', { method: 'PUT', body: JSON.stringify({ language }), keepalive: true }),
  onError: (message) => toast(message, true),
  onChange() {
    const groups = $$('#model-tree .table-group').map((group) => group.open);
    render();
    $$('#model-tree .table-group').forEach((group, i) => {
      group.open = groups[i];
    });
    renderConnectionStatus();
    contextLayout.render();
    document.dispatchEvent(new document.defaultView.Event('languagechange'));
  },
});
const workspace = createWorkspaces();
const codeTools = createCodeTools($('#code-reader'), { onNotice: toast });
const state = {
  models: [],
  settings: {},
  conversations: [],
  contextTab: 'tables',
  get view() {
    return workspace.current;
  },
};
const {
  renderHistory,
  renderBackgroundTasks,
  renderContext,
  renderModelTree,
  renderConnectionStatus,
  renderControls,
} = createWorkspacePanels(state, workspace, renderBusy);
const historyContext = mountChatContext($('#history-context'), {
  getView: () => state.view,
  api,
  onChange: (view) => {
    if (state.view === view) renderBusy();
  },
  onError: (message, view) => {
    if (state.view === view) toast(message, true);
  },
});
const contextLayout = createWorkspaceLayout({
  root: $('.workspace'),
  panel: $('#context-panel'),
  handle: $('#context-resizer'),
  toggle: $('#toggle-context'),
  sidebar: { panel: $('#sidebar'), handle: $('#sidebar-resizer') },
  save: (value) => api('/ui/layout', { method: 'PUT', body: JSON.stringify(value) }),
  onNotice: toast,
});
const tableTools = createTableTools($('#table-reader'), {
  getQuery: (id) =>
    state.view.messages.flatMap((message) => message.analysis || []).find((query) => query.id === id),
  onNotice: toast,
});
const connectionMonitor = createConnectionMonitor({
  api,
  getModels: () => [
    state.view.model,
    ...workspace
      .all()
      .filter((view) => view.busy)
      .map((view) => view.model),
  ],
  onChange: (result) => {
    const released = result.connection?.status === 'released';
    const redraw =
      released && state.view.model?.id === result.model_id && !state.view.model.snapshot_released;
    const patch = (model) =>
      model?.id === result.model_id
        ? released
          ? releaseSnapshot(model)
          : { ...model, live: result.live, port: result.port, connection: result.connection }
        : model;
    state.models = state.models.map(patch);
    for (const view of workspace.all()) view.model = patch(view.model);
    if (redraw) render();
    else if (state.view.model?.id === result.model_id) renderConnectionStatus();
  },
});

const {
  showModal,
  attachManager,
  openUserGuide,
  openConnections,
  selectConnection,
  updateModelSemantics,
  openDictionaryLibrary,
  openSettings,
  openFieldPreview,
  openSemantics,
  openCalibration,
} = createWorkspaceDialogs({
  state,
  workspace,
  available,
  useModel,
  connectionMonitor,
  renderControls,
  renderContext,
});
let composerImages = null;
hydrateIcons();
const { renderMessages, messagePaint } = createConversationView(() => state.view);
let taskSignature = '';
const generation = createGenerationManager({
  workspace,
  api,
  onChange: (view) => {
    if (state.view === view) {
      renderBusy();
      messagePaint.schedule();
    }
    const signature = workspace
      .all()
      .map((v) => `${v.key}:${v.busy}:${v.attention}:${v.conversation?.id || ''}`)
      .join('|');
    if (signature !== taskSignature) {
      taskSignature = signature;
      renderHistory();
      renderBackgroundTasks();
    }
  },
  onCreated: (conversation) => {
    state.conversations = [conversation, ...state.conversations.filter((c) => c.id !== conversation.id)];
    renderHistory();
  },
  onModel: (view, event) => {
    if (view.model?.id === event.model_id) {
      view.model.live = event.live;
      if (event.live) view.model.connection = { status: 'connected' };
      if (state.view === view) renderConnectionStatus();
    }
  },
  onComplete: (view, failed) => {
    void historyContext.load(view);
    if (state.view === view) {
      renderControls();
      if (!$('#prompt').value && view.draft) $('#prompt').value = view.draft;
      messagePaint.flush();
    } else
      toast(
        `“${view.conversation?.title || tr('后台会话')}”${view.cancelled ? tr('已中止生成') : failed ? tr('生成未完成，请查看原因') : view.messages.at(-1)?.clarification ? tr('有一项口径待确认') : tr('已生成完成')}`,
        failed
      );
    void reloadHistory().catch((error) => toast(error.message, true));
  },
  onError: (message, view) =>
    toast((state.view === view ? '' : `“${view.conversation?.title || tr('后台会话')}”：`) + message, true),
});

function available() {
  if (state.view.changingContext) {
    toast(tr('正在更新当前对话的上下文，请稍候'));
    return false;
  }
  if (state.view.busy || state.view.loading || state.view.syncing || state.view.preparing) {
    toast(
      state.view.syncing
        ? tr('正在同步当前对话的模型，请稍候')
        : state.view.loading
          ? tr('正在读取此会话')
          : state.view.preparing
            ? tr('正在检查语义，请稍候')
            : tr('此会话正在生成，可切换到其他页面继续使用')
    );
    return false;
  }
  return true;
}
function renderBusy() {
  historyContext.render();
  const view = state.view;
  const processing = Boolean(
    view.busy || view.preparing || view.syncing || view.loading || view.changingContext
  );
  $('#send-button').disabled = processing || view.images.some((image) => image.status === 'uploading');
  $('#refresh-model').disabled = processing;
  $('#refresh-model').title = state.view.syncing
    ? tr('正在同步模型')
    : state.view.busy
      ? tr('当前对话结束或中止后可同步模型')
      : tr('同步模型，继续当前对话');
  $('#stop-button').disabled = !state.view.busy || state.view.stopping;
  $('#stop-button').setAttribute(
    'aria-label',
    state.view.stopping ? tr('正在中止本次生成') : tr('中止本次生成')
  );
  $('#stop-button').title = state.view.stopping
    ? tr('正在中止本次生成')
    : tr('中止本次生成，保留已收到的内容');
  composerImages?.render();
  document.body.classList.toggle('busy', state.view.busy);
}

function render() {
  renderControls();
  renderHistory();
  renderContext();
  renderMessages();
  renderBackgroundTasks();
}
function saveDraft() {
  Object.assign(state.view, {
    draft: $('#prompt').value,
    queryName: $('#query-select').value,
    scrollTop: $('#message-scroll').scrollTop,
    pinned:
      $('#message-scroll').scrollHeight - $('#message-scroll').scrollTop - $('#message-scroll').clientHeight <
      100,
  });
}
function showView() {
  const input = $('#prompt');
  if (input.value !== state.view.draft) input.value = state.view.draft;
  render();
  renderConnectionStatus();
  void connectionMonitor.refresh();
  if (state.view.scrollTop !== undefined && !state.view.pinned)
    $('#message-scroll').scrollTo({ top: state.view.scrollTop, behavior: 'instant' });
}
function newChat(mode = state.view.mode) {
  saveDraft();
  workspace.newChat(mode);
  showView();
  $('#prompt').focus();
}
async function openAnalysisTemplates(messageId = null) {
  const seed = messageId ? await api(`/messages/${messageId}/analysis-template`) : null;
  showModal(
    messageId ? tr('保存分析模板') : tr('分析模板'),
    'ANALYSIS TEMPLATES',
    '<div id="analysis-template-manager"></div>',
    { wide: true }
  );
  const root = $('#analysis-template-manager');
  const manager = await mountAnalysisTemplates(root, {
    api,
    seed,
    getModel: () => state.view.model,
    getService: () => state.settings,
    onStarted: async (result, model, visible) => {
      state.conversations = [
        result.conversation,
        ...state.conversations.filter((c) => c.id !== result.conversation.id),
      ];
      const view = workspace.register(result.conversation, model);
      void historyContext.load(view);
      if (visible && root.isConnected) {
        saveDraft();
        if (!state.view.conversation && (state.view.draft || state.view.images.length))
          view.templateReturn = state.view;
        workspace.open(result.conversation, model);
        $('#modal').close();
        showView();
      }
      void generation.follow(view);
      renderHistory();
      renderBackgroundTasks();
    },
    onOpen: async (id) => {
      const conversation = await api(`/conversations/${id}`);
      state.conversations = [conversation, ...state.conversations.filter((c) => c.id !== id)];
      await openConversation(id);
      $('#modal').close();
    },
  });
  attachManager(root, manager);
}
function switchMode(mode) {
  saveDraft();
  workspace.switchMode(mode);
  showView();
}
function keepLatestSnapshot(model) {
  state.models = [
    model,
    ...state.models.filter((saved) => saved.source !== 'desktop' && saved.id !== model.id),
  ];
  for (const view of workspace.all())
    if (view.model?.source === 'desktop') {
      view.model = view.model.id === model.id ? model : releaseSnapshot(view.model);
    }
}
function useModel(model, connected = false) {
  saveDraft();
  const view = state.view;
  const firstReport =
    !view.model || (view.mode === 'analysis' && view.model.source === 'demo' && model.source === 'desktop');
  const keepDraft = firstReport && !view.conversation && !view.messages.length && !view.busy;
  if (connected) keepLatestSnapshot(model);
  if (keepDraft) view.model = model;
  else workspace.newChat(view.mode, model);
  $('#modal').close();
  showView();
  if (keepDraft) $('#prompt').focus();
  if (model.source === 'desktop' && model.semantics?.status !== 'ready' && !model.semantics?.can_answer)
    void openSemantics(model.id).catch((error) => toast(error.message, true));
}
async function syncCurrentModel() {
  const view = state.view;
  if (!view.model) return openConnections();
  if (view.model.source === 'demo') {
    toast(tr('当前为示例模型，请连接 Desktop 以读取真实模型'));
    return;
  }
  if (!available()) return;
  saveDraft();
  view.syncing = true;
  historyRequest++;
  renderBusy();
  renderMessages();
  try {
    const result = view.conversation
      ? await post(`/conversations/${view.conversation.id}/refresh-model`)
      : { model: await post(`/models/${view.model.id}/refresh`) };
    // Capture edits typed while the request was running. Navigation saves the
    // owning draft separately; a late response must never activate another view.
    if (state.view === view) saveDraft();
    historyRequest++;
    view.model = result.model;
    keepLatestSnapshot(result.model);
    if (result.conversation) {
      workspace.bind(view, result.conversation);
      view.contextUsage = null;
      void historyContext.load(view);
      state.conversations = state.conversations.map((c) =>
        c.id === result.conversation.id ? result.conversation : c
      );
      for (const message of view.messages) if (message.role === 'assistant') message.model_stale = true;
    }
    if (state.view === view) render();
    else renderHistory();
    toast(tr('模型已同步，可在原对话继续；历史回复已保留'));
  } finally {
    view.syncing = false;
    renderBusy();
    if (state.view === view) renderMessages();
  }
}
let historyRequest = 0;
async function reloadHistory() {
  const request = ++historyRequest,
    conversations = await api('/conversations');
  if (request !== historyRequest) return;
  const missing = workspace
    .all()
    .filter(
      (view) => view.busy && view.conversation && !conversations.some((c) => c.id === view.conversation.id)
    );
  state.conversations = [...missing.map((view) => view.conversation), ...conversations];
  for (const view of workspace.all()) {
    const current = conversations.find((c) => c.id === view.conversation?.id);
    if (current) view.conversation = current;
  }
  renderHistory();
  renderBackgroundTasks();
}
async function loadConversation(view) {
  if (view.loading || view.preparing || view.syncing || generation.isRunning(view)) return;
  view.loading = true;
  if (state.view === view) renderBusy();
  void historyContext.load(view);
  try {
    const [model, messages, status] = await Promise.all([
      api(`/models/${view.conversation.model_id}`),
      api(`/conversations/${view.conversation.id}/messages`),
      api(`/conversations/${view.conversation.id}/generation`),
    ]);
    Object.assign(view, { model, messages, loaded: true });
    if (status.running || messages.some((m) => m.status === 'streaming')) void generation.follow(view);
  } finally {
    view.loading = false;
    if (state.view === view) showView();
  }
}
async function openConversation(id) {
  const conversation = state.conversations.find((c) => c.id === id);
  if (!conversation) return;
  saveDraft();
  const view = workspace.open(conversation, state.models.find((m) => m.id === conversation.model_id) || null);
  showView();
  if (!generation.isRunning(view)) await loadConversation(view);
}

function canSendMessage() {
  if (!available()) return false;
  if (state.view.images.some((img) => img.status === 'uploading')) {
    toast(tr('请等待图片添加完成'));
    return false;
  }
  if (state.view.images.length > 4) {
    toast(tr('每条消息最多 4 张图片，请移除多余图片'));
    return false;
  }
  return true;
}

async function send(e) {
  e.preventDefault();
  if (!canSendMessage()) return;
  const draft = $('#prompt').value,
    images = state.view.images.filter((img) => img.id),
    content = draft.trim() || (images.length ? tr('请结合图片和当前上下文，帮我分析并处理。') : '');
  if (!content) return;
  if (!state.view.model) {
    await openConnections();
    return;
  }
  if (state.view.mode === 'analysis' && state.view.model.source !== 'desktop') {
    toast(tr('请先连接真实 Desktop 模型进行数据分析'));
    await openConnections();
    return;
  }
  if (!state.settings.ready) {
    await openSettings();
    return;
  }
  if (state.view.mode === 'mquery' && !$('#query-select').value) {
    toast(tr('此模型没有可用的 Power Query 查询'), true);
    return;
  }
  const view = state.view,
    modelId = view.model.id;
  const payload = structuredClone({
    content,
    language,
    image_ids: images.map((img) => img.id),
    images,
    points: view.mode === 'dax' ? view.points : [],
    query_name: view.mode === 'mquery' ? $('#query-select').value : '',
    llm_profile_id: state.settings.active_profile_id || null,
  });
  let pending;
  view.preparing = true;
  renderBusy();
  renderMessages();
  renderHistory();
  try {
    if (
      view.model.source === 'desktop' &&
      view.mode !== 'mquery' &&
      view.model.semantics?.status !== 'ready'
    ) {
      const dictionary = await api(`/models/${modelId}/semantics`);
      if (state.view !== view || view.model?.id !== modelId) return;
      updateModelSemantics(modelId, semanticSummary(dictionary));
      if (!view.model.semantics.can_answer) {
        await openSemantics(modelId);
        toast(tr('先建立初始语义词典，再继续这条需求'));
        return;
      }
    }
    if (view.busy || view.loading || view.syncing) return;
    if (payload.image_ids.some((id) => !view.images.some((img) => img.id === id))) {
      toast(tr('图片已变更，请确认后重新发送'));
      return;
    }
    view.draft = $('#prompt').value;
    if (view.draft === draft) $('#prompt').value = '';
    pending = generation.send(view, payload, { draft });
    renderMessages(true);
  } catch (error) {
    toast(error.message, true);
  } finally {
    view.preparing = false;
    if (state.view === view) {
      renderBusy();
      renderMessages();
    }
    renderHistory();
  }
  await pending;
}

$('#composer-form').onsubmit = send;
$('#stop-button').onclick = () => {
  void generation.stop(state.view);
};
composerImages = mountComposerImages($('#composer-form'), {
  getView: () => state.view,
  onNotice: toast,
  onChange: (view) => {
    if (state.view === view) renderBusy();
  },
});
function updateClarificationDraft(event) {
  const card = event.target.closest('[data-clarification]');
  if (!card) return;
  const id = card.dataset.clarification,
    draft = state.view.clarificationDrafts[id] || {};
  if (event.target.matches('[data-clarification-choice]')) draft.optionId = event.target.value;
  if (event.target.matches('[data-clarification-supplement]')) draft.supplement = event.target.value;
  state.view.clarificationDrafts[id] = draft;
  if (event.type === 'change') messagePaint.schedule();
}
$('#messages').addEventListener('change', updateClarificationDraft);
$('#messages').addEventListener('input', updateClarificationDraft);
$('#prompt').addEventListener('input', () => {
  state.view.draft = $('#prompt').value;
});
$('#prompt').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return;
  e.preventDefault();
  if (e.repeat || $('#send-button').disabled) return;
  $('#composer-form').requestSubmit();
});
document.addEventListener('keydown', (e) => {
  if (
    e.defaultPrevented ||
    e.isComposing ||
    e.keyCode === 229 ||
    e.repeat ||
    $('#modal').open ||
    $('#code-reader').open ||
    $('#table-reader').open ||
    $('#updates-dialog').open
  )
    return;
  if (e.key.toLowerCase() === 'k' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    newChat();
  }
});
$('#new-chat').onclick = () => newChat();
$('#settings-open').onclick = guard(openSettings);
$('#model-settings-chip').onclick = guard(openSettings);
$('#connect-open').onclick = guard(openConnections);
$('#calibration-open').onclick = openCalibration;
$('#analysis-templates-open').onclick = guard(() => openAnalysisTemplates());
$('#template-return').onclick = () => {
  const target = state.view.templateReturn;
  if (target) {
    saveDraft();
    workspace.restore(target);
    showView();
  }
};
$('#search-toggle').onclick = () => {
  const input = $('#history-search');
  input.hidden = !input.hidden;
  if (!input.hidden) input.focus();
  else {
    input.value = '';
    renderHistory();
  }
};
$('#history-search').oninput = renderHistory;
$('#field-search').oninput = renderModelTree;
$('#refresh-model').onclick = guard(syncCurrentModel);
$('#query-select').onchange = () => {
  const query = $('#query-select').value;
  if (state.view.conversation) newChat('mquery');
  state.view.queryName = query;
  $('#query-select').value = query;
};
$('#export-chat').onclick = () => {
  if (!state.view.conversation) {
    toast(tr('先开始一段对话再导出'));
    return;
  }
  const a = document.createElement('a');
  a.href = `/api/conversations/${state.view.conversation.id}/export?language=${language}`;
  a.download = 'conversation.md';
  document.body.append(a);
  a.click();
  a.remove();
};
document.addEventListener(
  'click',
  guard(async (e) => {
    const el = e.target.closest('button');
    if (!el) return;
    if (el.hasAttribute('data-open-guide')) {
      await openUserGuide();
      return;
    }
    if (el.dataset.guideAction === 'settings') {
      await openSettings();
      return;
    }
    if (el.dataset.guideAction === 'connect') {
      await openConnections();
      return;
    }
    if (await codeTools.handleClick(el)) return;
    if (tableTools.handleClick(el)) return;
    if (el.dataset.saveAnalysisTemplate) {
      await openAnalysisTemplates(el.dataset.saveAnalysisTemplate);
      return;
    }
    if (el.dataset.openImage) {
      const id = el.dataset.openImage;
      if (!/^[a-f0-9]{32}$/.test(id)) return;
      showModal(
        tr('查看图片'),
        'IMAGE',
        `<img class="image-full-preview" src="/api/images/${id}" alt="${tr('消息附图')}"><p class="field-help">${tr('图片保存在本机；发送消息时会提供给所选模型服务。')}</p>`
      );
      return;
    }
    if (el.dataset.openSemantics) {
      await openSemantics(el.dataset.openSemantics);
      return;
    }
    if (el.dataset.previewColumn !== undefined) {
      await openFieldPreview(
        el.dataset.previewTable,
        el.dataset.previewColumn,
        el.dataset.previewModel || state.view.model?.id
      );
      return;
    }
    if (el.dataset.confirmClarification) {
      if (!canSendMessage()) return;
      const view = state.view,
        message = view.messages.find((m) => m.id === el.dataset.confirmClarification);
      if (!message?.clarification || message.clarification.answered || message !== view.messages.at(-1)) {
        toast(tr('请回复会话中的最新口径问题'));
        return;
      }
      const reply = clarificationReply(message, view.clarificationDrafts[message.id]);
      view.draft = $('#prompt').value;
      const pending = generation.send(view, {
        ...reply,
        language,
        points: view.mode === 'dax' ? view.points : [],
        image_ids: view.images.filter((img) => img.id).map((img) => img.id),
        images: view.images.filter((img) => img.id),
        llm_profile_id: state.settings.active_profile_id || null,
      });
      renderMessages(true);
      await pending;
      return;
    }
    if (el.dataset.mode) switchMode(el.dataset.mode);
    if (el.dataset.contextTab) {
      state.contextTab = el.dataset.contextTab;
      $$('[data-context-tab]').forEach((b) => {
        const active = b === el;
        b.classList.toggle('active', active);
        b.setAttribute('aria-selected', String(active));
      });
      renderContext();
    }
    if (el.dataset.prompt !== undefined) {
      const input = $('#prompt'),
        cards = modeFor(state.view.mode).cards;
      const prefixes = [...MODES[state.view.mode].cards, ...modeFor(state.view.mode, 'en').cards];
      const previous = prefixes.find((card) => input.value.startsWith(card[3]));
      const draft = previous ? input.value.slice(previous[3].length) : input.value;
      input.value = cards[Number(el.dataset.prompt)][3] + draft;
      state.view.draft = input.value;
      input.focus();
    }
    if (el.dataset.ref) {
      const input = $('#prompt');
      input.setRangeText(el.dataset.ref, input.selectionStart, input.selectionEnd, 'end');
      state.view.draft = input.value;
      contextLayout.dismissOverlay();
      input.focus();
    }
    if (el.id === 'welcome-connect' || el.id === 'rescan') await openConnections();
    if (el.id === 'manage-dictionaries') await openDictionaryLibrary();
    if (el.id === 'demo-load' || el.dataset.port || el.dataset.snapshot) {
      await selectConnection(el);
      return;
    }
    if (el.dataset.query) {
      const ctx = state.view.model.context,
        code = ctx.table_queries[el.dataset.query] ?? ctx.shared_expressions[el.dataset.query];
      showModal(
        el.dataset.query,
        'POWER QUERY',
        `${codeBlock(code, 'powerquery')}<div class="modal-actions"><button class="primary-button" data-use-query="${esc(el.dataset.query)}">${tr('在清洗助手中使用')}</button></div>`
      );
    }
    if (el.dataset.useQuery) {
      newChat('mquery');
      state.view.queryName = el.dataset.useQuery;
      $('#query-select').value = el.dataset.useQuery;
      $('#modal').close();
    }
    if (el.dataset.conversation) await openConversation(el.dataset.conversation);
    if (el.dataset.deleteConversation) {
      const id = el.dataset.deleteConversation,
        owner = workspace.find(id);
      if (owner?.busy || owner?.preparing) {
        toast(tr('此会话仍在发送或生成，完成后可删除'));
        return;
      }
      showModal(
        tr('删除此对话？'),
        'LOCAL HISTORY',
        `<p class="notice">${tr('会删除此对话及关联代码记录，已写入 Desktop 的内容不受影响。')}</p><div class="modal-actions"><button class="primary-button" id="delete-confirm">${tr('删除对话')}</button></div>`
      );
      $('#delete-confirm').onclick = guard(async () => {
        await api(`/conversations/${id}`, { method: 'DELETE' });
        workspace.remove(id);
        showView();
        $('#modal').close();
        await reloadHistory();
      });
    }
    if (el.dataset.apply) await reviewWrite(el.dataset.apply, { state, available, showModal, render });
    if (el.dataset.revalidate) {
      if (!available()) return;
      const messageId = el.dataset.revalidate,
        view = state.view;
      el.disabled = true;
      el.textContent = tr('正在 Desktop 实跑验证…');
      try {
        const result = await post(`/messages/${messageId}/validate`);
        const message = view.messages.find((m) => m.id === messageId);
        if (message) message.artifacts = result.artifacts;
        if (view.model?.id === result.model.id) view.model = result.model;
        if (state.view === view) {
          renderContext();
          renderMessages();
        }
        toast(
          result.artifacts.every((a) => a.validation.ok && a.validation.run_verified)
            ? tr('本组代码已通过 Desktop 实跑验证')
            : tr('验证已完成，请查看每段代码的结果')
        );
      } finally {
        // Reconciliation may reuse this node for the newly available write button.
        if (el.dataset.revalidate === messageId) {
          el.disabled = false;
          el.textContent = tr('重新实跑本组代码');
        }
      }
    }
  })
);

async function init() {
  void mountDesktopUpdates({
    button: $('#updates-open'),
    dialog: $('#updates-dialog'),
    api,
    post,
    notice: toast,
    hasWork: () =>
      workspace
        .all()
        .some(
          (view) =>
            view.busy ||
            view.preparing ||
            view.syncing ||
            view.changingContext ||
            view.images.some((image) => image.status === 'uploading')
        ),
    hasDraft: () =>
      Boolean($('#prompt').value.trim()) ||
      workspace.all().some((view) => view.draft.trim() || view.images.length),
  }).then((checkWhenIdle) => {
    if (checkWhenIdle) setInterval(checkWhenIdle, 6 * 60 * 60 * 1000);
  });
  // A layout read failure must not block conversations or model connection.
  void api('/ui/layout')
    .then((value) => contextLayout.restore(value))
    .catch(() => {});
  try {
    const [settings, models, conversations] = await Promise.all([
      api('/settings'),
      api('/models'),
      api('/conversations'),
    ]);
    Object.assign(state, { settings, models, conversations });
    state.view.model = models[0] || null;
    render();
    renderConnectionStatus();
    void connectionMonitor.refresh();
    for (const conversation of conversations.filter((c) => c.generating)) {
      const view = workspace.register(
        conversation,
        models.find((m) => m.id === conversation.model_id) || null
      );
      void loadConversation(view).catch((error) => toast(error.message, true));
    }
  } catch (e) {
    render();
    toast(tr('无法连接本地服务：') + e.message, true);
  }
}
setInterval(() => {
  void connectionMonitor.refresh();
}, 3000);
init();
