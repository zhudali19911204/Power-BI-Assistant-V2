import { $, $$, api, post, toast, guard } from './ui.js';
import { tr, serviceText } from './i18n.js';
import { icon } from './icons.js';
import { escapeHtml as esc } from './html.js';
import { codeBlock } from './code_blocks.js';

function validationLabel(validation, column) {
  if (validation.run_verified && validation.ok)
    return column ? tr('Desktop 行上下文抽样通过') : tr('Desktop 实跑通过');
  if (validation.run_verified) return tr('Desktop 实跑未通过');
  return validation.ok ? tr('静态检查通过 · 未实跑') : tr('验证未通过');
}

function validationNotes(notes) {
  if (!notes?.length) return '';
  const text = notes.map((note) => esc(serviceText(note))).join('<br>');
  return `<div class="validation-detail">${text}</div>`;
}

export function artifactCard(obj) {
  const validation = obj.validation;
  const live = validation.run_verified && validation.ok;
  const column = obj.kind === 'dax_column';
  const name = column ? `${tr('计算列 ·')} ${esc(obj.home_table)}[${esc(obj.name)}]` : esc(obj.name);
  const state = !validation.ok ? 'failed' : live ? '' : 'warning';
  const sample = validation.sample;
  const calibration = (validation.calibration || []).map((point, index) => {
    const label = validation.calibration.length === 1 ? tr('校准结果') : tr('历史校准点 {0}', index + 1);
    const match = point.matched ? tr('匹配') : tr('未匹配');
    return `<div class="validation-detail">${tr('{0}：期望 {1} / 实际 {2} · {3}', label, esc(point.expected), esc(point.actual), match)}</div>`;
  });
  let apply = '';
  if (obj.kind === 'm_script') {
    apply = `<div class="validation-detail">${tr('复制脚本至 Power Query 高级编辑器，在 Desktop 中应用验证。')}</div>`;
  } else if (live) {
    const label = obj.applied_at ? tr('已写入模型') : tr('审阅并写入模型');
    apply = `<button data-apply="${esc(obj.id)}">${icon(obj.applied_at ? 'check' : 'database')}${label}</button>`;
  }
  const retry =
    obj.kind !== 'm_script' && obj.message_id
      ? `<button data-revalidate="${esc(obj.message_id)}">${icon('refresh')}${tr('重新实跑本组代码')}</button>`
      : '';
  return [
    `<div class="validation-card ${state}"><div class="validation-top">${icon(validation.ok ? 'check' : 'info')}`,
    `<strong>${name}</strong><span class="validation-badge">${validationLabel(validation, column)}</span></div>`,
    sample == null
      ? ''
      : `<div class="validation-detail">${tr('实跑结果：{0}', esc(typeof sample === 'object' ? JSON.stringify(sample) : sample))}</div>`,
    validationNotes(validation.errors),
    validationNotes(validation.warnings),
    ...calibration,
    apply,
    retry,
    '</div>',
  ].join('');
}

function writeTargetFields(obj, tables, column) {
  const tableLabel = column ? tr('所属表') : tr('存放表');
  const nameLabel = column ? tr('计算列名称') : tr('度量值名称');
  const options = tables.map(
    (table) =>
      `<option value="${esc(table)}" ${table === obj.home_table ? 'selected' : ''}>${esc(table)}</option>`
  );
  const help = column
    ? tr(
        '确认后按所选表的行上下文重新抽样验证，再创建计算列并执行完整计算。数据类型由引擎推断，计算耗时取决于表大小和表达式。'
      )
    : tr('可修改名称，并选择模型中已有的表。确认后将按所选名称和存放表重新验证，再写入模型。');
  return [
    `<div class="form-row"><label class="form-field">${nameLabel}`,
    `<input id="write-measure-name" required maxlength="300" value="${esc(obj.name)}" autocomplete="off"></label>`,
    `<label class="form-field">${tableLabel}<select id="write-home-table" required>`,
    `<option value="" ${!tables.includes(obj.home_table) ? 'selected' : ''} disabled>${tr('请选择{0}', tableLabel)}</option>`,
    ...options,
    `</select></label></div><p class="field-help">${help}</p>`,
  ].join('');
}

