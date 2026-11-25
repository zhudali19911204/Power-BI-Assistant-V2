import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {parseHTML} from 'linkedom';
import {mountDesktopUpdates} from '../frontend/updates.js';

const html = await readFile(new URL('../frontend/index.html', import.meta.url), 'utf8');
const release = {version:'2.0.14', size:1234567, sha256:'a'.repeat(64), notes:'<b>新功能</b>'};

function setup(options) {
  const {window, document} = parseHTML(html);
  const button = document.querySelector('#updates-open'), dialog = document.querySelector('#updates-dialog');
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.dispatchEvent(new window.Event('close')); };
  const notices = [];
  const ready = mountDesktopUpdates({button, dialog, notice: msg => notices.push(msg),
    api: async () => ({enabled:true, version:'2.0.13'}), hasWork: () => false, hasDraft: () => false, ...options});
  return {button, dialog, notices, ready, find: name => dialog.querySelector(`[data-update-${name}]`)};
}

test('a startup notice leaves the window alone and Later cancels a restart during download', async () => {
  let finish, busy = true;
  const calls = [];
  const ui = setup({hasWork: () => busy, post: async path => {
    calls.push(path);
    if (path.endsWith('/check')) return {release};
    if (path.endsWith('/download')) await new Promise(resolve => {finish = resolve;});
    return {};
  }});
  const checkWhenIdle = await ui.ready;
  await checkWhenIdle();
  assert.equal(ui.button.hidden, false);
  assert.equal(ui.notices.length, 1);
  assert.ok(!ui.dialog.open);
  ui.button.click();
  assert.equal(ui.find('notes').textContent, release.notes);
  assert.equal(ui.find('notes').childElementCount, 0);
  await ui.find('install').onclick();
  assert.match(ui.find('status').textContent, /任务/);
  busy = false;
  const download = ui.find('install').onclick();
  ui.find('later').click();
  ui.button.click();
  finish();
  await download;
  assert.ok(!calls.some(path => path.endsWith('/install')));
  assert.equal(ui.find('install').textContent, '重启并安装');
  await ui.find('install').onclick();
  assert.deepEqual(calls, ['/updates/check', '/updates/check', `/updates/${release.sha256}/download`, `/updates/${release.sha256}/install`]);
});

test('offline checks stay quiet and can retry; source previews do not fetch the feed', async () => {
  let offline = true;
  const ui = setup({post: async () => {
    if (offline) throw new Error('网络不可用');
    return {release:null};
  }});
  await ui.ready;
  assert.deepEqual(ui.notices, []);
  assert.match(ui.find('status').textContent, /网络/);
  offline = false;
  await ui.find('check').onclick();
  assert.match(ui.find('status').textContent, /最新/);
  assert.ok(ui.find('install').hidden);
  let checked = false;
  const preview = setup({api: async () => ({enabled:false}), post: () => {checked = true;}});
  await preview.ready;
  assert.ok(preview.button.hidden);
  assert.equal(checked, false);
});
