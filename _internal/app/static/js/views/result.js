/* 结果页：九项处理逐行摊开，before / after / counts / reason / next_action 都在。
 *
 * 预览失败只提示、不禁用下载：浏览器内嵌 PDF 预览依赖插件，
 * 有时候明明文件是好的也打不开，这时候拦住下载只会让人以为文件坏了。
 */

import { badge, formatSize, formatTime, formatValue, html, raw } from '../dom.js';
import { askSaveName, saveFile } from '../save-as.js';

const TONES = {
  adjusted: 'ok',
  unchanged: 'neutral',
  not_applicable: 'neutral',
  review_required: 'warn',
  failed: 'danger',
};

const JOB_TONES = {
  completed: 'ok',
  completed_with_warnings: 'warn',
  failed: 'danger',
  interrupted: 'danger',
};

export async function render(container, ctx) {
  const jobId = ctx.params.jobId;
  const meta = ctx.meta;
  container.innerHTML = html`<p class="muted">正在读取结果…</p>`;

  let report;
  try {
    report = await ctx.api.getReport(jobId);
  } catch (error) {
    container.innerHTML = html`
      <section class="card">
        <h1>打不开处理结果</h1>
        <p class="error" role="alert">${error.message}</p>
        <p class="muted">${error.hint}</p>
        <div class="btn-row"><a class="btn" href="#/history">回到历史</a></div>
      </section>
    `;
    return;
  }

  const results = report.results || [];
  const summary = report.summary || {};
  const output = report.output;
  const lastError = report.last_error;

  const rows = raw(
    results
      .map((item) => {
        const status = item.status;
        return html`
          <tr data-key="${item.key}">
            <th scope="row">
              ${item.title}
              <span class="hint">${item.key}</span>
            </th>
            <td>
              ${badge({
                icon: item.status_icon || meta.result_status_icons[status] || '•',
                label: item.status_label || meta.result_status_labels[status] || status,
                tone: TONES[status] || 'neutral',
              })}
            </td>
            <td>${formatValue(item.before)}</td>
            <td>${formatValue(item.after)}</td>
            <td class="small">${formatCounts(item.counts, meta)}</td>
            <td class="cell-reason">
              ${item.reason_code ? html`<div>${item.reason_code}</div>` : ''}
              ${item.message || ''}
            </td>
            <td class="small">${item.next_action || '—'}</td>
          </tr>
        `;
      })
      .join(''),
  );

  const summaryChips = raw(
    (meta.result_statuses || [])
      .map((status) =>
        badge({
          icon: meta.result_status_icons[status] || '•',
          label: `${meta.result_status_labels[status] || status} ${summary[status] ?? 0}`,
          tone: TONES[status] || 'neutral',
        }),
      )
      .join(' '),
  );

  const warnings = raw(
    (report.warnings || []).map((text) => html`<li>${text}</li>`).join(''),
  );

  const viewAudit = buildViewAudit(results, meta);

  container.innerHTML = html`
    <div class="card-head">
      <h1>处理结果</h1>
      ${badge({
        icon: meta.job_status_icons[report.status] || '•',
        label: meta.job_status_labels[report.status] || report.status,
        tone: JOB_TONES[report.status] || 'neutral',
      })}
    </div>
    <p class="muted">${report.original_name}</p>

    ${lastError
      ? html`
          <section class="card" id="result-error">
            <h2>这次没有完成</h2>
            <p class="error" role="alert">${lastError.message}</p>
            <dl class="kv small">
              <dt>原因代码</dt><dd>${lastError.code}</dd>
              <dt>阶段</dt><dd>${lastError.stage || '—'}</dd>
              <dt>下一步</dt><dd>${lastError.next_action || '—'}</dd>
            </dl>
          </section>
        `
      : ''}

    <section class="card">
      <h2>汇总</h2>
      <ul class="summary">${summaryChips}</ul>
      <dl class="kv small">
        <dt>文件</dt><dd>${report.original_name}</dd>
        <dt>页数</dt><dd>${report.source?.page_count ?? '—'} 页</dd>
        <dt>开始</dt><dd>${formatTime(report.started_at)}</dd>
        <dt>结束</dt><dd>${formatTime(report.finished_at)}</dd>
        <dt>输出文件</dt><dd>${output ? output.file_name : '尚未生成'}</dd>
        <dt>输出大小</dt><dd>${output ? formatSize(output.size) : '—'}</dd>
        <dt>最终校验</dt><dd>${output?.verified ? '已通过' : '未通过或未执行'}</dd>
      </dl>
    </section>

    <section class="card">
      <div class="btn-row">
        ${output
          ? html`<a
              class="btn btn-primary"
              id="download-output"
              href="${ctx.api.fileUrl(jobId, 'output')}"
              download="${output.file_name}"
              >下载处理后的 PDF</a
            >`
          : html`<span class="btn" id="download-output" aria-disabled="true"
              >暂无可下载的输出文件</span
            >`}
        <button type="button" class="btn" id="preview-output">在页面内预览</button>
        <button type="button" class="btn" id="reprocess">按原配置重新处理</button>
        <a class="btn" href="#/history">回到历史</a>
      </div>
      <p id="preview-notice" class="notice" aria-live="polite"></p>
      <div id="preview-area"></div>
      <p id="result-action-error" class="error" role="alert"></p>
    </section>

    ${viewAudit}

    <section class="card">
      <h2>九项处理明细</h2>
      <div class="table-wrap">
        <table id="result-table">
          <caption class="visually-hidden">
            九项处理的逐项结果
          </caption>
          <thead>
            <tr>
              <th scope="col">处理项</th>
              <th scope="col">结果</th>
              <th scope="col">处理前</th>
              <th scope="col">处理后</th>
              <th scope="col">计数</th>
              <th scope="col">原因</th>
              <th scope="col">下一步</th>
            </tr>
          </thead>
          <tbody>
            ${rows}
          </tbody>
        </table>
      </div>
    </section>

    ${report.warnings?.length
      ? html`
          <section class="card">
            <h2>需要留意</h2>
            <ul id="warnings">${warnings}</ul>
          </section>
        `
      : ''}

    <p class="hint" id="disclaimer">${report.disclaimer || ''}</p>
  `;

  const previewButton = container.querySelector('#preview-output');
  const previewArea = container.querySelector('#preview-area');
  const previewNotice = container.querySelector('#preview-notice');
  const reprocessButton = container.querySelector('#reprocess');
  const actionError = container.querySelector('#result-action-error');
  const downloadLink = container.querySelector('#download-output');

  // 下载前先问名字。元素仍然是带 href 的 <a>，右键「链接另存为」照旧可用；
  // 没有 <dialog> 的老浏览器不拦，直接走原生下载——新功能不能把出口堵死。
  if (downloadLink && output) {
    downloadLink.addEventListener('click', async (event) => {
      if (typeof HTMLDialogElement === 'undefined') return;
      event.preventDefault();
      actionError.textContent = '';
      const name = await askSaveName({
        fileName: output.file_name,
        originalName: report.original_name,
        sizeText: formatSize(output.size),
      });
      if (!name) return;
      try {
        await saveFile(ctx.api.fileUrl(jobId, 'output'), name);
      } catch (error) {
        actionError.textContent =
          '保存失败：文件可能已被移动，或网络中断。可以再点一次下载重试。';
      }
    });
  }

  previewButton.addEventListener('click', async () => {
    previewNotice.textContent = '';
    actionError.textContent = '';
    previewButton.disabled = true;
    const url = ctx.api.fileUrl(jobId, 'output');
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      // 只要确认文件读得到，就把渲染交给浏览器；内容不必在 JS 里留一份
      await response.body?.cancel?.();
      previewArea.innerHTML = raw(
        `<iframe class="preview-frame" title="输出文件预览" src="${url}"></iframe>`,
      );
    } catch (error) {
      previewArea.innerHTML = '';
      previewNotice.textContent =
        '页面内预览打不开（文件可能已被移动，或浏览器不支持内嵌 PDF），但下载不受影响。';
    } finally {
      previewButton.disabled = false;
    }
  });

  reprocessButton.addEventListener('click', async () => {
    actionError.textContent = '';
    reprocessButton.disabled = true;
    try {
      const fresh = await ctx.api.reprocess(jobId);
      ctx.toast('已按原配置新建任务');
      ctx.navigate(`#/config/${fresh.id}`);
    } catch (error) {
      actionError.textContent = [error.message, error.hint].filter(Boolean).join('｜');
    } finally {
      reprocessButton.disabled = false;
    }
  });
}

