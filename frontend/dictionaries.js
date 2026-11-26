import { serviceText, tr } from './i18n.js';
import { escapeHtml as esc } from './markdown.js';
import { icon } from './icons.js';

const $ = (selector, root) => root.querySelector(selector);
const names = { added: '新增', modified: '修改或受影响', removed: '删除' };
const savedFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});
const savedTime = (value) => {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return { date: String(value || tr('时间未记录')), time: '' };
  const parts = Object.fromEntries(savedFormatter.formatToParts(date).map((p) => [p.type, p.value]));
  return { date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` };
};

export async function mountDictionaryLibrary(
  root,
  { api, modelId = null, onAssociate = () => {}, onDeleted = () => {}, onBack = null }
) {
  let rows = [],
    targetBusy = false,
    filter = '',
    preview = null,
    confirmDelete = null,
    renaming = null,
    busy = false,
    disposed = false,
    message = '',
    isError = false,
    listView = null;
  const scroll = root.closest('dialog') || root;
  function capture() {
    const cards = [...root.querySelectorAll('[data-dictionary-card]')],
      viewport = scroll.getBoundingClientRect?.();
    const anchor =
      viewport &&
      cards.find((card) => {
        const r = card.getBoundingClientRect();
        return r.bottom > viewport.top && r.top < viewport.bottom;
      });
    return {
      top: scroll.scrollTop || 0,
      groups: new Set(
        [...root.querySelectorAll('[data-dictionary-group]')].map((e) => e.dataset.dictionaryGroup)
      ),
      expanded: new Set(
        [...root.querySelectorAll('[data-dictionary-group][open]')].map((e) => e.dataset.dictionaryGroup)
      ),
      anchor: anchor
        ? { id: anchor.dataset.dictionaryCard, offset: anchor.getBoundingClientRect().top - viewport.top }
        : null,
    };
  }
  function restore(view) {
    if (!view) return;
    root.querySelectorAll('[data-dictionary-group]').forEach((e) => {
      if (view.groups.has(e.dataset.dictionaryGroup))
        e.toggleAttribute('open', view.expanded.has(e.dataset.dictionaryGroup));
    });
    scroll.scrollTop = view.top;
    const anchor = [...root.querySelectorAll('[data-dictionary-card]')].find(
      (e) => e.dataset.dictionaryCard === view.anchor?.id
    );
    if (anchor)
      scroll.scrollTop +=
        anchor.getBoundingClientRect().top - scroll.getBoundingClientRect().top - view.anchor.offset;
  }
  function dictionaryName(row) {
    let name;
    if (renaming?.id === row.model_id) {
      name = [
        '<form class="dictionary-name-form" data-dictionary-rename-form>',
        `<input data-dictionary-name-input aria-label="${tr('词典名称')}" value="${esc(renaming.name)}" maxlength="160" required autocomplete="off">`,
        `<button type="submit" aria-label="${tr('保存词典名称')}" title="${tr('保存名称')}">${icon('check')}</button>`,
        `<button type="button" data-dictionary-name-cancel aria-label="${tr('取消重命名')}" title="${tr('取消')}">${icon('close')}</button></form>`,
      ].join('');
    } else {
      name = [
        `<strong>${esc(row.name)}</strong><button type="button" class="dictionary-name-edit" data-dictionary-rename="${esc(row.model_id)}" `,
        `aria-label="${tr('重命名 {0}', esc(row.name))}" title="${tr('重命名词典')}" ${row.busy ? 'disabled' : ''}>${icon('edit')}</button>`,
      ].join('');
    }
    if (row.is_current) return name + `<span class="dictionary-badge current">${tr('当前词典')}</span>`;
    if (row.busy) return name + `<span class="dictionary-badge running">${tr('正在使用')}</span>`;
    return name;
  }

  function card(row) {
    const savedAt = row.saved_at || row.updated_at,
      when = savedTime(savedAt),
      state = row.dictionary_notice || {},
      notes = [];
    const warning = ['needs_update', 'unverified'].includes(state.status);
    if (state.status === 'needs_update') {
      if (state.pending_objects) notes.push(tr('{0} 项对象待更新', state.pending_objects));
      if (state.pending_business) notes.push(tr('{0} 项业务待复核', state.pending_business));
      if (!notes.length && state.message) notes.push(serviceText(state.message));
    } else if (state.status === 'unverified') notes.push(tr('元数据待核对'));
    if (modelId && !row.is_current)
      notes.push(
        row.same_schema
          ? tr('与当前模型结构一致')
          : tr('结构存在差异，共有 {0} 个同名对象', row.shared_objects ?? 0)
      );
    const canAssociate = modelId && !row.is_current;
    return `<article class="dictionary-row ${row.is_current ? 'is-current' : ''}" data-dictionary-card="${esc(row.model_id)}">
      <div class="dictionary-identity"><span class="dictionary-mark">${icon('book')}</span><div class="dictionary-identity-text">
        <div class="dictionary-name-line">${dictionaryName(row)}</div>
        <p class="dictionary-path" title="${esc([row.report_name, row.path].filter(Boolean).join(' · ') || tr('未记录报表路径'))}">${row.report_name && row.report_name !== row.name ? `${tr('报表 {0} ·', esc(row.report_name))} ` : ''}${esc(row.path || tr('未记录报表路径'))}</p>
        ${notes.length ? `<p class="dictionary-health ${warning ? 'needs-review' : ''}">${warning ? '<span class="dictionary-health-dot"></span>' : ''}${notes.map(esc).join(' · ')}</p>` : ''}</div></div>
      <div class="dictionary-metrics"><div><strong>${row.coverage}<small>/${row.total}</small></strong><span>${tr('对象已梳理')}</span></div><div><strong>${row.business_count}</strong><span>${tr('业务记录')}</span></div><div><strong>${row.confirmed}</strong><span>${tr('已确认')}</span></div></div>
      <time class="dictionary-saved" datetime="${esc(savedAt)}" title="${esc(savedAt)}"><span>${esc(when.date)}</span><small>${esc(when.time)}</small></time>
      <div class="dictionary-actions">${
        row.is_current
          ? `<span class="dictionary-in-use">${icon('check')}${tr('当前模型使用中')}</span>`
          : `${canAssociate ? `<button type="button" class="dictionary-associate-button" data-dictionary-preview="${esc(row.model_id)}" ${row.busy || targetBusy ? 'disabled' : ''} ${row.busy || targetBusy ? `title="${tr('生成或梳理完成后可关联')}"` : ''}>${tr('预览并关联')}${icon('arrow-right')}</button>` : ''}<button type="button" class="dictionary-delete-button" data-dictionary-delete="${esc(row.model_id)}" aria-label="${tr('删除 {0} 的历史版本', esc(row.name))}" title="${tr('删除历史版本')}" ${row.busy ? 'disabled' : ''}>${icon('trash')}</button>`
      }</div>
      ${confirmDelete === row.model_id ? `<div class="dictionary-delete-panel"><div>${icon('info')}<p><strong>${tr('删除这个历史版本？')}</strong><span>${tr('仅删除此版本的业务定义。模型快照、聊天记录和其他模型已关联的副本会保留。')}</span></p></div><div><button type="button" class="secondary-button" data-dictionary-delete-cancel>${tr('取消')}</button><button type="button" class="secondary-button dictionary-danger" data-dictionary-delete-confirm="${esc(row.model_id)}">${tr('确认删除此版本')}</button></div></div>` : ''}</article>`;
  }
  function list() {
    const selected = rows.filter((r) =>
      [
        r.name,
        r.report_name,
        r.path,
        r.saved_at || r.updated_at,
        ...Object.values(savedTime(r.saved_at || r.updated_at)),
      ]
        .join(' ')
        .toLowerCase()
        .includes(filter.toLowerCase())
    );
    const groups = new Map();
    for (const row of selected) {
      const key = row.group || row.model_id;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    }
    $('[data-dictionary-count]', root).textContent = tr(
      '{0} 份词典 · {1} 个版本',
      groups.size,
      selected.length
    );
    $('[data-dictionary-list]', root).innerHTML = selected.length
      ? `<div class="dictionary-list-head" aria-hidden="true"><span>${tr('业务词典')}</span><span>${tr('内容概况')}</span><span>${tr('保存时间 · 北京时间')}</span><span>${tr('操作')}</span></div>${[
          ...groups,
        ]
          .sort((a, b) => Number(b[1].some((r) => r.is_current)) - Number(a[1].some((r) => r.is_current)))
          .map(([key, versions]) => {
            const primary = versions.find((r) => r.is_current) || versions[0],
              older = versions.filter((r) => r !== primary);
            return `<section class="dictionary-report" data-dictionary-report="${esc(key)}">${card(primary)}${older.length ? `<details class="dictionary-history" data-dictionary-group="${esc(key)}"><summary>${icon('chevron-right')}${tr('查看其他 {0} 个版本', older.length)}</summary><div class="dictionary-history-rows">${older.map(card).join('')}</div></details>` : ''}</section>`;
          })
          .join('')}`
      : `<div class="dictionary-empty">${icon('book')}<strong>${filter ? tr('没有匹配的历史业务词典') : tr('还没有保存的业务词典')}</strong><p>${filter ? tr('试试其他名称或路径。') : tr('建立模型语义后，词典会自动保存在这里。')}</p></div>`;
  }
  function previewMarkup() {
    const c = preview.changes || {},
      items = c.items || [];
    return `<section class="dictionary-preview" data-dictionary-preview-panel><button type="button" class="dictionary-back" data-dictionary-preview-back>${icon('arrow-left')}${tr('返回历史列表')}</button>
      <div class="dictionary-preview-heading"><span class="dictionary-mark">${icon('book')}</span><div><span>${tr('关联业务词典')}</span><h3>${esc(preview.source_name)}</h3></div></div>
      <div class="dictionary-diff-metrics">${[
        ['preserved', tr('保留')],
        ['added', tr('新增')],
        ['modified', tr('修改或受影响')],
        ['removed', tr('删除')],
      ]
        .map(([key, label]) => `<div><strong>${c[key] || 0}</strong><span>${label}</span></div>`)
        .join('')}</div>
      ${preview.dictionary_notice?.message ? `<p class="dictionary-preview-note">${icon('info')}${esc(serviceText(preview.dictionary_notice.message))}</p>` : ''}
      <p class="dictionary-preview-copy">${tr('当前已审阅的 {0} 项对象定义优先保留。可关联 {1} 项业务总览／术语／约定，受影响内容将标为待复核。', preview.preserved_reviews || 0, preview.business_records || 0)}</p>
      ${preview.metadata_status !== 'read' ? `<p class="dictionary-preview-note">${tr('完整模型元数据尚未核对，关联时会重新检查；实际差异可能增加。')}</p>` : ''}
      ${items.length ? `<details class="dictionary-differences"><summary>${tr('查看具体差异（{0} 项）', items.length)}${icon('chevron-down')}</summary><ul>${items.map((item) => `<li><span>${tr(names[item.change] || '变更')}</span><div><strong>${esc(item.table)}${item.kind === 'table' ? '' : '[' + esc(item.name) + ']'}</strong><p>${(item.reasons || []).map(esc).join('；')}</p></div></li>`).join('')}</ul></details>` : `<p class="dictionary-preview-copy">${tr('未发现对象结构或定义依赖变化。')}</p>`}
      <div class="dictionary-preview-footer"><span>${tr('仅关联本地定义；新增或受影响对象需更新，不调用模型服务。')}</span><button type="button" class="primary-button" data-dictionary-associate>${tr('确认关联并核对词典')}${icon('arrow-right')}</button></div></section>`;
  }
  function render(view = capture()) {
    if (disposed || !root.isConnected) return;
    root.innerHTML = `<div class="dictionary-page-top">${onBack ? `<button type="button" class="dictionary-back" data-dictionary-back-to-model>${icon('arrow-left')}${tr('返回当前模型')}</button>` : `<span>${tr('本地业务知识库')}</span>`}<span class="dictionary-page-caption">${tr('保存业务定义，管理历史版本')}</span></div>
      <p class="semantic-feedback ${isError ? 'error' : ''}" role="status" ${message ? '' : 'hidden'}>${esc(message)}</p>
      ${preview ? previewMarkup() : `<div class="dictionary-toolbar"><label class="dictionary-search">${icon('search')}<input type="search" data-dictionary-search value="${esc(filter)}" placeholder="${tr('搜索词典名称、路径或保存时间')}" aria-label="${tr('搜索历史业务词典')}"></label><span class="dictionary-result-count" data-dictionary-count></span><button type="button" class="dictionary-refresh" data-dictionary-refresh>${icon('refresh')}${tr('刷新列表')}</button></div><div class="dictionary-list" data-dictionary-list></div>`}`;
    if (!preview) list();
    restore(view);
    if (busy) root.querySelectorAll('button').forEach((b) => (b.disabled = true));
  }
  async function load() {
    const data = await api(
      '/semantic-dictionaries' + (modelId ? '?current_model_id=' + encodeURIComponent(modelId) : '')
    );
    if (disposed) return;
    rows = data.dictionaries;
    targetBusy = !!data.target_busy;
  }
  async function run(work) {
    if (busy || disposed) return;
    busy = true;
    root.querySelectorAll('button').forEach((b) => (b.disabled = true));
    try {
      await work();
      isError = false;
    } catch (error) {
      message = error.message;
      isError = true;
    } finally {
      busy = false;
      if (!disposed) render();
    }
  }
  root.onclick = (event) => {
    const button = event.target.closest('button');
    if (!button || busy || disposed) return;
    event.stopPropagation();
    if (button.hasAttribute('data-dictionary-back-to-model')) {
      onBack?.();
      return;
    }
    if (button.hasAttribute('data-dictionary-refresh'))
      void run(async () => {
        await load();
        confirmDelete = null;
        if (renaming) {
          const row = rows.find((r) => r.model_id === renaming.id);
          if (row) renaming.updated_at = row.updated_at;
          else renaming = null;
        }
        message = tr('历史词典列表已刷新');
      });
    if (button.dataset.dictionaryRename) {
      const row = rows.find((r) => r.model_id === button.dataset.dictionaryRename);
      renaming = { id: row.model_id, name: row.name, updated_at: row.updated_at };
      confirmDelete = null;
      message = '';
      isError = false;
      render();
      const input = $('[data-dictionary-name-input]', root);
      input?.focus();
      input?.select?.();
    }
    if (button.hasAttribute('data-dictionary-name-cancel')) {
      renaming = null;
      message = '';
      isError = false;
      render();
    }
    if (button.dataset.dictionaryPreview) {
      listView = capture();
      void run(async () => {
        const data = await api(
          `/models/${modelId}/semantics/reuse-preview?source_model_id=${encodeURIComponent(button.dataset.dictionaryPreview)}`
        );
        if (disposed) return;
        preview = data;
        message = '';
        render();
        scroll.scrollTop = 0;
      });
    }
    if (button.hasAttribute('data-dictionary-preview-back')) {
      preview = null;
      message = '';
      render(listView);
    }
    if (button.hasAttribute('data-dictionary-associate'))
      void run(async () => {
        const next = await api(`/models/${modelId}/semantics/reuse`, {
          method: 'POST',
          body: JSON.stringify({
            source_model_id: preview.source_model_id,
            source_updated_at: preview.source_updated_at,
          }),
        });
        if (!disposed) await onAssociate(next);
      });
    if (button.dataset.dictionaryDelete) {
      confirmDelete = button.dataset.dictionaryDelete;
      renaming = null;
      render();
    }
    if (button.hasAttribute('data-dictionary-delete-cancel')) {
      confirmDelete = null;
      render();
    }
    if (button.dataset.dictionaryDeleteConfirm)
      void run(async () => {
        const row = rows.find((r) => r.model_id === button.dataset.dictionaryDeleteConfirm);
        const query = new URLSearchParams({ updated_at: row.updated_at });
        if (modelId) query.set('current_model_id', modelId);
        const result = await api(`/semantic-dictionaries/${encodeURIComponent(row.model_id)}?${query}`, {
          method: 'DELETE',
        });
        if (disposed) return;
        confirmDelete = null;
        await load();
        message = serviceText(result.detail);
        await onDeleted(row.model_id);
      });
  };
  root.oninput = (event) => {
    if (event.target.matches('[data-dictionary-search]')) {
      filter = event.target.value;
      list();
    }
    if (event.target.matches('[data-dictionary-name-input]') && renaming) renaming.name = event.target.value;
  };
  root.onkeydown = (event) => {
    if (event.key === 'Escape' && renaming) {
      event.preventDefault();
      event.stopPropagation();
      renaming = null;
      message = '';
      isError = false;
      render();
    }
  };
  root.onsubmit = (event) => {
    if (!event.target.matches('[data-dictionary-rename-form]')) return;
    event.preventDefault();
    event.stopPropagation();
    if (busy || !renaming || event.target.reportValidity?.() === false) return;
    const name = $('[data-dictionary-name-input]', root).value.trim();
    renaming.name = name;
    if (!name) {
      message = tr('请填写词典名称');
      isError = true;
      render();
      return;
    }
    void run(async () => {
      await api(`/semantic-dictionaries/${encodeURIComponent(renaming.id)}/name`, {
        method: 'PUT',
        body: JSON.stringify({ name, updated_at: renaming.updated_at }),
      });
      if (disposed) return;
      renaming = null;
      await load();
      message = tr('词典名称已更新');
    });
  };
  root.innerHTML = `<p class="notice">${tr('正在读取历史业务词典…')}</p>`;
  try {
    await load();
  } catch (error) {
    message = error.message;
    isError = true;
  }
  render();
  return {
    dispose() {
      disposed = true;
    },
    refresh: () => run(load),
  };
}
