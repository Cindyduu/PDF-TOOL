/* 页面 4：处理结果 + 后置校验 `#/done/:batchId`。
 *
 * 这一页要同时回答两个问题：「产物在哪」和「改完到底合不合规」。
 * 所以每份文件都必须同时给出下载入口与后置校验结论，缺一个用户就得回别处找。
 *
 * 三条行为约束：
 * - **处理明细只列勾选过的**（§3.5）：没勾的项也跑在流水线里，但把九行全列出来
 *   会让人以为"我勾了十项"，所以按 `config.process_items` 过滤；
 * - **后置校验固定九行**：哪怕某项被跳过也占一行，显示破折号 + 原因，
 *   少一行就会被读成"这一项没问题"；
 * - **失败的文件也要显示**：它没有产物、没有后置校验，但它是「失败 Y」里的
 *   那一个 Y，藏起来数字就对不上。
 */

import { badge, formatSize, html, raw } from '../dom.js';
import { askSaveName, saveFile } from '../save-as.js';

/** 还在跑的四种状态。到了终态就停止轮询，别让页面一直发请求。 */
const PROCESS_ACTIVE = ['queued', 'running', 'process_queued', 'processing', 'post_verifying'];
const TERMINAL = ['completed', 'completed_with_warnings', 'failed', 'interrupted'];
const POLL_MS = 1200;
/** 批量下载的间隔。浏览器会拦同一刻发起的多个下载。 */
const DOWNLOAD_GAP_MS = 350;

const RESULT_TONES = {
  adjusted: 'ok',
  unchanged: 'neutral',
  not_applicable: 'neutral',
  review_required: 'warn',
  failed: 'danger',
};

const VERIFY_TONES = {
  pass: 'ok',
  warning: 'warn',
  fail: 'danger',
  skipped: 'neutral',
};

const JOB_TONES = {
  completed: 'ok',
  completed_with_warnings: 'warn',
  failed: 'danger',
  interrupted: 'danger',
};

