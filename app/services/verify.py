"""前置/后置校验的执行（R13、§8.3）。

这个模块只做一件事：**打开文件、跑校验项、拼出 §8.3 结构**。
状态迁移、入队、落盘都在 `JobService`（它才是 job.json 的所有者），
这里拿到的 `job` 是只读快照。

三条硬约束（都来自 PRD，不是风格偏好）：

1. **零写入**。不调用任何 `save()`，不写临时 PDF。校验前后原件 sha256 必须
   一致（AC-R13-N4），所以这里连"另存一份再看"的余地都没有。跑完还会拿
   实测 sha256 和上传时记录的值对一次——对不上说明我们改了用户的文件，
   这是比"某项检查没过"严重得多的事，必须让任务失败而不是照常出报告。
2. **后置校验共用同一引擎**，只是 `phase` 不同、目标文件不同（§10.3 第 5 条）。
   所以 `build_verify_result` 不认"前置还是后置"，只认"跑哪个文件"。
3. **不缓存**。前置与后置各跑一次，互不引用（§10.3 第 6 条）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pikepdf
import pymupdf

from app.core.storage import utcnow_iso
from app.pdf.checks import PHASE_POST as _CHECKS_PHASE_POST
from app.pdf.checks import PHASE_PRECHECK as _CHECKS_PHASE_PRECHECK
from app.pdf.checks import CheckContext, run_check
from app.pdf.verify import sha256_file
from app.schemas import VERIFY_KEYS, summarize_verify_items

__all__ = [
    "PHASE_PRECHECK",
    "PHASE_POST",
    "VERIFY_PHASES",
    "VerifyError",
    "build_failed_verify_result",
    "build_verify_result",
]

PHASE_PRECHECK = "precheck"
PHASE_POST = "post"
VERIFY_PHASES: tuple[str, ...] = (PHASE_PRECHECK, PHASE_POST)

# 阶段名在两处各写一份（这里给服务层用，`app.pdf.checks` 给校验项用，
# 反过来导入会成环）。同值断言让"某天只改了一边"在 import 时就炸掉。
assert (_CHECKS_PHASE_PRECHECK, _CHECKS_PHASE_POST) == (PHASE_PRECHECK, PHASE_POST)


class VerifyError(RuntimeError):
    """校验无法给出可信结论（文件打不开、原件被改动等）。

    单**项**失败不走这里：那由 `run_check` 内部转成 `fail` 结果。
    只有"整份结论不成立"才抛。
    """


ProgressCallback = Callable[[str, Any, int, int], None]


def build_verify_result(
    job: dict[str, Any],
    *,
    path: Path,
    filename: str,
    phase: str = PHASE_PRECHECK,
    on_item: ProgressCallback | None = None,
) -> dict[str, Any]:
    """跑 `config.verify_items` 里的每一项，返回 §8.3 结构。

    `on_item(key, result, index, total)` 每完成一项调一次，worker 用它推
    `verify_progress` 事件——界面上的"正在检查第 N 项（共 M 项）"就来自这里，
    不需要额外维护进度数值。`index` 从 1 开始，与界面的编号口径一致。
    """
    if phase not in VERIFY_PHASES:
        raise ValueError(f"未知校验阶段：{phase}")

    keys = _selected_keys(job)
    total = len(keys)
    target = Path(path)
    if not target.is_file():
        raise VerifyError("待校验文件已不存在")

    size_bytes = target.stat().st_size
    if size_bytes == 0:
        raise VerifyError("待校验文件是空的")

    try:
        doc = pymupdf.open(str(target))
    except Exception as exc:  # noqa: BLE001
        raise VerifyError(f"无法打开 PDF：{type(exc).__name__}: {exc}") from exc
    try:
        try:
            pdf = pikepdf.open(target)
        except Exception as exc:  # noqa: BLE001
            raise VerifyError(f"无法解析 PDF 结构：{type(exc).__name__}: {exc}") from exc
        try:
            ctx = CheckContext(
                path=target,
                filename=filename,
                size_bytes=size_bytes,
                page_count=doc.page_count,
                doc=doc,
                pdf=pdf,
                phase=phase,
            )
            items = []
            for index, key in enumerate(keys, start=1):
                result = run_check(key, ctx)
                items.append(result.as_dict())
                if on_item is not None:
                    on_item(key, result, index, total)
        finally:
            pdf.close()
    finally:
        doc.close()

    _assert_source_untouched(job, target, phase=phase)

    return {
        "job_id": job["id"],
        "phase": phase,
        "generated_at": utcnow_iso(),
        "file": {
            "name": filename,
            "size_bytes": size_bytes,
            "page_count": ctx.page_count,
        },
        "items": items,
        "summary": summarize_verify_items(items),
    }


def build_failed_verify_result(
    job: dict[str, Any],
    *,
    filename: str,
    reason: str,
    phase: str = PHASE_POST,
    path: Path | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    """整块记 `fail` 的校验结果（AC-R27-E1）。

    用在哪：后置校验连文件都打不开（产物损坏、被外部占用、磁盘读错）。这时
    我们**不知道**十二项各自的结论，所以不能像单项异常那样只标一项 —— 只标一项
    等于默许另外八项"通过"，那是凭空造出来的结论。

    为什么仍然返回一份完整结构而不是 `None`：`None` 在页面上的含义是「没执行」，
    而这里明明执行了、只是拿不到结论。两种情形必须能区分，否则用户会以为
    后置校验被跳过了。
    """
    if phase not in VERIFY_PHASES:
        raise ValueError(f"未知校验阶段：{phase}")

    size_bytes = 0
    if path is not None:
        try:
            size_bytes = Path(path).stat().st_size
        except OSError:
            size_bytes = 0

    detail = next_action or "请重新处理该文件；若反复失败，请检查磁盘与文件是否被其他程序占用"
    items = [
        {
            "key": key,
            "status": "fail",
            "summary": reason,
            "findings": [
                {"level": "fail", "message": reason, "detail": detail}
            ],
            "duration_ms": 0,
        }
        for key in _selected_keys(job)
    ]
    return {
        "job_id": job["id"],
        "phase": phase,
        "generated_at": utcnow_iso(),
        "file": {"name": filename, "size_bytes": size_bytes, "page_count": 0},
        "items": items,
        "summary": summarize_verify_items(items),
    }


def _selected_keys(job: dict[str, Any]) -> list[str]:
    """取本次要跑的校验项，按 `VERIFY_KEYS` 的声明顺序。

    顺序不跟用户勾选顺序走：界面上的编号（1–9）就是声明顺序，
    报告里的行序必须和它一致，否则"第 4 项"在两处指的不是同一件事。
    """
    raw = job.get("config", {}).get("verify_items")
    picked = {key for key in (raw or ()) if key in VERIFY_KEYS}
    return [key for key in VERIFY_KEYS if key in picked]


def _assert_source_untouched(
    job: dict[str, Any], path: Path, *, phase: str
) -> None:
    """前置校验跑完必须证明原件没被动过（AC-R13-N4）。

    只对前置校验做：后置校验的目标是产物，它的 sha256 本来就该和处理前不同。
    这里多读一遍文件的代价是必要的——"我们说自己只读"和"文件真的没变"
    是两件事，报告只能建立在后者上。
    """
    if phase != PHASE_PRECHECK:
        return
    recorded = (job.get("source") or {}).get("sha256")
    if not recorded:
        return
    actual = sha256_file(path)
    if actual != recorded:
        raise VerifyError(
            "校验过程改动了原始文件："
            f"上传时 {recorded[:12]}…，校验后 {actual[:12]}…"
        )
