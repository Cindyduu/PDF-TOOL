/* 页面 3：自动处理页 `#/process/:batchId`。
 *
 * 这一页是唯一有破坏性动作的地方，所以两处特别处理：
 * - **不可逆的项默认不勾，勾的时候弹确认**：「注释扁平化」（g6，AC-R26-E1）
 *   与「清除高亮和批注」（g7，AC-R34-N5 那条「不可逆」约定）都走这条路。
 *   其余五项都能重跑一次再改回来，不需要拦人；
 * - 豁免页沿用 v2.1 的解析与校验：越界给行内错误，但**不阻断**其他输入，
 *   用户改一个数字不该把已经填好的日志开关也一起作废。
 */

import { html, raw } from '../dom.js';

const FLATTEN = 'g6_flatten';
const REMOVE = 'g7_remove_annotations';

/** 勾选时需要二次确认的组：不可逆，点错就没有回头路。 */
const DESTRUCTIVE = {
  [FLATTEN]: {
    title: '注释扁平化',
    message: '注释扁平化不可逆，处理后将无法还原原始批注。是否继续？',
  },
  [REMOVE]: {
    title: '清除高亮和批注',
    message:
      '清除高亮和批注不可逆，处理后被删除的批注无法还原（图章、表单域、链接会保留）。是否继续？',
  },
};

