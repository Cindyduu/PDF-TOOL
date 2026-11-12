"""输出文件验证：能打开、页数对、结构完整、元数据真的清空。

流水线只发布**验证通过**的文件。任何一步「没验」都等于没做，
所以这里检查的都是可以直接观测的事实，不看处理器自己怎么说。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
import pymupdf

__all__ = ["VerifyReport", "verify_pdf_file", "metadata_problems", "sha256_file"]


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    size: int = 0
    sha256: str = ""
    page_count: int | None = None
    problems: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)

    def raise_if_bad(self) -> None:
        if not self.ok:
            raise ValueError("；".join(self.problems) or "验证失败")


def sha256_file(path: Path | str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def metadata_problems(pdf: pikepdf.Pdf) -> list[str]:
    """文档描述必须为空：DocInfo 无内容，且不存在 XMP 元数据流。"""
    problems: list[str] = []
    info = pdf.trailer.get("/Info")
    if info is not None:
        if not isinstance(info, pikepdf.Dictionary):
            problems.append("trailer /Info 不是字典")
        elif len(info) > 0:
            problems.append(f"trailer /Info 仍有 {len(info)} 个字段")
    if pdf.Root.get("/Metadata") is not None:
        problems.append("Root /Metadata 仍然存在")
    return problems


def verify_pdf_file(
    path: Path | str,
    *,
    expected_pages: int | None = None,
    require_no_metadata: bool = False,
) -> VerifyReport:
    """打开并检查一个 PDF。返回报告，不抛异常。"""
    target = Path(path)
    problems: list[str] = []

    if not target.is_file():
        return VerifyReport(ok=False, problems=("文件不存在",))
    size = target.stat().st_size
    if size == 0:
        return VerifyReport(ok=False, size=0, problems=("文件为空",))
    with open(target, "rb") as handle:
        magic = handle.read(5)
    if magic != b"%PDF-":
        return VerifyReport(
            ok=False, size=size, problems=(f"文件头不是 PDF：{magic!r}",)
        )

    digest = sha256_file(target)
    page_count: int | None = None
    details: dict = {}

    try:
        with pikepdf.open(target) as pdf:
            page_count = len(pdf.pages)
            details["pikepdf_pages"] = page_count
            if page_count <= 0:
                problems.append("页数为 0")
            if pdf.Root.get("/Pages") is None:
                problems.append("缺少 /Root /Pages")
            if require_no_metadata:
                problems.extend(metadata_problems(pdf))
    except pikepdf.PasswordError:
        problems.append("文件被加密，无法打开")
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都算不可用
        problems.append(f"pikepdf 无法打开：{type(exc).__name__}: {exc}")

    try:
        with pymupdf.open(str(target)) as doc:
            details["pymupdf_pages"] = doc.page_count
            if doc.needs_pass:
                problems.append("文件需要密码，无法打开")
            elif page_count is not None and doc.page_count != page_count:
                problems.append(
                    f"两个解析器页数不一致：pikepdf={page_count} pymupdf={doc.page_count}"
                )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"PyMuPDF 无法打开：{type(exc).__name__}: {exc}")

    if expected_pages is not None and page_count is not None:
        if page_count != expected_pages:
            problems.append(f"页数变化：期望 {expected_pages}，实际 {page_count}")

    return VerifyReport(
        ok=not problems,
        size=size,
        sha256=digest,
        page_count=page_count,
        problems=tuple(problems),
        details=details,
    )
