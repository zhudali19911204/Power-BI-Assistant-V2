import { language, serviceText, tr } from './i18n.js';

export const $ = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => [...root.querySelectorAll(s)];

export async function api(path, options = {}) {
  const response = await fetch('/api' + path, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-UI-Language': language, ...options.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: tr('服务暂时不可用') }));
    if (Array.isArray(body.detail) && body.detail.every((issue) => typeof issue?.msg === 'string')) {
      const messages = body.detail.map((issue) => {
        const field = issue.loc?.filter((part) => part !== 'body').join('.');
        return [field, serviceText(issue.msg.replace(/^Value error,\s*/, ''))].filter(Boolean).join(' · ');
      });
      throw new Error(messages.join('；') || tr('请检查填写内容'));
    }
    throw new Error(typeof body.detail === 'string' ? serviceText(body.detail) : JSON.stringify(body.detail));
  }
  return response.json();
}
export const post = (path, body = {}) => api(path, { method: 'POST', body: JSON.stringify(body) });
export function toast(text, error = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (error ? ' error' : '');
  el.textContent = text;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), 5500);
}
export const guard =
  (fn) =>
  async (...args) => {
    try {
      await fn(...args);
    } catch (e) {
      toast(e.message, true);
    }
  };
