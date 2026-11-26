import { language, tr } from './i18n.js';
import { icon } from './icons.js';
import { markdown } from './markdown.js';

const firstQuestion = () => tr('完成第一次提问');

function readChapters(doc, source) {
  const parsed = doc.createElement('div');
  parsed.innerHTML = markdown(source);
  const chapters = [];
  let chapter;
  // The standalone guide starts with release information and a table of contents.
  // Group the rendered headings so examples inside fenced code stay in their chapter.
  for (const node of [...parsed.children]) {
    if (node.tagName === 'H2') {
      const title = node.textContent.trim();
      const button = doc.createElement('button');
      button.type = 'button';
      button.textContent = title;
      chapter = { title, button, body: doc.createElement('div') };
      chapter.body.className = 'message-content';
      chapters.push(chapter);
    }
    if (chapter) chapter.body.append(node);
  }
  const start = chapters.find((item) => item.title === firstQuestion());
  return start ? [start, ...chapters.filter((item) => item !== start)] : chapters;
}

function addActions(chapter) {
  const start = chapter.title === firstQuestion();
  const actions = [];
  if (start || chapter.title === tr('配置大模型服务')) {
    actions.push(
      '<button type="button" class="secondary-button" data-guide-action="settings">' +
        icon('settings') +
        `${tr('模型服务设置')}</button>`
    );
  }
  if (start || chapter.title === tr('连接和同步报表')) {
    actions.push(
      '<button type="button" class="secondary-button" data-guide-action="connect">' +
        icon('monitor') +
        `${tr('连接 Power BI 报表')}</button>`
    );
  }
  if (actions.length) {
    const steps = start && chapter.body.querySelector('ol');
    (steps || chapter.body.firstElementChild).insertAdjacentHTML(
      'afterend',
      '<div class="guide-actions">' + actions.join('') + '</div>'
    );
  }
}

function showReader(root, chapters) {
  root.innerHTML = `<div class="guide-sidebar">
    <label class="guide-search">${icon('search')}<input type="search" placeholder="${tr('搜索主题或关键词')}" aria-label="${tr('搜索使用说明')}"></label>
    <nav class="guide-chapters" aria-label="${tr('使用说明目录')}"></nav>
  </div><article class="guide-reading" tabindex="0" aria-label="${tr('章节内容')}"></article>`;
  const search = root.querySelector('input');
  const nav = root.querySelector('nav');
  const reader = root.querySelector('article');
  const anchors = new Map();
  let current, matchedPassage;
  let words = [];
  const matchesText = (text) => words.every((word) => text.includes(word));

  function select(chapter) {
    matchedPassage?.removeAttribute('data-guide-match');
    current?.button.removeAttribute('aria-current');
    current = chapter;
    if (chapter) {
      chapter.button.setAttribute('aria-current', 'true');
      reader.replaceChildren(chapter.body);
      reader.setAttribute('aria-labelledby', chapter.body.firstElementChild.id);
    } else {
      reader.removeAttribute('aria-labelledby');
      reader.innerHTML = `<p class="guide-empty" role="status">${tr('没有找到相关章节，可以试试其他关键词。')}</p>`;
    }
    reader.scrollTop = 0;
    if (chapter && words.length) {
      matchedPassage = [...chapter.body.querySelectorAll('h2,h3,h4,h5,h6,tr,li,p')].find((passage) =>
        matchesText(passage.textContent.toLowerCase())
      );
      if (matchedPassage) {
        matchedPassage.setAttribute('data-guide-match', '');
        reader.scrollTop = Math.max(
          0,
          matchedPassage.getBoundingClientRect().top - reader.getBoundingClientRect().top - 6
        );
      }
    }
  }

  function filter(preferred) {
    words = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const matches = chapters.filter((item) => matchesText(item.searchText));
    if (words.length) {
      matches.sort(
        (a, b) => Number(matchesText(b.title.toLowerCase())) - Number(matchesText(a.title.toLowerCase()))
      );
    }
    nav.replaceChildren(...matches.map((item) => item.button));
    select(matches.includes(preferred) ? preferred : matches[0] || null);
  }

  for (const chapter of chapters) {
    chapter.searchText = chapter.body.textContent.toLowerCase();
    chapter.button.onclick = () => select(chapter);
    for (const heading of chapter.body.querySelectorAll('h2,h3,h4,h5,h6')) {
      const slug = heading.textContent
        .trim()
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\s_-]/gu, '')
        .replace(/\s+/g, '-');
      heading.id = 'guide-' + slug;
      heading.tabIndex = -1;
      const target = { chapter, heading };
      anchors.set('#' + slug, target);
      anchors.set('#' + encodeURIComponent(slug), target);
    }
    addActions(chapter);
  }
  for (const chapter of chapters) {
    for (const link of chapter.body.querySelectorAll('a[href]')) {
      const href = link.getAttribute('href');
      const target = anchors.get(href);
      if (target) {
        link.removeAttribute('target');
        link.setAttribute('href', '#' + target.heading.id);
        link.onclick = (event) => {
          event.preventDefault();
          search.value = '';
          filter(target.chapter);
          target.heading.focus({ preventScroll: true });
          target.heading.scrollIntoView?.({ block: 'start' });
        };
      } else if (!/^https?:\/\//i.test(href)) {
        link.replaceWith(...link.childNodes);
      }
    }
  }
  search.oninput = () => filter();
  filter(chapters[0]);
}

export async function mountUserGuide(root, signal) {
  try {
    const response = await fetch(language === 'en' ? '/help/user-guide.en.md' : '/help/user-guide.md', {
      signal,
    });
    if (!response.ok) throw new Error('Guide unavailable');
    const source = await response.text();
    if (signal.aborted) return;
    const chapters = readChapters(root.ownerDocument, source);
    if (!chapters.length) throw new Error('Guide has no chapters');
    showReader(root, chapters);
  } catch {
    if (signal.aborted) return;
    root.innerHTML = `<div class="guide-empty"><p role="status">${tr('无法读取本机使用说明。')}</p><button type="button" class="secondary-button" data-open-guide>${tr('重新加载')}</button></div>`;
  }
}
