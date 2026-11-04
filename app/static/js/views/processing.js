/* 处理中：轮询状态与事件流。
 *
 * 刻意不画进度条、不显示百分比：流水线各阶段的耗时差着数量级，
 * 任何百分比都是编的。这里只如实列出"已经走过的阶段"和"当前阶段"。
 * 刷新页面会重新拉一遍 job + events，所以刷新不丢状态。
 */

import { badge, formatSize, formatTime, html, raw } from '../dom.js';

const TERMINAL = ['completed', 'completed_with_warnings', 'failed', 'interrupted'];
const POLL_MS = 700;

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

export async function render(container, ctx) {
  const jobId = ctx.params.jobId;
  const meta = ctx.meta;

  container.innerHTML = html`
    <div class="card-head">
      <h1>正在处理</h1>
      <span id="job-status">${badge({ icon: '⋯', label: '读取状态…', tone: 'neutral' })}</span>
    </div>
    <p class="muted">
      页面会自动更新。也可以先关掉页面——处理完成后结果会留在历史记录里。
    </p>

    <section class="card">
      <h2>处理阶段</h2>
      <p class="hint">只列出真实执行到的阶段，不估算百分比。</p>
      <ul class="stage-list" id="stage-list" aria-live="polite"></ul>
    </section>

    <section class="card">
      <h2>文件</h2>
      <dl class="kv" id="job-facts">
        <dt>任务号</dt><dd>${jobId}</dd>
      </dl>
    </section>

    <p id="processing-error" class="error" role="alert"></p>
  `;

  const statusBox = container.querySelector('#job-status');
  const stageList = container.querySelector('#stage-list');
  const facts = container.querySelector('#job-facts');
  const errorBox = container.querySelector('#processing-error');

  const seen = new Set();
  let lastSeq = 0;
  let timer = null;
  let stopped = false;
  let finished = false;

  function renderStages(currentStage) {
    const items = (meta.stage_names || []).map((stage) => {
      const label = meta.stage_labels[stage] || stage;
      let state = 'pending';
      let mark = '·';
      if (stage === currentStage) {
        state = 'current';
        mark = '▶';
      } else if (seen.has(stage)) {
        state = 'done';
        mark = '✔';
      }
      return html`
        <li data-stage="${stage}" data-state="${state}">
          <span class="mark" aria-hidden="true">${mark}</span>
          <span>${label}</span>
          <span class="visually-hidden">${stateLabel(state)}</span>
        </li>
      `;
    });
    stageList.innerHTML = raw(items.join(''));
  }

  function renderStatus(job) {
    statusBox.innerHTML = badge({
      icon: meta.job_status_icons[job.status] || '•',
      label: meta.job_status_labels[job.status] || job.status,
      tone: TONES[job.status] || 'neutral',
    });
  }

  function renderFacts(job) {
    facts.innerHTML = html`
      <dt>文件名</dt><dd>${job.original_name}</dd>
      <dt>页数</dt><dd>${job.source.page_count} 页</dd>
      <dt>大小</dt><dd>${formatSize(job.source.size)}</dd>
      <dt>开始时间</dt><dd>${formatTime(job.started_at)}</dd>
      <dt>任务号</dt><dd>${job.id}</dd>
    `;
  }

  async function tick() {
    if (stopped) return;
    let job;
    try {
      job = await ctx.api.getJob(jobId);
    } catch (error) {
      stop();
      errorBox.textContent = [error.message, error.hint].filter(Boolean).join('｜');
      return;
    }
    if (stopped) return;

    try {
      const payload = await ctx.api.listEvents(jobId, lastSeq);
      (payload.events || []).forEach((event) => {
        if (typeof event.seq === 'number') lastSeq = Math.max(lastSeq, event.seq);
        if (event.type === 'stage' && event.stage) seen.add(event.stage);
      });
    } catch (error) {
      // 事件流拉不到不该把页面搞崩：状态本身才是事实来源
      console.warn('事件流读取失败', error);
    }
    if (stopped) return;

    renderStatus(job);
    renderFacts(job);
    renderStages(job.current_stage);

    if (TERMINAL.includes(job.status) && !finished) {
      finished = true;
      stop();
      // 终态一律去结果页：失败/中断也在那里看原因和下一步
      ctx.navigate(`#/result/${jobId}`);
    }
  }

  function stop() {
    stopped = true;
    if (timer) window.clearInterval(timer);
    timer = null;
  }

  await tick();
  if (!stopped) {
    timer = window.setInterval(tick, POLL_MS);
  }

  return stop;
}

function stateLabel(state) {
  if (state === 'done') return '（已完成）';
  if (state === 'current') return '（进行中）';
  return '（未开始）';
}
