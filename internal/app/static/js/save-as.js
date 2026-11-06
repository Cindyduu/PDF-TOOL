/* 「另存为」面板：问用户要一个文件名，并告诉他文件会落到哪里。
 *
 * 为什么用原生 <dialog> 而不是自己拼遮罩：焦点陷阱、Esc 关闭、背景惰性
 * 都由浏览器管。这三样自己实现比看起来麻烦得多，而且很容易漏掉无障碍。
 *
 * 这个模块只负责"问名字"，不负责"怎么存"。两者分开的理由很实际：面板
 * 要在任何浏览器里都能工作，而"选位置"只有部分浏览器支持——把两件事
 * 搅在一起，降级时就得改面板逻辑。
 */

import { html } from './dom.js';
import { validateName } from './filename.js';

const HINT_PICKER = '点「保存」会弹出系统窗口，可以选择保存位置。';
const HINT_DOWNLOAD = '当前浏览器不支持选择位置，文件会存到默认下载目录；名字可以改。';

const PDF_TYPES = [
  { description: 'PDF 文件', accept: { 'application/pdf': ['.pdf'] } },
];

/** 按浏览器能力给出诚实的提示，别承诺做不到的事。 */
function defaultLocationHint() {
  return typeof window.showSaveFilePicker === 'function' ? HINT_PICKER : HINT_DOWNLOAD;
}

let dialog = null;
let resolvePending = null;
let state = { ok: false, name: '', error: '', notice: '' };

/** 按输入框当前内容重算校验，并把结果贴回界面。 */
function refresh() {
  const input = dialog.querySelector('#save-as-name');
  const errorEl = dialog.querySelector('#save-as-error');
  const noticeEl = dialog.querySelector('#save-as-notice');
  const confirmEl = dialog.querySelector('#save-as-confirm');

  state = validateName(input.value);
  errorEl.textContent = state.ok ? '' : state.error;
  noticeEl.textContent = state.ok ? state.notice : '';
  confirmEl.disabled = !state.ok;
  input.setAttribute('aria-invalid', state.ok ? 'false' : 'true');
}

function buildDialog() {
  const node = document.createElement('dialog');
  node.className = 'save-as';
  node.setAttribute('aria-labelledby', 'save-as-title');
  node.innerHTML = html`
    <form method="dialog" id="save-as-form">
      <h2 id="save-as-title">保存处理后的 PDF</h2>
      <dl class="kv small">
        <dt>原文件</dt>
        <dd id="save-as-original">—</dd>
        <dt>大小</dt>
        <dd id="save-as-size">—</dd>
      </dl>
      <label class="field" for="save-as-name">文件名</label>
      <input type="text" id="save-as-name" autocomplete="off" spellcheck="false" />
      <p class="field-error" id="save-as-error" role="alert"></p>
      <p class="hint" id="save-as-notice"></p>
      <p class="hint" id="save-as-location"></p>
      <div class="btn-row">
        <button type="submit" value="save" class="btn btn-primary" id="save-as-confirm">
          保存
        </button>
        <button type="submit" value="cancel" class="btn" id="save-as-cancel">取消</button>
      </div>
    </form>
  `;

  node.querySelector('#save-as-name').addEventListener('input', refresh);

  // 名字不合法时拦住提交：光靠禁用按钮挡不住在输入框里按回车
  node.querySelector('#save-as-form').addEventListener('submit', (event) => {
    if (!state.ok) {
      event.preventDefault();
      node.querySelector('#save-as-name').focus();
    }
  });

  node.addEventListener('close', () => {
    // 只有"确认且当前合法"才算数；Esc 和取消都走这里
    const accepted = node.returnValue === 'save' && state.ok;
    const resolver = resolvePending;
    resolvePending = null;
    if (resolver) resolver(accepted ? state.name : null);
  });

  document.body.append(node);
  return node;
}

/**
 * 弹出面板，等用户确认一个文件名。
 *
 * @param {{fileName: string, originalName?: string, sizeText?: string, locationHint?: string}} options
 * @returns {Promise<string|null>} 确认的文件名；取消或关掉则 null
 */
export function askSaveName({ fileName, originalName, sizeText, locationHint } = {}) {
  if (!dialog) dialog = buildDialog();
  // 老浏览器没有 showModal 时直接返回 null，调用方退回原生链接下载
  if (typeof dialog.showModal !== 'function') return Promise.resolve(null);

  if (dialog.open) dialog.close('cancel');
  // returnValue 会跨次保留，不清掉的话上次的 'save' 会被当成这次的结果
  dialog.returnValue = '';

  dialog.querySelector('#save-as-original').textContent = originalName || '—';
  dialog.querySelector('#save-as-size').textContent = sizeText || '—';
  dialog.querySelector('#save-as-location').textContent =
    locationHint || defaultLocationHint();

  const input = dialog.querySelector('#save-as-name');
  input.value = fileName || '';
  refresh();

  dialog.showModal();
  input.focus();
  input.select(); // 预填名全选，想改名直接打字覆盖

  return new Promise((resolve) => {
    resolvePending = resolve;
  });
}

/** 把新名字同时放进 URL 和 download 属性。 */
function namedUrl(url, name) {
  const target = new URL(url, window.location.origin);
  target.searchParams.set('name', name);
  return target.href;
}

/** 用给定名字触发一次浏览器下载（落盘位置由浏览器决定）。
 *
 * 名字既写进 `download` 属性，也拼进 URL：Chrome 在同源下载时优先采信
 * 服务端 `Content-Disposition` 里的文件名，光靠 `download` 属性改不动名。
 * 两处都给，是为了让「能读 header 的浏览器」和「只认 download 的浏览器」
 * 拿到同一个名字。
 */
function downloadWithName(url, name) {
  const anchor = document.createElement('a');
  anchor.href = namedUrl(url, name);
  anchor.download = name;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

/**
 * 保存处理后的 PDF：能选位置就让用户选，不能就退回普通下载。
 *
 * 返回 'picker'（写进了用户选的位置）/ 'download'（落到默认下载目录）/
 * 'cancelled'（用户在系统窗口里点了取消）。调用方靠这个值决定要不要提示，
 * 所以不吞掉结果。
 *
 * @param {string} url 下载地址
 * @param {string} name 用户确认过的文件名
 * @returns {Promise<'picker'|'download'|'cancelled'>}
 */
export async function saveFile(url, name) {
  const target = namedUrl(url, name);

  if (typeof window.showSaveFilePicker !== 'function') {
    downloadWithName(url, name);
    return 'download';
  }

  let handle;
  try {
    handle = await window.showSaveFilePicker({ suggestedName: name, types: PDF_TYPES });
  } catch (error) {
    if (error && error.name === 'AbortError') return 'cancelled';
    // NotAllowedError：用户激活已过期（面板关到这里的间隔太长）；
    // SecurityError：跨域 iframe 或被策略禁掉。这两种都不该让用户白点一次，
    // 退回普通下载——名字仍然是对的，只是位置回到默认下载目录。
    downloadWithName(url, name);
    return 'download';
  }

  // 先拿到响应再建写入流：fetch 失败时不该在磁盘上留下一个空文件
  const response = await fetch(target);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);

  const writable = await handle.createWritable();
  try {
    // 流式写，大文件不进内存；pipeTo 成功会自动 close()
    await response.body.pipeTo(writable);
  } catch (error) {
    await writable.abort().catch(() => {});
    throw error;
  }
  return 'picker';
}
