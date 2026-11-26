import { tr } from './i18n.js';
import { escapeHtml as esc } from './html.js';
import { icon } from './icons.js';
import { codeLanguage, highlightCode } from './code_highlight.js';

export function codeBlock(
  value,
  language = '',
  { title, copyLabel = tr('复制'), expand = true, wrapped = true } = {}
) {
  const source = String(value ?? ''),
    mode = codeLanguage(language);
  const label = title ?? (mode === 'm' ? 'POWER QUERY · M' : String(language || 'CODE').toUpperCase());
  // JSON encoding keeps CRLF, tabs and trailing whitespace intact through HTML attribute parsing.
  return `<div class="code-block" data-code-language="${mode}" data-code-title="${esc(label)}" data-code-source="${esc(JSON.stringify(source))}" data-code-wrapped="${wrapped}">
    <div class="code-heading"><span>${esc(label)}</span><div class="code-actions">
      <button type="button" data-code-toggle-wrap aria-pressed="${wrapped}" title="${tr('切换自动换行')}">${tr('自动换行')}</button>
      ${expand ? `<button type="button" data-code-expand>${tr('展开阅读')}</button>` : ''}
      <button type="button" class="copy-code">${icon('copy')}${esc(copyLabel)}</button>
    </div></div><pre tabindex="0" aria-label="${tr('{0} 代码', esc(label))}"><code>${highlightCode(source, mode)}</code></pre></div>`;
}

export function codeSource(block) {
  try {
    const value = JSON.parse(block.getAttribute('data-code-source'));
    if (typeof value === 'string') return value;
  } catch {
    /* Legacy code blocks have only visible text. */
  }
  return block.querySelector('code')?.textContent || '';
}

export function toggleCodeWrap(block) {
  const wrapped = block.getAttribute('data-code-wrapped') === 'false';
  block.setAttribute('data-code-wrapped', String(wrapped));
  block.querySelector('[data-code-toggle-wrap]')?.setAttribute('aria-pressed', String(wrapped));
}

export async function copyCodeText(text, block, clipboard = globalThis.navigator?.clipboard) {
  try {
    await clipboard.writeText(text);
    return;
  } catch {
    /* Use the active dialog when clipboard access is unavailable. */
  }
  const document = block.ownerDocument,
    focused = document.activeElement,
    input = document.createElement('textarea');
  input.className = 'code-copy-buffer';
  input.value = text;
  input.setAttribute('aria-hidden', 'true');
  input.tabIndex = -1;
  (block.closest('dialog') || document.body).append(input);
  try {
    input.select();
    if (!document.execCommand('copy')) throw new Error(tr('复制失败，请选中代码后按 Ctrl+C'));
  } finally {
    input.remove();
    focused?.focus();
  }
}

/** A separate dialog keeps the original write form, evidence and scrolling position mounted. */
export function createCodeTools(dialog, { copy = copyCodeText, onNotice = () => {} } = {}) {
  const body = dialog.querySelector('[data-code-reader-body]');
  let opener = null;
  dialog.addEventListener('close', () => {
    body.replaceChildren();
    if (opener?.isConnected) opener.focus();
    opener = null;
  });
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    dialog.close();
  });
  return {
    async handleClick(button) {
      if (button.hasAttribute('data-code-reader-close')) {
        dialog.close();
        return true;
      }
      const block = button.closest('.code-block');
      if (!block) return false;
      if (button.hasAttribute('data-code-toggle-wrap')) {
        toggleCodeWrap(block);
        return true;
      }
      if (button.hasAttribute('data-code-expand')) {
        opener = button;
        body.innerHTML = codeBlock(codeSource(block), block.dataset.codeLanguage, {
          title: block.dataset.codeTitle,
          expand: false,
          wrapped: block.dataset.codeWrapped !== 'false',
        });
        dialog.querySelector('[data-code-reader-note]').hidden = !block.closest('.message.streaming');
        if (!dialog.open) dialog.showModal();
        return true;
      }
      if (button.classList.contains('copy-code')) {
        await copy(codeSource(block), block);
        onNotice(tr('代码已复制'));
        return true;
      }
      return false;
    },
  };
}
