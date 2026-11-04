/* 首页：拖放或点击选择 PDF，支持一次多个。
 *
 * 拖放区本身是个 role="button" + tabindex="0" 的容器：键盘用户按回车/空格
 * 也能打开选择框，不用先找到隐藏的 input。
 *
 * 选 1 个走老的"单个配置页"，选多个进批次页——单个文件是最高频的场景，
 * 不该为了批量把它的步骤变长。
 */

import { html } from '../dom.js';

export function render(container, ctx) {
  const limit = Number(ctx.meta?.max_batch_files) || 20;

  container.innerHTML = html`
    <h1>上传 PDF 文件</h1>
    <p class="muted">
      可以一次选择多个文件批量处理，也可以只选一个。文件只保存在本机目录里，不会上传到外部服务。
    </p>

    <section class="card">
      <div
        id="dropzone"
        class="dropzone"
        role="button"
        tabindex="0"
        aria-describedby="dropzone-hint"
        aria-label="选择或拖入 PDF 文件"
      >
        <strong>把 PDF 拖到这里</strong>
        <p class="muted">或者按回车键选择文件</p>
        <p id="dropzone-hint" class="hint">
          只接受 PDF 文件，扩展名 .pdf；一次最多 ${limit} 个
        </p>
      </div>
      <input
        id="file-input"
        class="visually-hidden"
        type="file"
        accept="application/pdf,.pdf"
        multiple
        tabindex="-1"
        aria-hidden="true"
      />
      <p id="upload-status" class="hint" aria-live="polite"></p>
      <p id="upload-error" class="error" role="alert"></p>
    </section>
  `;

  const dropzone = container.querySelector('#dropzone');
  const input = container.querySelector('#file-input');
  const status = container.querySelector('#upload-status');
  const errorBox = container.querySelector('#upload-error');
  let busy = false;

  function showError(message, hint) {
    errorBox.textContent = [message, hint].filter(Boolean).join('｜');
  }

  function clearError() {
    errorBox.textContent = '';
  }

  function setBusy(value) {
    busy = value;
    if (value) dropzone.setAttribute('aria-busy', 'true');
    else dropzone.removeAttribute('aria-busy');
  }

  async function upload(file) {
    if (busy || !file) return;
    clearError();
    setBusy(true);
    status.textContent = `正在读取并检查「${file.name}」…`;

    try {
      const job = await ctx.api.createJob(file);
      status.textContent = `已接收「${job.original_name}」，共 ${job.source.page_count} 页`;
      ctx.toast('文件已上传');
      ctx.navigate(`#/config/${job.id}`);
    } catch (error) {
      status.textContent = '';
      showError(error.message, error.hint);
    } finally {
      setBusy(false);
      input.value = '';
    }
  }

  async function uploadBatch(files) {
    if (busy || !files.length) return;
    clearError();
    setBusy(true);
    status.textContent = `正在读取并检查 ${files.length} 个文件…`;

    try {
      const payload = await ctx.api.createBatch(files);
      const accepted = (payload.jobs || []).length;
      const rejected = (payload.rejected || []).length;
      status.textContent = rejected
        ? `已接收 ${accepted} 个文件，${rejected} 个未通过检查`
        : `已接收 ${accepted} 个文件`;
      ctx.toast(rejected ? `${rejected} 个文件未通过检查` : '文件已上传');
      ctx.navigate(`#/batch/${payload.batch_id}`);
    } catch (error) {
      status.textContent = '';
      showError(error.message, error.hint);
    } finally {
      setBusy(false);
      input.value = '';
    }
  }

  function pickFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    if (files.length > limit) {
      showError(
        `一次最多 ${limit} 个文件`,
        `这次选了 ${files.length} 个，请分批上传`,
      );
      return;
    }
    if (files.length === 1) upload(files[0]);
    else uploadBatch(files);
  }

  dropzone.addEventListener('click', () => {
    if (!busy) input.click();
  });

  dropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
      event.preventDefault();
      if (!busy) input.click();
    }
  });

  input.addEventListener('change', () => pickFiles(input.files));

  ['dragenter', 'dragover'].forEach((type) => {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.add('is-dragover');
    });
  });

  ['dragleave', 'dragend'].forEach((type) => {
    dropzone.addEventListener(type, () => {
      dropzone.classList.remove('is-dragover');
    });
  });

  dropzone.addEventListener('drop', (event) => {
    event.preventDefault();
    dropzone.classList.remove('is-dragover');
    pickFiles(event.dataTransfer?.files);
  });

  // 拖到页面其他地方时不要被浏览器打开成新标签页
  const swallow = (event) => event.preventDefault();
  window.addEventListener('dragover', swallow);
  window.addEventListener('drop', swallow);

  // 首屏把焦点交给拖放区：键盘用户进来就能直接回车打开选择框，不必先 Tab 找目标
  container.querySelector('#dropzone')?.focus?.();

  return () => {
    window.removeEventListener('dragover', swallow);
    window.removeEventListener('drop', swallow);
  };
}
