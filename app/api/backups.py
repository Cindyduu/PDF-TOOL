"""备份 API：列表、创建、下载、校验（拿令牌）、恢复。

协议层只做三件事：取参数、调服务、把 `AppError` 交给全局处理器。
恢复刻意做成两段式（inspect 拿令牌 → restore 带令牌），
这样"手滑点了一下"不会直接覆盖现有数据。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from app.core.naming import content_disposition
from app.errors import AppError, ErrorCode

router = APIRouter(prefix="/api/backups", tags=["backups"])

NEXT_ACTION_PICK_BACKUP = "请选择一个 .zip 备份文件后重试"


def _ctx(request: Request):
    return request.app.state.ctx


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str


@router.get("")
def list_backups(request: Request) -> dict:
    return _ctx(request).backups.list_backups()


@router.post("", status_code=201)
def create_backup(request: Request) -> dict:
    """立即备份当前数据。写完会自校验，读不回来的包不会留在磁盘上。"""
    return {"backup": _ctx(request).backups.create(reason="manual")}


@router.post("/inspect")
def inspect_backup(
    request: Request,
    files: list[UploadFile] | None = File(default=None),
) -> dict:
    """只读校验上传的备份包，通过则返回摘要与短期令牌（不落库、不改数据）。"""
    uploads = [item for item in (files or []) if item is not None]
    if not uploads:
        raise AppError(
            ErrorCode.INVALID_BACKUP,
            "没有收到备份文件",
            next_action=NEXT_ACTION_PICK_BACKUP,
            http_status=400,
        )
    if len(uploads) > 1:
        raise AppError(
            ErrorCode.INVALID_BACKUP,
            "一次只能校验一个备份文件",
            details={"count": len(uploads)},
            next_action=NEXT_ACTION_PICK_BACKUP,
            http_status=400,
        )

    upload = uploads[0]
    try:
        return _ctx(request).backups.inspect_upload(upload.filename or "backup.zip", upload.file)
    finally:
        upload.file.close()


@router.post("/restore")
def restore_backup(payload: RestoreRequest, request: Request) -> dict:
    """按令牌执行恢复。令牌一次性，过期或用过都要重新校验。"""
    return _ctx(request).backups.restore(payload.token)


@router.get("/{backup_id}/file")
def download_backup(backup_id: str, request: Request) -> FileResponse:
    path = _ctx(request).backups.path(backup_id)
    return FileResponse(
        path,
        media_type="application/zip",
        headers={
            "Content-Disposition": content_disposition(f"{backup_id}.zip"),
        },
    )
