"""R8 Fast Web View（线性化）。

线性化在流水线最后一步做，做完必须**再检查一遍**：
首对象含 `/Linearized`、`/L` 与文件长度一致、存在 hint 结构。
检查不过就退回线性化之前的候选，只把这一项记为失败，其他成果全部保留。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pikepdf

from app.pdf.context import OpContext
from app.pdf.outcome import OpOutcome, new_counts

__all__ = ["process", "inspect_linearization", "check", "finalize"]

_LINEARIZED = b"/Linearized"
_LENGTH = re.compile(rb"/L\s+(\d+)")
_HINT = re.compile(rb"/H\s*\[")


def inspect_linearization(path: Path | str) -> dict[str, Any]:
    """读取线性化参数字典并交叉验证。"""
    target = Path(path)
    size = target.stat().st_size
    with open(target, "rb") as handle:
        head = handle.read(4096)

    index = head.find(_LINEARIZED)
    declared: int | None = None
    has_hint = False
    if index >= 0:
        window = head[index : index + 512]
        match = _LENGTH.search(window)
        if match:
            declared = int(match.group(1))
        has_hint = _HINT.search(window) is not None

    try:
        with pikepdf.open(target) as pdf:
            linearized = bool(pdf.is_linearized)
    except Exception:  # noqa: BLE001 - 打不开就是没通过
        linearized = False

    return {
        "linearized": linearized,
        "declared_length": declared,
        "length_matches": declared == size,
        "has_hint": has_hint,
        "size": size,
    }


def check(path: Path | str) -> tuple[bool, list[str]]:
    """线性化验收：三项同时满足才算通过。"""
    info = inspect_linearization(path)
    problems: list[str] = []
    if not info["linearized"]:
        problems.append("文件未被识别为线性化")
    if info["declared_length"] is None:
        problems.append("找不到线性化参数 /L")
    elif not info["length_matches"]:
        problems.append(
            f"线性化参数 /L={info['declared_length']} 与实际长度 {info['size']} 不一致"
        )
    if not info["has_hint"]:
        problems.append("缺少 hint 结构 /H")
    return (not problems, problems)


def process(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    counts = new_counts(checked=1)
    already = bool(pdf.is_linearized)
    if already:
        counts["compliant"] = 1
        return OpOutcome(
            before={"linearized": True},
            after={"linearized": True},
            counts=counts,
            reason_code="already_linearized",
            message="文件已经是 Fast Web View，无需重做",
        )
    counts["modified"] = 1
    return OpOutcome(
        before={"linearized": False},
        after=None,
        counts=counts,
        reason_code="linearize_pending",
        message="正在执行 Fast Web View",
        changed=True,
    )


def finalize(outcome: OpOutcome, path: Path | str) -> OpOutcome:
    """保存后回填实测结果。失败时把状态改成 failed 并给出下一步。"""
    info = inspect_linearization(path)
    outcome.after = {"linearized": info["linearized"], "size": info["size"]}
    ok, problems = check(path)
    if ok:
        outcome.status = "adjusted"
        outcome.reason_code = "linearized"
        outcome.message = "已完成 Fast Web View（线性化）"
        return outcome

    outcome.status = "failed"
    outcome.counts["modified"] = 0
    outcome.counts["failed"] = 1
    outcome.changed = False
    outcome.reason_code = "linearize_failed"
    outcome.message = "线性化结果未通过检查，已退回线性化之前的文件"
    outcome.warnings.extend(problems)
    outcome.next_action = "其他处理结果不受影响，可继续使用输出文件；如需 Fast Web View，请用其他工具另存后重试"
    return outcome
