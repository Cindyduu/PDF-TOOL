"""任务 API：上传、读取、配置、开始处理、事件、报告、下载。

设计约束：
- 只做协议层的事：取参数、调服务、把 AppError 交给全局处理器；
- 所有阻塞 IO 端点用同步函数（FastAPI 会放进线程池），不占用事件循环；
- `POST /api/jobs` 只接受一个文件（多文件直接拒绝，保持既有语义）；
  批量走 `POST /api/jobs/batch`，两者互不影响；
- 下载端点只吐出**已通过最终验证**的文件，没就绪一律 409，文件没了 404。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict

from app.core.naming import (
    content_disposition,
    html_content_disposition,
    preferred_download_name,
)
from app.errors import AppError, ErrorCode
from app.report import render_batch_report, render_file_report
from app.services.verify import VERIFY_PHASES

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

NEXT_ACTION_PICK_FILE = "请选择一个 PDF 文件后重试"

FILE_KINDS = ("output", "source")

PHASE_SLUGS = {"precheck": "前置", "post": "后置"}


def _ctx(request: Request):
    return request.app.state.ctx


def _assert_phase(phase: str) -> str:
    if phase not in VERIFY_PHASES:
        raise AppError(
            ErrorCode.INVALID_CONFIG,
            "不支持的校验阶段",
            details={"phase": phase, "allowed": list(VERIFY_PHASES)},
            next_action="前置校验请用 precheck，后置校验请用 post",
            http_status=400,
        )
    return phase


def _html_response(document: str, filename: str) -> HTMLResponse:
    """把渲染好的文档当文件发出去。

    `media_type` 里显式写 `charset=utf-8`：报告正文全是中文，少了它浏览器
    会按 latin-1 猜编码，整页变乱码。`Content-Disposition` 用 HTML 版构造，
    回退名保留 `.html` —— 退回成 `.pdf` 会让系统拿 PDF 阅读器去开它。
    """
    return HTMLResponse(
        content=document,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": html_content_disposition(filename)},
    )


class ConfigPatch(BaseModel):
    """配置补丁。字段缺失表示"不改"，显式给 null 表示"清空"。

    v4.0 把配置拆成「校验项」与「处理项」两组：校验项决定只读检查跑哪些，
    处理项决定流水线跑哪些。两组互不影响，用户完全可以只做其中一件。
    """

    model_config = ConfigDict(extra="forbid")

    exempt_pages_raw: str | None = None
    verify_items: list[str] | None = None
    skip_precheck: bool | None = None
    process_items: list[str] | None = None
    write_process_log: bool | None = None


def _patch_kwargs(patch: ConfigPatch) -> dict:
    """把补丁翻译成 `update_config` 的关键字参数。

    「不改」只有一种表达方式：**字段不出现在请求里**。
    因此除了 `exempt_pages_raw`（`null` = 清空输入框）之外，
    其余字段显式给 `null` 一律当作非法值拒绝 —— 否则
    「null 是清空还是不改」会变成前后端各猜一套的灰色地带。
    """
    provided = patch.model_fields_set
    kwargs: dict = {}
    if "exempt_pages_raw" in provided:
        kwargs["exempt_pages_raw"] = patch.exempt_pages_raw or ""
    for key in ("verify_items", "skip_precheck", "process_items", "write_process_log"):
        if key not in provided:
            continue
        value = getattr(patch, key)
        if value is None:
            raise AppError(
                ErrorCode.INVALID_CONFIG,
                f"{key} 不能为 null",
                details={"field": key, "received": None},
                next_action="不修改该字段请不要传它；清空请传空列表或 false",
                http_status=400,
            )
        kwargs[key] = value
    return kwargs


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
        # v4.0：单文件上传等价于「1 个文件的批次」（§9.3）。批次号由服务层
        # 在缺省时自动生成，所以这里仍然是一次 `create_job`——错误码与
        # 状态码保持原样，不会被批处理那套「一个坏不拖垮整批」的包装吞掉。
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
    return _ctx(request).jobs.apply_batch_config(
        batch_id, **_patch_kwargs(patch)
    )


@router.post("/batch/{batch_id}/start")
def start_batch(batch_id: str, request: Request) -> dict:
    return _ctx(request).jobs.start_batch(batch_id)


@router.post("/batch/{batch_id}/verify", status_code=202)
def verify_batch(
    batch_id: str, request: Request, patch: ConfigPatch | None = None
) -> dict:
    """提交前置校验（R13）。body 可选，用来带上准备页勾选的 `verify_items`。

    为什么先写配置再入队：页面 1 上「勾选 → 点校验」是一个动作，用户没有
    第二个机会去 PATCH 配置。分开两个请求会让"勾了但没保存"变成静默丢配置。

    整批逐个提交，单个失败进 `skipped` 而不是整个请求 4xx —— 与
    `POST /batch/{id}/start` 同一取舍：一个文件没勾校验项，不该拦住另外十九个。
    """
    ctx = _ctx(request)
    if patch is not None:
        ctx.jobs.apply_batch_config(batch_id, **_patch_kwargs(patch))
    return ctx.jobs.submit_batch_verify(batch_id)


@router.get("/batch/{batch_id}/verify-report")
def get_batch_verify_report(
    batch_id: str,
    request: Request,
    phase: str = Query(default="precheck"),
) -> dict:
    """批次校验汇总（§8.4）。数据全部来自已落盘的结果，不重新跑校验。"""
    _assert_phase(phase)
    return _ctx(request).jobs.batch_verify_report(batch_id, phase=phase)


@router.get("/batch/{batch_id}/verify-report.html")
def get_batch_verify_report_html(
    batch_id: str,
    request: Request,
    phase: str = Query(default="precheck"),
) -> HTMLResponse:
    """批次校验报告 HTML（R23）。每次请求现渲染，不落盘。

    不落盘是刻意的：报告反映的是「现在」的结果，缓存一份就可能和页面上的
    数字对不上。渲染是纯函数、耗时在毫秒级，没必要为它维护失效逻辑。
    """
    _assert_phase(phase)
    report = _ctx(request).jobs.batch_verify_report(batch_id, phase=phase)
    filename = f"PDF校验报告_{batch_id}_{phase}.html"
    return _html_response(render_batch_report(report), filename)


@router.get("/batch/{batch_id}/process-log.html")
def get_batch_process_log_html(batch_id: str, request: Request) -> FileResponse:
    """批次处理日志 HTML（R28）。发的是处理结束时**已生成**的那份文件。

    与校验报告相反，日志是「当时发生了什么」的凭证，必须发原件 ——
    用现在的 job.json 重渲染，用户手里的日志就会悄悄变样。
    """
    path = _ctx(request).jobs.batch_process_log(batch_id)
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": html_content_disposition(f"PDF处理日志_{batch_id}.html")
        },
    )


@router.get("/batch/{batch_id}/results")
def get_batch_results(batch_id: str, request: Request) -> dict:
    """批次处理 + 后置校验结果（页面 4 数据源，§9.1）。

    只读：数据全部来自已落盘的 job.json / report.json，不触发任何处理。
    与 `verify-report` 不同，这里**不会**因为某个文件没有后置校验就 404 ——
    页面 4 还要显示失败文件，它们本来就没有后置校验。
    """
    return _ctx(request).jobs.batch_results(batch_id)


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
    return _ctx(request).jobs.update_config(job_id, **_patch_kwargs(patch))


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


@router.get("/{job_id}/verify-report.html")
def get_file_verify_report_html(
    job_id: str,
    request: Request,
    phase: str = Query(default="precheck"),
) -> HTMLResponse:
    """单文件校验报告 HTML（R23）。页面 3 / 页面 4 的「查看报告」都用它。"""
    _assert_phase(phase)
    result = _ctx(request).jobs.file_verify_report(job_id, phase=phase)
    filename = f"PDF校验报告_{job_id}_{phase}.html"
    return _html_response(render_file_report(result), filename)


@router.post("/{job_id}/reprocess", status_code=201)
def reprocess(job_id: str, request: Request) -> dict:
    """按原配置复制出一个全新 ID 的任务，原件与产物路径完全独立。"""
    return _ctx(request).history.reprocess(job_id)


@router.get("/{job_id}/file")
def get_file(
    job_id: str,
    request: Request,
    kind: str = Query(default="output"),
    name: str | None = Query(default=None, max_length=512),
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

    # 用户在「另存为」面板里改过的名字。它只进响应头，不参与任何路径拼接；
    # 名字会过 sanitize_filename，控制字符和路径分隔符都被剔掉，所以没有
    # 响应头注入面。给不出可用名字时退回任务默认名，响应体一个字节都不变。
    download_name = preferred_download_name(name, download_name)

    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(download_name)},
    )
