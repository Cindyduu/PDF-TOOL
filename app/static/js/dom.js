/* 极小的 DOM 辅助。
 *
 * 为什么不用框架：这是个本地单页工具，六个视图加起来不到一千行；
 * 引一个框架反而要打包、要构建、还要考虑 CDN 断网。
 *
 * 关键是 `html` 模板默认转义插值：文件名、原因代码、错误信息都来自
 * 用户或文件系统，直接拼进 innerHTML 就等于自己开一个注入口子。
 * 需要原样插入时显式用 raw()，一眼能看出哪里是故意的。
 */

const RAW = Symbol('raw');

/** 已转义过的片段：再插回模板时原样输出，不再二次转义。 */
class RawHtml extends String {
  constructor(value) {
    super(String(value ?? ''));
    this[RAW] = String(value ?? '');
  }
}

export function raw(value) {
  return new RawHtml(value);
}

export function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function render(value) {
  if (value === null || value === undefined || value === false) return '';
  if (typeof value === 'object' && RAW in value) return value[RAW];
  if (Array.isArray(value)) return value.map(render).join('');
  return escapeHtml(value);
}

/**
 * 标签模板，插值默认转义。
 *
 * 返回值是 RawHtml（String 的子类），所以 html`` 可以直接赋给 innerHTML，
 * 也可以嵌进另一个 html`` 而不被二次转义；`.join('')` 之后会退化成普通
 * 字符串，那种情况要显式包一层 raw()。
 */
export function html(strings, ...values) {
  let out = strings[0];
  values.forEach((value, index) => {
    out += render(value) + strings[index + 1];
  });
  return new RawHtml(out);
}

/** 由 HTML 片段造一个节点（片段必须只有一个根元素）。 */
export function el(markup) {
  const template = document.createElement('template');
  template.innerHTML = markup.trim();
  const node = template.content.firstElementChild;
  if (!node) throw new Error('片段没有根元素');
  return node;
}

/** 把时间戳（ISO 字符串）转成本地可读格式，坏数据原样显示。 */
export function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

export function formatSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return '—';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}

/** 状态徽标：图标 + 文字，颜色只是第三重提示。 */
export function badge({ icon, label, tone }) {
  return html`<span class="badge" data-tone="${tone || 'neutral'}"><span class="icon" aria-hidden="true">${icon || '•'}</span>${label}</span>`;
}

/** 把任意结构化值渲染成可读文本（before / after 用）。 */
export function formatValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) {
    return value.length ? value.map((item) => formatValue(item)).join('、') : '—';
  }
  if (typeof value === 'object') {
    const parts = Object.entries(value)
      .filter(([, item]) => item !== null && item !== undefined && item !== '')
      .map(([key, item]) => `${key}: ${formatValue(item)}`);
    return parts.length ? parts.join('；') : '—';
  }
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
}
