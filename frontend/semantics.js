import { serviceText, tr } from './i18n.js';
import { escapeHtml as esc } from './markdown.js';
import { icon } from './icons.js';
import {
  businessData,
  hasBusinessKnowledge,
  businessMarkup,
  businessEditMarkup,
  businessBody,
  targetRow,
  filterRow,
} from './business_dictionary.js';

const $ = (selector, root) => root.querySelector(selector);
const states = { confirmed: '用户已确认', candidate: '候选含义 · 待确认', unresolved: '业务含义未知' };
const kinds = { table: '表', column: '字段', measure: '度量值' };
export function semanticSummary(p) {
  const confirmed = p.entries.filter((entry) => entry.state === 'confirmed');
  return {
    status: p.status,
    stage: p.stage,
    coverage: p.coverage,
    total: p.entries.length,
    confirmed: confirmed.length,
    search_names: confirmed
      .filter((entry) => entry.business_name || entry.aliases?.length)
      .map(({ kind, table, name, business_name, aliases }) => ({
        kind,
        table,
        name,
        business_name,
        aliases,
      })),
    can_answer:
      hasBusinessKnowledge(p) ||
      p.status === 'ready' ||
      !!p.has_history ||
      p.entries.some((e) => ['candidate', 'confirmed'].includes(e.state) || e.previous_definition),
    pending: p.entries.filter((e) => e.state === 'unresolved').length,
    ...(p.dictionary_notice ? { dictionary_notice: p.dictionary_notice } : {}),
  };
}

function facts(entry) {
  const f = entry.facts;
  const items = [
    [tr('模型描述'), f.description],
    [tr('实际数据类型'), f.actual_dtype || f.dtype],
    [tr('源字段'), f.source_column],
    [tr('显示格式'), f.format_string],
    [tr('模型公式'), f.expression],
    [tr('表内字段'), f.columns?.join('、')],
  ];
  return `<details class="semantic-facts"><summary>${tr('模型事实与依据')}</summary>${
    items
      .filter(([, value]) => value)
      .map(([label, value]) => `<p><b>${label}</b><span>${esc(value)}</span></p>`)
      .join('') || `<p>${tr('模型没有提供额外描述，不能仅凭名称认定业务含义。')}</p>`
  }
    ${(f.relationships || []).map((r) => `<p><b>${tr('关系')}</b><span>${esc(r.from_table)}[${esc(r.from_column)}] → ${esc(r.to_table)}[${esc(r.to_column)}] · ${r.is_active ? tr('活动') : tr('非活动')}</span></p>`).join('')}</details>`;
}

function entryMarkup(entry) {
  return `<article class="semantic-entry" data-semantic-entry="${esc(entry.id)}"><div class="semantic-entry-heading"><strong>${esc(entry.table)}${entry.kind === 'table' ? '' : '[' + esc(entry.name) + ']'}</strong><span class="semantic-state ${entry.state}">${tr(states[entry.state] || '待确认')}</span></div>
    <p class="semantic-business-name">${tr(kinds[entry.kind])} · ${esc(entry.business_name || tr('尚无业务名称'))}${entry.role ? ' · ' + esc(entry.role) : ''}</p>
    ${entry.state === 'unresolved' && entry.change ? `<p class="semantic-change-note">${entry.change.kind === 'added' ? tr('新增条目') : tr('待更新条目')} · ${entry.change.reasons.map(esc).join('；')}</p>` : ''}
    <p class="semantic-meaning">${esc(entry.meaning || tr('业务含义尚未确定，需要结合模型描述和业务定义确认。'))}</p>
    ${entry.usage_notes ? `<p class="semantic-meaning"><b>${tr('使用说明：')}</b>${esc(entry.usage_notes)}</p>` : ''}
    ${entry.previous_definition ? `<details class="semantic-facts"><summary>${tr('查看更新前定义（仅供核对，已不作为当前确认口径）')}</summary><p>${esc(entry.previous_definition.meaning || tr('此前未定义业务含义'))}</p></details>` : ''}
    <p class="semantic-unit">${tr('单位：{0}{1}', esc(entry.unit || tr('未知 / 未确认')), entry.aliases.length ? ` ${tr('· 别名：')}` + entry.aliases.map(esc).join('、') : '')}</p>${facts(entry)}
    <div class="semantic-entry-actions"><button class="secondary-button" data-edit-semantic="${esc(entry.id)}">${tr('编辑与确认')}</button>${entry.kind === 'column' ? `<button class="text-link" data-semantic-preview="${esc(entry.id)}">${icon('eye')}${tr('查看实际取值')}</button>` : ''}</div></article>`;
}