function formatCounts(counts, meta) {
  if (!counts) return '—';
  const parts = (meta.count_keys || [])
    .filter((key) => Number(counts[key]) > 0)
    .map((key) => `${meta.count_labels[key] || key} ${counts[key]}`);
  return parts.length ? parts.join('，') : '—';
}

/* 「初始视图核对」卡片。
 *
 * 存在的理由：用户是拿 Acrobat 的「文档属性 → 初始视图」三个下拉框来验收的，
 * 而表格里的 `page_mode: /UseOutlines` 在 Acrobat 里根本没有对应文字。
 * 这里把两边并排放：左边是 Acrobat 里会看到的字，右边是 PDF 里的原始键值，
 * 对不上时一眼能看出是"工具没改"还是"改的和你以为的不是同一个键"。
 *
 * 只在三项都有结果时显示——缺项时并排对照会误导。
 */
const VIEW_ROWS = [
  { key: 'bookmark_panel', title: '导览标签', field: 'page_mode', label: 'page_mode_label' },
  { key: 'initial_layout', title: '页面布局', field: 'page_layout', label: 'page_layout_label' },
  { key: 'initial_zoom', title: '放大率', field: 'open_action', label: 'zoom_label' },
];

function buildViewAudit(results, meta) {
  const byKey = new Map((results || []).map((item) => [item.key, item]));
  if (!VIEW_ROWS.every((row) => byKey.has(row.key))) return '';

  const rows = VIEW_ROWS.map((row) => {
    const item = byKey.get(row.key);
    const shown = item.after?.[row.label] ?? '—';
    const changed = item.status === 'adjusted';
    return html`
      <tr data-view="${row.key}">
        <th scope="row">${row.title}</th>
        <td class="small">${shown}</td>
        <td class="small">${rawView(item, row)}</td>
        <td>${changed ? '已调整' : '已符合'}</td>
      </tr>
    `;
  }).join('');

  const targets = meta.view_labels?.targets || {};
  return html`
    <section class="card" id="view-audit">
      <h2>初始视图核对</h2>
      <p class="muted">
        左边是你在 Acrobat「文档属性 → 初始视图」里会看到的文字，可以直接对号；
        右边是文件里真正的键值。目标值：导览标签 ${targets.page_mode || '书签面板和页面'}、
        页面布局 ${targets.page_layout || '默认'}、放大率 ${targets.zoom || '默认'}。
      </p>
      <div class="table-wrap">
        <table>
          <caption class="visually-hidden">初始视图三项与 Acrobat 显示文字的对照</caption>
          <thead>
            <tr>
              <th scope="col">项目</th>
              <th scope="col">Acrobat 显示（处理后）</th>
              <th scope="col">原始键值（处理前 → 处理后）</th>
              <th scope="col">结论</th>
            </tr>
          </thead>
          <tbody>${raw(rows)}</tbody>
        </table>
      </div>
    </section>
  `;
}

function rawView(item, row) {
  const show = (value) => (value === null || value === undefined ? '（无）' : String(value));
  if (row.key !== 'initial_zoom') {
    return `${show(item.before?.[row.field])} → ${show(item.after?.[row.field])}`;
  }
  // 放大率没有单一键：打开动作的形态、fit 类型、缩放因子三者一起才说得清
  const one = (view) =>
    `${show(view?.open_action)}｜fit=${show(view?.fit_type)}｜zoom=${show(view?.zoom)}`;
  return `${one(item.before)} → ${one(item.after)}`;
}
