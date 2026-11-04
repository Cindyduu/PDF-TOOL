"""R3 初始布局 / R4 初始缩放 / R5 书签面板。

三项都只碰文档级的一个键，容易写「假成功」：
- R3 是**删除** `/PageLayout`，不是强行设成某个布局；
- R4 只删「打开时跳转」的打开动作，`/S /JavaScript` 之类的行为绝不能误删；
- R5 以 `/PageMode /UseOutlines` 为验收依据，没有大纲也要照常设置并提示面板可能为空。
"""

from __future__ import annotations

from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from app.pdf.context import OpContext
from app.pdf.outcome import OpOutcome, new_counts

__all__ = ["process_initial_layout", "process_initial_zoom", "process_bookmark_panel"]

_VIEW_ACTIONS = frozenset({"/GoTo"})


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------- #
# R3 初始布局
# --------------------------------------------------------------------------- #


def process_initial_layout(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    counts = new_counts(checked=1)
    current = _text(pdf.Root.get("/PageLayout"))
    before = {"page_layout": current}
    if current is None:
        counts["compliant"] = 1
        return OpOutcome(
            before=before,
            after={"page_layout": None},
            counts=counts,
            reason_code="no_page_layout",
            message="文档没有初始布局设定，无需修改",
        )

    del pdf.Root["/PageLayout"]
    counts["modified"] = 1
    return OpOutcome(
        before=before,
        after={"page_layout": _text(pdf.Root.get("/PageLayout"))},
        counts=counts,
        reason_code="page_layout_removed",
        message=f"已删除初始布局设定（原为 {current}）",
    )


# --------------------------------------------------------------------------- #
# R4 初始缩放
# --------------------------------------------------------------------------- #


def process_initial_zoom(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    counts = new_counts(checked=1)
    action = pdf.Root.get("/OpenAction")
    kind = _open_action_kind(action)
    before = {"open_action": kind}

    if action is None:
        counts["compliant"] = 1
        return OpOutcome(
            before=before,
            after={"open_action": None},
            counts=counts,
            reason_code="no_open_action",
            message="文档没有打开动作，无需修改",
        )

    if kind in ("dest_array", "named_dest", "goto"):
        del pdf.Root["/OpenAction"]
        counts["modified"] = 1
        return OpOutcome(
            before=before,
            after={"open_action": _open_action_kind(pdf.Root.get("/OpenAction"))},
            counts=counts,
            reason_code="open_action_removed",
            message=f"已移除打开时的视图动作（{kind}）",
        )

    counts["not_applicable"] = 1
    return OpOutcome(
        before=before,
        after={"open_action": kind},
        counts=counts,
        reason_code="open_action_not_view",
        message=f"打开动作不是视图跳转（{kind}），保留原样",
        warnings=[f"/OpenAction 类型为 {kind}，属于其他行为，未删除"],
    )


def _open_action_kind(action: Any) -> str | None:
    if action is None:
        return None
    if isinstance(action, Array):
        return "dest_array"
    if isinstance(action, (String, Name)):
        return "named_dest"
    if isinstance(action, Dictionary):
        subtype = action.get("/S")
        name = str(subtype) if subtype is not None else "unknown"
        return "goto" if name in _VIEW_ACTIONS else name.lstrip("/").lower()
    return "unknown"


# --------------------------------------------------------------------------- #
# R5 书签面板
# --------------------------------------------------------------------------- #


def process_bookmark_panel(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    counts = new_counts(checked=1)
    current = _text(pdf.Root.get("/PageMode"))
    has_outline = isinstance(pdf.Root.get("/Outlines"), Dictionary)
    before = {"page_mode": current, "has_outline": has_outline}
    after = {"page_mode": "/UseOutlines", "has_outline": has_outline}

    if current == "/UseOutlines":
        counts["compliant"] = 1
        outcome = OpOutcome(
            before=before,
            after=after,
            counts=counts,
            reason_code="already_use_outlines",
            message="打开时已显示书签面板",
        )
    else:
        pdf.Root["/PageMode"] = Name("/UseOutlines")
        counts["modified"] = 1
        outcome = OpOutcome(
            before=before,
            after=after,
            counts=counts,
            reason_code="page_mode_set",
            message="已设置打开时显示书签面板",
        )

    if not has_outline:
        outcome.warn("该文档没有书签，面板打开后可能是空的")
        outcome.next_action = "如需面板有内容，请先在源文件中添加书签后重新处理"
    return outcome
