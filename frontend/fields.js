import { language, serviceText, tr } from './i18n.js';
import { escapeHtml as esc } from './markdown.js';
import { icon } from './icons.js';
import { codeBlock } from './code_blocks.js';

const number = (value) => (value == null ? tr('未统计') : Number(value).toLocaleString(language));
export function fieldValue(entry) {
  if (entry.nonfinite) return `<span class="field-value-null">${tr('非有限数值')}</span>`;
  if (entry.value === null) return `<span class="field-value-null">${tr('空值 BLANK()')}</span>`;
  if (entry.value === '') return `<span class="field-value-null">${tr('空字符串 ""')}</span>`;
  return (
    esc(String(entry.value)) +
    (entry.value_truncated ? `<span class="field-value-null">${tr('…（值已截断）')}</span>` : '')
  );
}

function statistics(profile) {
  const stats = profile.statistics;
  const pct = stats.row_count ? ` · ${((100 * stats.blank_count) / stats.row_count).toFixed(1)}%` : '';
  return `<div class="field-statistics"><div><span>${tr('总行数')}</span><strong>${number(stats.row_count)}</strong></div>
    <div><span>${tr('不同取值（含空值）')}</span><strong>${number(stats.distinct_count)}</strong></div>
    <div><span>${tr('模型空值{0}', pct)}</span><strong>${number(stats.blank_count)}</strong></div>
    <div><span>${tr('空字符串')}</span><strong>${number(stats.empty_string_count)}</strong></div></div>`;
}

export function fieldProfile(profile, { compact = false } = {}) {
  const values = compact ? profile.values.slice(0, 8) : profile.values,
    stats = profile.statistics;
  return `<div class="field-profile">
    <p class="field-preview-scope">${esc(serviceText(profile.scope))}</p>${statistics(profile)}
    <div class="field-samples"><span>${tr('取值样例')}</span>${profile.samples.map((entry) => `<span class="field-sample">${fieldValue(entry)}</span>`).join('') || `<small>${tr('此字段没有可显示的取值')}</small>`}</div>
    ${profile.search_terms.length ? `<p class="field-preview-note">${tr('搜索：{0} · 匹配 {1} 个不同取值。统计卡片和样例仍为全模型范围。', profile.search_terms.map(esc).join(' / '), number(stats.matched_distinct_count))}</p>` : ''}
    ${
      values.length
        ? `<div class="field-value-table"><table><thead><tr><th>${tr('取值')}</th><th>${tr('出现行数')}</th><th>${tr('占总行数')}</th></tr></thead><tbody>${values.map((entry) => `<tr><td>${fieldValue(entry)}</td><td>${number(entry.count)}</td><td>${stats.row_count ? ((100 * entry.count) / stats.row_count).toFixed(2) + '%' : '—'}</td></tr>`).join('')}</tbody></table></div>`
        : `<p class="field-preview-note">${profile.search_terms.length ? tr('未找到匹配取值，可清空搜索或尝试其他同义词。') : tr('此字段没有可显示的取值。')}</p>`
    }
    <p class="field-preview-note">${tr('按出现次数排序。{0}{1}', profile.truncated ? tr('结果已截断，不代表全部取值。') : tr('本次返回 {0} 个{1}不同取值。', profile.values.length, profile.search_terms.length ? tr('匹配的') : ''), values.length < profile.values.length ? ` ${tr('此处预览前 {0} 项。', values.length)}` : '')}</p>
    <p class="field-preview-time">${tr('本机实际读取 · {0} · {1} 秒', esc(profile.checked_at), esc(profile.seconds))}</p>
  </div>`;
}

function exactEvidence(record, batch, queryLabel) {
  const p = record.profile;
  const values = p.values.map((v) => fieldValue(v)).join('、');
  return `<p class="field-preview-note">${tr('精确核对 {0}', p.search_terms.map(esc).join('、'))}</p>
    <p class="field-preview-note">${values ? `${tr('实际命中')} ` + values : tr('该字段未命中本次核对的值。')}</p>
    <p class="field-preview-scope">${esc(serviceText(p.scope))}</p>
    <p class="field-preview-time">${tr('本机实际读取 · {0}{1}', esc(record.checked_at), queryLabel !== record.label ? ` ${tr('· 批量查询见')} ` + esc(queryLabel) : batch.batch_seconds != null ? ` ${tr('· 整批')} ` + esc(batch.batch_seconds) + ` ${tr('秒')}` : '')}</p>`;
}