export async function render(container, ctx) {
  const batchId = ctx.params.batchId;
  const meta = ctx.meta;

  container.innerHTML = html`<p class="muted">正在读取处理结果…</p>`;

  let data = null;
  let timer = null;
  let disposed = false;

  function stopPoll() {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  }

  function draw() {
    if (disposed || !data) return;

    const files = data.files || [];
    const summary = data.summary || {};

    if (!files.length) {
      container.innerHTML = html`
        <section class="card">
          <h1>这个批次里没有文件</h1>
          <div class="btn-row"><a class="btn" href="#/home">回到首页</a></div>
        </section>
      `;
      return;
    }

    const running = files.some((file) => PROCESS_ACTIVE.includes(file.status));
    const headline = running
      ? `正在处理 · ${summary.files} 个文件 · 已完成 ${summary.succeeded} / 失败 ${summary.failed}`
      : `处理完成 · ${summary.files} 个文件 · 成功 ${summary.succeeded} / 失败 ${summary.failed}`;

    const logsReady = files.some((file) => file.process_log_path);

    container.innerHTML = html`
      <div class="card-head">
        <h1>处理结果</h1>
        <a class="btn small" href="#/home">返回首页</a>
      </div>
      <p class="muted" id="done-headline">${headline}</p>

      <div id="done-files">
        ${raw(files.map((file) => fileBlock(file, meta, ctx)).join(''))}
      </div>

      <section class="card">
        <div class="btn-row">
          <button type="button" class="btn" id="export-verify">导出校验报告</button>
          <button type="button" class="btn" id="download-all">批量下载</button>
          <button type="button" class="btn" id="export-log" ${logsReady ? '' : raw('disabled')}>
            导出处理日志
          </button>
          <a class="btn" href="#/home">返回首页</a>
        </div>
        <p class="hint">
          ${logsReady
            ? '处理日志按文件记录了每一项改动前后的值。'
            : '这一批没有勾选「生成处理日志」，所以没有可导出的日志。需要的话可以在处理页重新勾选后再跑一次。'}
        </p>
        <p id="done-note" class="hint" aria-live="polite"></p>
        <p id="done-error" class="error" role="alert"></p>
      </section>

      <p class="hint" id="done-disclaimer">${meta.disclaimer || ''}</p>
    `;

    wire(files);
  }

  function wire(files) {
    const note = container.querySelector('#done-note');
    const errorBox = container.querySelector('#done-error');

    container.querySelectorAll('[data-action]').forEach((node) => {
      node.addEventListener('click', () => {
        const jobId = node.dataset.job;
        const action = node.dataset.action;
        const file = files.find((item) => item.job_id === jobId);
        if (!file) return;
        errorBox.textContent = '';
        if (action === 'download') saveOne(file, errorBox);
        else if (action === 'preview') previewOne(file, node);
        else if (action === 'report') window.open(ctx.api.jobVerifyReportHtmlUrl(jobId, 'post'), '_blank');
      });
    });

    container.querySelector('#export-verify')?.addEventListener('click', () => {
      const hasPost = files.some((file) => file.post_verify);
      if (!hasPost) {
        note.textContent = '还没有后置校验结果，暂时没有可导出的报告。';
        return;
      }
      window.open(ctx.api.batchVerifyReportHtmlUrl(batchId, 'post'), '_blank');
    });

    container.querySelector('#download-all')?.addEventListener('click', () => {
      const ready = files.filter((file) => file.output);
      if (!ready.length) {
        note.textContent = '还没有可下载的文件。';
        return;
      }
      // 逐个触发：同一刻发起多个下载会被浏览器拦掉一部分
      ready.forEach((file, index) => {
        window.setTimeout(() => {
          const anchor = document.createElement('a');
          anchor.href = ctx.api.fileUrl(file.job_id, 'output');
          anchor.download = file.output.file_name;
          document.body.append(anchor);
          anchor.click();
          anchor.remove();
        }, index * DOWNLOAD_GAP_MS);
      });
      note.textContent = `开始下载 ${ready.length} 个文件，产物同时留在历史记录里，随时可以再下。`;
    });

    container.querySelector('#export-log')?.addEventListener('click', () => {
      if (!files.some((file) => file.process_log_path)) {
        note.textContent = '这一批没有勾选「生成处理日志」。';
        return;
      }
      window.open(ctx.api.batchProcessLogHtmlUrl(batchId), '_blank');
    });

    // 预览按钮的 iframe 塞在各自的 slot 里，重绘后由 draw 恢复；
    // 这里只处理"点开"的那一下。
    function previewOne(file, button) {
      const slot = container.querySelector(`[data-preview="${file.job_id}"]`);
      if (!slot) return;
      if (slot.firstElementChild) {
        slot.innerHTML = '';
        button.textContent = '预览';
        return;
      }
      const url = ctx.api.fileUrl(file.job_id, 'output');
      slot.innerHTML = raw(
        `<iframe class="preview-frame" title="${escapeAttr(file.original_name)} 预览" src="${url}"></iframe>`,
      );
      button.textContent = '收起预览';
    }
  }

  async function saveOne(file, errorBox) {
    // 没有 <dialog> 的浏览器不拦，直接走原生下载——新功能不能把出口堵死
    if (typeof HTMLDialogElement === 'undefined') {
      window.location.href = ctx.api.fileUrl(file.job_id, 'output');
      return;
    }
    const name = await askSaveName({
      fileName: file.output.file_name,
      originalName: file.original_name,
      sizeText: formatSize(file.output.size),
    });
    if (!name) return;
    try {
      await saveFile(ctx.api.fileUrl(file.job_id, 'output'), name);
    } catch (error) {
      errorBox.textContent = '保存失败：文件可能已被移动，或网络中断。可以再点一次下载重试。';
    }
  }

  function schedulePoll() {
    stopPoll();
    if (disposed) return;
    const files = data?.files || [];
    if (!files.some((file) => PROCESS_ACTIVE.includes(file.status))) return;
    timer = window.setTimeout(tick, POLL_MS);
  }

  async function tick() {
    timer = null;
    if (disposed) return;
    try {
      const fresh = await ctx.api.getBatchResults(batchId);
      if (disposed) return;
      // 内容没变就不重绘：重绘会打断用户正在看的预览 iframe
      if (fingerprint(fresh) !== fingerprint(data)) {
        data = fresh;
        draw();
      } else {
        data = fresh;
      }
    } catch (error) {
      console.warn('处理结果刷新失败', error);
    } finally {
      schedulePoll();
    }
  }

  try {
    data = await ctx.api.getBatchResults(batchId);
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

  draw();
  schedulePoll();

  return () => {
    disposed = true;
    stopPoll();
  };
}

/* ------------------------------------------------------------------ */

function fileBlock(file, meta, ctx) {
  const status = file.status;
  const ok = status === 'completed' || status === 'completed_with_warnings';
  const finished = TERMINAL.includes(status);

  const tone = JOB_TONES[status] || (finished ? 'neutral' : 'info');
  const label = meta.job_status_labels?.[status] || status;

  const title = ok
    ? '处理成功'
    : status === 'interrupted'
      ? '处理中断'
      : finished
        ? '处理失败'
        : '正在处理';

  const size = formatSize(file.output?.size ?? file.source?.size_bytes);
  const pages = file.output?.page_count ?? file.source?.page_count ?? '—';

  return html`
    <section class="card verify-file" data-job="${file.job_id}">
      <div class="card-head">
        <h2>${file.original_name}</h2>
        ${badge({
          icon: meta.job_status_icons?.[status] || '•',
          label: `${title}（${label}）`,
          tone,
        })}
        ${badge({
          icon: postVerifyIcon(file.post_verify, meta),
          label: postVerifyLine(file.post_verify),
          tone: postVerifyTone(file.post_verify),
        })}
      </div>

      <p class="hint">
        ${size} · ${pages} 页
        ${file.output ? ` · 输出 ${file.output.file_name}` : ''}
      </p>

      ${file.last_error
        ? html`
            <p class="error" role="alert">
              ${file.last_error.message}
              ${file.last_error.next_action || ''}
            </p>
          `
        : ''}

      ${processTable(file, meta)}
      ${postVerifyTable(file.post_verify, meta)}

      <div class="btn-row">
        ${file.output
          ? html`<a
              class="btn small"
              href="${ctx.api.fileUrl(file.job_id, 'output')}"
              download="${file.output.file_name}"
              data-action="download"
              data-job="${file.job_id}"
              >下载</a
            >`
          : html`<span class="btn small" aria-disabled="true">暂无可下载文件</span>`}
        ${file.output
          ? html`<button
              type="button"
              class="btn small"
              data-action="preview"
              data-job="${file.job_id}"
            >
              预览
            </button>`
          : ''}
        <button
          type="button"
          class="btn small"
          data-action="report"
          data-job="${file.job_id}"
          ${file.post_verify ? '' : raw('disabled')}
        >
          校验报告
        </button>
      </div>
      <div data-preview="${file.job_id}"></div>
    </section>
  `;
}

/** 处理明细：只列勾选过的项，顺序按流水线的 `results` 顺序。 */
function processTable(file, meta) {
  const picked = selectedOperations(file.config, meta);
  const rows = (file.results || []).filter((item) => !picked.size || picked.has(item.key));

  if (!rows.length) {
    return html`<p class="muted">这个文件没有处理明细。</p>`;
  }

  const body = raw(
    rows
      .map((item) => {
        const status = item.status;
        return html`<tr data-key="${item.key}">
          <th scope="row">${item.title || meta.operation_titles?.[item.key] || item.key}</th>
          <td>
            ${badge({
              icon: item.status_icon || meta.result_status_icons?.[status] || '•',
              label: item.status_label || meta.result_status_labels?.[status] || status,
              tone: RESULT_TONES[status] || 'neutral',
            })}
          </td>
          <td class="small">${formatCounts(item.counts, meta)}</td>
          <td class="small">${item.message || item.reason_code || '—'}</td>
        </tr>`;
      })
      .join(''),
  );

  return html`
    <div class="table-wrap">
      <table>
        <caption class="visually-hidden">这次勾选的处理项与结果</caption>
        <thead>
          <tr>
            <th scope="col">处理项</th>
            <th scope="col">结果</th>
            <th scope="col">计数</th>
            <th scope="col">说明</th>
          </tr>
        </thead>
        <tbody>
          ${body}
        </tbody>
      </table>
    </div>
  `;
}

/** 后置校验明细：固定九行，`skipped` 显示破折号 + 原因。 */
function postVerifyTable(postVerify, meta) {
  if (!postVerify) {
    return html`<p class="muted">── 后置校验 ── 这个文件没有后置校验结果。</p>`;
  }

  const body = raw(
    (meta.verify_keys || [])
      .map((key, index) => {
        const item = (postVerify.items || []).find((entry) => entry.key === key);
        const title = meta.verify_titles?.[key] || key;
        if (!item) {
          return html`<tr data-key="${key}">
            <th scope="row">${index + 1}. ${title}</th>
            <td>${badge({ icon: '—', label: '—', tone: 'neutral' })}</td>
            <td class="small">本次未执行</td>
          </tr>`;
        }
        const status = item.status;
        const summary = item.summary || (status === 'skipped' ? '本次未执行' : '');
        return html`<tr data-key="${key}">
          <th scope="row">${index + 1}. ${title}</th>
          <td>
            ${badge({
              icon: meta.verify_status_icons?.[status] || '•',
              label: meta.verify_status_labels?.[status] || status,
              tone: VERIFY_TONES[status] || 'neutral',
            })}
          </td>
          <td class="small" title="${summary}">${summary || '—'}</td>
        </tr>`;
      })
      .join(''),
  );

  return html`
    <p class="hint">── 后置校验 ──</p>
    <div class="table-wrap">
      <table>
        <caption class="visually-hidden">处理之后的十二项校验</caption>
        <thead>
          <tr>
            <th scope="col">校验项</th>
            <th scope="col">结果</th>
            <th scope="col">说明</th>
          </tr>
        </thead>
        <tbody>
          ${body}
        </tbody>
      </table>
    </div>
  `;
}

/** 勾选的组 → 展开成流水线的十个操作键（组之间共享的操作去重）。 */
function selectedOperations(config, meta) {
  const groups = config?.process_items || [];
  const ops = new Set();
  const mapping = meta.process_group_operations || {};
  groups.forEach((group) => {
    (mapping[group] || []).forEach((op) => ops.add(op));
  });
  return ops;
}

function postVerifyLine(postVerify) {
  if (!postVerify) return '后置校验：未执行';
  const summary = postVerify.summary || {};
  if (Number(summary.fail) > 0) return `后置校验：${summary.fail} 项不通过`;
  if (Number(summary.warning) > 0) return `后置校验：${summary.warning} 项预警`;
  if (Number(summary.pass) > 0) return '后置校验：全部通过';
  return '后置校验：没有产生结论';
}

function postVerifyTone(postVerify) {
  if (!postVerify) return 'neutral';
  const summary = postVerify.summary || {};
  if (Number(summary.fail) > 0) return 'danger';
  if (Number(summary.warning) > 0) return 'warn';
  if (Number(summary.pass) > 0) return 'ok';
  return 'neutral';
}

function postVerifyIcon(postVerify, meta) {
  if (!postVerify) return '—';
  const tone = postVerifyTone(postVerify);
  if (tone === 'danger') return meta.verify_status_icons?.fail || '✕';
  if (tone === 'warn') return meta.verify_status_icons?.warning || '!';
  if (tone === 'ok') return meta.verify_status_icons?.pass || '✓';
  return '—';
}

function formatCounts(counts, meta) {
  if (!counts) return '—';
  const parts = (meta.count_keys || [])
    .filter((key) => Number(counts[key]) > 0)
    .map((key) => `${meta.count_labels?.[key] || key} ${counts[key]}`);
  return parts.length ? parts.join('，') : '—';
}

/** 轮询判重用的指纹：只关心会影响渲染的字段。 */
function fingerprint(payload) {
  if (!payload) return '';
  return (payload.files || [])
    .map((file) => `${file.job_id}:${file.status}:${file.post_verify?.generated_at || ''}`)
    .join('|');
}

function escapeAttr(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
