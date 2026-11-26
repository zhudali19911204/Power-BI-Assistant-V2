import { serviceText, tr } from './i18n.js';
/** Refresh connection state without replacing messages, fields or drafts. */
export function createConnectionMonitor({ api, getModels, onChange = () => {}, onUnavailable = () => {} }) {
  const pending = new Map();
  function refreshOne(model) {
    if (pending.has(model.id)) return pending.get(model.id);
    const request = (async () => {
      try {
        const result = await api(`/models/${model.id}/connection`);
        if (result.model_id === model.id) onChange(result);
      } catch (error) {
        // A local HTTP failure does not prove that Desktop itself is disconnected.
        onUnavailable(error, model);
      }
    })().finally(() => pending.delete(model.id));
    pending.set(model.id, request);
    return request;
  }
  return {
    refresh() {
      const models = [
        ...new Map(
          getModels()
            .filter((model) => model?.source === 'desktop')
            .map((model) => [model.id, model])
        ).values(),
      ];
      return Promise.all(models.map(refreshOne));
    },
  };
}

export function connectionPresentation(model) {
  if (!model)
    return {
      label: tr('连接 Power BI'),
      detail: tr('连接模型，让回答基于真实字段'),
      kind: 'idle',
      context: tr('连接模型，让回答基于真实字段'),
    };
  if (model.source !== 'desktop')
    return {
      label: tr('示例模型'),
      detail: tr('示例数据结构 · 仅供体验'),
      kind: 'idle',
      context: tr('仅示例结构，无法实跑'),
    };
  const connection = model.connection || {},
    status = connection.status || (model.live ? 'connected' : 'checking');
  const options = {
    connected: [tr('Desktop 已连接'), 'connected', tr('实时模型'), tr('本机实时连接')],
    checking: [tr('正在检测连接'), 'reconnecting', tr('正在检测并恢复连接'), tr('正在检测 Desktop 连接')],
    reconnecting: [tr('自动重连中'), 'reconnecting', tr('正在恢复 Desktop 连接'), tr('正在自动重连')],
    disconnected: [
      tr('等待 Desktop'),
      'reconnecting',
      tr('等待原报表恢复，自动重连中'),
      tr('原报表暂不可用，正在等待恢复'),
    ],
    changed: [tr('结构待同步'), 'warning', tr('模型结构已变化，请同步后继续'), tr('结构已变化，需要同步')],
    released: [tr('快照已释放'), 'warning', tr('同步对应报表后可继续'), tr('仅保留历史记录，请同步模型')],
    ambiguous: [
      tr('请选择报表'),
      'warning',
      tr('发现多个匹配报表，请手动选择'),
      tr('多个报表匹配，需要手动选择'),
    ],
  };
  const [label, kind, context, state] = options[status] || options.checking;
  return { label, kind, context, state, detail: serviceText(connection.detail) || state };
}

export function releaseSnapshot(model) {
  return {
    ...model,
    live: false,
    snapshot_released: true,
    context: { tables: {}, measures: [], relationships: [], table_queries: {}, shared_expressions: {} },
    connection: { status: 'released', detail: '旧模型快照已释放，请同步对应报表后继续' },
  };
}
