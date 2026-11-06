/* 备份与恢复。
 *
 * 恢复是两段式：先"校验"拿到摘要与短期令牌，再"确认恢复"。
 * 一步到位的按钮会让人在没看清包里是什么的情况下覆盖现有数据。
 */

import { formatSize, formatTime, html, raw } from '../dom.js';

export async function render(container, ctx) {
  container.innerHTML = html`
    <h1>备份与恢复</h1>
    <p class="muted">
      备份包含全部任务记录与原始/输出 PDF。恢复会先给当前数据做一次自动备份，
      再整体切换；任何一步失败，当前数据都不会被改动。
    </p>

    <section class="card">
      <h2>创建备份</h2>
      <div class="btn-row">
        <button type="button" class="btn btn-primary" id="create-backup">立即创建备份</button>
        <button type="button" class="btn" id="reload-backups">刷新列表</button>
      </div>
      <p id="backup-note" class="hint" aria-live="polite"></p>
      <p id="backup-error" class="error" role="alert"></p>
    </section>

    <section class="card">
      <h2>已有备份</h2>
      <div id="backup-list" aria-live="polite">
        <p class="muted">正在读取…</p>
      </div>
    </section>

    <section class="card">
      <h2>从备份恢复</h2>
      <label class="field" for="restore-file">选择备份文件（.zip）</label>
      <input type="file" id="restore-file" accept=".zip,application/zip" />
      <p id="restore-summary" class="hint" aria-live="polite"></p>
      <p id="restore-notice" class="notice" aria-live="polite"></p>
      <div class="btn-row">
        <button type="button" class="btn" id="restore-inspect" disabled>校验备份</button>
        <button type="button" class="btn btn-primary" id="restore-confirm" disabled>
          确认恢复
        </button>
      </div>
      <p id="restore-error" class="error" role="alert"></p>
    </section>
  `;

  const createButton = container.querySelector('#create-backup');
  const reloadButton = container.querySelector('#reload-backups');
  const listBox = container.querySelector('#backup-list');
  const note = container.querySelector('#backup-note');
  const backupError = container.querySelector('#backup-error');
  const fileInput = container.querySelector('#restore-file');
  const inspectButton = container.querySelector('#restore-inspect');
  const confirmButton = container.querySelector('#restore-confirm');
  const restoreSummary = container.querySelector('#restore-summary');
  const restoreNotice = container.querySelector('#restore-notice');
  const restoreError = container.querySelector('#restore-error');

  let token = null;

  async function loadBackups() {
    backupError.textContent = '';
    try {
      const payload = await ctx.api.listBackups();
      const backups = payload.backups || [];
      if (!backups.length) {
        listBox.innerHTML = html`<p class="empty">还没有备份。</p>`;
        return;
      }
      const rows = raw(
        backups
          .map(
            (item) => html`
              <tr data-backup="${item.id}">
                <th scope="row">${item.id}</th>
                <td class="small">${formatTime(item.created_at)}</td>
                <td class="small">${formatSize(item.size)}</td>
                <td>
                  <a
                    class="btn small"
                    data-action="download-backup"
                    href="${item.download_url || `/api/backups/${item.id}/file`}"
                    download
                    >下载</a
                  >
                </td>
              </tr>
            `,
          )
          .join(''),
      );
      listBox.innerHTML = html`
        <div class="table-wrap">
          <table id="backup-table">
            <caption class="visually-hidden">备份文件列表</caption>
            <thead>
              <tr>
                <th scope="col">备份号</th>
                <th scope="col">创建时间</th>
                <th scope="col">大小</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              ${rows}
            </tbody>
          </table>
        </div>
      `;
    } catch (error) {
      listBox.innerHTML = '';
      backupError.textContent = `备份列表暂时读不到：${error.message}`;
    }
  }

  createButton.addEventListener('click', async () => {
    createButton.disabled = true;
    backupError.textContent = '';
    note.textContent = '正在打包…';
    try {
      const payload = await ctx.api.createBackup();
      note.textContent = `备份已创建：${payload.backup?.id || ''}`;
      ctx.toast('备份已创建');
      await loadBackups();
    } catch (error) {
      note.textContent = '';
      backupError.textContent = [error.message, error.hint].filter(Boolean).join('｜');
    } finally {
      createButton.disabled = false;
    }
  });

  reloadButton.addEventListener('click', loadBackups);

  fileInput.addEventListener('change', () => {
    token = null;
    confirmButton.disabled = true;
    restoreSummary.textContent = '';
    restoreNotice.textContent = '';
    restoreError.textContent = '';
    inspectButton.disabled = !fileInput.files?.length;
  });

  inspectButton.addEventListener('click', async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    inspectButton.disabled = true;
    restoreError.textContent = '';
    restoreSummary.textContent = '正在校验…';
    try {
      const payload = await ctx.api.inspectBackup(file);
      token = payload.token;
      const summary = payload.summary || {};
      restoreSummary.textContent = [
        `备份内任务数：${summary.job_count ?? 0}`,
        `创建时间：${formatTime(summary.created_at)}`,
        `格式版本：${summary.format_version ?? '—'}`,
        `大小：${formatSize(summary.size)}`,
      ].join('｜');
      restoreNotice.textContent = summary.note || '校验通过，可以恢复。恢复会替换当前数据。';
      confirmButton.disabled = !token;
    } catch (error) {
      restoreSummary.textContent = '';
      restoreNotice.textContent = '';
      restoreError.textContent = [error.message, error.hint].filter(Boolean).join('｜');
      confirmButton.disabled = true;
    } finally {
      inspectButton.disabled = false;
    }
  });

  confirmButton.addEventListener('click', async () => {
    if (!token) return;
    confirmButton.disabled = true;
    restoreError.textContent = '';
    restoreNotice.textContent = '正在恢复…';
    try {
      const payload = await ctx.api.restoreBackup(token);
      restoreNotice.textContent = `恢复完成，当前数据版本：${payload.generation_id || '已切换'}`;
      token = null;
      ctx.toast('恢复完成');
      await loadBackups();
    } catch (error) {
      restoreNotice.textContent = '';
      restoreError.textContent = [error.message, error.hint].filter(Boolean).join('｜');
    } finally {
      confirmButton.disabled = true;
    }
  });

  await loadBackups();
}
