/* 页面 1：准备页 `#/prepare/:batchId`。
 *
 * 这一页只做一件事——「决定这次要查什么」，然后二选一往下走：
 * 勾了校验项就去校验结果页，勾了「不执行前置校验」就直接进自动处理页。
 *
 * 两条容易被做错的规则，在这里各有一个明确落点：
 * - **跳过校验互斥**：勾了「不执行前置校验」时，十个校验项必须置灰，
 *   而不是仅仅"不生效"——否则用户以为自己勾的项还会跑，只是没跑而已；
 * - **一项都不勾要拦**：拦截发生在点按钮时而不是勾选时，因为用户可能先
 *   全部取消再重勾，边勾边报错只会让页面不停闪红。
 */

import { formatSize, html, raw } from '../dom.js';

/** 每项的补充说明。标题与顺序都来自 `/api/meta`，这里只补"这项到底查什么"。 */
const VERIFY_HINTS = {
  filename_naming: '小写 a-z / 0-9 / 连字符 / 下划线',
  file_size: '≤100MB 通过，100–200MB 预警，>200MB 不通过',
  external_links: '链接到外部网站或外部文件的跳转',
  page_orientation: '横向内容放在纵向页面上会提示',
  text_layer: '能否被检索、复制；扫描件通常不通过',
  annotations: '批注、高亮、便签等注释对象',
  // v4.3：取消标准 14 豁免（D-19）——阅读器自带字形不等于文件自带字形，
  // 所以说明里要点明「每种字体都要真嵌入」，不能再说「未嵌入时可能错乱」这种
  // 听起来像"只是建议"的话。
  font_embedding: '每种字体都须真嵌入（文件属性里字体名后带「嵌入字符集」）',
  // v4.1 把原来的「目录页码」拆成两项（R32 / R33）：目录看的是首页有没有
  // 目录、目录文字是不是蓝色、内链能不能跳到指定页；页码只看是不是从第 1 页
  // 开始。两句说明必须各说各的，否则用户又会以为是同一件事。
  toc: '首页是否有目录、目录文字是否为蓝色、内链能否跳到指定页',
  page_labels: '页码是否从第 1 页开始（与物理页序区分）',
  // v4.2：书签这一项从「有没有」扩到「缩放对不对」，说明必须跟着改宽，
  // 否则用户看报告时不知道 WARNING 说的是缺书签还是缩放没设。
  // v4.3（§18.1）：先看有没有书签，再看页数——有书签就一定要查缩放，
  // 只有「没书签」才可能被页数豁免。
  bookmarks: '有书签则查承前缩放；无书签时 ≤5 页跳过、>5 页告警',
  initial_view: '导览标签=书签面板和页面、页面布局=默认、放大率=默认',
  metadata_security: '文档描述字段、XMP 元数据流是否清空；是否带加密或权限设置',
};

