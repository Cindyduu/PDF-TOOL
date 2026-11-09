/* 配置页：确认文件信息、选标签处理方式、填豁免页码，然后开始处理。
 *
 * "开始处理"会先保存再入队：用户改完页码直接点开始是常态，
 * 如果只入队不保存，页面上填的东西就会被悄悄丢掉。
 */

import { badge, formatSize, formatTime, html, raw } from '../dom.js';

const TERMINAL = ['completed', 'completed_with_warnings', 'failed', 'interrupted'];

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

  // 已经开始或已经结束的任务不该停在配置页——刷新到这里时直接送走
  if (job.status === 'queued' || job.status === 'running') {
    ctx.navigate(`#/processing/${jobId}`);
    return;
  }
  if (TERMINAL.includes(job.status)) {
    ctx.navigate(`#/result/${jobId}`);
    return;
  }

  const meta = ctx.meta;
  const detection = job.tag_detection || {};
  const config = job.config || {};
  const detectionLabel = meta.tag_status_labels[detection.status] || detection.status;
  const detectionTone =
    detection.status === 'detection_failed'
      ? 'danger'
      : detection.status === 'tagged'
        ? 'ok'
        : 'warn';

  const choices = raw(
    (meta.tag_choices || [])
      .map((choice) => {
        const checked = config.tag_choice === choice ? 'checked' : '';
        const id = `tag-choice-${choice}`;
        return html`
          <div class="option">
            <input
              type="radio"
              id="${id}"
              name="tag-choice"
              value="${choice}"
              ${raw(checked)}
            />
            <label for="${id}">
              ${meta.tag_choice_labels[choice] || choice}
              ${choiceHint(choice)}
            </label>
          </div>
        `;
      })
      .join(''),
  );

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
      <h2>标签检测结果</h2>
      <p>
        ${badge({ icon: '•', label: detectionLabel, tone: detectionTone })}
      </p>
      <dl class="kv small">
        <dt>结构树</dt><dd>${detection.has_struct_tree ? '存在' : '不存在'}</dd>
        <dt>标记位</dt><dd>${detection.has_markinfo ? '存在' : '不存在'}</dd>
        <dt>父树</dt><dd>${detection.parent_tree_ok ? '正常' : '缺失或不完整'}</dd>
        <dt>内容标记数</dt><dd>${detection.mcid_count ?? 0}</dd>
      </dl>
      <p class="hint">${detection.message || '检测只做判断，不会修改文件。'}</p>
      <div class="btn-row">
        <button type="button" class="btn" id="retry-detect">重新检测标签</button>
      </div>
      <p id="detect-note" class="hint" aria-live="polite"></p>
    </section>

    <section class="card">
      <h2>处理设置</h2>
      <fieldset>
        <legend>标签处理方式</legend>
        ${choices}
      </fieldset>

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
  const detectNote = container.querySelector('#detect-note');
  const startButton = container.querySelector('#start-job');
  const saveButton = container.querySelector('#save-config');
  const retryButton = container.querySelector('#retry-detect');
  const exemptInput = container.querySelector('#exempt-pages');

  function showError(error) {
    errorBox.textContent = [error.message, error.hint].filter(Boolean).join('｜');
  }

  function clearError() {
    errorBox.textContent = '';
  }

  function selectedChoice() {
    const checked = container.querySelector('input[name="tag-choice"]:checked');
    return checked ? checked.value : null;
  }

  function setBusy(busy) {
    [startButton, saveButton, retryButton].forEach((button) => {
      button.disabled = busy;
    });
    exemptInput.disabled = busy;
  }

  async function save({ silent = false } = {}) {
    const updated = await ctx.api.updateConfig(jobId, {
      exempt_pages_raw: exemptInput.value,
      tag_choice: selectedChoice(),
    });
    job = updated;
    exemptInput.value = updated.config.exempt_pages_raw || '';
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
      ctx.navigate(`#/processing/${jobId}`);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  });

  retryButton.addEventListener('click', async () => {
    clearError();
    detectNote.textContent = '正在重新检测…';
    setBusy(true);
    try {
      await ctx.api.retryTagDetection(jobId);
      detectNote.textContent = '已重新检测，正在刷新页面…';
      ctx.refresh();
    } catch (error) {
      detectNote.textContent = '';
      showError(error);
    } finally {
      setBusy(false);
    }
  });
}

function choiceHint(choice) {
  const hints = {
    generate: html`<span class="hint">为没有标签的文件建立结构树</span>`,
    skip: html`<span class="hint">完全不改动标签相关结构</span>`,
    keep: html`<span class="hint">保留文件里已有的标签</span>`,
    regenerate: html`<span class="hint">丢掉现有结构后重建（仅标记位的情况）</span>`,
  };
  return hints[choice] || '';
}
