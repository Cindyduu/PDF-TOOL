/* 单任务配置页（历史入口）`#/config/:jobId`。
 *
 * v4.0 起新流程不再经过这里（上传统一进准备页），这个页面只从「历史」进入，
 * 用来给一个已存在的任务改豁免页、决定要不要跳过前置校验。
 *
 * v4.0 删掉的东西（R30）：标签检测结果卡片、标签处理方式四选一、
 * 「重新检测标签」按钮。标签相关字段仍然留在老 job.json 里，但这里
 * 既不读也不写——AC-R30-E1 要求代码里不再有 `tag_choice` 的读写。
 */

import { badge, formatSize, formatTime, html } from '../dom.js';

const TERMINAL = ['completed', 'completed_with_warnings', 'failed', 'interrupted'];
const VERIFYING = ['verify_queued', 'verifying'];
const PROCESSING = ['queued', 'running', 'process_queued', 'processing', 'post_verifying'];

export async function render(container, ctx) {
  const jobId = ctx.params.jobId;
  container.innerHTML = html`<p class="muted">正在读取文件信息…</p>`;

  let job;
  try {
    job = await ctx.api.getJob(jobId);
  } catch (error) {
    container.innerHTML = html`
      <section class="card">
        <h1>打不开这个任务</h1>
        <p class="error" role="alert">${error.message}</p>
        <p class="muted">${error.hint}</p>
        <div class="btn-row"><a class="btn" href="#/home">回到首页</a></div>
      </section>
    `;
    return;
  }

  // 已经开始或已经结束的任务不该停在配置页——刷新到这里时直接送到对应页面。
  // 送去哪一页由状态决定：校验中/校验完/处理中各有各的落脚点。
  if (VERIFYING.includes(job.status)) {
    ctx.navigate(`#/verify/${job.batch_id}`);
    return;
  }
  if (job.status === 'verified') {
    ctx.navigate(`#/process/${job.batch_id}`);
    return;
  }
  if (PROCESSING.includes(job.status)) {
    ctx.navigate(`#/done/${job.batch_id}`);
    return;
  }
  if (TERMINAL.includes(job.status)) {
    ctx.navigate(`#/result/${jobId}`);
    return;
  }

  const meta = ctx.meta;
  const config = job.config || {};

  container.innerHTML = html`
    <div class="card-head">
      <h1>配置处理项</h1>
      ${badge({
        icon: meta.job_status_icons[job.status] || '•',
        label: meta.job_status_labels[job.status] || job.status,
        tone: 'info',
      })}
    </div>
    <p class="muted">确认下面的信息后开始处理。处理过程中不需要再操作，也不需要改任何文件。</p>

    <section class="card">
      <h2>文件信息</h2>
      <dl class="kv">
        <dt>文件名</dt><dd>${job.original_name}</dd>
        <dt>页数</dt><dd>${job.source.page_count} 页</dd>
        <dt>大小</dt><dd>${formatSize(job.source.size)}</dd>
        <dt>上传时间</dt><dd>${formatTime(job.created_at)}</dd>
        <dt>任务号</dt><dd>${job.id}</dd>
      </dl>
    </section>

    <section class="card">
      <h2>处理设置</h2>

      <label class="field" for="exempt-pages">豁免页码</label>
      <input
        type="text"
        id="exempt-pages"
        name="exempt-pages"
        value="${config.exempt_pages_raw || ''}"
        placeholder="例如 1-3, 8"
        aria-describedby="exempt-hint"
      />
      <p id="exempt-hint" class="hint">
        这些页不做注释展平与缩放继承。留空表示不豁免。支持 1-3、8 这样的写法。
      </p>

      <div class="option">
        <input
          type="checkbox"
          id="skip-precheck"
          name="skip-precheck"
          ${config.skip_precheck ? 'checked' : ''}
        />
        <label for="skip-precheck">
          不执行前置校验，直接处理
          <span class="hint">
            勾选后跳过校验直接开始；无论勾不勾，处理完成后都会自动跑一遍后置校验。
          </span>
        </label>
      </div>

      <p id="config-error" class="error" role="alert"></p>

      <div class="btn-row">
        <button type="button" class="btn btn-primary" id="start-job">开始处理</button>
        <button type="button" class="btn" id="save-config">仅保存配置</button>
        <a class="btn" href="#/home">取消</a>
      </div>
      <p id="config-note" class="hint" aria-live="polite"></p>
    </section>
  `;

  const errorBox = container.querySelector('#config-error');
  const note = container.querySelector('#config-note');
  const startButton = container.querySelector('#start-job');
  const saveButton = container.querySelector('#save-config');
  const exemptInput = container.querySelector('#exempt-pages');
  const skipBox = container.querySelector('#skip-precheck');

  function showError(error) {
    errorBox.textContent = [error.message, error.hint].filter(Boolean).join('｜');
  }

  function clearError() {
    errorBox.textContent = '';
  }

  function setBusy(busy) {
    [startButton, saveButton].forEach((button) => {
      button.disabled = busy;
    });
    exemptInput.disabled = busy;
    skipBox.disabled = busy;
  }

  async function save({ silent = false } = {}) {
    const updated = await ctx.api.updateConfig(jobId, {
      exempt_pages_raw: exemptInput.value,
      skip_precheck: skipBox.checked,
    });
    job = updated;
    exemptInput.value = updated.config.exempt_pages_raw || '';
    skipBox.checked = Boolean(updated.config.skip_precheck);
    if (!silent) {
      const pages = updated.config.exempt_pages || [];
      note.textContent = pages.length
        ? `配置已保存，豁免页：${pages.join('、')}`
        : '配置已保存';
    }
    return updated;
  }

  saveButton.addEventListener('click', async () => {
    clearError();
    note.textContent = '';
    setBusy(true);
    try {
      await save();
      ctx.toast('配置已保存');
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  });

  startButton.addEventListener('click', async () => {
    clearError();
    note.textContent = '';
    setBusy(true);
    try {
      await save({ silent: true });
      await ctx.api.startJob(jobId);
      ctx.navigate(`#/done/${job.batch_id}`);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  });
}
