/* 批次页（v2.1 遗留入口）`#/batch/:batchId`。
 *
 * v4.0 的动线改成了 prepare → verify → process → done，这一页不再是必经之路，
 * 但旧书签、旧链接还会指过来，所以保留：统一设置 + 一张状态表。
 *
 * v4.0 删掉的东西（R30）：标签处理方式单选组、「标签状态」列、
 * 「重新检测标签」。标签字段留在老 job.json 里，这里既不读也不写。
 */

import { badge, formatTime, html, raw } from '../dom.js';

const TERMINAL = ['completed', 'completed_with_warnings', 'failed', 'interrupted'];
/** 还在跑的六个状态：校验两个 + 处理四个。 */
const ACTIVE = ['verify_queued', 'verifying', 'queued', 'running', 'process_queued', 'processing', 'post_verifying'];
const CONFIGURABLE = ['configuring'];

const TONES = {
  configuring: 'info',
  verify_queued: 'neutral',
  verifying: 'info',
  verified: 'ok',
  queued: 'neutral',
  running: 'info',
  process_queued: 'neutral',
  processing: 'info',
  post_verifying: 'info',
  completed: 'ok',
  completed_with_warnings: 'warn',
  failed: 'danger',
  interrupted: 'danger',
  awaiting_tag_decision: 'warn',
};

const POLL_MS = 1200;