const GROUP_HINTS = {
  g1_bookmarks: '书签与链接打开时沿用当前缩放比例',
  g2_metadata: '清除标题、作者、主题、关键字等描述信息',
  g3_initial_view: '恢复默认布局、默认放大率，并在打开时显示书签面板',
  g4_link_color: '把链接覆盖范围内的文字改为蓝色，并同步链接边框颜色',
  g5_security: '写入安全属性，并开启快速 Web 查看（线性化）',
  g6_flatten: '把批注与高亮压进页面内容，处理后无法还原',
  g7_remove_annotations: '删掉高亮、批注、便签等注释对象，保留链接、图章与表单域',
};

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

  const jobs = payload.jobs || [];
  if (!jobs.length) {
    container.innerHTML = html`
      <section class="card">
        <h1>这个批次里没有文件</h1>
        <div class="btn-row"><a class="btn" href="#/home">回到首页</a></div>
      </section>
    `;
    return undefined;
  }

  const groups = meta.process_groups || [];
  const titles = meta.process_group_titles || {};
  const requirements = meta.process_group_requirements || {};
  const config = jobs[0]?.config || {};
  const saved = Array.isArray(config.process_items)
    ? config.process_items
    : meta.default_process_groups || [];
  const initial = new Set(saved);

  const items = raw(
    groups
      .map((group, index) => {
        const id = `process-group-${group}`;
        const danger = DESTRUCTIVE[group];
        return html`
          <div class="option" data-group="${group}">
            <input
              type="checkbox"
              id="${id}"
              name="process-group"
              value="${group}"
              ${initial.has(group) ? raw('checked') : ''}
            />
            <label for="${id}">
              ${index + 1}. ${titles[group] || group}
              ${danger
                ? html`<span class="hint danger">⚠ 不可逆，处理后无法还原</span>`
                : html`<span class="hint">${GROUP_HINTS[group] || ''}</span>`}
              <span class="hint">${requirements[group] || ''}</span>
            </label>
          </div>
        `;
      })
      .join(''),
  );

  container.innerHTML = html`
    <div class="card-head">
      <h1>自动处理</h1>
      <a class="btn small" href="#/verify/${batchId}">返回校验结果</a>
    </div>
    <p class="muted">本批共 <strong>${jobs.length}</strong> 个文件。勾选这次要做的处理，同一份设置会应用到全部文件。</p>

    <section class="card">
      <h2>处理项</h2>
      <fieldset id="process-groups" class="checklist">
        <legend class="visually-hidden">七项处理</legend>
        ${items}
      </fieldset>

      <label class="field" for="exempt-pages">豁免页</label>
      <input
        type="text"
        id="exempt-pages"
        name="exempt-pages"
        value="${config.exempt_pages_raw || ''}"
        placeholder="例如 3,7-9（留空表示不豁免）"
        aria-describedby="exempt-hint"
      />
      <p id="exempt-hint" class="hint">
        仅影响书签、链接、注释、清除批注四项。会按每个文件各自的页数校验；越界的文件会被跳过，不会写入非法页码。
      </p>
      <p id="exempt-error" class="error" role="alert"></p>

      <div class="option">
        <input
          type="checkbox"
          id="write-log"
          ${config.write_process_log ? raw('checked') : ''}
        />
        <label for="write-log">
          生成处理日志（HTML）
          <span class="hint">处理完成后可在结果页导出，逐项列出处理前后的值</span>
        </label>
      </div>
    </section>

    <section class="card">
      <h2>输出方式</h2>
      <p>
        <span class="icon" aria-hidden="true">●</span>
        处理完成后逐个下载或批量下载（可另存为到指定目录）
      </p>
      <p class="hint">
        产物同时保留在本地历史中，可以从「历史」页再次下载。
      </p>
    </section>

    <section class="card">
      <p id="process-error" class="error" role="alert"></p>
      <div class="btn-row">
        <button type="button" class="btn btn-primary" id="start-process">开始处理</button>
        <a class="btn" href="#/home">取消</a>
      </div>
      <p id="process-note" class="hint" aria-live="polite"></p>
    </section>
  `;

  const boxes = Array.from(container.querySelectorAll('input[name="process-group"]'));
  const exemptInput = container.querySelector('#exempt-pages');
  const logBox = container.querySelector('#write-log');
  const startButton = container.querySelector('#start-process');
  const errorBox = container.querySelector('#process-error');
  const exemptError = container.querySelector('#exempt-error');
  const note = container.querySelector('#process-note');

  function showError(message, hint) {
    errorBox.textContent = [message, hint].filter(Boolean).join('｜');
  }

  function clearError() {
    errorBox.textContent = '';
  }

  function selected() {
    return boxes.filter((box) => box.checked).map((box) => box.value);
  }

  // 不可逆的项勾选时二次确认；取消则回退为不勾（AC-R26-E1 / R34）。
  boxes.forEach((box) => {
    box.addEventListener('change', () => {
      clearError();
      const danger = DESTRUCTIVE[box.value];
      if (!danger || !box.checked) return;
      if (!window.confirm(danger.message)) box.checked = false;
    });
  });

  async function submit() {
    clearError();
    exemptError.textContent = '';
    const picked = selected();

    if (!picked.length) {
      showError('请至少勾选一项处理');
      return;
    }

    startButton.disabled = true;
    note.textContent = '正在提交处理…';
    try {
      const applied = await ctx.api.patchBatchConfig(batchId, {
        process_items: picked,
        exempt_pages_raw: exemptInput.value,
        write_process_log: logBox.checked,
      });
      const invalid = applied.invalid || [];
      if (invalid.length) {
        // 豁免页在部分文件上越界：如实列出，但不阻断——越界的文件会被跳过，
        // 其余文件照常开始，这正是批量入口存在的意义。
        exemptError.textContent = invalid
          .map((item) => `${item.name}：${item.message}`)
          .join('；');
      }

      const result = await ctx.api.startBatch(batchId);
      const started = (result.started || []).length;
      const skipped = result.skipped || [];
      if (!started) {
        showError(
          '没有文件开始处理',
          skipped.map((item) => `${item.name}：${item.message}`).join('；'),
        );
        note.textContent = '';
        return;
      }
      ctx.toast(skipped.length ? `${skipped.length} 个文件被跳过` : '处理已开始');
      ctx.navigate(`#/done/${batchId}`);
    } catch (error) {
      note.textContent = '';
      showError(error.message, error.hint);
    } finally {
      startButton.disabled = false;
    }
  }

  startButton.addEventListener('click', submit);

  return undefined;
}