export async function reviewWrite(id, { state, available, showModal, render }) {
  if (!available()) return;
  const view = state.view,
    obj = view.messages.flatMap((m) => m.artifacts || []).find((o) => o.id === id);
  if (!obj) return;
  const measure = obj.kind === 'dax_measure',
    column = obj.kind === 'dax_column',
    hasHome = measure || column,
    objectLabel = column ? tr('计算列') : tr('度量值'),
    tables = Object.keys(view.model.context.tables);
  const action = hasHome ? tr('写入') + objectLabel : tr('创建计算表');
  const target = hasHome ? `${tr('，存放于')} ` + esc(obj.home_table) : '';
  const policy = measure
    ? `<label class="check-label"><input type="checkbox" id="overwrite-measure">${tr('如果所选表中存在同名度量值，允许覆盖')}</label>`
    : `<p class="field-help">${column ? tr('仅支持新建计算列，不覆盖已有字段；同名时请修改列名。') : tr('仅支持新建计算表，不覆盖同名已有表。')}</p>`;
  showModal(
    tr('确认写入模型'),
    'REVIEW CHANGE',
    [
      `<p class="notice gold" id="write-target-notice">${tr('将{0}', action)} <strong>${esc(obj.name)}</strong>`,
      `${tr('{0}。完成后请在 Desktop 按 Ctrl+S 保存报表。', target)}</p>`,
      hasHome ? writeTargetFields(obj, tables, column) : '',
      codeBlock(obj.expression, 'dax', { title: obj.name + ' · DAX' }),
      policy,
      `<div class="modal-actions"><button class="secondary-button" id="cancel-write">${tr('取消')}</button>`,
      `<button class="primary-button" id="confirm-write">${tr('确认写入')}</button></div>`,
    ].join('')
  );
  if (hasHome) {
    const preview = () => {
      $('#write-target-notice').textContent = tr(
        '将写入{0} {1}，存放于 {2}。完成后请在 Desktop 按 Ctrl+S 保存报表。',
        objectLabel,
        $('#write-measure-name').value.trim() || tr('（未填写名称）'),
        $('#write-home-table').value || tr('（未选择表）')
      );
    };
    $('#write-measure-name').oninput = preview;
    $('#write-home-table').onchange = preview;
  }
  $('#cancel-write').onclick = () => $('#modal').close();
  $('#confirm-write').onclick = guard(async (e) => {
    const body = { overwrite: !!$('#overwrite-measure')?.checked };
    if (hasHome) {
      body.name = $('#write-measure-name').value.trim();
      body.home_table = $('#write-home-table').value;
      if (!body.name || body.name.length > 300) {
        toast(tr('请填写 1～300 字的{0}名称', objectLabel), true);
        $('#write-measure-name').focus();
        return;
      }
      if (!tables.includes(body.home_table)) {
        toast(tr('请选择模型中已有的存放表'), true);
        return;
      }
    }
    const b = e.currentTarget,
      fields = $$('input,select', $('#modal-body'));
    b.disabled = true;
    b.textContent = tr('重新验证并写入…');
    fields.forEach((el) => (el.disabled = true));
    try {
      const r = await post(`/artifacts/${id}/apply`, body);
      Object.assign(obj, {
        name: r.name ?? obj.name,
        home_table: r.home_table ?? obj.home_table,
        validation: r.validation ?? obj.validation,
        applied_at: r.applied_at || new Date().toISOString(),
      });
      $('#modal').close();
      try {
        view.model = await api(`/models/${view.model.id}`);
      } catch (error) {
        toast(tr('已写入，模型信息读取失败，请同步模型：') + error.message, true);
      }
      if (state.view === view) render();
      toast(serviceText(r.detail));
    } finally {
      b.disabled = false;
      b.textContent = tr('确认写入');
      fields.forEach((el) => (el.disabled = false));
    }
  });
}
