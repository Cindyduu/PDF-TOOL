"""校验单测的公共夹具。

`CheckContext` 要求调用方自己打开并关闭 `fitz.Document` 与 `pikepdf.Pdf`，
测试里逐个手写 `open` / `close` 既啰嗦又容易漏（漏了在 Windows 上会锁文件）。
这里用一个工厂夹具把生命周期管起来，测试只关心"打开哪个文件"。
"""

from __future__ import annotations

from pathlib import Path

import fitz
import pikepdf
import pytest

from app.pdf.checks import PHASE_PRECHECK, CheckContext


@pytest.fixture
def ctx_factory():
    """按任意路径构造 `CheckContext`；夹具结束时统一关闭句柄。

    `phase` 默认前置校验：绝大多数校验项只在前置阶段有意义，让它默认成立，
    需要验后置行为的用例才显式传 `phase="post"`（AC-R27-N4）。
    """
    opened: list[tuple[object, object]] = []

    def _open(
        path: Path,
        filename: str | None = None,
        size_bytes: int | None = None,
        page_count: int | None = None,
        phase: str = PHASE_PRECHECK,
    ) -> CheckContext:
        path = Path(path)
        doc = fitz.open(str(path))
        pdf = pikepdf.open(str(path))
        opened.append((doc, pdf))
        return CheckContext(
            path=path,
            filename=path.name if filename is None else filename,
            size_bytes=path.stat().st_size if size_bytes is None else size_bytes,
            page_count=doc.page_count if page_count is None else page_count,
            doc=doc,
            pdf=pdf,
            phase=phase,
        )

    yield _open

    for doc, pdf in opened:
        doc.close()
        pdf.close()


@pytest.fixture
def check_ctx(fixture_pdfs: Path, ctx_factory):
    """按样本文件名构造 `CheckContext`（样本目录由 `fixture_pdfs` 保证存在）。"""

    def _open(
        name: str,
        filename: str | None = None,
        size_bytes: int | None = None,
        page_count: int | None = None,
        phase: str = PHASE_PRECHECK,
    ) -> CheckContext:
        return ctx_factory(
            fixture_pdfs / name,
            filename=filename,
            size_bytes=size_bytes,
            page_count=page_count,
            phase=phase,
        )

    return _open
