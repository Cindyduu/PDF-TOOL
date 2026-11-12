"""上传文件的物理检查：页数、大小、加密、签名、损坏。

这里只做"事实认定"，不写任何业务状态；拒绝与否由调用方转成统一错误模型。
所有拒绝都必须给出用户能执行的下一步，而不是一句"文件无效"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pikepdf
import pymupdf

from app.config import Limits
from app.errors import AppError, ErrorCode

PDF_MAGIC = b"%PDF-"

NEXT_ACTION_UPLOAD = "请换一个 PDF 文件后重试"


@dataclass(frozen=True)
class PdfFacts:
    page_count: int
    size: int
    sha256: str
    encrypted: bool
    signed: bool


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _iter_fields(pdf: pikepdf.Pdf):
    """遍历 /AcroForm /Fields，含 /Kids 子域。"""
    acroform = pdf.Root.get("/AcroForm")
    if acroform is None:
        return
    stack = list(acroform.get("/Fields") or [])
    seen: set[tuple[int, int]] = set()
    while stack:
        field = stack.pop()
        try:
            objgen = field.objgen
        except AttributeError:  # pragma: no cover - 直接对象
            objgen = (0, 0)
        if objgen in seen:
            continue
        seen.add(objgen)
        yield field
        kids = field.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            stack.extend(kids)


def has_signature(pdf: pikepdf.Pdf) -> bool:
    """签名域且已有签名值（/V）才算已签名；空签名域不拒绝。"""
    for field in _iter_fields(pdf):
        if str(field.get("/FT", "")) != "/Sig":
            continue
        value = field.get("/V")
        if value is not None and len(value.keys()) > 0:
            return True
    return False


def _encrypted_error() -> AppError:
    return AppError(
        ErrorCode.ENCRYPTED_PDF,
        "该 PDF 已加密，无法处理",
        retryable=False,
        next_action="请先用 PDF 软件去除密码保护后再上传",
        http_status=400,
    )


def _corrupt_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CORRUPT_PDF,
        "该文件已损坏，无法读取",
        details={"reason": detail},
        retryable=False,
        next_action="请重新导出该 PDF 后再上传",
        http_status=400,
    )


def inspect_pdf(path: Path, limits: Limits | None = None) -> PdfFacts:
    limits = limits or Limits()
    target = Path(path)

    if not target.is_file():
        raise AppError(
            ErrorCode.SOURCE_MISSING,
            "文件不存在",
            details={"path": target.name},
            next_action="请重新上传文件",
            http_status=404,
        )

    size = target.stat().st_size
    if size == 0:
        raise AppError(
            ErrorCode.EMPTY_FILE,
            "文件是空的",
            retryable=False,
            next_action=NEXT_ACTION_UPLOAD,
            http_status=400,
        )
    if size > limits.max_upload_bytes:
        raise AppError(
            ErrorCode.FILE_TOO_LARGE,
            "文件超过大小上限",
            details={"size": size, "limit": limits.max_upload_bytes},
            retryable=False,
            next_action=f"请上传不超过 {limits.max_upload_bytes // (1024 * 1024)} MB 的文件",
            http_status=413,
        )

    with open(target, "rb") as handle:
        if not handle.read(len(PDF_MAGIC)).startswith(PDF_MAGIC):
            raise AppError(
                ErrorCode.NOT_A_PDF,
                "这不是 PDF 文件",
                retryable=False,
                next_action=NEXT_ACTION_UPLOAD,
                http_status=400,
            )

    digest = sha256_file(target)

    try:
        pdf = pikepdf.open(target)
    except pikepdf.PasswordError as exc:
        raise _encrypted_error() from exc
    except Exception as exc:  # pikepdf.PdfError 等
        raise _corrupt_error(type(exc).__name__) from exc

    with pdf:
        signed = has_signature(pdf)

    try:
        doc = pymupdf.open(target)
    except Exception as exc:
        raise _corrupt_error(type(exc).__name__) from exc

    try:
        if doc.needs_pass:
            raise _encrypted_error()
        page_count = doc.page_count
    finally:
        doc.close()

    if page_count <= 0:
        raise _corrupt_error("no_pages")
    if page_count > limits.max_pages:
        raise AppError(
            ErrorCode.TOO_MANY_PAGES,
            f"页数超过上限（{page_count} 页）",
            details={"page_count": page_count, "limit": limits.max_pages},
            retryable=False,
            next_action=f"请拆分后再上传，单个文件不超过 {limits.max_pages} 页",
            http_status=400,
        )
    if signed:
        raise AppError(
            ErrorCode.SIGNED_PDF,
            "该 PDF 已包含数字签名，处理会使签名失效",
            retryable=False,
            next_action="请先移除签名后再上传，或联系签名方重新出具",
            http_status=400,
        )

    return PdfFacts(
        page_count=page_count,
        size=size,
        sha256=digest,
        encrypted=False,
        signed=False,
    )
