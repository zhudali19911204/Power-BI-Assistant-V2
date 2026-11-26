import { tr } from './i18n.js';
const CHAT_MIN_WIDTH = 480,
  MOBILE_BREAKPOINT = 1050;

/** Coordinate both sidebars while preserving their requested widths across smaller windows. */
export function createWorkspaceLayout({
  root,
  panel,
  handle,
  toggle,
  sidebar = null,
  win = window,
  viewportWidth = () => globalThis.innerWidth,
  save = async () => {},
  onNotice = () => {},
}) {
  let drag = null,
    saves = Promise.resolve();
  const mobile = () => viewportWidth() <= MOBILE_BREAKPOINT;
  const visible = () =>
    mobile() ? panel.classList.contains('mobile-open') : !panel.classList.contains('collapsed');
  const close = panel.querySelector('#context-close');
  const shell = sidebar?.panel.parentElement,
    resizeRoot = shell || root;
  const available = () => shell?.clientWidth || viewportWidth();
  const clamp = (item, value) => Math.round(Math.max(item.min, Math.min(item.maximum(), value)));
  const width = (item) => clamp(item, item.preferred ?? item.defaults());
  const left = sidebar
    ? {
        ...sidebar,
        key: 'sidebar_width',
        property: '--sidebar-width',
        min: 215,
        max: 400,
        direction: 1,
        defaults: () => (viewportWidth() >= 1600 ? 265 : viewportWidth() <= 1200 ? 215 : 248),
        enabled: () => viewportWidth() > 700,
        // A visible model panel must still fit at its minimum width.
        maximum: () =>
          Math.max(215, Math.min(400, available() - CHAT_MIN_WIDTH - (!mobile() && visible() ? 240 : 0))),
      }
    : null;
  const right = {
    panel,
    handle,
    key: 'context_width',
    property: '--context-panel-width',
    min: 240,
    max: 520,
    direction: -1,
    defaults: () => (viewportWidth() >= 1600 ? 300 : viewportWidth() <= 1200 ? 240 : 278),
    enabled: () => !mobile() && visible(),
    maximum: () =>
      Math.max(
        240,
        Math.min(
          520,
          (left
            ? available() - (left.enabled() ? width(left) : 65)
            : root.clientWidth || viewportWidth() - 248) - CHAT_MIN_WIDTH
        )
      ),
  };
  const panels = [left, right].filter(Boolean);
  for (const item of panels) {
    item.preferred = null;
    item.touched = false;
  }
  function render() {
    for (const item of panels) {
      const value = width(item);
      item.panel.style.setProperty(item.property, `${value}px`);
      item.handle.setAttribute('aria-valuemin', String(item.min));
      item.handle.setAttribute('aria-valuemax', String(item.maximum()));
      item.handle.setAttribute('aria-valuenow', String(value));
      item.handle.setAttribute('aria-valuetext', tr('{0} 像素', value));
      item.handle.hidden = !item.enabled();
    }
    toggle.setAttribute('aria-expanded', String(visible()));
    toggle.title = visible() ? tr('收起模型字段面板') : tr('展开模型字段面板');
    toggle.setAttribute('aria-label', toggle.title);
  }
  function persist(item) {
    item.touched = true;
    const value = { [item.key]: item.preferred };
    // Serialize partial updates so the two panels never overwrite each other's preference.
    saves = saves
      .then(() => save(value))
      .catch(() => onNotice(tr('面板宽度暂未保存，重启后可能恢复默认宽度'), true));
  }
  function finish(commit) {
    if (!drag) return;
    const previous = drag,
      { item } = previous;
    drag = null;
    resizeRoot.classList.remove('resizing-context');
    if (!commit) item.preferred = previous.preferred;
    if (item.handle.hasPointerCapture?.(previous.id)) item.handle.releasePointerCapture(previous.id);
    render();
    if (commit && item.preferred !== previous.preferred) persist(item);
  }
  for (const item of panels) {
    const target = item.handle;
    target.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || event.isPrimary === false || !item.enabled() || drag) return;
      event.preventDefault();
      target.focus();
      item.touched = true;
      drag = { item, id: event.pointerId, x: event.clientX, width: width(item), preferred: item.preferred };
      target.setPointerCapture?.(event.pointerId);
      resizeRoot.classList.add('resizing-context');
    });
    target.addEventListener('pointermove', (event) => {
      if (drag?.item !== item || event.pointerId !== drag.id) return;
      item.preferred = clamp(item, drag.width + item.direction * (event.clientX - drag.x));
      render();
    });
    target.addEventListener('pointerup', (event) => {
      if (drag?.item === item && drag.id === event.pointerId) finish(true);
    });
    target.addEventListener('pointercancel', (event) => {
      if (drag?.item === item && drag.id === event.pointerId) finish(false);
    });
    target.addEventListener('lostpointercapture', () => {
      if (drag?.item === item) finish(false);
    });
    target.addEventListener('dblclick', () => {
      if (!item.enabled()) return;
      finish(false);
      item.preferred = null;
      render();
      persist(item);
    });
    target.addEventListener('keydown', (event) => {
      if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === 'Escape' && drag) {
        event.preventDefault();
        finish(false);
        return;
      }
      if (!item.enabled() || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      finish(false);
      const step = (event.shiftKey ? 64 : 16) * item.direction;
      const next =
        event.key === 'Home'
          ? item.min
          : event.key === 'End'
            ? item.maximum()
            : clamp(item, width(item) + (event.key === 'ArrowRight' ? step : -step));
      if (next === item.preferred) return;
      item.preferred = next;
      render();
      persist(item);
    });
  }
  function dismissOverlay() {
    if (!mobile() || !visible()) return;
    panel.classList.remove('mobile-open');
    render();
    toggle.focus();
  }
  close?.addEventListener('click', dismissOverlay);
  panel.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || event.isComposing || event.keyCode === 229 || !mobile() || !visible())
      return;
    event.preventDefault();
    dismissOverlay();
  });
  toggle.addEventListener('click', () => {
    finish(false);
    if (mobile() && visible()) {
      dismissOverlay();
      return;
    }
    panel.classList.toggle(mobile() ? 'mobile-open' : 'collapsed');
    render();
    if (mobile()) {
      const search = panel.classList.contains('has-model')
        ? panel.querySelector('#field-search:not(:disabled)')
        : null;
      (search || close)?.focus();
    }
  });
  win.addEventListener('resize', () => {
    finish(false);
    render();
  });
  win.addEventListener('blur', () => finish(false));
  render();
  return {
    dismissOverlay,
    render,
    restore(value) {
      for (const item of panels) {
        if (item.touched) continue;
        const candidate = value?.[item.key];
        item.preferred =
          Number.isInteger(candidate) && candidate >= item.min && candidate <= item.max ? candidate : null;
      }
      render();
    },
  };
}
