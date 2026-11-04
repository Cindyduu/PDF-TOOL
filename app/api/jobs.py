"""任务 API：上传、读取、配置、标签检测重试、开始处理、事件、报告、下载。

设计约束：
- 只做协议层的事：取参数、调服务、把 AppError 交给全局处理器；
- 所有阻塞 IO 端点用同步函数（FastAPI 会放进线程池），不占用事件循环；
- `POST /api/jobs` 只接受一个文件（多文件直接拒绝，保持既有语义）；
  批量走 `POST /api/jobs/batch`，两者互不影响；
- 下载端点只吐出**已通过最终验证**的文件，没就绪一律 409，文件没了 404。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from app.core.naming import content_disposition
from app.errors import AppError, ErrorCode

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

NEXT_ACTION_PICK_FILE = "请选择一个 PDF 文件后重试"

FILE_KINDS = ("output", "source")


def _ctx(request: Request):
    return request.app.state.ctx


class ConfigPatch(BaseModel):
    """配置补丁。字段缺失表示"不改"，显式给 null 表示"清空"。"""

    model_config = ConfigDict(extra="forbid")

    exempt_pages_raw: str | None = None
    tag_choice: str | None = None


@router.post("", status_code=201)
def create_job(
    request: Request,
    files: list[UploadFile] | None = File(default=None),
) -> dict:
    ctx = _ctx(request)
    uploads = [item for item in (files or []) if item is not None]

    if not uploads:
        raise AppError(
            ErrorCode.EMPTY_FILE,
            "没有收到文件",
            retryable=False,
            next_action=NEXT_ACTION_PICK_FILE,
            http_status=400,
        )
    if len(uploads) > 1:
        raise AppError(
            ErrorCode.MULTIPLE_FILES,
            "一次只能处理一个文件",
            details={
                "count": len(uploads),
                "names": [item.filename or "" for item in uploads],
            },
            retryable=False,
            next_action="请只选择一个 PDF 文件后重新上传",
            http_status=400,
        )

    upload = uploads[0]
    try:
        return ctx.jobs.create_job(upload.filename or "document.pdf", upload.file)
    finally:
        upload.file.close()


@router.post("/batch", status_code=201)
def create_batch(
    request: Request,
    files: list[UploadFile] | None = File(default=None),
) -> dict:
    """批量上传：一次最多 `max_batch_files` 个文件，逐个建任务。

    与单文件端点 `POST /api/jobs` 的关键差别：这里**接受多文件**，
    并且单个文件失败不拖垮整批 —— 坏文件进 `rejected`，好文件照常建任务。
    """
    ctx = _ctx(request)
    uploads = [item for item in (files or []) if item is not None]

    if not uploads:
        raise AppError(
            ErrorCode.EMPTY_FILE,
            "没有收到文件",
            retryable=False,
            next_action=NEXT_ACTION_PICK_FILE,
            http_status=400,
        )

    items = [(item.filename or "document.pdf", item.file) for item in uploads]
    try:
        return ctx.jobs.create_batch(items)
    finally:
        for item in uploads:
            item.file.close()


@router.get("/batch/{batch_id}")
def get_batch(batch_id: str, request: Request) -> dict:
    """批次详情：完整任务对象，供批次页渲染表格与轮询进度。"""
    ctx = _ctx(request)
    jobs = ctx.jobs.batch_jobs(batch_id)
    return {"batch_id": batch_id, "total": len(jobs), "jobs": jobs}


@router.patch("/batch/{batch_id}/config")
def update_batch_config(
    batch_id: str, patch: ConfigPatch, request: Request
) -> dict:
    provided = patch.model_fields_set
    exempt_raw = (
        (patch.exempt_pages_raw or "") if "exempt_pages_raw" in provided else None
    )
    return _ctx(request).jobs.apply_batch_config(
        batch_id,
        exempt_pages_raw=exempt_raw,
        tag_choice=patch.tag_choice,
        tag_choice_provided="tag_choice" in provided,
    )


@router.post("/batch/{batch_id}/start")
def start_batch(batch_id: str, request: Request) -> dict:
    return _ctx(request).jobs.start_batch(batch_id)


@router.get("")
def list_jobs(
    request: Request,
    q: str | None = Query(default=None),
    status: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
) -> dict:
    """历史列表。`q` 按文件名/任务号模糊匹配，`status` 精确匹配状态。"""
    return _ctx(request).history.list_jobs(
        query=q, status=status, batch_id=batch_id
    )


@router.get("/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    return _ctx(request).jobs.get_job(job_id)


@router.patch("/{job_id}/config")
def update_config(job_id: str, patch: ConfigPatch, request: Request) -> dict:
    provided = patch.model_fields_set
    exempt_raw = (
        (patch.exempt_pages_raw or "") if "exempt_pages_raw" in provided else None
    )
    return _ctx(request).jobs.update_config(
        job_id,
        exempt_pages_raw=exempt_raw,
        tag_choice=patch.tag_choice,
        tag_choice_provided="tag_choice" in provided,
    )


@router.post("/{job_id}/tag-detection/retry")
def retry_tag_detection(job_id: str, request: Request) -> dict:
    return _ctx(request).jobs.retry_tag_detection(job_id)


@router.post("/{job_id}/start")
def start_job(job_id: str, request: Request) -> dict:
    """校验通过才入队。校验失败一律 4xx，绝不让非法配置进入流水线。"""
    ctx = _ctx(request)
    job = ctx.jobs.assert_startable(job_id)
    return ctx.jobs.enqueue(job)


@router.get("/{job_id}/events")
def list_events(
    job_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> dict:
    """增量事件流：只有阶段名，没有百分比。"""
    return _ctx(request).jobs.list_events(job_id, after=after)


@router.get("/{job_id}/report")
def get_report(job_id: str, request: Request) -> dict:
    return _ctx(request).jobs.get_report(job_id)


@router.post("/{job_id}/reprocess", status_code=201)
def reprocess(job_id: str, request: Request) -> dict:
    """按原配置复制出一个全新 ID 的任务，原件与产物路径完全独立。"""
    return _ctx(request).history.reprocess(job_id)


@router.get("/{job_id}/file")
def get_file(
    job_id: str,
    request: Request,
    kind: str = Query(default="output"),
) -> FileResponse:
    ctx = _ctx(request)
    if kind not in FILE_KINDS:
        raise AppError(
            ErrorCode.INVALID_CONFIG,
            "不支持的文件类型",
            details={"kind": kind, "allowed": list(FILE_KINDS)},
            next_action="请从界面上的下载按钮重新下载",
            http_status=400,
        )

    if kind == "source":
        path = ctx.jobs.source_path(job_id)
        job = ctx.jobs.get_job(job_id)
        download_name = job["original_name"]
    else:
        path = ctx.jobs.output_path(job_id)
        job = ctx.jobs.get_job(job_id)
        download_name = job["output"]["file_name"]

    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(download_name)},
    )
