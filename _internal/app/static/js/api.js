/* 后端 API 的唯一出口。
 *
 * 约定：
 * - 所有错误都翻成 ApiError，带 code / message / next_action —— 界面只负责
 *   把这三样摆出来，不去猜"500 大概是哪里炸了"；
 * - 只走相对路径，不引用任何外部域名（本地工具要能离线跑）；
 * - 文案类枚举统一从 /api/meta 取，前端不另抄一份中文表。
 */

const JSON_HEADERS = { 'Content-Type': 'application/json' };

export class ApiError extends Error {
  constructor(payload, status) {
    const body = payload && typeof payload === 'object' ? payload : {};
    super(body.message || `请求失败（HTTP ${status}）`);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.code || 'http_error';
    this.nextAction = body.next_action || '';
    this.details = body.details || {};
  }

  /** 给用户看的一句话：优先给"下一步该做什么"，没有就退回消息本身。 */
  get hint() {
    return this.nextAction || this.message;
  }
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (cause) {
    throw new ApiError(
      {
        code: 'network_error',
        message: '无法连接本地服务，请确认程序仍在运行',
        next_action: '确认程序窗口没有关闭后重试',
      },
      0,
    );
  }

  const type = response.headers.get('content-type') || '';
  let payload = null;
  if (type.includes('application/json')) {
    try {
      payload = await response.json();
    } catch (cause) {
      payload = null;
    }
  }

  if (!response.ok) throw new ApiError(payload, response.status);
  return payload;
}

function buildQuery(params) {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      search.set(key, String(value));
    }
  });
  const query = search.toString();
  return query ? `?${query}` : '';
}

let metaPromise = null;

export const api = {
  /** 界面文案与枚举。只取一次，之后走缓存。 */
  meta() {
    if (!metaPromise) {
      metaPromise = request('/api/meta').catch((error) => {
        metaPromise = null; // 失败不缓存，下次还能重试
        throw error;
      });
    }
    return metaPromise;
  },

  createJob(file) {
    const form = new FormData();
    form.append('files', file);
    return request('/api/jobs', { method: 'POST', body: form });
  },

  /** 批量上传：一次多个文件，返回 { batch_id, jobs, rejected }。 */
  createBatch(files) {
    const form = new FormData();
    Array.from(files || []).forEach((file) => form.append('files', file));
    return request('/api/jobs/batch', { method: 'POST', body: form });
  },

  getBatch(batchId) {
    return request(`/api/jobs/batch/${encodeURIComponent(batchId)}`);
  },

  patchBatchConfig(batchId, patch) {
    return request(`/api/jobs/batch/${encodeURIComponent(batchId)}/config`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify(patch),
    });
  },

  startBatch(batchId) {
    return request(`/api/jobs/batch/${encodeURIComponent(batchId)}/start`, {
      method: 'POST',
    });
  },

  listJobs(params) {
    return request(`/api/jobs${buildQuery(params)}`);
  },

  getJob(jobId) {
    return request(`/api/jobs/${encodeURIComponent(jobId)}`);
  },

  updateConfig(jobId, patch) {
    return request(`/api/jobs/${encodeURIComponent(jobId)}/config`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify(patch),
    });
  },

  retryTagDetection(jobId) {
    return request(`/api/jobs/${encodeURIComponent(jobId)}/tag-detection/retry`, {
      method: 'POST',
    });
  },

  startJob(jobId) {
    return request(`/api/jobs/${encodeURIComponent(jobId)}/start`, { method: 'POST' });
  },

  listEvents(jobId, after = 0) {
    return request(`/api/jobs/${encodeURIComponent(jobId)}/events${buildQuery({ after })}`);
  },

  getReport(jobId) {
    return request(`/api/jobs/${encodeURIComponent(jobId)}/report`);
  },

  reprocess(jobId) {
    return request(`/api/jobs/${encodeURIComponent(jobId)}/reprocess`, { method: 'POST' });
  },

  fileUrl(jobId, kind = 'output') {
    return `/api/jobs/${encodeURIComponent(jobId)}/file?kind=${encodeURIComponent(kind)}`;
  },

  // ---- 备份（阶段 J 提供实现，界面先按契约写好） ----
  listBackups() {
    return request('/api/backups');
  },

  createBackup() {
    return request('/api/backups', { method: 'POST' });
  },

  inspectBackup(file) {
    const form = new FormData();
    form.append('files', file);
    return request('/api/backups/inspect', { method: 'POST', body: form });
  },

  restoreBackup(token) {
    return request('/api/backups/restore', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ token }),
    });
  },
};
