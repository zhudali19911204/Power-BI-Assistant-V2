import { $, $$, api, post, toast, guard } from './ui.js';
import { tr, serviceText } from './i18n.js';
import { icon } from './icons.js';
import { escapeHtml as esc } from './html.js';
import { mountUserGuide } from './user_guide.js';
import { mountModelManager } from './models.js';
import { mountFieldPreview } from './fields.js';
import { mountDictionaryLibrary } from './dictionaries.js';
import { mountSemanticWorkbench, semanticSummary } from './semantics.js';
import { mountCalibrationEditor } from './calibration.js';

export function createWorkspaceDialogs({
  state,
  workspace,
  available,
  useModel,
  connectionMonitor,
  renderControls,
  renderContext,
}) {
  let modalCleanup = null;
  let modalReturn = null;

  function attachManager(root, manager) {
    if (root.isConnected && $('#modal').open) modalCleanup = () => manager.dispose();
    else manager.dispose();
  }

  function showModal(title, eyebrow, html, { wide = false, modelSettings = false } = {}) {
    modalReturn = null;
    modalCleanup?.();
    modalCleanup = null;
    $('#modal-close').setAttribute('aria-label', tr('关闭弹窗'));
    $('#modal').classList.toggle('wide-modal', wide);
    $('#modal').classList.toggle('model-settings-modal', modelSettings);
    $('#modal-title').textContent = title;
    $('#modal-eyebrow').textContent = eyebrow;
    $('#modal-body').innerHTML = html;
    if (!$('#modal').open) $('#modal').showModal();
  }
  function closeModalLayer() {
    if (modalReturn) modalReturn();
    else $('#modal').close();
  }
  function openUserGuide() {
    showModal(
      tr('使用说明'),
      '',
      `<div id="user-guide"><p class="guide-empty" role="status">${tr('正在读取使用说明…')}</p></div>`,
      { wide: true }
    );
    const controller = new AbortController();
    modalCleanup = () => controller.abort();
    return mountUserGuide($('#user-guide'), controller.signal);
  }
  function modalHasEditor() {
    return !!$(
      '#model-manager [data-model-form], [data-template-editor], [data-template-run-form], #semantic-workbench:not([hidden]) form',
      $('#modal')
    );
  }
  $('#modal-close').onclick = closeModalLayer;
  $('#modal').addEventListener('close', () => {
    if ($('#modal').open) return;
    modalReturn = null;
    modalCleanup?.();
    modalCleanup = null;
  });
  $('#modal').addEventListener('cancel', (event) => {
    // Escape can dismiss IME input too. Only an explicit close should discard this editor.
    if (modalHasEditor()) {
      event.preventDefault();
      return;
    }
    if (modalReturn) {
      event.preventDefault();
      modalReturn();
    }
  });
  $('#modal').addEventListener('click', (e) => {
    if (e.target !== $('#modal') || modalHasEditor()) return;
    const r = e.target.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)
      closeModalLayer();
  });

  async function openConnections() {
    showModal(
      tr('连接 Power BI'),
      'LOCAL MODEL',
      `<p class="notice" id="connection-scanning">${tr('正在查找本机 Power BI Desktop 实例…')}</p>`
    );
    const scanning = $('#connection-scanning'),
      view = state.view;
    const [instances, models] = await Promise.all([api('/desktop/instances'), api('/models')]);
    if (!scanning.isConnected || !$('#modal').open || state.view !== view) return;
    state.models = models;
    const buttons = instances.map((instance) => {
      const name = esc(instance.report?.name || 'Power BI Desktop');
      const detail = esc(instance.report?.path || `${tr('本地实例 · PID')} ${instance.pid}`);
      return `<button class="instance-button" data-port="${instance.port}">${icon('monitor')}<span>${name} · ${instance.port}<small>${detail}</small></span></button>`;
    });
    const nameField = instances.some((instance) => !instance.report?.name)
      ? `<label class="form-field">${tr('未识别报表名称时，可填写用于后续匹配的名称')}` +
        `<input id="connection-report-name" maxlength="260" placeholder="${tr('例如：采购费用分析；以后打开同一报表填写相同名称')}"></label>`
      : '';
    const snapshots = models.slice(0, 8).map((model) => {
      const status = model.live
        ? tr('实时连接')
        : model.source === 'demo'
          ? tr('示例模型')
          : tr('离线快照 · 无法实跑');
      return `<button class="snapshot-button" data-snapshot="${model.id}">${icon('database')}<span>${esc(model.name)}<small>${status}</small></span></button>`;
    });
    $('#modal-body').innerHTML = [
      `<div class="semantic-tools"><button type="button" class="secondary-button" id="manage-dictionaries">${tr('管理历史业务词典')}</button></div>`,
      `<p class="notice">${tr('先在本机 Power BI Desktop 打开报表。连接后按报表名称与文件路径匹配本地语义词典，并核对字段、关系和公式；不会修改报表。')}</p>`,
      buttons.length
        ? buttons.join('')
        : `<p class="field-help">${tr('未找到已打开的实例。请打开报表后重新扫描。')}</p>`,
      nameField,
      `<div class="modal-actions"><button class="secondary-button" id="demo-load">${tr('体验示例模型')}</button>`,
      `<button class="primary-button" id="rescan">${icon('refresh')}${tr('重新扫描')}</button></div>`,
      snapshots.length
        ? `<div class="snapshot-section"><div class="section-label">${tr('本地保存的模型结构')}</div>${snapshots.join('')}</div>`
        : '',
    ].join('');
  }

  async function selectConnection(button) {
    const view = state.view,
      dialog = $('#modal');
    const current = () => dialog.open && button.isConnected && state.view === view;
    if (!current() || button.disabled) return;
    for (const control of $$('#modal-body button')) control.disabled = true;
    const { port, snapshot } = button.dataset;
    try {
      let model;
      if (port) {
        button.textContent = tr('正在读取模型…');
        const reportName = $('#connection-report-name')?.value.trim() || '';
        model = await post('/models/connect', { port: Number(port), report_name: reportName });
      } else if (snapshot) model = await api(`/models/${snapshot}`);
      else model = await post('/models/demo');
      if (!current()) {
        if (port) void connectionMonitor.refresh();
        return;
      }
      useModel(model, !!port);
      if (port)
        toast(
          serviceText(
            model.semantics?.dictionary_notice?.message ||
              (model.semantics?.status === 'ready' && model.semantics.stage) ||
              tr('已读取 Desktop 模型')
          )
        );
    } catch (error) {
      if (!current()) return;
      await openConnections();
      throw error;
    }
  }

  function updateModelSemantics(modelId, summary) {
    for (const model of state.models) if (model.id === modelId) model.semantics = summary;
    for (const view of workspace.all()) if (view.model?.id === modelId) view.model.semantics = summary;
    if (state.view.model?.id === modelId) renderContext();
  }

  async function openDictionaryLibrary() {
    const modelId = state.view.model?.source === 'desktop' ? state.view.model.id : null;
    showModal(tr('业务词典管理'), 'DICTIONARY LIBRARY', '<section id="dictionary-library"></section>', {
      wide: true,
    });
    const root = $('#dictionary-library');
    const manager = await mountDictionaryLibrary(root, {
      api,
      modelId,
      onAssociate: async (profile) => {
        updateModelSemantics(modelId, semanticSummary(profile));
        toast(serviceText(profile.dictionary_notice?.message) || tr('业务词典已关联'));
        await openSemantics(modelId);
      },
      onDeleted: (deletedId) => updateModelSemantics(deletedId, null),
    });
    attachManager(root, manager);
  }

  async function openSettings() {
    showModal(tr('模型服务设置'), 'MODEL SERVICES', '<section id="model-manager"></section>', {
      modelSettings: true,
    });
    await mountModelManager($('#model-manager'), {
      api,
      onSettings: (settings) => {
        state.settings = settings;
        renderControls();
      },
      onNotice: toast,
    });
  }

  async function openFieldPreview(table, column, modelId = state.view.model?.id) {
    if (!modelId) {
      toast(tr('请先连接本机 Power BI Desktop'));
      return;
    }
    showModal(`${table}[${column}]`, 'FIELD VALUES', '<section id="field-preview"></section>');
    const root = $('#field-preview'),
      manager = await mountFieldPreview(root, { api, modelId, table, column });
    attachManager(root, manager);
  }

  async function openSemantics(modelId = state.view.model?.id) {
    if (!modelId) return;
    showModal(tr('模型语义层'), 'MODEL SEMANTICS', '<section id="semantic-workbench"></section>', {
      wide: true,
    });
    const root = $('#semantic-workbench');
    let manager,
      previewCleanup = null;
    async function childPanel({ id, title: childTitle, eyebrow: childEyebrow, mount, onReturn = () => {} }) {
      if (root.hidden || !root.isConnected || !$('#modal').open) return;
      manager?.pause();
      const dialog = $('#modal'),
        title = $('#modal-title').textContent,
        eyebrow = $('#modal-eyebrow').textContent;
      const scrollTop = dialog.scrollTop;
      const child = document.createElement('section');
      child.id = id;
      let active = true,
        childManager;
      function cleanup() {
        active = false;
        childManager?.dispose();
        child.remove();
        if (modalReturn === back) modalReturn = null;
      }
      function back() {
        if (!active) return;
        cleanup();
        previewCleanup = null;
        root.hidden = false;
        $('#modal-title').textContent = title;
        $('#modal-eyebrow').textContent = eyebrow;
        $('#modal-close').setAttribute('aria-label', tr('关闭弹窗'));
        dialog.scrollTop = scrollTop;
        manager?.resume();
        onReturn();
      }
      previewCleanup = cleanup;
      modalReturn = back;
      root.hidden = true;
      root.after(child);
      $('#modal-title').textContent = childTitle;
      $('#modal-eyebrow').textContent = childEyebrow;
      $('#modal-close').setAttribute('aria-label', tr('返回模型语义层'));
      dialog.scrollTop = 0;
      const mounted = await mount(child, back);
      if (active && child.isConnected) childManager = mounted;
      else mounted.dispose();
    }
    function preview(table, column, sourceModelId) {
      return childPanel({
        id: 'field-preview',
        title: `${table}[${column}]`,
        eyebrow: 'FIELD VALUES',
        mount: (child, back) =>
          mountFieldPreview(child, { api, modelId: sourceModelId, table, column, onBack: back }),
      });
    }
    function dictionaries() {
      return childPanel({
        id: 'dictionary-library',
        title: tr('业务词典管理'),
        eyebrow: 'DICTIONARY LIBRARY',
        onReturn: () => void manager?.refresh(),
        mount: (child, back) =>
          mountDictionaryLibrary(child, {
            api,
            modelId,
            onBack: back,
            onAssociate: (profile) => {
              toast(serviceText(profile.dictionary_notice?.message) || tr('业务词典已关联'));
              back();
            },
            onDeleted: (deletedId) => updateModelSemantics(deletedId, null),
          }),
      });
    }
    manager = await mountSemanticWorkbench(root, {
      api,
      modelId,
      onUpdate: (summary) => updateModelSemantics(modelId, summary),
      onPreview: (...args) => void preview(...args),
      onManageDictionaries: () => void dictionaries(),
    });
    if (root.isConnected && $('#modal').open)
      modalCleanup = () => {
        previewCleanup?.();
        manager.dispose();
      };
    else {
      previewCleanup?.();
      manager.dispose();
    }
  }

  function openCalibration() {
    if (!available()) return;
    if (!state.view.model?.live) {
      toast(tr('校准生成需要先连接本机 Desktop 的实时模型'), true);
      return;
    }
    showModal(
      tr('用已知结果校准'),
      'CALIBRATION',
      `<p class="notice">${tr('可添加多个筛选条件，所有条件同时生效，共同对应一个已知正确结果。将对回复中的最后一个度量值实跑校准，失败时最多自动修复两轮。')}</p><form id="calibration-form"></form>`
    );
    const form = $('#calibration-form');
    const editor = mountCalibrationEditor(
      form,
      state.view.model.context,
      state.view.points[0] || {},
      (message) => toast(message)
    );
    $('[data-clear-calibration]', form).onclick = () => {
      state.view.points = [];
      renderControls();
      $('#modal').close();
    };
    form.onsubmit = guard(async (e) => {
      e.preventDefault();
      state.view.points = [editor.read()];
      renderControls();
      $('#modal').close();
      toast(tr('已设置 {0} 个筛选条件和 1 个校准结果', state.view.points[0].filters.length));
    });
  }

  return {
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
  };
}
