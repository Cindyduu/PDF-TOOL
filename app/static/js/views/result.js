/* 结果页：十项处理逐行摊开，before / after / counts / reason / next_action 都在。
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

    ${contentIntegrityCard(report, meta)}

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
      <h2>十项处理明细</h2>
      <div class="table-wrap">
        <table id="result-table">
          <caption class="visually-hidden">
            十项处理的逐项结果
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

    ${postVerifyCard(report.post_verify, meta)}

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

  // 单文件的后置校验报告。没有后置校验时按钮不存在，这里也就不会绑上。
  container.querySelector('#open-verify-report')?.addEventListener('click', () => {
    window.open(ctx.api.jobVerifyReportHtmlUrl(jobId, 'post'), '_blank');
  });
}

function formatCounts(counts, meta) {
  if (!counts) return '—';
  const parts = (meta.count_keys || [])
    .filter((key) => Number(counts[key]) > 0)
    .map((key) => `${meta.count_labels[key] || key} ${counts[key]}`);
  return parts.length ? parts.join('，') : '—';
}

/* R43：卡片上的徽标只说「一致 / 不一致 / 未核对」，用户读完仍不知道自己能不能
 * 拿这份文件去申报。这里补一句**结论式**的话，并给出独立复核的入口。
 *
 * 只做文案映射，不重算结论：页数、变化页号、归因全部读后端给的
 * `report.content_integrity`；结论文案本身仍以 `content_integrity_summary`
 * 为唯一来源（§R28 一份数据源），这里不去复述它，只解释「这对你意味着什么」。
 *
 * 三种「不算一致」的状态必须劝阻：`ok=false`、`missing`（没核对）、
 * `unknown`（想核对但没核对成）。把「没核对」写得好听，等于替用户下了一个
 * 工具没做过的结论——申报材料被退回时，他不会记得这一行当时写的是什么。
 *
 * 为什么要分 `missing` 和 `unknown` 两态：`ok` 只有真/假两种取值，但
 * `compare()` 在指纹算不出来时返回的是 `ok=true` + `warning`（它故意不阻断
 * 处理）。只看 `ok`，这一态会被渲染成绿色的「一致」，而实际上一个字都没比过。
 * 假绿灯比红灯糟得多。
 */
function integrityState(data) {
  if (!data) return 'missing';
  if (data.warning) return 'unknown';
  return data.ok === false ? 'bad' : 'good';
}

/* 徽标与提示句必须同源（都走 `integrityState`）：徽标写「一致」而提示写
 * 「没核对成」，用户会去信那个绿色的。`missing` 与 `unknown` 都不是「一致」。
 */
function integrityBadge(data) {
  return {
    good: { tone: 'ok', label: '一致', icon: '✓' },
    bad: { tone: 'danger', label: '不一致', icon: '✗' },
    missing: { tone: 'warn', label: '未核对', icon: '—' },
    unknown: { tone: 'warn', label: '未核对', icon: '—' },
  }[integrityState(data)];
}

/* 归因里存的是处理项的英文键（`remove_annotations`、`link_color`），直接摊在
 * 屏幕上等于没解释。中文名从 `/api/meta` 的 `operation_titles` 取，取不到就
 * 退回键名——宁可难看，也不能因为少一条映射就整句消失。
 */
function attributedText(data, meta) {
  const titles = (meta && meta.operation_titles) || {};
  return (data.attributed_to || []).map((key) => titles[key] || key).join('、');
}

function contentIntegrityNotice(report, meta) {
  const data = report.content_integrity;
  const state = integrityState(data);
  if (state === 'missing') {
    return '本次没有核对内容一致性。请不要仅凭本页结论提交申报，先按下面的方法自己比对一遍。';
  }
  if (state === 'unknown') {
    return `${data.warning}。不要仅凭本页结论提交申报，请按下面的方法自己比对一遍。`;
  }
  if (state === 'bad') {
    return '处理前后的内容不一致，产物没有发布。不要把它用于申报，请把这份结果和原因代码一起反馈。';
  }
  const changed = data.changed_pages || {};
  const textPages = changed.text || [];
  const contentPages = changed.content || [];
  const attributed = attributedText(data, meta) || '已勾选的处理项';
  if (textPages.length) {
    return `文字层在第 ${pageList(textPages)} 页有变化（来自「${attributed}」）。这是注释处理带来的预期变化，申报前建议抽查这几页。`;
  }
  if (contentPages.length) {
    return `文字一字未改；第 ${pageList(contentPages)} 页的页面绘制指令有变化（来自「${attributed}」）。这是外观层面的改动，不影响内容。`;
  }
  return '处理前后内容一致：页数、尺寸、文字、图片、字体均未改变，本次只改格式相关结构。';
}