function editMarkup(entry) {
  return `<form data-semantic-edit><button type="button" class="text-link" data-semantic-back>${icon('arrow-left')}${tr('返回词典')}</button><p class="notice">${tr('{0}{1}。保存为候选与确认定义分开处理；只有用户确认的定义才作为已确定的业务语义使用。', esc(entry.table), entry.kind === 'table' ? '' : '[' + esc(entry.name) + ']')}</p>
    <label class="form-field">${tr('业务名称')}<input name="business_name" maxlength="160" value="${esc(entry.business_name)}"></label>
    <label class="form-field">${tr('业务含义与口径')}<textarea name="meaning" maxlength="1500" rows="5">${esc(entry.meaning)}</textarea></label>
    <label class="form-field">${tr('使用说明 / 适用边界')}<textarea name="usage_notes" maxlength="1500" rows="3" placeholder="${tr('补充表的业务粒度、指标的适用场景或限制；无需重复 DAX 公式')}">${esc(entry.usage_notes || '')}</textarea></label>
    <div class="form-row"><label class="form-field">${tr('字段角色')}<input name="role" maxlength="120" placeholder="${tr('例如：成本中心编号、费用分类、业务日期')}" value="${esc(entry.role)}"></label><label class="form-field">${tr('单位 / 币种')}<input name="unit" maxlength="100" placeholder="${tr('不确定时留空')}" value="${esc(entry.unit)}"></label></div>
    <label class="form-field">${tr('业务别名（用逗号分隔）')}<input name="aliases" value="${esc(entry.aliases.join('，'))}" placeholder="${tr('例如：人员费用，人力费用')}"></label>
    ${facts(entry)}<div class="modal-actions"><button type="submit" class="secondary-button">${tr('保存为待确认')}</button><button type="button" class="primary-button" data-confirm-semantic>${tr('确认业务定义')}</button></div></form>`;
}

