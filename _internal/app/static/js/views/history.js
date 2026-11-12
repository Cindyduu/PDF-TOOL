/* 历史记录：所有任务都在这里，文件不在了也照样列出来。
 *
 * "记录还在、文件没了"是真实会发生的（用户手工整理目录、恢复备份失败等）。
 * 这时把行藏起来才是最糟的选择——用户会以为任务丢了。这里如实标出
 * "输出缺失 / 原件缺失"，并把对应操作禁用掉。
 */

import { badge, formatTime, html, raw } from '../dom.js';

const TONES = {
  configuring: 'info',
  awaiting_tag_decision: 'warn',
  queued: 'neutral',
  running: 'info',
  completed: 'ok',
  completed_with_warnings: 'warn',
  failed: 'danger',
  interrupted: 'danger',
};

const CONFIG_STATUSES = ['configuring', 'awaiting_tag_decision'];
const ACTIVE_STATUSES = ['queued', 'running'];

export async function render(container, ctx) {
  const meta = ctx.meta;

  const statusOptions = raw(
    [
      html`<option value="">全部状态</option>`,
      ...(meta.job_statuses || []).map(
        (status) => html`<option value="${status}">${meta.job_status_labels[status] || status}</option>`,
      ),
    ].join(''),
  );

  container.innerHTML = html`
    <h1>历史记录</h1>
    <p class="muted">
      记录不会因为刷新或重启消失。文件被移走之后记录仍然在，只是会标成"缺失"。
    </p>

    <section class="card">
      <div class="filters">
        <div>
          <label class="field" for="history-search">搜索</label>
          <input type="search" id="history-search" placeholder="文件名或任务号" />
        </div>
        <div>
          <label class="field" for="history-status">状态</label>
          <select id="history-status">
            ${statusOptions}
          </select>
        </div>
      </div>
      <p id="history-error" class="error" role="alert"></p>
      <div id="history-body" aria-live="polite">
        <p class="muted">正在读取…</p>
      </div>
    </section>
  `;

  const searchInput = container.querySelector('#history-search');
  const statusSelect = container.querySelector('#history-status');
  const body = container.querySelector('#history-body');
  const errorBox = container.querySelector('#history-error');

  let debounce = null;
  let requestId = 0;

  async function load() {
    const current = ++requestId;
    errorBox.textContent = '';
    try {
      const payload = await ctx.api.listJobs({
        q: searchInput.value.trim(),
        status: statusSelect.value,
      });
      if (current !== requestId) return; // 慢请求不许覆盖新结果
      renderList(payload.jobs || []);
    } catch (error) {
      if (current !== requestId) return;
      body.innerHTML = '';
      errorBox.textContent = [error.message, error.hint].filter(Boolean).join('｜');
    }
  }

  function renderList(jobs) {
    if (!jobs.length) {
      body.innerHTML = html`<p class="empty">没有符合条件的记录。</p>`;
      return;
    }

    const rows = raw(
      jobs
        .map((job) => {
          const status = job.status;
          const tone = TONES[status] || 'neutral';
          const openTarget = ACTIVE_STATUSES.includes(status)
            ? `#/processing/${job.id}`
            : CONFIG_STATUSES.includes(status)
              ? `#/config/${job.id}`
              : `#/result/${job.id}`;

          const download = job.output_available
            ? html`<a
                class="btn small"
                data-action="download"
                href="${ctx.api.fileUrl(job.id, 'output')}"
                download="${job.output_name}"
                >下载</a
              >`
            : html`<span class="btn small" data-action="download" aria-disabled="true"
                >下载（文件缺失）</span
              >`;

          const reprocess = job.source_available
            ? html`<button type="button" class="btn small" data-action="reprocess" data-job="${job.id}">
                重新处理
              </button>`
            : html`<span class="btn small" data-action="reprocess" aria-disabled="true"
                >重新处理（原件缺失）</span
              >`;

          return html`
            <tr data-job="${job.id}">
              <th scope="row">
                ${job.original_name}
                <span class="hint">${job.id}</span>
              </th>
              <td>
                ${badge({
                  icon: meta.job_status_icons[status] || '•',
                  label: meta.job_status_labels[status] || status,
                  tone,
                })}
              </td>
              <td class="small">${formatTime(job.created_at)}</td>
              <td class="small">
                ${job.output_available ? '输出可用' : '输出缺失'}｜${job.source_available
                  ? '原件可用'
                  : '原件缺失'}
                ${job.review_required ? '｜含待复核项' : ''}
              </td>
              <td>
                <div class="btn-row">
                  <a class="btn small" data-action="open" href="${openTarget}">查看</a>
                  ${download} ${reprocess}
                </div>
              </td>
            </tr>
          `;
        })
        .join(''),
    );

    body.innerHTML = html`
      <div class="table-wrap">
        <table id="history-table">
          <caption class="visually-hidden">历史任务列表</caption>
          <thead>
            <tr>
              <th scope="col">文件</th>
              <th scope="col">状态</th>
              <th scope="col">创建时间</th>
              <th scope="col">文件可用性</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            ${rows}
          </tbody>
        </table>
      </div>
      <p class="hint">共 ${jobs.length} 条记录</p>
    `;

    body.querySelectorAll('button[data-action="reprocess"]').forEach((button) => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        errorBox.textContent = '';
        try {
          const fresh = await ctx.api.reprocess(button.dataset.job);
          ctx.toast('已按原配置新建任务');
          ctx.navigate(`#/config/${fresh.id}`);
        } catch (error) {
          errorBox.textContent = [error.message, error.hint].filter(Boolean).join('｜');
          button.disabled = false;
        }
      });
    });
  }

  searchInput.addEventListener('input', () => {
    window.clearTimeout(debounce);
    debounce = window.setTimeout(load, 220);
  });

  statusSelect.addEventListener('change', load);

  await load();

  return () => window.clearTimeout(debounce);
}
