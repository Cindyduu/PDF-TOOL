/* 应用装配：加载文案契约 → 挂路由 → 渲染视图。
 *
 * 每次切换视图前先调用上一个视图返回的清理函数：处理中页面的轮询定时器
 * 必须随页面卸载停掉，否则离开之后还在后台打接口。
 */

import { api } from './api.js';
import { el, html } from './dom.js';
import { createRouter, navigate } from './router.js';
import * as backup from './views/backup.js';
import * as batch from './views/batch.js';
import * as config from './views/config.js';
import * as history from './views/history.js';
import * as home from './views/home.js';
import * as processing from './views/processing.js';
import * as result from './views/result.js';

const VIEWS = { home, config, batch, processing, result, history, backup };
const TITLES = {
  home: '上传文件',
  config: '处理配置',
  batch: '批量处理',
  processing: '处理中',
  result: '处理结果',
  history: '历史记录',
  backup: '备份与恢复',
};

const outlet = document.getElementById('route-view');
const toastEl = document.getElementById('toast');

let meta = null;
let router = null;
let cleanup = null;
let toastTimer = null;

export function toast(message) {
  if (!toastEl) return;
  toastEl.textContent = message;
  toastEl.classList.add('is-visible');
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toastEl.classList.remove('is-visible');
  }, 2600);
}

function markNav(name) {
  document.querySelectorAll('#nav a[data-nav]').forEach((link) => {
    if (link.dataset.nav === name) {
      link.setAttribute('aria-current', 'page');
    } else {
      link.removeAttribute('aria-current');
    }
  });
}

function renderFatal(error) {
  outlet.innerHTML = '';
  outlet.append(
    el(html`<section class="card">
      <h1>页面加载失败</h1>
      <p class="error" role="alert">${error.message}</p>
      <p class="muted">${error.hint || ''}</p>
      <div class="btn-row">
        <button type="button" class="btn btn-primary" id="retry-boot">重新加载</button>
      </div>
    </section>`),
  );
  const button = outlet.querySelector('#retry-boot');
  button?.addEventListener('click', () => location.reload());
}

async function mount(route) {
  if (typeof cleanup === 'function') {
    try {
      cleanup();
    } catch (error) {
      console.warn('视图清理失败', error);
    }
  }
  cleanup = null;

  markNav(route.name);
  document.title = `${TITLES[route.name] || 'PDF 自动设置工具'} · PDF 自动设置工具`;

  const view = VIEWS[route.name];
  if (!view) {
    navigate('#/home');
    return;
  }

  outlet.innerHTML = '';
  const ctx = {
    params: route.params || {},
    meta,
    navigate,
    refresh: () => router?.refresh(),
    toast,
    api,
  };

  try {
    const result = await view.render(outlet, ctx);
    if (typeof result === 'function') cleanup = result;
    else if (result && typeof result.destroy === 'function') cleanup = result.destroy;
  } catch (error) {
    console.error(error);
    renderFatal(error);
  }
}

async function boot() {
  outlet.innerHTML = '';
  outlet.append(el(html`<p class="muted">正在加载…</p>`));
  try {
    meta = await api.meta();
  } catch (error) {
    renderFatal(error);
    return;
  }
  router = createRouter(mount);
}

boot();