export async function render(container, ctx) {
  const batchId = ctx.params.batchId;
  const meta = ctx.meta;

  container.innerHTML = html`<p class="muted">正在读取批次信息…</p>`;

  let payload;
  try {
    payload = await ctx.api.getBatch(batchId);
  } catch (error) {
    container.innerHTML = html`
      <section class="card">
        <h1>打不开这个批次</h1>
        <p class="error" role="alert">${error.message}</p>
        <p class="muted">${error.hint}</p>
        <div class="btn-row"><a class="btn" href="#/home">回到首页</a></div>
      </section>
    `;
    return undefined;
  }

  let jobs = payload.jobs || [];

  container.innerHTML = html`
    <h1>批量处理</h1>
    <p class="muted">
      本批共 <strong id="batch-count">${jobs.length}</strong> 个文件。可以统一设置，也可以逐个打开单独配置。
    </p>

    <section class="card">
      <h2>统一设置</h2>

      <label class="field" for="batch-exempt">豁免页码</label>
      <input
        type="text"
        id="batch-exempt"
        name="batch-exempt"
        placeholder="例如 1-3, 8（留空表示不豁免）"
        aria-describedby="batch-exempt-hint"
      />
      <p id="batch-exempt-hint" class="hint">
        会按每个文件各自的页数校验；越界的文件会被跳过，不会写入非法页码。
      </p>

      <p id="batch-error" class="error" role="alert"></p>

      <div class="btn-row">
        <button type="button" class="btn" id="apply-config">应用到全部</button>
        <button type="button" class="btn btn-primary" id="start-batch">全部开始</button>
        <button type="button" class="btn" id="download-all">全部下载</button>
        <a class="btn" href="#/home">再传一批</a>
      </div>
      <p id="batch-note" class="hint" aria-live="polite"></p>
    </section>

    <section class="card">
      <h2>文件列表</h2>
      <div id="batch-body" aria-live="polite"></div>
    </section>
  `;

  const body = container.querySelector('#batch-body');
  const countEl = container.querySelector('#batch-count');
  const errorBox = container.querySelector('#batch-error');
  const note = container.querySelector('#batch-note');
  const exemptInput = container.querySelector('#batch-exempt');
  const applyButton = container.querySelector('#apply-config');
  const startButton = container.querySelector('#start-batch');
  const downloadButton = container.querySelector('#download-all');

  let timer = null;
  let disposed = false;

  function showError(message, hint) {
    errorBox.textContent = [message, hint].filter(Boolean).join('｜');
  }

  function clearError() {
    errorBox.textContent = '';
  }

  function actionsFor(job) {
    const id = job.id;
    const batchId = job.batch_id;
    const parts = [];
    if (CONFIGURABLE.includes(job.status)) {
      parts.push(html`<a class="btn small" href="#/config/${id}">单独配置</a>`);
    } else if (ACTIVE.includes(job.status)) {
      // 校验阶段和处理阶段各有各的页面，去错了看不到进度
      const target = ['verify_queued', 'verifying'].includes(job.status)
        ? `#/verify/${batchId}`
        : `#/done/${batchId}`;
      parts.push(html`<a class="btn small" href="${target}">查看进度</a>`);
    } else {
      parts.push(html`<a class="btn small" href="#/result/${id}">查看结果</a>`);
    }
    if (job.output) {
      parts.push(html`<a
        class="btn small"
        data-action="download"
        href="${ctx.api.fileUrl(id, 'output')}"
        download="${job.output.file_name}"
        >下载</a
      >`);
    }
    return raw(parts.join(''));
  }

  function renderRows() {
    if (!jobs.length) {
      body.innerHTML = html`<p class="empty">这一批没有文件。</p>`;
      return;
    }

    const rows = raw(
      jobs
        .map((job) => {
          const tone = TONES[job.status] || 'neutral';
          const pageCount = job.source?.page_count ?? '—';
          return html`
            <tr data-job="${job.id}">
              <th scope="row">
                ${job.original_name}
                <span class="hint">${job.id}</span>
              </th>
              <td class="small">${pageCount} 页</td>
              <td>
                ${badge({
                  icon: meta.job_status_icons[job.status] || '•',
                  label: meta.job_status_labels[job.status] || job.status,
                  tone,
                })}
                ${job.last_error ? html`<div class="hint">${job.last_error.message || ''}</div>` : ''}
              </td>
              <td class="small">${formatTime(job.created_at)}</td>
              <td>
                <div class="btn-row">${actionsFor(job)}</div>
              </td>
            </tr>
          `;
        })
        .join(''),
    );

    body.innerHTML = html`
      <div class="table-wrap">
        <table id="batch-table">
          <caption class="visually-hidden">批次内的文件</caption>
          <thead>
            <tr>
              <th scope="col">文件</th>
              <th scope="col">页数</th>
              <th scope="col">处理状态</th>
              <th scope="col">上传时间</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            ${rows}
          </tbody>
        </table>
      </div>
    `;
  }

  function summary() {
    const done = jobs.filter((job) => TERMINAL.includes(job.status)).length;
    const running = jobs.filter((job) => ACTIVE.includes(job.status)).length;
    if (!running && done === jobs.length && jobs.length) {
      note.textContent = `全部结束：${done} 个文件已处理完`;
    } else if (running) {
      note.textContent = `处理中：${done} / ${jobs.length} 已完成`;
    }
    countEl.textContent = String(jobs.length);
  }

  function schedulePoll() {
    stopPoll();
    if (disposed) return;
    if (!jobs.some((job) => ACTIVE.includes(job.status))) return;
    timer = window.setTimeout(tick, POLL_MS);
  }

  function stopPoll() {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  }

  async function tick() {
    timer = null;
    if (disposed) return;
    try {
      const fresh = await ctx.api.getBatch(batchId);
      if (disposed) return;
      jobs = fresh.jobs || [];
      renderRows();
      summary();
    } catch (error) {
      // 轮询失败不弹错，下一轮继续试；真正的失败在按钮操作里报
      console.warn('批次状态刷新失败', error);
    } finally {
      schedulePoll();
    }
  }

  applyButton.addEventListener('click', async () => {
    clearError();
    note.textContent = '正在应用设置…';
    applyButton.disabled = true;
    try {
      const patch = { exempt_pages_raw: exemptInput.value };
      const result = await ctx.api.patchBatchConfig(batchId, patch);
      const updated = (result.updated || []).length;
      const invalid = (result.invalid || []).length;
      note.textContent = invalid
        ? `已更新 ${updated} 个，${invalid} 个未能应用（页码越界或已开始处理）`
        : `已更新 ${updated} 个文件的设置`;
      if (invalid) {
        showError(
          '部分文件没能应用统一设置',
          (result.invalid || [])
            .map((item) => `${item.name}：${item.message}`)
            .join('；'),
        );
      }
      await tick();
    } catch (error) {
      note.textContent = '';
      showError(error.message, error.hint);
    } finally {
      applyButton.disabled = false;
    }
  });

  startButton.addEventListener('click', async () => {
    clearError();
    note.textContent = '正在开始处理…';
    startButton.disabled = true;
    try {
      const result = await ctx.api.startBatch(batchId);
      const started = (result.started || []).length;
      const skipped = (result.skipped || []).length;
      note.textContent = skipped
        ? `已开始 ${started} 个，${skipped} 个被跳过`
        : `已开始 ${started} 个文件`;
      if (skipped) {
        showError(
          '有文件没能开始',
          (result.skipped || [])
            .map((item) => `${item.name}：${item.message}`)
            .join('；'),
        );
      }
      await tick();
      schedulePoll();
    } catch (error) {
      note.textContent = '';
      showError(error.message, error.hint);
    } finally {
      startButton.disabled = false;
    }
  });

  downloadButton.addEventListener('click', () => {
    const ready = jobs.filter((job) => job.output);
    if (!ready.length) {
      showError('还没有可下载的文件', '等文件处理完成后再点一次');
      return;
    }
    // 逐个触发：浏览器对同时发起的多个下载会拦截，间隔一下更可靠
    ready.forEach((job, index) => {
      window.setTimeout(() => {
        const anchor = document.createElement('a');
        anchor.href = ctx.api.fileUrl(job.id, 'output');
        anchor.download = job.output.file_name;
        document.body.append(anchor);
        anchor.click();
        anchor.remove();
      }, index * 350);
    });
    note.textContent = `开始下载 ${ready.length} 个文件`;
  });

  renderRows();
  summary();
  schedulePoll();

  return () => {
    disposed = true;
    stopPoll();
  };
}