export function fieldEvidence(records = []) {
  const matchedFields = new Map(),
    batchQueries = new Map(),
    visible = [];
  for (const r of records) {
    if (!r.scope_term || !r.ok) continue;
    if (r.profile.dax_queries?.length) batchQueries.set(r.label, r);
    if (!r.profile.values.length) continue;
    const term = r.scope_term.toLowerCase();
    if (!matchedFields.has(term)) matchedFields.set(term, new Set());
    for (const v of r.profile.values)
      matchedFields.get(term).add(JSON.stringify([r.table, r.column, v.value]));
    if (r.confirmed_value == null && r.adopted_value == null) visible.push(r);
  }
  const queryLabels = new Map();
  return visible
    .map((record) => {
      // The original DAX owner may be hidden. Keep one expandable query per visible batch.
      const batch = batchQueries.get(record.query_label) || record;
      if (!queryLabels.has(batch)) queryLabels.set(batch, record.label);
      const queryLabel = queryLabels.get(batch);
      const queries = queryLabel === record.label ? batch.profile.dax_queries : [];
      const status =
        matchedFields.get(record.scope_term.toLowerCase()).size > 1 ? tr('多个候选命中') : tr('精确命中');
      return `<details class="field-evidence" data-field-evidence="${esc(record.id)}">
    <summary>${icon('search')}<strong>${esc(record.label)} · ${esc(record.scope_term)} · ${esc(record.table)}[${esc(record.column)}]</strong><span>${status}</span>${icon('chevron-down')}</summary>
    <div class="field-evidence-body">${record.reason ? `<p class="field-preview-note">${esc(record.reason)}</p>` : ''}
      ${exactEvidence(record, batch, queryLabel)}
      <button class="text-link" data-preview-model="${esc(record.model_id)}" data-preview-table="${esc(record.table)}" data-preview-column="${esc(record.column)}">${icon('table')}${tr('打开字段预览')}</button>
      ${queries?.length ? `<details class="field-query-details"><summary>${tr('查看取值查询依据')}</summary>${queries.map((query) => codeBlock(query, 'dax')).join('')}</details>` : ''}
    </div></details>`;
    })
    .join('');
}

export async function mountFieldPreview(root, { api, modelId, table, column, onBack = null }) {
  root.innerHTML = `${onBack ? `<button type="button" class="text-link field-preview-back" data-field-back>${icon('arrow-left')}${tr('返回业务字典')}</button>` : ''}<p class="notice">${tr('读取当前模型中的字段实际内容。空值按模型中的 BLANK() 统计，空字符串单独列出可识别的数量。')}</p>
    <form class="field-preview-search" data-field-search><input name="search" type="search" maxlength="120" placeholder="${tr('搜索取值，可尝试人员、personnel 等同义词')}" aria-label="${tr('搜索字段取值')}"><button class="primary-button" type="submit">${tr('搜索取值')}</button><button class="secondary-button" type="button" data-field-clear>${tr('清空')}</button></form>
    <div data-field-result aria-live="polite"></div>`;
  let sequence = 0,
    disposed = false;
  const form = root.querySelector('form'),
    result = root.querySelector('[data-field-result]');
  async function load() {
    if (disposed) return;
    const request = ++sequence;
    const search = form.querySelector('input').value.trim();
    form.querySelectorAll('button').forEach((button) => (button.disabled = true));
    result.innerHTML = `<p class="field-preview-note">${tr('正在读取字段取值和空值统计…')}</p>`;
    try {
      const profile = await api(`/models/${encodeURIComponent(modelId)}/fields/preview`, {
        method: 'POST',
        body: JSON.stringify({ table, column, search, limit: 50 }),
      });
      if (request === sequence) result.innerHTML = fieldProfile(profile);
    } catch (error) {
      if (request === sequence)
        result.innerHTML = `<p class="field-preview-error">${esc(error.message)}</p><button type="button" class="secondary-button" data-field-retry>${tr('重新读取')}</button>`;
    } finally {
      if (request === sequence)
        form.querySelectorAll('button').forEach((button) => (button.disabled = false));
    }
  }
  form.onsubmit = (event) => {
    event.preventDefault();
    void load();
  };
  root.onclick = (event) => {
    if (event.target.closest('[data-field-back]')) {
      onBack?.();
      return;
    }
    if (event.target.closest('[data-field-clear]')) {
      form.querySelector('input').value = '';
      void load();
    }
    if (event.target.closest('[data-field-retry]')) void load();
  };
  await load();
  return {
    dispose() {
      disposed = true;
      sequence++;
      root.onclick = null;
      form.onsubmit = null;
    },
  };
}
