"""十二项只读校验的引擎骨架（PRD §10）。

设计要点，全部来自 PRD 里被反复强调的那几条：

1. **只读**：这里不调用 `doc.save()` / `pdf.save()`，不写任何文件。校验前后
   原文件 sha256 必须一致（AC-R13-N4），所以连"临时另存一份再看"都不允许。
2. **单项隔离**：某一项抛异常只把这一项记成 `fail`，其余项照常跑完
   （AC-R13-E2）。一个 5000 页文档的字体表坏掉，不该让"文件命名"也变成未知。
3. **超时保护**：单项超过 60 秒记 `fail`（§10.3 第 3 条）。校验跑在 worker 的
   单线程队列里，一项卡死等于整批任务停摆。
4. **不缓存**：前置与后置各跑一次，互不引用（§10.3 第 6 条）。后置校验要
   反映处理后的真实状态，复用前置结果等于自己骗自己。

`CheckResult` 的字段名与 `app.schemas.validate_verify_result()` 一一对应，
序列化只走 `as_dict()`，避免各处手写字典时漏字段。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from app.schemas import VERIFY_KEYS, VERIFY_STATUSES

__all__ = [
    "CHECK_TIMEOUT_SECONDS",
    "CHECKS",
    "PHASE_POST",
    "PHASE_PRECHECK",
    "CheckContext",
    "CheckResult",
    "Finding",
    "make_result",
    "run_check",
    "run_checks",
]

# 单项上限。够跑完几千页的字体表，又能让"死循环"在用户失去耐心前结束。
CHECK_TIMEOUT_SECONDS = 60.0

# 阶段名。`app.services.verify` 里也有同值常量，但那一层依赖本层，反过来
# 导入会成环；两边各写一份、并由 `verify` 侧断言相等，是这里能接受的做法。
PHASE_PRECHECK = "precheck"
PHASE_POST = "post"


@dataclass(frozen=True)
class Finding:
    """一条明细。

    `level` 只用四态，与 `VERIFY_STATUSES` 同源——报告里不该出现第五种
    "提示"级别，否则前端又要为它写一套样式和文案。
    """

    level: str
    message: str
    page: int | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"level": self.level, "message": self.message}
        if self.page is not None:
            payload["page"] = self.page
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class CheckContext:
    """一次校验需要的全部输入。

    `doc` 与 `pdf` 由调用方打开并负责关闭：校验本身不该决定文件的生命周期，
    否则前置校验与后置校验就要各自实现一套"什么时候打开、什么时候关"。

    `phase` 决定"这个文件是谁命名的"：前置校验面对用户上传的原名，后置校验
    面对工具生成的产物名（R27、AC-R27-N4）。默认前置，是为了让所有老调用点
    和单测不必逐个改签名。
    """

    path: Path
    filename: str
    size_bytes: int
    page_count: int
    doc: Any  # pymupdf.Document
    pdf: Any  # pikepdf.Pdf
    phase: str = PHASE_PRECHECK


@dataclass
class CheckResult:
    key: str
    status: str
    summary: str
    findings: list[Finding] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "summary": self.summary,
            "findings": [item.as_dict() for item in self.findings],
            "duration_ms": self.duration_ms,
        }


def make_result(
    key: str,
    status: str,
    summary: str,
    findings: Iterable[Finding] = (),
) -> CheckResult:
    """构造结果并顺手校验状态合法，避免某个 check 手滑写出第五种状态。"""
    if status not in VERIFY_STATUSES:
        raise ValueError(f"未知校验状态：{status}")
    return CheckResult(
        key=key,
        status=status,
        summary=summary,
        findings=list(findings),
    )


def _failure(key: str, summary: str, detail: str | None = None) -> CheckResult:
    return make_result(
        key,
        "fail",
        summary,
        [Finding(level="fail", message=summary, detail=detail)],
    )


def run_check(key: str, ctx: CheckContext) -> CheckResult:
    """执行单项。异常与超时都在这里转成 `fail`，调用方拿到的永远是结果。"""
    return _run_one(key, ctx, CHECK_TIMEOUT_SECONDS)


def run_checks(
    keys: Iterable[str],
    ctx: CheckContext,
    timeout: float = CHECK_TIMEOUT_SECONDS,
) -> list[CheckResult]:
    """逐项执行；顺序即传入顺序（也就是界面展示顺序）。"""
    return [_run_one(key, ctx, timeout) for key in keys]


def _run_one(key: str, ctx: CheckContext, timeout: float) -> CheckResult:
    runner = CHECKS.get(key)
    if runner is None:
        raise KeyError(f"未知校验项：{key}")

    started = time.perf_counter()
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = runner(ctx)
        except BaseException as exc:  # noqa: BLE001 - 隔离是本函数的职责
            box["error"] = exc

    # 校验跑在子线程里，主线程才能"到点就走"。pymupdf / pikepdf 的解析过程
    # 大部分会释放 GIL，所以这个 join(timeout) 在真实卡死场景下确实能返回。
    worker = threading.Thread(target=target, name=f"check-{key}", daemon=True)
    worker.start()
    worker.join(timeout)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if worker.is_alive():
        return _failure(
            key,
            f"检查超时（超过 {int(timeout)} 秒）",
            detail="该文件可能过大或结构异常，其余校验项不受影响",
        )

    error = box.get("error")
    if error is not None:
        first_line = str(error).strip().splitlines()[0] if str(error).strip() else ""
        return _failure(
            key,
            f"检查异常：{type(error).__name__}",
            detail=first_line or None,
        )

    result = box.get("result")
    if not isinstance(result, CheckResult):
        return _failure(key, "检查未返回结果", detail=f"{type(result).__name__}")

    result.duration_ms = elapsed_ms
    return result


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

# 这里直接导入 `run` 而不是先导入模块再取属性：本文件顶部有
# `from __future__ import annotations`，它会在模块命名空间里绑定一个名叫
# `annotations` 的 `_Feature` 对象，`from ... import annotations` 拿到的
# 就是这个对象而不是同名子模块。
from app.pdf.checks.annotations import run as _run_annotations  # noqa: E402
from app.pdf.checks.external_links import run as _run_external_links  # noqa: E402
from app.pdf.checks.fonts import run as _run_fonts  # noqa: E402
from app.pdf.checks.initial_view import run as _run_initial_view  # noqa: E402
from app.pdf.checks.metadata import run as _run_metadata  # noqa: E402
from app.pdf.checks.naming import run as _run_naming  # noqa: E402
from app.pdf.checks.orientation import run as _run_orientation  # noqa: E402
from app.pdf.checks.outlines import run as _run_outlines  # noqa: E402
from app.pdf.checks.page_labels import run as _run_page_labels  # noqa: E402
from app.pdf.checks.size import run as _run_size  # noqa: E402
from app.pdf.checks.text_layer import run as _run_text_layer  # noqa: E402
from app.pdf.checks.toc import run as _run_toc  # noqa: E402

CHECKS: dict[str, Callable[[CheckContext], CheckResult]] = {
    "filename_naming": _run_naming,
    "file_size": _run_size,
    "external_links": _run_external_links,
    "page_orientation": _run_orientation,
    "text_layer": _run_text_layer,
    "annotations": _run_annotations,
    "font_embedding": _run_fonts,
    "toc": _run_toc,
    "page_labels": _run_page_labels,
    "bookmarks": _run_outlines,
    "initial_view": _run_initial_view,
    # 键名带 `_security` 后缀，与处理键 `metadata` 区分（见 `app/schemas.py` 注释）。
    "metadata_security": _run_metadata,
}

# 注册表与契约必须严格同构：少一个键意味着某份需求永远不会被执行，
# 多一个键意味着界面上会出现一个点不开的勾选框。
assert set(CHECKS) == set(VERIFY_KEYS), (
    f"校验注册表与 VERIFY_KEYS 不一致：{set(CHECKS) ^ set(VERIFY_KEYS)}"
)
