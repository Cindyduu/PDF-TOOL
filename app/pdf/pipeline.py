"""九项处理流水线：**只编排**，不写业务逻辑。

每一项都走同一个模式，没有例外：

1. 打开「上一有效候选」；
2. 现场算出上下文（页码映射、命名目标表、豁免页）；
3. 交给处理器改这份内存中的文档；
4. 处理器说改了，才另存为临时文件；
5. **重新打开临时文件验证**（页数、结构、两个解析器都能读）；
6. 验证通过才原子替换成新的工作候选；不通过就整项作废，保留上一候选。

第 5、6 步是这套流水线唯一的价值所在：处理器说「改好了」不算数，
磁盘上那个文件真能打开、页数没变、结构完整才算数。

`fast_web_view` 是唯一例外——它必须在保存时就把线性化做进去，
所以保存后再回读检查，不通过则退回线性化之前的候选。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pikepdf

from app.core.atomic import fsync_dir
from app.core.storage import utcnow_iso
from app.pdf import annotations, bookmarks, initial_view, linearize, links, metadata
from app.pdf.context import OpContext
from app.pdf.outcome import OpOutcome
from app.pdf.tags import generate as tags_generate
from app.pdf.verify import VerifyReport, sha256_file, verify_pdf_file
from app.schemas import OPERATION_KEYS, OPERATION_TITLES, STAGE_NAMES

__all__ = [
    "PipelineError",
    "PipelineResult",
    "Handler",
    "run_pipeline",
    "HANDLERS",
]

Handler = Callable[[pikepdf.Pdf, OpContext], OpOutcome]


class PipelineError(RuntimeError):
    """整条流水线无法产出可用文件。单项目失败不走这里。"""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        problems: tuple[str, ...] = (),
        results: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.problems = problems
        self.results = results or []


# --------------------------------------------------------------------------- #
# 处理器表
# --------------------------------------------------------------------------- #

HANDLERS: dict[str, Handler] = {
    "flatten_annotations": annotations.process,
    "bookmarks_inherit_zoom": bookmarks.process,
    "links_inherit_zoom": links.process,
    "initial_layout": initial_view.process_initial_layout,
    "initial_zoom": initial_view.process_initial_zoom,
    "bookmark_panel": initial_view.process_bookmark_panel,
    "metadata": metadata.process,
    # tagged_pdf 由阶段 F 注入，见 run_pipeline(tag_processor=...)
    "fast_web_view": linearize.process,
}

# 线性化必须最后做：之后再存盘会丢掉线性化结构。
LINEARIZE_KEY = "fast_web_view"
TAG_KEY = "tagged_pdf"

for _key in HANDLERS:
    assert _key in OPERATION_KEYS, f"处理器表里有未知处理项：{_key}"
assert OPERATION_KEYS[-1] == LINEARIZE_KEY, "fast_web_view 必须是最后一项"


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PipelineResult:
    output: Path
    page_count: int
    results: list[dict[str, Any]]
    overall_status: str
    warnings: list[str] = field(default_factory=list)
    changed_operations: tuple[str, ...] = ()
    source_sha256: str = ""
    output_sha256: str = ""
    size: int = 0

    def result_for(self, key: str) -> dict[str, Any]:
        for item in self.results:
            if item["key"] == key:
                return item
        raise KeyError(key)

    @property
    def failed_keys(self) -> tuple[str, ...]:
        return tuple(r["key"] for r in self.results if r["status"] == "failed")


# --------------------------------------------------------------------------- #
# 保存与校验
# --------------------------------------------------------------------------- #


def _save(pdf: pikepdf.Pdf, path: Path, *, linearize_file: bool = False) -> None:
    """统一保存入口。

    `fix_metadata_version=False` 是必须的：pikepdf 默认会去「修正」XMP 里声明的
    PDF 版本，一旦文档里还有 XMP 就会把它写回去，等于把 R6 的成果悄悄撤销。
    """
    pdf.save(str(path), linearize=linearize_file, fix_metadata_version=False)


def _rejected(
    outcome: OpOutcome,
    report: VerifyReport,
    *,
    label: str,
) -> OpOutcome:
    """把一次「验证没通过」的改动整体作废，并如实降级成失败。

    计数必须保持 `checked == 五个归宿之和` 这条不变量（报告要能对得上账）：
    候选文件被丢弃，等于这一项检查过的对象**一个都没生效**，
    因此除 `checked` 外的归宿全部清零，`failed` 取 `checked`。
    之前只把 `failed` 抬到 1 而不同步 `checked`，会让六元组自相矛盾。
    """
    outcome.status = "failed"
    checked = max(1, outcome.counts["checked"])
    for bucket in ("modified", "compliant", "exempt", "not_applicable"):
        outcome.counts[bucket] = 0
    outcome.counts["checked"] = checked
    outcome.counts["failed"] = checked
    outcome.changed = False
    outcome.reason_code = "output_verification_failed"
    outcome.message = f"{label}的结果未通过文件验证，已回退到处理前的文件"
    for problem in report.problems:
        outcome.warn(f"验证未通过：{problem}")
    outcome.next_action = "其他处理项不受影响；请确认源文件完整后重试"
    return outcome


def _commit(tmp: Path, target: Path) -> None:
    """原子替换工作候选。调用前必须确保 PDF 已经关闭。"""
    os.replace(tmp, target)
    fsync_dir(target.parent)


# --------------------------------------------------------------------------- #
# 单项执行
# --------------------------------------------------------------------------- #


def _run_handler(
    current: Path,
    key: str,
    handler: Handler,
    *,
    page_count: int,
    exempt_pages: tuple[int, ...],
    work_dir: Path,
) -> tuple[dict[str, Any], bool]:
    """跑一项处理器。返回 (结果字典, 是否真的改动了候选文件)。"""
    started_at = utcnow_iso()
    label = OPERATION_TITLES[key]
    tmp = work_dir / f".candidate-{key}.pdf"

    with pikepdf.open(current) as pdf:
        ctx = OpContext.build(pdf, exempt_pages)
        outcome = handler(pdf, ctx)
        if not outcome.changed:
            return (
                outcome.to_result(key, started_at=started_at, finished_at=utcnow_iso()),
                False,
            )
        _save(pdf, tmp)

    report = verify_pdf_file(tmp, expected_pages=page_count)
    if not report.ok:
        tmp.unlink(missing_ok=True)
        rejected = _rejected(outcome, report, label=label)
        return (
            rejected.to_result(key, started_at=started_at, finished_at=utcnow_iso()),
            False,
        )

    _commit(tmp, current)
    return outcome.to_result(key, started_at=started_at, finished_at=utcnow_iso()), True


def _run_linearize(
    current: Path,
    *,
    page_count: int,
    work_dir: Path,
) -> tuple[dict[str, Any], bool]:
    """线性化：保存时就做进去，保存后回读检查，不通过则退回。"""
    key = LINEARIZE_KEY
    started_at = utcnow_iso()
    tmp = work_dir / f".candidate-{key}.pdf"

    with pikepdf.open(current) as pdf:
        ctx = OpContext.build(pdf)
        outcome = linearize.process(pdf, ctx)
        if not outcome.changed:
            return (
                outcome.to_result(key, started_at=started_at, finished_at=utcnow_iso()),
                False,
            )
        _save(pdf, tmp, linearize_file=True)

    linearize.finalize(outcome, tmp)
    if outcome.status == "failed":
        tmp.unlink(missing_ok=True)
        return (
            outcome.to_result(key, started_at=started_at, finished_at=utcnow_iso()),
            False,
        )

    report = verify_pdf_file(tmp, expected_pages=page_count)
    if not report.ok:
        tmp.unlink(missing_ok=True)
        rejected = _rejected(outcome, report, label=OPERATION_TITLES[key])
        return (
            rejected.to_result(key, started_at=started_at, finished_at=utcnow_iso()),
            False,
        )

    _commit(tmp, current)
    return outcome.to_result(key, started_at=started_at, finished_at=utcnow_iso()), True


def _tag_placeholder(tag_choice: str | None) -> Handler:
    """默认标签处理器：交给 `tags.generate`，它自己按用户选择分支。"""
    return tags_generate.build_processor(tag_choice)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def _notify(callback: Callable[[str], None] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


def _stage_for(key: str) -> str:
    return key if key in STAGE_NAMES else "preparing"


def run_pipeline(
    source: Path | str,
    output: Path | str,
    *,
    exempt_pages: tuple[int, ...] | list[int] = (),
    tag_choice: str | None = None,
    tag_processor: Handler | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PipelineResult:
    """按九项顺序处理 `source`，产出 `output`。

    单项失败不影响其他项；只有「最终文件不可用」才抛 `PipelineError`。
    """
    src = Path(source)
    dst = Path(output)
    if not src.is_file():
        raise PipelineError(
            "源文件不存在",
            reason_code="source_missing",
            problems=("source_missing",),
        )

    _notify(stage_callback, "preparing")

    source_report = verify_pdf_file(src)
    if not source_report.ok or source_report.page_count is None:
        raise PipelineError(
            "源文件无法读取",
            reason_code="source_unreadable",
            problems=source_report.problems,
        )
    page_count = source_report.page_count
    exempt = tuple(sorted({int(page) for page in exempt_pages}))

    dst.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=".pipeline-", dir=str(dst.parent)))
    try:
        current = work_dir / "candidate.pdf"
        shutil.copyfile(src, current)
        fsync_dir(work_dir)

        handlers: dict[str, Handler] = dict(HANDLERS)
        handlers[TAG_KEY] = tag_processor or _tag_placeholder(tag_choice)

        results: list[dict[str, Any]] = []
        changed: list[str] = []
        for key in OPERATION_KEYS:
            _notify(stage_callback, _stage_for(key))
            if key == LINEARIZE_KEY:
                result, did_change = _run_linearize(
                    current, page_count=page_count, work_dir=work_dir
                )
            else:
                result, did_change = _run_handler(
                    current,
                    key,
                    handlers[key],
                    page_count=page_count,
                    exempt_pages=exempt,
                    work_dir=work_dir,
                )
            results.append(result)
            if did_change:
                changed.append(key)

        _notify(stage_callback, "verifying")
        # R6 要求在**最终文件**（含线性化之后）再验一次元数据
        final_report = verify_pdf_file(
            current, expected_pages=page_count, require_no_metadata=True
        )
        if not final_report.ok:
            raise PipelineError(
                "输出文件未通过最终验证",
                reason_code="output_verification_failed",
                problems=final_report.problems,
                results=results,
            )

        _notify(stage_callback, "publishing")
        _commit(current, dst)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    warnings: list[str] = []
    for item in results:
        for warning in item["warnings"]:
            warnings.append(f"{item['title']}：{warning}")

    has_failure = any(item["status"] == "failed" for item in results)
    has_review = any(item["status"] == "review_required" for item in results)
    overall = "completed_with_warnings" if (has_failure or has_review) else "completed"

    return PipelineResult(
        output=dst,
        page_count=page_count,
        results=results,
        overall_status=overall,
        warnings=warnings,
        changed_operations=tuple(changed),
        source_sha256=source_report.sha256,
        output_sha256=sha256_file(dst),
        size=dst.stat().st_size,
    )