const SKIP_HINT =
  '跳过前置校验后，校验项不会执行，也不会产生校验报告。处理完成后的后置校验仍然强制执行。';

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
        <p class="muted">可能是上传时全部未通过检查。请回到首页重新选择文件。</p>
        <div class="btn-row"><a class="btn btn-primary" href="#/home">回到首页</a></div>
      </section>
    `;
    return undefined;
  }

  const verifyKeys = meta.verify_keys || [];
  const titles = meta.verify_titles || {};
  const requirements = meta.verify_requirements || {};

  // 默认全勾（AC-R13-N5）。老批次可能存过子集，那就按存下来的来，
  // 不覆盖用户上次的选择。
  const saved = jobs[0]?.config?.verify_items;
  const initial = new Set(Array.isArray(saved) ? saved : verifyKeys);
  const skipSaved = Boolean(jobs[0]?.config?.skip_precheck);

  const items = raw(
    verifyKeys
      .map((key, index) => {
        const id = `verify-item-${key}`;
        return html`
          <div class="option" data-key="${key}">
            <input
              type="checkbox"
              id="${id}"
              name="verify-item"
              value="${key}"
              ${initial.has(key) ? raw('checked') : ''}
            />
            <label for="${id}">
              ${index + 1}. ${titles[key] || key}
              <span class="hint">${VERIFY_HINTS[key] || ''}</span>
              <span class="hint">${requirements[key] || ''}</span>
            </label>
          </div>
        `;
      })
      .join(''),
  );

  const rows = raw(
    jobs
      .map(
        (job) => html`
          <tr data-job="${job.id}">
            <th scope="row">
              ${job.original_name}
              <span class="hint">${job.id}</span>
            </th>
            <td class="small">${formatSize(job.source?.size)}</td>
            <td class="small">${job.source?.page_count ?? '—'} 页</td>
          </tr>
        `,
      )
      .join(''),
  );

  const totalSize = jobs.reduce(
    (sum, job) => sum + (Number(job.source?.size) || 0),
    0,
  );

  container.innerHTML = html`
    <div class="card-head">
      <h1>准备校验</h1>
      <a class="btn small" href="#/home">返回首页</a>
    </div>
    <p class="muted" id="prepare-count">
      已上传 <strong>${jobs.length}</strong> 个文件 · 合计 ${formatSize(totalSize)}
    </p>

    <section class="card">
      <h2>文件检查</h2>
      <p class="muted">决定这次要查哪些项。校验只读文件，不会改动任何内容。</p>

      <div class="option">
        <input type="checkbox" id="verify-all" ${raw('checked')} />
        <label for="verify-all"><strong>全选</strong></label>
      </div>

      <fieldset id="verify-items" class="checklist">
        <legend class="visually-hidden">十二项校验</legend>
        ${items}
      </fieldset>

      <div class="option" id="skip-row">
        <input type="checkbox" id="skip-precheck" ${skipSaved ? raw('checked') : ''} />
        <label for="skip-precheck">不执行前置校验，直接进入自动处理</label>
      </div>
      <p class="hint" id="skip-hint">${SKIP_HINT}</p>
    </section>

    <section class="card">
      <h2>文件列表</h2>
      <div class="table-wrap">
        <table id="prepare-table">
          <caption class="visually-hidden">本批要校验的文件</caption>
          <thead>
            <tr>
              <th scope="col">文件</th>
              <th scope="col">大小</th>
              <th scope="col">页数</th>
            </tr>
          </thead>
          <tbody>
            ${rows}
          </tbody>
        </table>
      </div>
    </section>

    <section class="card">
      <p id="prepare-error" class="error" role="alert"></p>
      <div class="btn-row">
        <button type="button" class="btn btn-primary" id="prepare-submit">开始校验</button>
        <a class="btn" href="#/home">取消</a>
      </div>
      <p id="prepare-note" class="hint" aria-live="polite"></p>
    </section>
  `;

  const allBox = container.querySelector('#verify-all');
  const skipBox = container.querySelector('#skip-precheck');
  const itemBoxes = Array.from(container.querySelectorAll('input[name="verify-item"]'));
  const submitButton = container.querySelector('#prepare-submit');
  const errorBox = container.querySelector('#prepare-error');
  const note = container.querySelector('#prepare-note');

  function showError(message, hint) {
    errorBox.textContent = [message, hint].filter(Boolean).join('｜');
  }

  function clearError() {
    errorBox.textContent = '';
  }

  /** 「全选」与子项互相同步：全勾 → 全选勾；有一个没勾 → 全选不勾（且半选）。 */
  function syncAll() {
    const checked = itemBoxes.filter((box) => box.checked).length;
    allBox.checked = checked === itemBoxes.length;
    allBox.indeterminate = checked > 0 && checked < itemBoxes.length;
  }

  /** 跳过校验时置灰十二项并清空「全选」的视觉状态，取消后原样恢复。 */
  function syncSkip() {
    const skipping = skipBox.checked;
    itemBoxes.forEach((box) => {
      box.disabled = skipping;
    });
    allBox.disabled = skipping;
    submitButton.textContent = skipping ? '进入自动处理' : '开始校验';
    if (skipping) {
      allBox.checked = false;
      allBox.indeterminate = false;
    } else {
      syncAll();
    }
  }

  allBox.addEventListener('change', () => {
    itemBoxes.forEach((box) => {
      box.checked = allBox.checked;
    });
    allBox.indeterminate = false;
  });

  itemBoxes.forEach((box) => box.addEventListener('change', syncAll));
  skipBox.addEventListener('change', () => {
    clearError();
    syncSkip();
  });

  syncAll();
  syncSkip();

  function selectedKeys() {
    return itemBoxes.filter((box) => box.checked).map((box) => box.value);
  }

  async function submit() {
    clearError();
    const skipping = skipBox.checked;
    const picked = selectedKeys();

    if (!skipping && !picked.length) {
      showError('请至少勾选一项校验，或勾选『不执行前置校验』');
      return;
    }

    submitButton.disabled = true;
    note.textContent = skipping ? '正在进入自动处理…' : '正在提交校验…';
    try {
      if (skipping) {
        await ctx.api.patchBatchConfig(batchId, { skip_precheck: true });
        ctx.navigate(`#/process/${batchId}`);
        return;
      }

      // 配置与提交一起发：勾选结果必须和「点校验」是同一次动作，
      // 否则中间任何一步失败都会留下"界面勾了、后端没收到"的状态。
      const result = await ctx.api.verifyBatch(batchId, {
        verify_items: picked,
        skip_precheck: false,
      });
      const started = (result.started || []).length;
      const skipped = result.skipped || [];
      if (!started) {
        showError(
          '没有文件进入校验',
          skipped.map((item) => `${item.name}：${item.message}`).join('；'),
        );
        note.textContent = '';
        return;
      }
      ctx.toast(skipped.length ? `${skipped.length} 个文件被跳过` : '校验已提交');
      ctx.navigate(`#/verify/${batchId}`);
    } catch (error) {
      note.textContent = '';
      showError(error.message, error.hint);
    } finally {
      submitButton.disabled = false;
    }
  }

  submitButton.addEventListener('click', submit);

  return undefined;
}
