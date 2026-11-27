import { $, $$ } from './ui.js';
import { language, serviceText, tr } from './i18n.js';
import { icon } from './icons.js';
import { escapeHtml as esc } from './html.js';
import { MODES, modeFor } from './modes.js';
import { connectionPresentation } from './connections.js';
import { modelTree } from './model_tree.js';

export function createWorkspacePanels(state, workspace, renderBusy) {
  function renderHistory() {
    const needle = $('#history-search').value.toLowerCase();
    const rows = state.conversations.filter((c) => c.title.toLowerCase().includes(needle));
    $('#history-list').innerHTML = rows.length
      ? rows
          .map((c) => {
            const view = workspace.find(c.id),
              busy = view?.busy || (c.generating && !view?.loaded);
            const current = state.view.conversation?.id === c.id;
            const last = view?.messages.at(-1),
              pending = last?.status === 'complete' && last.clarification && !last.clarification.answered;
            const badge = busy
              ? `<small class="history-task running">${tr('生成中')}</small>`
              : last?.analysis_resume
                ? `<small class="history-task clarification">${tr('待继续')}</small>`
                : pending
                  ? `<small class="history-task clarification">${tr('待确认')}</small>`
                  : view?.attention
                    ? `<small class="history-task ${view.attention}">${view.attention === 'error' ? tr('查看错误') : view.attention === 'cancelled' ? tr('已中止') : tr('已完成')}</small>`
                    : '';
            return `<div class="history-item ${current ? 'active' : ''}"><button class="history-name" data-conversation="${c.id}"${current ? ' aria-current="true"' : ''} title="${esc(c.title)}">${icon(MODES[c.mode]?.historyIcon || 'chat')}<span>${esc(c.title)}</span>${badge}</button><button class="icon-button" data-delete-conversation="${c.id}" aria-label="${tr('删除 {0}', esc(c.title))}" ${busy || view?.preparing ? `disabled title="${tr('发送或生成完成后可删除')}"` : ''}>${icon('trash')}</button></div>`;
          })
          .join('')
      : '<p class="muted history-empty">' +
        (needle ? tr('没有匹配的对话') : `${tr('还没有对话')}<br>${tr('从一个数据问题开始吧')}`) +
        '</p>';
  }
  function renderBackgroundTasks() {
    const tasks = workspace.all().filter((view) => view !== state.view && view.busy);
    const banner = $('#background-generations');
    banner.hidden = !tasks.length;
    banner.innerHTML = tasks.length
      ? `<span class="background-dot"></span><span>${tr('{0} 个会话正在后台生成', tasks.length)}</span>${tasks
          .filter((v) => v.conversation)
          .slice(0, 2)
          .map(
            (view) =>
              `<button data-conversation="${view.conversation.id}" title="${esc(view.conversation.title)}">${tr('查看：{0}', esc(view.conversation.title))}</button>`
          )
          .join('')}`
      : '';
  }
  function renderContext() {
    const model = state.view.model,
      ctx = model?.context;
    $('#context-panel').classList.toggle('has-model', !!model);
    const search = $('#field-search');
    search.disabled = !model || !!model.snapshot_released;
    const searchLabel = {
      tables: tr('搜表、字段、度量值或业务别名'),
      relations: tr('搜索关系中的表或字段'),
      queries: tr('搜索 Power Query 查询'),
    }[state.contextTab];
    search.placeholder = searchLabel;
    search.setAttribute('aria-label', searchLabel);
    if (model?.snapshot_released) {
      $('#model-summary').innerHTML =
        `<div class="model-name">${esc(model.name)}</div><div class="model-state">${tr('快照已释放')}</div>`;
      $('#model-tree').innerHTML =
        `<p class="history-empty muted">${tr('历史消息和结果仍可查看。打开对应报表并点击“同步模型”，即可重新读取字段。')}</p>`;
      return;
    }
    $('#model-summary').innerHTML = model
      ? `<div class="model-name">${esc(model.name)}</div><div class="model-state">${model.live ? tr('● 本机实时连接') : model.source === 'demo' ? tr('○ 示例数据结构 · 仅供体验') : tr('○ 离线模型快照')}</div><div class="model-counts"><span><b>${Object.keys(ctx.tables).length}</b>${tr('表')}</span><span><b>${Object.values(ctx.tables).flat().length}</b>${tr('字段')}</span><span><b>${ctx.measures.length}</b>${tr('度量值')}</span></div>`
      : '';
    if (model?.source === 'desktop')
      $('#model-summary').insertAdjacentHTML(
        'beforeend',
        `<button class="text-link semantic-open-button" data-open-semantics="${esc(model.id)}">${icon('book')}${tr('模型语义层')} <small>${['needs_update', 'unverified'].includes(model.semantics?.dictionary_notice?.status) ? esc(serviceText(model.semantics.dictionary_notice.message)) : model.semantics?.status === 'ready' ? tr('已梳理 · 可审阅定义') : model.semantics?.can_answer ? tr('已有语义可用 · 更新可稍后处理') : model.semantics?.status === 'building' ? tr('正在后台梳理') : tr('需要先梳理语义')}</small></button>`
      );
    if (!model) {
      $('#model-tree').innerHTML =
        `<div class="model-empty"><div class="empty-model-icon">${icon('database')}</div><p>${tr('连接报表后，可在这里查看字段并插入引用。')}</p></div>`;
      return;
    }
    renderConnectionStatus();
    renderModelTree();
  }
  function renderModelTree() {
    const model = state.view.model;
    if (!model || model.snapshot_released) return;
    const ctx = model.context,
      needle = $('#field-search').value.trim().toLowerCase();
    if (state.contextTab === 'tables') {
      $('#model-tree').innerHTML = modelTree(ctx, needle, model.semantics?.search_names);
    } else if (state.contextTab === 'relations') {
      $('#model-tree').innerHTML =
        ctx.relationships
          .filter((r) =>
            (r.from_table + r.to_table + r.from_column + r.to_column).toLowerCase().includes(needle)
          )
          .map(
            (r) =>
              `<div class="relation-item">${esc(r.from_table)}[${esc(r.from_column)}]${icon('arrow-right')}${esc(r.to_table)}[${esc(r.to_column)}]<span>${r.cross_filter === 'both' ? tr('双向筛选') : tr('单向筛选')} · ${r.is_active ? tr('活动关系') : tr('非活动关系')}</span></div>`
          )
          .join('') || `<p class="history-empty muted">${tr('暂无匹配的关系')}</p>`;
    } else {
      const queries = { ...ctx.table_queries, ...ctx.shared_expressions };
      $('#model-tree').innerHTML =
        Object.keys(queries)
          .filter((n) => n.toLowerCase().includes(needle))
          .map(
            (name) =>
              `<button class="query-button" data-query="${esc(name)}">${icon('code')}${esc(name)}</button>`
          )
          .join('') || `<p class="history-empty muted">${tr('模型中暂无可用 M 查询')}</p>`;
    }
  }
  function renderConnectionStatus() {
    const model = state.view.model,
      view = connectionPresentation(model);
    $('#connection-label').textContent = view.label;
    $('#connect-open').title = view.detail;
    $('.status-dot').classList.toggle('connected', view.kind === 'connected');
    $('.status-dot').classList.toggle('reconnecting', view.kind === 'reconnecting');
    $('.status-dot').classList.toggle('warning', view.kind === 'warning');
    $('#composer-context').textContent = model ? model.name + ' · ' + view.context : view.context;
    const status = $('.model-state');
    if (status) status.textContent = (view.kind === 'connected' ? '● ' : '○ ') + (view.state || view.detail);
  }

  function renderControls() {
    $('#template-return').hidden = !state.view.templateReturn;
    const query = state.view.queryName;
    $('#query-select').hidden = state.view.mode !== 'mquery';
    const ctx = state.view.model?.context;
    $('#query-select').innerHTML =
      Object.keys({ ...ctx?.table_queries, ...ctx?.shared_expressions })
        .map((t) => `<option value="${esc(t)}">${esc(t)}</option>`)
        .join('') || `<option value="">${tr('没有可用查询')}</option>`;
    if (state.view.conversation?.query_name) $('#query-select').value = state.view.conversation.query_name;
    else if ([...$('#query-select').options].some((o) => o.value === query)) $('#query-select').value = query;
    state.view.queryName = $('#query-select').value;
    $('#calibration-open').hidden = state.view.mode !== 'dax' || !state.view.model;
    $('#calibration-label').textContent = state.view.points.length
      ? state.view.points[0].filters.length
        ? tr('校准 · {0} 个条件', state.view.points[0].filters.length)
        : tr('校准 · 全模型')
      : tr('校准');
    const strength = state.settings.reasoning_control?.supported
      ? state.settings.reasoning_level === 'high'
        ? tr('高强度')
        : tr('低强度')
      : state.settings.reasoning_control
        ? tr('强度不可调')
        : '';
    $('#active-llm').textContent = state.settings.model
      ? state.settings.model + (strength ? ` · ${strength}` : '')
      : tr('配置大模型');
    $('#model-settings-chip').title = state.settings.name
      ? tr('{0} · 点击切换或添加模型', state.settings.name)
      : tr('点击添加模型');
    $('#sidebar-model').textContent = state.settings.model || tr('连接你的大模型服务器');
    const mode = modeFor(state.view.mode);
    $('#page-title').textContent = mode.title;
    $('#prompt').placeholder = mode.placeholder;
    $('#conversation-caption').textContent = state.view.conversation?.title || tr('新对话');
    $('#context-strip').hidden = !state.view.model;
    const actions = $('#prompt-actions');
    if (actions.dataset.modeFor !== state.view.mode || actions.dataset.language !== language) {
      actions.dataset.modeFor = state.view.mode;
      actions.dataset.language = language;
      actions.innerHTML = mode.cards
        .map(
          ([symbol, label, description], i) =>
            `<button type="button" data-prompt="${i}" title="${esc(description)}">${icon(symbol)}${esc(label)}</button>`
        )
        .join('');
    }
    $('.composer-note').innerHTML =
      '<span class="tiny-dot"></span>' +
      (state.view.mode === 'analysis'
        ? tr('只读查询 · 查询结果用于 AI 分析 · 依据和结果本地留存')
        : tr('模型在本机读取 · AI 在服务器推理 · 写入前由你确认'));
    $$('.nav-item').forEach((el) => el.classList.toggle('active', el.dataset.mode === state.view.mode));
    renderBusy();
  }

  return {
    renderControls,
    renderHistory,
    renderBackgroundTasks,
    renderContext,
    renderModelTree,
    renderConnectionStatus,
  };
}
