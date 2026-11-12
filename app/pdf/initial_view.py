"""R3 初始布局 / R4 初始缩放 / R5 书签面板。

三项都只碰文档级的一个键，容易写「假成功」：
- R3 是**删除** `/PageLayout`，不是强行设成某个布局；
- R4 只删「打开时跳转」的打开动作，`/S /JavaScript` 之类的行为绝不能误删；
- R5 以 `/PageMode /UseOutlines` 为验收依据，没有大纲也要照常设置并提示面板可能为空。

报告口径：每一项的 `before` / `after` 都同时给出**原始 PDF 键值**和
**Acrobat 对话框里的文字**（`*_label`）。只给裸键的话，用户在 Acrobat 里
看到的是"默认""书签面板和页面"，对不上号就会以为工具没动过文件——
第一版就是这么被误判成"初始视图没改"的。
"""

from __future__ import annotations

from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from app.pdf import view_labels
from app.pdf.context import OpContext
from app.pdf.destinations import PageResolver, collect_named_dests, resolve_dest
from app.pdf.outcome import OpOutcome, new_counts

__all__ = [
    "process_initial_layout",
    "process_initial_zoom",
    "process_bookmark_panel",
    "open_action_kind",
    "resolve_open_action",
    "zoom_view",
]

_VIEW_ACTIONS = frozenset({"/GoTo"})

_RESOLVABLE_KINDS = frozenset({"dest_array", "named_dest", "goto"})


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _number(value: Any) -> float | None:
    """把 PDF 数值转成 Python float；转不了返回 None（报告里保持 JSON 安全）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# R3 初始布局
# --------------------------------------------------------------------------- #


def process_initial_layout(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    counts = new_counts(checked=1)
    current = _text(pdf.Root.get("/PageLayout"))
    before = {
        "page_layout": current,
        "page_layout_label": view_labels.layout_label(current),
    }

    if current is None:
        counts["compliant"] = 1
        return OpOutcome(
            before=before,
            after={
                "page_layout": None,
                "page_layout_label": view_labels.TARGET_LAYOUT_LABEL,
            },
            counts=counts,
            reason_code="no_page_layout",
            message=(
                "已符合目标值"
                f"（页面布局={view_labels.TARGET_LAYOUT_LABEL}）"
            ),
        )

    # 间接对象、非标准取值一律删除：`del` 只摘掉 Root 上的这个键，
    # 不碰被别的对象共享的值本身。
    del pdf.Root["/PageLayout"]
    counts["modified"] = 1
    after_raw = _text(pdf.Root.get("/PageLayout"))
    return OpOutcome(
        before=before,
        after={
            "page_layout": after_raw,
            "page_layout_label": view_labels.layout_label(after_raw),
        },
        counts=counts,
        reason_code="page_layout_removed",
        message=(
            f"已删除初始布局设定（原为 {before['page_layout_label']}），"
            f"现在显示为「{view_labels.TARGET_LAYOUT_LABEL}」"
        ),
    )


# --------------------------------------------------------------------------- #
# R4 初始缩放
# --------------------------------------------------------------------------- #


def process_initial_zoom(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    counts = new_counts(checked=1)
    action = pdf.Root.get("/OpenAction")
    kind = open_action_kind(action)
    before = zoom_view(pdf, action, kind)

    if action is None:
        counts["compliant"] = 1
        return OpOutcome(
            before=before,
            after=zoom_view(pdf, None, None),
            counts=counts,
            reason_code="no_open_action",
            message=(
                "已符合目标值"
                f"（放大率={view_labels.TARGET_ZOOM_LABEL}）"
            ),
        )

    if kind in _RESOLVABLE_KINDS:
        # 只摘掉 Root 上的键：命名目标本身可能同时被书签/链接引用，
        # 动它等于动了别人的跳转。
        del pdf.Root["/OpenAction"]
        counts["modified"] = 1
        remaining = pdf.Root.get("/OpenAction")
        return OpOutcome(
            before=before,
            after=zoom_view(pdf, remaining, open_action_kind(remaining)),
            counts=counts,
            reason_code="open_action_removed",
            message=(
                f"已移除打开时的视图动作（{kind}），"
                f"放大率现在为「{view_labels.TARGET_ZOOM_LABEL}」"
            ),
        )

    counts["not_applicable"] = 1
    return OpOutcome(
        before=before,
        after=dict(before),
        counts=counts,
        reason_code="open_action_not_view",
        message=f"打开动作不是视图跳转（{kind}），保留原样",
        warnings=[f"/OpenAction 类型为 {kind}，属于其他行为，未删除"],
    )


def zoom_view(pdf: pikepdf.Pdf, action: Any, kind: str | None) -> dict[str, Any]:
    """把打开动作翻译成「Acrobat 放大率 + 原始键值」的展示视图。"""
    resolved = resolve_open_action(pdf, action, kind)
    fit_type = resolved.fit_type if resolved is not None else None
    zoom = _number(resolved.zoom) if resolved is not None else None
    return {
        "open_action": kind,
        "fit_type": fit_type,
        "open_page": resolved.page_number if resolved is not None else None,
        "zoom": zoom,
        "zoom_label": view_labels.zoom_label(fit_type, zoom),
    }


def resolve_open_action(pdf: pikepdf.Pdf, action: Any, kind: str | None) -> Any:
    """解析打开动作指向的目的地；不是视图跳转或解析失败都返回 None。"""
    if action is None or kind not in _RESOLVABLE_KINDS:
        return None
    raw = action
    if isinstance(action, Dictionary):
        raw = action.get("/D")
    if raw is None:
        return None
    try:
        return resolve_dest(raw, collect_named_dests(pdf), PageResolver(pdf))
    except Exception:  # noqa: BLE001 - 坏文档不能拖垮这一项
        return None


def open_action_kind(action: Any) -> str | None:
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
    before = {
        "page_mode": current,
        "page_mode_label": view_labels.mode_label(current),
        "has_outline": has_outline,
    }
    after = {
        "page_mode": "/UseOutlines",
        "page_mode_label": view_labels.TARGET_MODE_LABEL,
        "has_outline": has_outline,
    }

    if current == "/UseOutlines":
        counts["compliant"] = 1
        outcome = OpOutcome(
            before=before,
            after=after,
            counts=counts,
            reason_code="already_use_outlines",
            message=(
                "已符合目标值"
                f"（导览标签={view_labels.TARGET_MODE_LABEL}）"
            ),
        )
    else:
        pdf.Root["/PageMode"] = Name("/UseOutlines")
        counts["modified"] = 1
        outcome = OpOutcome(
            before=before,
            after=after,
            counts=counts,
            reason_code="page_mode_set",
            message=(
                f"已设置打开时显示书签面板（原为 {before['page_mode_label']}），"
                f"导览标签现在为「{view_labels.TARGET_MODE_LABEL}」"
            ),
        )

    if not has_outline:
        outcome.warn("该文档没有书签，面板打开后可能是空的")
        outcome.next_action = "如需面板有内容，请先在源文件中添加书签后重新处理"
    return outcome
