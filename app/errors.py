"""统一错误模型。

所有对外错误都必须是 AppError，序列化为
`{code, message, details, retryable, next_action}`。
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    # 输入
    NOT_A_PDF = "not_a_pdf"
    FILE_TOO_LARGE = "file_too_large"
    EMPTY_FILE = "empty_file"
    MULTIPLE_FILES = "multiple_files"
    TOO_MANY_FILES = "too_many_files"
    ENCRYPTED_PDF = "encrypted_pdf"
    SIGNED_PDF = "signed_pdf"
    CORRUPT_PDF = "corrupt_pdf"
    TOO_MANY_PAGES = "too_many_pages"
    INVALID_PAGES = "invalid_pages"
    INVALID_CONFIG = "invalid_config"
    # 任务
    JOB_NOT_FOUND = "job_not_found"
    JOB_ALREADY_STARTED = "job_already_started"
    JOB_NOT_READY = "job_not_ready"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    OUTPUT_NOT_READY = "output_not_ready"
    OUTPUT_MISSING = "output_missing"
    SOURCE_MISSING = "source_missing"
    # 文件访问
    INVALID_ID = "invalid_id"
    PATH_NOT_ALLOWED = "path_not_allowed"
    OPEN_FOLDER_FAILED = "open_folder_failed"
    # 备份
    INVALID_BACKUP = "invalid_backup"
    BACKUP_NOT_FOUND = "backup_not_found"
    RESTORE_CONFLICT = "restore_conflict"
    RESTORE_TOKEN_INVALID = "restore_token_invalid"
    RESTORE_FAILED = "restore_failed"
    # 通用
    LOCKED = "locked"
    INTERNAL_ERROR = "internal_error"


class AppError(Exception):
    """带下一步建议的应用异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        next_action: str | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable
        self.next_action = next_action
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
            "next_action": self.next_action,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"AppError({self.code!r}, {self.message!r})"


class PathTraversalError(AppError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(
            ErrorCode.PATH_NOT_ALLOWED,
            message,
            next_action="请通过界面重新选择文件或刷新页面",
            **kwargs,
        )


class LockError(AppError):
    def __init__(self, name: str, holder: dict[str, Any] | None = None) -> None:
        super().__init__(
            ErrorCode.LOCKED,
            f"操作被占用（{name}），请稍后重试",
            details={"lock": name, "holder": holder or {}},
            retryable=True,
            next_action="等待当前操作结束后重试",
            http_status=409,
        )
