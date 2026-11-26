import {english} from './en.js';

export let language = 'zh-CN';

// Only application copy goes through this function. Arguments may be field names,
// user input or markup already escaped by the renderer and are inserted verbatim.
export function translate(locale, source, ...values) {
  const message = locale === 'en' && Object.hasOwn(english, source) ? english[source] : source;
  return values.length ? message.replace(/\{(\d+)\}/g, (_, index) => values[index]) : message;
}

export function tr(source, ...values) {
  return translate(language, source, ...values);
}

// These legacy service fields contain formatted status text. Match only known
// application messages and keep identifiers, query titles and diagnostic details intact.
const servicePatterns = [
  [/^根据验证结果修复 · (\d+)\/2$/, 'Repairing code · $1/2'],
  [/^模型暂未返回新数据，继续等待（连续 (\d+) 秒）$/, 'Waiting for model output · $1 s'],
  [/^正在批量核对 (\d+) 个口径 · (\d+) 项候选字段$/, 'Checking $1 definitions across $2 candidate fields'],
  [/^正在执行第 (\d+) 批分析查询 · 本批 (\d+) 项$/, 'Running query batch $1 · Queries: $2'],
  [/^正在查询语义层 · (.*)$/, 'Searching the dictionary · $1'],
  [/^正在检查字段实际取值 (.*)$/, 'Checking field values $1'],
  [/^正在 Desktop 查询 (.*)$/, 'Querying Desktop: $1'],
  [/^(Q\d+) 查询未通过，正在修复查询$/, '$1 failed. Repairing query'],
  [/^业务词典需更新：(\d+) 项对象待补充或更新，(\d+) 项业务记录待复核$/, 'Dictionary update needed. Objects to update: $1. Business records to review: $2.'],
  [/^已保留 (\d+) 项语义，(\d+) 项待增量梳理$/, 'Definitions kept: $1 · Pending updates: $2'],
  [/^已保留全部 (\d+) 项对象定义，可单独生成业务总览$/, 'All $1 object definitions are saved. The overview can be built separately.'],
  [/^已保留全部 (\d+) 项语义，无需重复梳理$/, 'All $1 definitions are up to date.'],
  [/^全部重新梳理尚余 (\d+) 项，可继续梳理$/, 'Resume full build · Remaining objects: $1'],
  [/^正在梳理字段语义 · (\d+\/\d+)$/, 'Building field definitions · $1'],
  [/^正在调整本批次返回格式 · (\d+\/\d+)$/, 'Correcting batch output format · $1'],
  [/^已复用报表“(.*)”的语义词典 · (\d+) 项$/, 'Reusing the dictionary for “$1” · Entries: $2'],
  [/^已复用 (\d+) 项，(\d+) 项需梳理或核对$/, 'Entries reused: $1 · To build or review: $2'],
  [/^读取模型失败：(.*)$/, 'Could not read model: $1'],
  [/^请先连接模板所属报表“(.*)”$/, 'Connect the report for this template: “$1”'],
  [/^请填写参数：(.+)$/, 'Enter parameter values for: $1'],
];

export function serviceText(source) {
  if (language !== 'en' || typeof source !== 'string') return source;
  if (Object.hasOwn(english, source)) return english[source];
  for (const [pattern, replacement] of servicePatterns) {
    if (pattern.test(source)) return source.replace(pattern, replacement);
  }
  return source;
}

export function mountLanguage(doc, {onChange, save, onError}) {
  const button = doc.querySelector('#language-toggle');
  const label = button.querySelector('span');
  const bindings = [];
  // Capture only the original HTML shell, before any report, draft or chat is mounted.
  for (const element of doc.body.querySelectorAll('*')) {
    for (const node of element.childNodes) {
      if (node.nodeType !== 3) continue;
      const original = node.data, key = original.trim();
      if (Object.hasOwn(english, key)) {
        bindings.push(() => { node.data = original.replace(key, () => tr(key)); });
      }
    }
    for (const attr of ['placeholder', 'aria-label', 'title', 'alt']) {
      const key = element.getAttribute(attr);
      if (key && Object.hasOwn(english, key)) bindings.push(() => element.setAttribute(attr, tr(key)));
    }
  }

  function apply(next) {
    language = next === 'en' ? 'en' : 'zh-CN';
    doc.documentElement.setAttribute('lang', language);
    bindings.forEach(update => update());
    const toEnglish = language === 'zh-CN';
    label.textContent = toEnglish ? 'English' : '中文';
    label.lang = toEnglish ? 'en' : 'zh-CN';
    button.title = toEnglish ? '切换到英文' : 'Switch to Chinese';
    button.setAttribute('aria-label', button.title);
  }

  apply(doc.documentElement.getAttribute('lang'));
  let saves = Promise.resolve();
  button.onclick = () => {
    const next = language === 'en' ? 'zh-CN' : 'en';
    apply(next);
    onChange();
    // Keep clicks responsive and commit them in order, even on a slow disk.
    saves = saves.then(() => save(next)).catch(() => {
      if (language === next) onError(tr('语言偏好未保存，重启后请重新选择。'));
    });
    return saves;
  };
}