export async function mountSemanticWorkbench(
  root,
  { api, modelId, onUpdate = () => {}, onPreview = () => {}, onManageDictionaries = () => {} }
) {
  let profile,
    service,
    editing = null,
    filter = '',
    onlyPending = false,
    onlyChanges = false,
    busy = false,
    timer = null,
    disposed = false,
    paused = false;
  let tab = 'objects',
    renderedTab = 'objects',
    businessEditing = null;
  let pendingImport = null;
  const scroll = root.closest('dialog') || root;
  const listViews = {};
  function captureListView() {
    if (!$('[data-semantic-entries]', root)) return null;
    const entries = [...root.querySelectorAll('[data-semantic-entry]')];
    const expanded = new Map(
      entries.map((entry) => [
        entry.dataset.semanticEntry,
        [...entry.querySelectorAll('details')].flatMap((details, index) =>
          details.hasAttribute('open') ? [index] : []
        ),
      ])
    );
    const viewport = scroll.getBoundingClientRect?.();
    const anchor =
      viewport &&
      entries.find((entry) => {
        const rect = entry.getBoundingClientRect?.();
        return rect && rect.bottom > viewport.top && rect.top < viewport.bottom;
      });
    return {
      top: scroll.scrollTop || 0,
      expanded,
      deltaOpen: !!$('.semantic-delta details[open]', root),
      anchor: anchor
        ? { id: anchor.dataset.semanticEntry, offset: anchor.getBoundingClientRect().top - viewport.top }
        : null,
    };
  }
  function restoreListView(view) {
    if (!view) return;
    const entries = [...root.querySelectorAll('[data-semantic-entry]')];
    for (const entry of entries)
      for (const [index, details] of [...entry.querySelectorAll('details')].entries())
        details.toggleAttribute(
          'open',
          view.expanded.get(entry.dataset.semanticEntry)?.includes(index) || false
        );
    $('.semantic-delta details', root)?.toggleAttribute('open', view.deltaOpen);
    scroll.scrollTop = view.top;
    const anchor = view.anchor && entries.find((entry) => entry.dataset.semanticEntry === view.anchor.id);
    if (anchor)
      scroll.scrollTop +=
        anchor.getBoundingClientRect().top - scroll.getBoundingClientRect().top - view.anchor.offset;
  }
  function feedback(text, error = false) {
    const el = $('[data-semantic-feedback]', root),
      view = captureListView();
    if (el) {
      el.textContent = text;
      el.hidden = false;
      el.classList.toggle('error', error);
      restoreListView(view);
    }
  }
  function list() {
    const changed = new Set((profile.changes?.items || []).map((e) => e.id));
    const entries = profile.entries.filter(
      (e) =>
        (!onlyPending || e.state !== 'confirmed') &&
        (!onlyChanges || changed.has(e.id)) &&
        [e.table, e.name, e.business_name, e.meaning, ...e.aliases]
          .join(' ')
          .toLowerCase()
          .includes(filter.toLowerCase())
    );
    $('[data-semantic-entries]', root).innerHTML =
      entries.map(entryMarkup).join('') || `<p class="notice">${tr('没有匹配的条目。')}</p>`;
  }
  function overviewDue() {
    const overview = businessData(profile).overview;
    return (
      !overview ||
      (!overview.reviewed_at &&
        overview.state !== 'confirmed' &&
        (profile.overview_pending || overview.state === 'unresolved'))
    );
  }
  function buildDisabled() {
    return (
      profile.status === 'building' ||
      (profile.status === 'ready' && semanticSummary(profile).pending === 0 && !overviewDue())
    );
  }
  function importDisabled() {
    return profile.status === 'building' || !!editing || !!businessEditing;
  }
  function importPanel() {
    if (!pendingImport) return '';
    const { name, preview: p } = pendingImport;
    const count = p.objects + p.items + Number(p.overview);
    return `<section class="notice semantic-import-preview" aria-label="${tr('导入预览')}"><strong>${tr('导入预览 · {0}', esc(name))}</strong>
      <p>${tr('将合并 {0} 项对象定义、{1} 项业务术语或约定{2}。{3}', p.objects, p.items, p.overview ? tr('和业务总览') : '', p.unmatched ? tr('当前模型缺少 {0} 个对象，已跳过。', p.unmatched) : '')}</p>
      <p>${p.compatible ? tr('文件与当前模型匹配，本次合并的条目采用文件中的定义。') : tr('模型结构或元数据有差异，导入内容需复核。')}${p.protected ? tr('保留当前词典已有的 {0} 项定义。', p.protected) : ''}${p.review ? tr('共 {0} 项待复核。', p.review) : ''}</p>
      <button type="button" class="secondary-button" data-cancel-semantic-import>${tr('取消')}</button>
      ${count ? `<button type="button" class="primary-button" data-confirm-semantic-import>${tr('确认导入')}</button>` : ''}</section>`;
  }
  function render() {
    listViews[renderedTab] = captureListView() || listViews[renderedTab];
    const summary = semanticSummary(profile);
    const delta = profile.changes;
    const dictionaryName =
      profile.dictionary_name ||
      profile.reuse?.source_name ||
      profile.reuse?.report_name ||
      (summary.can_answer && profile.report_identity?.name);
    const canRebuild = summary.can_answer && profile.entries.length > 0;
    root.innerHTML = `<header class="semantic-model-heading"><div><span class="semantic-section-label">${tr('当前模型')}</span><h3>${esc(profile.report_identity?.name || tr('模型业务语义'))}</h3>${profile.report_identity?.path ? `<p class="semantic-model-path">${esc(profile.report_identity.path)}</p>` : ''}</div><div class="semantic-file-actions">
      <input type="file" accept=".json,application/json" data-semantic-file hidden>
      <button type="button" class="secondary-button" data-import-semantics ${importDisabled() ? 'disabled' : ''}>${tr('导入语义层')}</button>
      <a class="secondary-button" href="/api/models/${encodeURIComponent(modelId)}/semantics/export" download>${tr('导出语义层')}</a>
      <button type="button" class="secondary-button semantic-library-action" data-manage-dictionaries>${icon('book')}${tr('词典管理')}</button></div></header>
      ${importPanel()}
      <div class="semantic-status-grid ${delta ? 'has-delta' : ''}"><div class="semantic-progress"><strong>${esc(serviceText(profile.stage))}</strong><span>${tr('已梳理 {0}/{1} 项 · 用户确认 {2} 项', profile.coverage, profile.entries.length, summary.confirmed)}</span>${dictionaryName ? `<p class="semantic-binding">${tr('词典：{0}{1}', esc(dictionaryName), profile.reuse ? ` · ${profile.reuse.matched_by === 'user_selection' ? tr('手动关联') : tr('自动关联')}${profile.reuse.source_deleted ? ` ${tr('· 来源历史版本已删除，当前副本保留')}` : ''}` : '')}</p>` : ''}</div>
      ${delta ? `<div class="semantic-delta"><strong>${tr('本次模型同步')}</strong><p>${tr('保留 {0} 项 · 新增 {1} 项 · 修改或受影响 {2} 项 · 删除 {3} 项', delta.preserved, delta.added, delta.modified, delta.removed)}</p><p>${summary.can_answer ? tr('可继续提问；未知业务口径按需确认。') : tr('已有定义保留，只梳理待更新条目。')}</p>${delta.items.length ? `<details><summary>${tr('查看变更清单')}</summary><ul>${delta.items.map((item) => `<li><b>${{ added: tr('新增'), modified: tr('修改或受影响'), removed: tr('删除') }[item.change]}</b> ${esc(item.table)}${item.kind === 'table' ? '' : '[' + esc(item.name) + ']'} · ${item.reasons.map(esc).join('；')}</li>`).join('')}</ul></details>` : ''}</div>` : ''}</div>
      ${profile.error ? `<p class="semantic-error">${esc(serviceText(profile.error))}</p>` : ''}
      ${profile.dictionary_notice ? `<div class="semantic-model-notice ${['needs_update', 'unverified'].includes(profile.dictionary_notice.status) ? 'semantic-change-note' : 'semantic-current-note'}"><span>${esc(serviceText(profile.dictionary_notice.message))}</span>${profile.dictionary_notice.pending_business ? `<button type="button" class="text-link" data-review-business>${tr('复核受影响的业务记录')}</button>` : ''}</div>` : ''}
      <div class="semantic-model-tools"><button type="button" class="primary-button" data-build-semantics ${buildDisabled() ? 'disabled' : ''}>${icon('sparkles')}${profile.status === 'building' ? tr('正在后台梳理…') : buildDisabled() ? tr('对象语义已是最新') : summary.pending === 0 && overviewDue() && !profile.rebuild_remaining?.length ? tr('生成业务总览') : profile.rebuild_remaining?.length ? tr('继续全量梳理（剩余 {0} 项）', profile.pending_ids.length) : delta ? tr('仅梳理待更新 {0} 项', summary.pending) : profile.coverage > 0 ? tr('继续梳理（剩余 {0} 项）', summary.pending) : tr('开始梳理模型语义')}</button>
      ${canRebuild ? `<button type="button" class="secondary-button" data-rebuild-semantics ${profile.status === 'building' ? 'disabled' : ''}>${tr('全部重新梳理')}</button>` : ''}<span>${tr('模型服务：{0} · 生成建议后需审阅', esc(service?.name || service?.model || tr('当前模型服务')))}</span>
      <details class="semantic-help"><summary>${tr('服务与使用说明')}</summary><p>${tr('将待梳理对象的结构、公式、描述及相关关系发送至 {0} 解释含义；不修改 Desktop。对象梳理完成后，同一服务会根据对象定义和整体模型结构生成业务总览；业务术语和分析约定由你填写。', esc(service?.base_url || tr('Anthropic 官方服务')))}</p><p>${tr('语义词典保存在本机，重新打开同一报表时自动匹配并核对模型。字段名称和取值属于模型事实；自动解释是候选建议，需要你审阅。编号归属以实际查值为依据，有歧义时再确认。')}</p></details></div>
      ${canRebuild ? `<div data-rebuild-prompt class="notice" hidden><p>${tr('将把全部 {0} 个模型对象发送到当前模型服务，分批更新本词典中的自动候选。不会覆盖仍有效的人工编辑、已确认定义或业务词条。', profile.entries.length)}</p><button type="button" class="secondary-button" data-cancel-rebuild>${tr('取消')}</button> <button type="button" class="primary-button" data-confirm-rebuild>${tr('确认全部重新梳理')}</button></div>` : ''}
      <p data-semantic-feedback class="semantic-feedback" role="status" hidden></p>
      <nav class="semantic-tabs" aria-label="${tr('当前模型维护视图')}">${[
        ['overview', tr('业务总览')],
        ['business', tr('业务术语与分析约定')],
        ['objects', tr('对象词典')],
      ]
        .map(
          ([value, name]) =>
            `<button type="button" class="${tab === value ? 'primary-button' : 'secondary-button'}" data-semantic-tab="${value}" aria-pressed="${tab === value}">${name}</button>`
        )
        .join('')}</nav>
      <section data-semantic-content>${businessEditing ? businessEditMarkup(businessEditing, profile.entries) : tab !== 'objects' ? businessMarkup(profile, tab) : editing ? editMarkup(editing) : `<div class="semantic-filter"><input type="search" data-semantic-search value="${esc(filter)}" placeholder="${tr('搜索表、字段、业务名称或含义')}" aria-label="${tr('搜索语义词典')}"><label><input type="checkbox" data-semantic-pending ${onlyPending ? 'checked' : ''}>${tr('只看待确认项')}</label>${delta ? `<label><input type="checkbox" data-semantic-changes ${onlyChanges ? 'checked' : ''}>${tr('只看本次变更')}</label>` : ''}</div><div class="semantic-entry-grid" data-semantic-entries></div>`}</section>`;
    renderedTab = tab;
    if (!editing && !businessEditing) {
      if (tab === 'objects') list();
      restoreListView(listViews[tab]);
    } else scroll.scrollTop = 0;
  }
  function schedule() {
    clearTimeout(timer);
    if (!disposed && !paused && root.isConnected && profile?.status === 'building')
      timer = setTimeout(() => void refresh(), 1800);
  }
  async function refresh() {
    try {
      const next = await api(`/models/${modelId}/semantics`);
      if (disposed || paused) return;
      profile = next;
      onUpdate(semanticSummary(profile));
      // Do not rebuild an editor the user is currently typing in.
      if (!editing && !businessEditing) render();
      else {
        const el = $('.semantic-progress', root);
        el.innerHTML = `<strong>${esc(serviceText(profile.stage))}</strong><span>${tr('已梳理 {0}/{1} 项', profile.coverage, profile.entries.length)}</span>`;
      }
    } catch (error) {
      if (disposed || paused) return;
      if (!profile) root.innerHTML = `<p class="semantic-error">${esc(error.message)}</p>`;
      else feedback(error.message, true);
    }
    schedule();
  }
  async function save(confirmed) {
    const form = $('[data-semantic-edit]', root);
    if (!form.reportValidity()) return;
    const value = (name) => $(`[name="${name}"]`, form).value.trim();
    const body = {
      business_name: value('business_name'),
      meaning: value('meaning'),
      usage_notes: value('usage_notes'),
      role: value('role'),
      unit: value('unit'),
      aliases: value('aliases')
        .split(/[,，;；]/)
        .map((x) => x.trim())
        .filter(Boolean),
      confirmed,
    };
    profile = await api(`/models/${modelId}/semantics/${editing.id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
    editing = null;
    onUpdate(semanticSummary(profile));
    render();
    feedback(confirmed ? tr('业务定义已确认，后续 DAX 和分析将共同使用') : tr('已保存为待确认候选'));
    schedule();
  }
  async function run(work) {
    if (busy) return;
    busy = true;
    root.querySelectorAll('button').forEach((button) => (button.disabled = true));
    try {
      await work();
    } catch (error) {
      feedback(error.message, true);
    } finally {
      busy = false;
      root.querySelectorAll('button').forEach((button) => (button.disabled = false));
      if (profile && buildDisabled()) $('[data-build-semantics]', root).disabled = true;
      const rebuild = $('[data-rebuild-semantics]', root);
      if (rebuild) rebuild.disabled = profile.status === 'building';
      const importButton = $('[data-import-semantics]', root);
      if (importButton) importButton.disabled = importDisabled();
    }
  }
  async function previewImport(file) {
    pendingImport = null;
    $('.semantic-import-preview', root)?.remove();
    if (file.size > 64 * 1024 * 1024) throw new Error(tr('语义层文件超过 64 MB'));
    const body = await file.text();
    const preview = await api(`/models/${modelId}/semantics/import-preview`, { method: 'POST', body });
    if (disposed) return;
    pendingImport = { name: file.name, body, preview };
    render();
  }
  async function applyImport() {
    const { body, preview } = pendingImport;
    const result = await api(
      `/models/${modelId}/semantics/import?updated_at=${encodeURIComponent(preview.updated_at)}`,
      { method: 'POST', body }
    );
    if (disposed) return;
    pendingImport = null;
    profile = result.profile;
    onUpdate(semanticSummary(profile));
    render();
    schedule();
    feedback(
      tr(
        '已导入 {0} 项对象定义、{1} 项业务术语或约定{2}。',
        result.preview.objects,
        result.preview.items,
        result.preview.overview ? tr('和业务总览') : ''
      )
    );
  }
  async function saveBusiness(confirmed) {
    const form = $('[data-business-form]', root);
    if (!form.reportValidity()) return;
    const body = businessBody(form, businessEditing, confirmed);
    if (body.kind === 'convention' && !body.targets.length && !body.filters.length) {
      const hint = $('[data-required-business-mapping]', form);
      feedback(hint.textContent, true);
      hint.focus();
      return;
    }
    const path =
      businessEditing.kind === 'overview'
        ? 'overview'
        : 'items' + (businessEditing.record?.id ? '/' + businessEditing.record.id : '');
    const method = businessEditing.kind === 'overview' || businessEditing.record?.id ? 'PUT' : 'POST';
    profile = await api(`/models/${modelId}/semantics/business/${path}`, {
      method,
      body: JSON.stringify(body),
    });
    businessEditing = null;
    onUpdate(semanticSummary(profile));
    render();
    feedback(
      confirmed ? tr('业务定义已确认，将用于后续查询和回答') : tr('已保存为候选，尚不作为已确认业务条件')
    );
    schedule();
  }
  function editBusiness(kind, record = null) {
    pendingImport = null;
    businessEditing = { kind, record, revision: businessData(profile).revision };
    render();
  }
  root.onclick = (event) => {
    const button = event.target.closest('button');
    if (!button || busy) return;
    if (button.hasAttribute('data-import-semantics')) $('[data-semantic-file]', root).click();
    if (button.hasAttribute('data-cancel-semantic-import')) {
      pendingImport = null;
      render();
    }
    if (button.hasAttribute('data-confirm-semantic-import') && pendingImport) void run(applyImport);
    if (button.hasAttribute('data-manage-dictionaries')) onManageDictionaries(modelId);
    if (button.dataset.semanticTab) {
      tab = button.dataset.semanticTab;
      editing = null;
      businessEditing = null;
      render();
    }
    if (button.hasAttribute('data-review-business')) {
      tab = businessData(profile).overview?.state === 'unresolved' ? 'overview' : 'business';
      editing = null;
      businessEditing = null;
      render();
    }
    if (button.hasAttribute('data-business-overview'))
      editBusiness('overview', businessData(profile).overview);
    if (button.dataset.businessNew) editBusiness(button.dataset.businessNew);
    if (button.dataset.businessEdit) {
      const record = businessData(profile).items.find((e) => e.id === button.dataset.businessEdit);
      if (record) editBusiness(record.kind, record);
    }
    if (button.hasAttribute('data-business-back')) {
      businessEditing = null;
      render();
      void refresh();
    }
    if (button.hasAttribute('data-business-confirm')) void run(() => saveBusiness(true));
    if (button.hasAttribute('data-business-add-target'))
      $('[data-business-targets]', root).insertAdjacentHTML('beforeend', targetRow(profile.entries));
    if (button.hasAttribute('data-business-add-filter'))
      $('[data-business-filters]', root).insertAdjacentHTML('beforeend', filterRow(profile.entries));
    if (button.hasAttribute('data-business-remove')) button.closest('.business-reference').remove();
    if (button.dataset.businessDelete) {
      button.textContent = tr('确认删除此词条');
      button.dataset.businessDeleteConfirm = button.dataset.businessDelete;
      delete button.dataset.businessDelete;
    } else if (button.dataset.businessDeleteConfirm)
      void run(async () => {
        profile = await api(
          `/models/${modelId}/semantics/business/items/${button.dataset.businessDeleteConfirm}?revision=${businessData(profile).revision}`,
          { method: 'DELETE' }
        );
        onUpdate(semanticSummary(profile));
        render();
        feedback(tr('业务词条已删除'));
      });
    if (button.dataset.editSemantic) {
      pendingImport = null;
      editing = profile.entries.find((e) => e.id === button.dataset.editSemantic);
      render();
    }
    if (button.hasAttribute('data-semantic-back')) {
      editing = null;
      render();
    }
    if (button.hasAttribute('data-confirm-semantic')) void run(() => save(true));
    if (button.hasAttribute('data-rebuild-semantics')) $('[data-rebuild-prompt]', root).hidden = false;
    if (button.hasAttribute('data-cancel-rebuild')) $('[data-rebuild-prompt]', root).hidden = true;
    const full = button.hasAttribute('data-confirm-rebuild');
    if ((full || button.hasAttribute('data-build-semantics')) && profile.status !== 'building')
      void run(async () => {
        if (full) $('[data-rebuild-prompt]', root).hidden = true;
        profile = await api(`/models/${modelId}/semantics/build`, {
          method: 'POST',
          body: JSON.stringify({
            llm_profile_id: service?.active_profile_id || null,
            ...(full ? { mode: 'full' } : {}),
          }),
        });
        onUpdate(semanticSummary(profile));
        if (!editing && !businessEditing) render();
        schedule();
      });
    if (button.dataset.semanticPreview) {
      const entry = profile.entries.find((e) => e.id === button.dataset.semanticPreview);
      onPreview(entry.table, entry.name, modelId);
    }
  };
  root.oninput = (event) => {
    if (event.target.matches('[data-semantic-search]')) {
      filter = event.target.value;
      list();
    }
  };
  root.onchange = (event) => {
    if (event.target.matches('[data-semantic-pending]')) {
      onlyPending = event.target.checked;
      list();
    }
    if (event.target.matches('[data-semantic-changes]')) {
      onlyChanges = event.target.checked;
      list();
    }
    if (event.target.matches('[data-semantic-file]')) {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (file && !importDisabled()) void run(() => previewImport(file));
    }
    if (event.target.matches('[data-business-scope]'))
      $('[data-business-scope-objects]', root).hidden = event.target.value !== 'objects';
  };
  root.onsubmit = (event) => {
    event.preventDefault();
    if (businessEditing) void run(() => saveBusiness(false));
    else if (editing) void run(() => save(false));
  };
  root.innerHTML = `<p class="notice">${tr('正在读取模型语义词典…')}</p>`;
  service = await api('/settings');
  await refresh();
  return {
    pause() {
      paused = true;
      clearTimeout(timer);
    },
    resume() {
      if (!disposed) {
        paused = false;
        schedule();
      }
    },
    refresh,
    dispose() {
      disposed = true;
      clearTimeout(timer);
    },
  };
}
