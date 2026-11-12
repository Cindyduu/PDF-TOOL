/* 页面 2：校验结果页 `#/verify/:batchId`。
 *
 * 用户看完校验结论只有两个真实诉求——「看看报告细节」和「继续处理」。
 * 所以这里只有三个按钮，且「进入自动处理」视觉上最重。
 *
 * 两条行为约束：
 * - **默认折叠**（AC-R23-N2）：十个文件各摊开十行会变成一百行，
 *   用户第一眼要的是结论，不是明细；
 * - **结果只从后端读**（AC-R23-N3）：刷新页面必须还能看到同一份结论，
 *   且不重新跑校验。所以这里只调 `GET /batch/{id}`，从不触发校验。
 */

import { badge, formatSize, html, raw } from '../dom.js';

const ACTIVE = ['verify_queued', 'verifying'];
const POLL_MS = 1200;

const TONES = {
  pass: 'ok',
  warning: 'warn',
  fail: 'danger',
  skipped: 'neutral',
};

export async function render(container, ctx) {
  const batchId = ctx.params.batchId;
  const meta = ctx.meta;

  container.innerHTML = html`<p class="muted">正在读取校验结果…</p>`;

  let jobs = [];
  let timer = null;
  let disposed = false;
  /** 展开过明细的文件。轮询重绘时用它恢复，否则每次刷新都会折叠回去。 */
  const expanded = new Set();
  /** 单文件默认摊开这件事是否已经做过，见 `seedDefaultExpansion`。 */
  let defaultExpanded = false;

  /**
   * 单文件时默认摊开明细：只有一份结果，摊开比折叠有用；多文件时保持折叠，
   * 让汇总行先说话。
   *
   * 必须放在「第一次看到这份结果」的时候做，而不是页面挂载时：从首页点进来
   * 时校验往往还在跑（`verify` 为空），结果要等轮询才回来。只在挂载时判断的
   * 话，同一个单文件批次会「先打开页面就折叠、等跑完再打开就展开」，全看手速。
   *
   * 只摊一次：用户手动收起之后，轮询重绘不能再把它弹开。
   */
  function seedDefaultExpansion() {
    if (defaultExpanded) return;
    if (jobs.length !== 1) return;
    if (!jobs[0].verify) return;
    expanded.add(jobs[0].id);
    defaultExpanded = true;
  }

  function stopPoll() {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  }

  function statusCell(item) {
    const status = item.status;
    const label = meta.verify_status_labels?.[status] || status;
    const icon = meta.verify_status_icons?.[status] || '•';
    const tone = TONES[status] || 'neutral';
    // `skipped` 不显示成 PASS 也不显示成 FAIL，只显示破折号 + 原因；
    // 原因放在 title 里，鼠标停一下就能看到，不占一整列。
    const title = status === 'skipped' ? item.summary || '本次未执行' : item.summary || '';
    return html`<span title="${title}"
      >${badge({ icon, label, tone })}</span
    >`;
  }

  function detailTable(verify) {
    const rows = raw(
      (meta.verify_keys || [])
        .map((key, index) => {
          const item = (verify.items || []).find((entry) => entry.key === key);
          if (!item) {
            return html`<tr data-key="${key}">
              <th scope="row">${index + 1}. ${meta.verify_titles?.[key] || key}</th>
              <td>${badge({ icon: '—', label: '—', tone: 'neutral' })}</td>
              <td class="small">本次未勾选</td>
            </tr>`;
          }
          return html`<tr data-key="${key}">
            <th scope="row">${index + 1}. ${meta.verify_titles?.[key] || key}</th>
            <td>${statusCell(item)}</td>
            <td class="small">${item.summary || ''}</td>
          </tr>`;
        })
        .join(''),
    );

    return html`
      <div class="table-wrap">
        <table>
          <caption class="visually-hidden">十二项校验明细</caption>
          <thead>
            <tr>
              <th scope="col">校验项</th>
              <th scope="col">结果</th>
              <th scope="col">说明</th>
            </tr>
          </thead>
          <tbody>
            ${rows}
          </tbody>
        </table>
      </div>
    `;
  }

  function fileBlock(job) {
    const verify = job.verify;
    const isOpen = expanded.has(job.id);

    if (!verify) {
      const busy = ACTIVE.includes(job.status);
      return html`
        <section class="card verify-file" data-job="${job.id}">
          <div class="card-head">
            <h2>${job.original_name}</h2>
            ${badge({
              icon: meta.job_status_icons?.[job.status] || '•',
              label: meta.job_status_labels?.[job.status] || job.status,
              tone: busy ? 'info' : 'neutral',
            })}
          </div>
          <p class="muted">
            ${busy ? '正在校验，结果出来后这里会自动更新。' : '这个文件没有校验结果。'}
          </p>
        </section>
      `;
    }

    const summary = verify.summary || {};
    const worst = worstStatus(summary);
    return html`
      <section class="card verify-file" data-job="${job.id}">
        <div class="card-head">
          <h2>${job.original_name}</h2>
          ${badge({
            icon: meta.verify_status_icons?.[worst] || '•',
            label: summaryLine(summary, meta),
            tone: TONES[worst] || 'neutral',
          })}
          <button
            type="button"
            class="btn small"
            data-toggle="${job.id}"
            aria-expanded="${isOpen ? 'true' : 'false'}"
          >
            ${isOpen ? '收起明细' : '展开明细'}
          </button>
        </div>
        <p class="hint">
          ${formatSize(verify.file?.size_bytes)} · ${verify.file?.page_count ?? '—'} 页
        </p>
        ${isOpen ? detailTable(verify) : ''}
      </section>
    `;
  }

  function draw() {
    if (disposed) return;

    if (!jobs.length) {
      container.innerHTML = html`
        <section class="card">
          <h1>这个批次里没有文件</h1>
          <div class="btn-row"><a class="btn" href="#/home">回到首页</a></div>
        </section>
      `;
      return;
    }

    const verified = jobs.filter((job) => job.verify);
    const totals = { pass: 0, warning: 0, fail: 0, skipped: 0 };
    verified.forEach((job) => {
      Object.entries(job.verify.summary || {}).forEach(([status, count]) => {
        if (status in totals) totals[status] += Number(count) || 0;
      });
    });

    const running = jobs.some((job) => ACTIVE.includes(job.status));
    const headline = verified.length
      ? `校验完成 · ${jobs.length} 个文件 · 通过 ${totals.pass} 项 / 预警 ${totals.warning} 项 / 不通过 ${totals.fail} 项`
      : running
        ? '正在校验…'
        : '还没有校验结果';

    container.innerHTML = html`
      <div class="card-head">
        <h1>校验结果</h1>
        <a class="btn small" href="#/home">返回首页</a>
      </div>
      <p class="muted" id="verify-headline">${headline}</p>

      <div id="verify-files">${raw(jobs.map(fileBlock).join(''))}</div>

      <section class="card">
        <div class="btn-row">
          <button type="button" class="btn" id="open-report">查看校验报告</button>
          <a class="btn" href="#/home">重新上传重新校验</a>
          <button type="button" class="btn btn-primary" id="to-process">
            进入自动处理
          </button>
        </div>
        <p id="verify-note" class="hint" aria-live="polite"></p>
      </section>
    `;

    container.querySelectorAll('[data-toggle]').forEach((button) => {
      button.addEventListener('click', () => {
        const jobId = button.dataset.toggle;
        if (expanded.has(jobId)) expanded.delete(jobId);
        else expanded.add(jobId);
        draw();
      });
    });

    container.querySelector('#open-report')?.addEventListener('click', () => {
      const note = container.querySelector('#verify-note');
      if (!verified.length) {
        note.textContent = '还没有校验结果，暂时没有可导出的报告。';
        return;
      }
      window.open(ctx.api.batchVerifyReportHtmlUrl(batchId, 'precheck'), '_blank');
    });

    container.querySelector('#to-process')?.addEventListener('click', () => {
      ctx.navigate(`#/process/${batchId}`);
    });
  }

  function schedulePoll() {
    stopPoll();
    if (disposed) return;
    if (!jobs.some((job) => ACTIVE.includes(job.status))) return;
    timer = window.setTimeout(tick, POLL_MS);
  }

  async function tick() {
    timer = null;
    if (disposed) return;
    try {
      const payload = await ctx.api.getBatch(batchId);
      if (disposed) return;
      jobs = payload.jobs || [];
      seedDefaultExpansion();
      draw();
    } catch (error) {
      // 轮询失败不弹错，下一轮继续试
      console.warn('校验结果刷新失败', error);
    } finally {
      schedulePoll();
    }
  }

  try {
    const payload = await ctx.api.getBatch(batchId);
    jobs = payload.jobs || [];
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

  // 首个渲染前把默认展开状态定下来：只有一个文件时摊开更有用，
  // 多个文件时保持折叠，让汇总行先说话。
  seedDefaultExpansion();

  draw();
  schedulePoll();

  return () => {
    disposed = true;
    stopPoll();
  };
}

function worstStatus(summary) {
  if (Number(summary.fail) > 0) return 'fail';
  if (Number(summary.warning) > 0) return 'warning';
  if (Number(summary.pass) > 0) return 'pass';
  return 'skipped';
}

function summaryLine(summary, meta) {
  const parts = (meta.verify_statuses || [])
    .filter((status) => Number(summary[status]) > 0)
    .map((status) => `${meta.verify_status_labels?.[status] || status} ${summary[status]}`);
  return parts.length ? parts.join(' / ') : '—';
}
