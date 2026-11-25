export async function mountDesktopUpdates({button, dialog, api, post, notice, hasWork, hasDraft}) {
  const info = await api('/updates').catch(() => null);
  if (!info?.enabled) return;
  const find = name => dialog.querySelector(`[data-update-${name}]`);
  const status = find('status'), notes = find('notes'), install = find('install');
  let release = null, downloaded = '', lastNotice = '', phase = 'idle', message = '', restartAfterDownload = false;
  find('current').textContent = `当前版本 ${info.version}`;

  function render() {
    const label = release ? `发现新版 ${release.version}` : '检查更新';
    button.querySelector('[data-update-label]').textContent = label;
    button.title = label;
    button.setAttribute('aria-label', label);
    status.textContent = message;
    notes.textContent = release?.notes || '';
    notes.hidden = !notes.textContent;
    find('draft').hidden = !release || !hasDraft();
    find('check').disabled = phase !== 'idle';
    find('close').disabled = find('later').disabled = phase === 'installing';
    install.hidden = !release;
    install.disabled = phase !== 'idle';
    install.textContent = downloaded && downloaded === release?.sha256 ? '重启并安装' : '更新并重启';
  }

  async function check(automatic = false) {
    if (phase !== 'idle') return;
    phase = 'checking';
    message = '正在检查更新…';
    render();
    try {
      release = (await post('/updates/check')).release;
      message = release ? `新版 ${release.version} 可用，下载大小 ${(release.size / 1024**2).toFixed(1)} MB。` : '当前已是最新版本。';
      if (automatic && release && lastNotice !== release.sha256) {
        lastNotice = release.sha256;
        notice(`发现新版 ${release.version}，可在左下角查看更新。`);
      }
    } catch (error) {
      release = null;
      message = error.message;
    } finally {
      phase = 'idle';
      render();
    }
  }

  async function update() {
    if (!release || phase !== 'idle') return;
    if (hasWork()) {
      message = '助手正在处理任务，请在任务结束后更新。';
      render();
      return;
    }
    const path = `/updates/${release.sha256}`;
    restartAfterDownload = true;
    try {
      if (downloaded !== release.sha256) {
        phase = 'downloading';
        message = '正在下载并校验安装包…';
        render();
        await post(path + '/download');
        downloaded = release.sha256;
      }
      if (!restartAfterDownload || !dialog.open || hasWork()) {
        message = '安装包已下载。准备好后可重启并安装。';
        return;
      }
      phase = 'installing';
      message = '正在启动安装器，完成后会重新打开助手。';
      render();
      await post(path + '/install');
    } catch (error) {
      downloaded = '';
      message = error.message;
      phase = 'idle';
    } finally {
      if (phase === 'downloading') phase = 'idle';
      render();
    }
  }

  function later() {
    if (phase === 'installing') return;
    restartAfterDownload = false;
    dialog.close();
  }
  find('close').onclick = find('later').onclick = later;
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    later();
  });
  dialog.addEventListener('close', () => { restartAfterDownload = false; });
  find('install').onclick = update;
  find('check').onclick = () => check();
  button.onclick = () => {
    if (!dialog.open) dialog.showModal();
    render();
    if (!release) void check();
  };
  button.hidden = false;
  await check(true);
  return () => { if (!dialog.open) return check(true); };
}