/* R36 内容一致性：这一张卡回答「工具只改了格式，还是也动了内容」。
 *
 * 结论文案直接用后端给的 `content_integrity_summary`，前端不重算。那句话由
 * `content_fingerprint.describe_integrity` 生成，处理日志用的是同一个函数——
 * 两边各算一遍，迟早会出现「日志说 3 页变化、结果页说 2 页」，而且没人能说清
 * 哪个对。
 *
 * 没有 `content_integrity`（老任务，或护栏在建指纹前就抛了）时照样出这张卡，
 * 写「未核对」。这一行的存在本身就是结论：用户需要知道**这件事没被验证过**，
 * 而不是从「没有这一行」去猜它验过了（AC-R36-E2）。
 */
function contentIntegrityCard(report, meta) {
  const data = report.content_integrity;
  const summary = report.content_integrity_summary || '内容一致性未核对';
  const rows = [];
  if (data) {
    const counts = data.page_count || {};
    if (counts.before !== undefined && counts.before !== null) {
      rows.push(['页数', `${counts.before} → ${counts.after}`]);
    }
    const changed = data.changed_pages || {};
    if ((changed.content || []).length) rows.push(['内容流变化页', pageList(changed.content)]);
    if ((changed.text || []).length) rows.push(['文本层变化页', pageList(changed.text)]);
    if ((data.attributed_to || []).length) rows.push(['归因到', attributedText(data, meta)]);
    if ((data.problems || []).length) rows.push(['问题', data.problems.join('；')]);
  }

  // 徽标与提示句必须同源（都走 `integrityState`）：徽标写「一致」而提示写
  // 「没核对成」，用户会去信那个绿色的。`missing` 与 `unknown` 都不是「一致」。
  const { tone, label, icon } = integrityBadge(data);

  return html`
    <section class="card" id="content-integrity">
      <div class="card-head">
        <h2>内容一致性</h2>
        ${badge({ icon, label, tone })}
      </div>
      <p id="content-integrity-summary">${summary}</p>
      <p class="notice" id="content-integrity-notice">${contentIntegrityNotice(report)}</p>
      ${rows.length
        ? html`<dl class="kv small">
            ${raw(rows.map(([name, value]) => html`<dt>${name}</dt><dd>${value}</dd>`).join(''))}
          </dl>`
        : ''}
      <p class="hint">
        处理前后各算一次内容指纹（页数、尺寸、图片、字体、文本层、内容流）。
        差异必须能归因到已勾选的处理项，归不了因就判为改动了内容。
      </p>
      <p class="hint">
        要自己复核：把原件和产物各放一个目录，运行
        <code>python scripts/verify_content_same.py 原件目录 产物目录</code>
        —— 它不复用本工具的判断逻辑，会重新提取文字、尺寸、图片、字体再比一遍。
      </p>
    </section>
  `;
}

function pageList(pages, limit = 10) {
  const shown = pages.slice(0, limit).join('、');
  return pages.length > limit ? `${shown} 等 ${pages.length} 页` : shown;
}

/* 后置校验卡片（R27）。
 *
 * 与「十项处理明细」分开两张表：一张说"我改了什么"，一张说"改完还合不合规"，
 * 混在一张表里用户会拿处理结果当校验结论读。
 *
 * 固定九行：没跑到的项也占一行显示破折号，缺行会被读成"这项没问题"。
 */
const VERIFY_TONES = {
  pass: 'ok',
  warning: 'warn',
  fail: 'danger',
  skipped: 'neutral',
};

function postVerifyCard(postVerify, meta) {
  if (!postVerify) {
    return html`
      <section class="card" id="post-verify">
        <h2>后置校验</h2>
        <p class="muted">这个文件没有后置校验结果（处理没有走到那一步）。</p>
      </section>
    `;
  }

  const summary = postVerify.summary || {};
  const rows = raw(
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
        return html`<tr data-key="${key}">
          <th scope="row">${index + 1}. ${title}</th>
          <td>
            ${badge({
              icon: meta.verify_status_icons?.[status] || '•',
              label: meta.verify_status_labels?.[status] || status,
              tone: VERIFY_TONES[status] || 'neutral',
            })}
          </td>
          <td class="small">${item.summary || (status === 'skipped' ? '本次未执行' : '—')}</td>
        </tr>`;
      })
      .join(''),
  );

  return html`
    <section class="card" id="post-verify">
      <div class="card-head">
        <h2>后置校验</h2>
        <button type="button" class="btn small" id="open-verify-report">校验报告</button>
      </div>
      <p class="hint">
        处理完成之后自动重跑一遍十二项检查。校验结论只说明文件当前状态，
        不代表已通过任何官方审评。
      </p>
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
            ${rows}
          </tbody>
        </table>
      </div>
      <p class="hint">
        通过 ${summary.pass ?? 0} 项 · 预警 ${summary.warning ?? 0} 项 · 不通过
        ${summary.fail ?? 0} 项 · 未执行 ${summary.skipped ?? 0} 项
      </p>
    </section>
  `;
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
