import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {parseHTML} from 'linkedom';
import {mountUserGuide} from '../frontend/user_guide.js';

const source = await readFile(new URL('../docs/USER_GUIDE.md', import.meta.url), 'utf8');

test('the offline reader starts with the first question, searches chapter text and follows local links', async t => {
  const {document, window} = parseHTML('<main id="guide"></main>');
  const root = document.querySelector('main');
  const controller = new AbortController();
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/help/user-guide.md');
    assert.equal(options.signal, controller.signal);
    return new Response(source);
  });
  await mountUserGuide(root, controller.signal);
  const topic = () => root.querySelector('article h2')?.textContent;
  const nav = root.querySelector('nav'), search = root.querySelector('input');
  const filter = term => { search.value = term; search.dispatchEvent(new window.Event('input')); };
  assert.equal(topic(), '完成第一次提问');
  assert.equal(nav.firstElementChild.getAttribute('aria-current'), 'true');
  assert.equal(nav.querySelectorAll('button').length, [...source.matchAll(/^## /gm)].length);
  const dax = [...root.querySelectorAll('article a')].find(link => link.textContent === '编写 DAX');
  assert.equal(dax.getAttribute('target'), null);
  dax.click();
  assert.equal(topic(), '使用 DAX 助手');
  filter('上下文压缩');
  assert.equal(topic(), '对话、图片和导出');
  assert.equal(root.querySelector('[data-guide-match]').textContent, '上下文压缩');
  assert.match(root.querySelector('article').textContent, /80%/);
  filter('power query');assert.equal(topic(), '使用 Power Query');
  filter('aPi kEy');
  assert.ok([...nav.children].some(el => el.textContent === '配置大模型服务'));
  filter('一个未出现的关键词');
  assert.equal(nav.children.length, 0);
  assert.match(root.querySelector('[role="status"]').textContent, /没有找到/);
  filter('');
  const install = [...nav.children].find(button => button.textContent === '安装与启动');
  install.click();
  assert.ok(root.querySelector('article pre code'));
  const backup = [...root.querySelectorAll('article a')].find(link => link.textContent === '备份与恢复');
  backup.click();assert.equal(topic(), '备份与恢复');
  assert.ok(root.querySelector('article table'));
});

test('a failed read can be retried and closing a pending read leaves the next screen alone', async t => {
  const {document} = parseHTML('<main></main>');
  const root = document.querySelector('main');
  const fetch = t.mock.method(globalThis, 'fetch', async () => new Response('', {status: 404}));
  await mountUserGuide(root, new AbortController().signal);
  assert.equal(root.querySelector('[data-open-guide]').textContent, '重新加载');
  let finish;
  fetch.mock.mockImplementation(() => new Promise(resolve => {finish = resolve;}));
  const controller = new AbortController();
  const pending = mountUserGuide(root, controller.signal);
  controller.abort();
  root.textContent = '已关闭';
  finish(new Response(source));
  await pending;
  assert.equal(root.textContent, '已关闭');
  fetch.mock.mockImplementation(async () => new Response(source));
  await mountUserGuide(root, new AbortController().signal);
  assert.equal(root.querySelector('article h2').textContent, '完成第一次提问');
});
