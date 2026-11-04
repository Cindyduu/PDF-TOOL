"""R2 注释展平。

做法：把注释的**正常外观**（`/AP /N`）画进页面内容流，然后从 `/Annots` 里摘掉它。
不调用任何「整文档 bake」——那会连豁免页和 `Link` 一起处理掉，属于越权。

几条硬规则：

- `Link` 一律保留（它本身就是超文本链接）。
- 豁免页整页跳过，注释对象一个字节都不动。
- 无外观、隐藏、外观状态不可靠的注释：保留原对象并计入失败，如实报告。
- `Widget` 展平后必须同步从 `/AcroForm /Fields` 摘除，摘不掉就保留原对象。
"""

from __future__ import annotations

from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Stream

from app.pdf.context import OpContext
from app.pdf.outcome import OpOutcome, new_counts

__all__ = ["process", "count_annotations"]

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_REASON_TEXT = {
    "annotation_no_appearance": "没有正常外观（/AP /N）",
    "annotation_ambiguous_state": "外观有多个状态且无法可靠选择",
    "annotation_unsupported_appearance": "外观对象类型不受支持",
    "annotation_bad_rect": "注释矩形缺失或为零面积",
    "annotation_hidden": "注释被标记为隐藏或不可见",
    "widget_removal_failed": "表单域无法从字段树安全摘除",
    "annotation_bad_bbox": "外观流的 BBox 缺失或为零面积",
    "annotation_transform_failed": "外观流变换矩阵不可逆",
}


# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #


def _numbers(value: Any, count: int) -> tuple[float, ...] | None:
    if not isinstance(value, Array) or len(value) != count:
        return None
    try:
        return tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None


def _compose(outer: tuple[float, ...], inner: tuple[float, ...]) -> tuple[float, ...]:
    """先 inner 后 outer 的仿射复合。"""
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _invert(matrix: tuple[float, ...]) -> tuple[float, ...] | None:
    a, b, c, d, e, f = matrix
    det = a * d - b * c
    if abs(det) < 1e-12:
        return None
    return (
        d / det,
        -b / det,
        -c / det,
        a / det,
        (c * f - d * e) / det,
        (b * e - a * f) / det,
    )


def _rect(annot: Any) -> tuple[float, float, float, float] | None:
    values = _numbers(annot.get("/Rect"), 4)
    if values is None:
        return None
    x0, y0, x1, y1 = values
    left, right = min(x0, x1), max(x0, x1)
    bottom, top = min(y0, y1), max(y0, y1)
    if right - left <= 0 or top - bottom <= 0:
        return None
    return (left, bottom, right, top)


def _transform_for(stream: Stream, rect: tuple[float, float, float, float]) -> tuple[float, ...] | None:
    """求把外观流 BBox 映射到注释 Rect 的变换（外层 CTM）。

    `Do` 会自己再乘一次外观流的 `/Matrix`，所以这里先把它除掉，
    最终效果正好是「BBox 贴到 Rect」。
    """
    bbox = _numbers(stream.get("/BBox"), 4)
    if bbox is None:
        return None
    bx0, by0, bx1, by1 = bbox
    bx0, bx1 = min(bx0, bx1), max(bx0, bx1)
    by0, by1 = min(by0, by1), max(by0, by1)
    if bx1 - bx0 <= 0 or by1 - by0 <= 0:
        return None

    rx0, ry0, rx1, ry1 = rect
    sx = (rx1 - rx0) / (bx1 - bx0)
    sy = (ry1 - ry0) / (by1 - by0)
    to_rect = (sx, 0.0, 0.0, sy, rx0 - bx0 * sx, ry0 - by0 * sy)

    matrix = _numbers(stream.get("/Matrix"), 6) or _IDENTITY
    inverse = _invert(matrix)
    if inverse is None:
        return None
    return _compose(to_rect, inverse)


def _ops(name: str, transform: tuple[float, ...], rect: tuple[float, float, float, float]) -> bytes:
    a, b, c, d, e, f = transform
    rx0, ry0, rx1, ry1 = rect
    return (
        "q\n"
        f"{rx0:.4f} {ry0:.4f} {rx1 - rx0:.4f} {ry1 - ry0:.4f} re W n\n"
        f"{a:.6f} {b:.6f} {c:.6f} {d:.6f} {e:.4f} {f:.4f} cm\n"
        f"{name} Do\n"
        "Q\n"
    ).encode("latin-1")


# --------------------------------------------------------------------------- #
# 外观与表单
# --------------------------------------------------------------------------- #


def _appearance_stream(annot: Any) -> tuple[Stream | None, str]:
    appearance = annot.get("/AP")
    if not isinstance(appearance, Dictionary):
        return None, "annotation_no_appearance"
    normal = appearance.get("/N")
    if normal is None:
        return None, "annotation_no_appearance"
    if isinstance(normal, Stream):
        return normal, ""
    if isinstance(normal, Dictionary):
        state = annot.get("/AS")
        if state is not None:
            candidate = normal.get(str(state))
            if isinstance(candidate, Stream):
                return candidate, ""
        return None, "annotation_ambiguous_state"
    return None, "annotation_unsupported_appearance"


def _register_xobject(pdf: pikepdf.Pdf, page: pikepdf.Page, stream: Stream) -> str:
    if not stream.is_indirect:
        stream = pdf.make_indirect(stream)
    resources = page.resources
    xobjects = resources.get("/XObject")
    if not isinstance(xobjects, Dictionary):
        resources["/XObject"] = Dictionary()
        xobjects = resources["/XObject"]
    index = 1
    while f"/FlatAnnot{index}" in xobjects:
        index += 1
    name = f"/FlatAnnot{index}"
    xobjects[name] = stream
    return name


def _remove_widget(pdf: pikepdf.Pdf, widget: Any) -> str:
    """把表单域从 `/AcroForm /Fields` 摘除。

    返回 `removed` / `absent` / `untracked`：
    - `absent`：没有 `/AcroForm`，或这个域本来就不在字段树里——没有东西要摘，安全；
    - `untracked`：域是直接对象，无法在字段树里定位，摘除不可靠。
    """
    acroform = pdf.Root.get("/AcroForm")
    if not isinstance(acroform, Dictionary):
        return "absent"
    fields = acroform.get("/Fields")
    if not isinstance(fields, Array):
        return "absent"
    target = widget.objgen
    if target == (0, 0):
        return "untracked"
    if not _remove_from_fields(fields, target):
        return "absent"
    if len(fields) == 0:
        del pdf.Root["/AcroForm"]
    return "removed"


def _remove_from_fields(fields: Array, target: tuple[int, int]) -> bool:
    for index in range(len(fields) - 1, -1, -1):
        field = fields[index]
        if not isinstance(field, Dictionary):
            continue
        if field.objgen == target:
            del fields[index]
            return True
        kids = field.get("/Kids")
        if isinstance(kids, Array) and _remove_from_fields(kids, target):
            if len(kids) == 0:
                del field["/Kids"]
            return True
    return False


def _strip_popup_reference(annots: list[Any], popup: Any) -> None:
    """删掉 Popup 后，把其他注释里指向它的 `/Popup` 一并清掉。"""
    popup_objgen = popup.objgen
    popup_rect = _numbers(popup.get("/Rect"), 4)
    for other in annots:
        reference = other.get("/Popup")
        if reference is None:
            continue
        if popup_objgen != (0, 0) and getattr(reference, "objgen", (0, 0)) == popup_objgen:
            del other["/Popup"]
            continue
        if popup_rect is not None:
            other_rect = _numbers(reference.get("/Rect"), 4)
            if other_rect == popup_rect:
                del other["/Popup"]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def count_annotations(pdf: pikepdf.Pdf) -> int:
    total = 0
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if isinstance(annots, Array):
            total += len(annots)
    return total


def process(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    before_total = count_annotations(pdf)
    pages_with_annots = sum(
        1
        for page in pdf.pages
        if isinstance(page.obj.get("/Annots"), Array) and len(page.obj.Annots) > 0
    )
    before = {"annotations": before_total, "pages_with_annotations": pages_with_annots}

    counts = new_counts()
    warnings: list[str] = []

    for page_number, page in enumerate(pdf.pages, start=1):
        annots = page.obj.get("/Annots")
        if not isinstance(annots, Array) or len(annots) == 0:
            continue
        entries = list(annots)

        if ctx.is_exempt(page_number):
            counts["checked"] += len(entries)
            counts["exempt"] += len(entries)
            continue

        keep: list[Any] = []
        for annot in entries:
            counts["checked"] += 1
            action, reason = _handle(pdf, page, annot, entries)
            if action == "remove":
                counts["modified"] += 1
                continue
            if action == "failed":
                counts["failed"] += 1
                warnings.append(
                    f"第 {page_number} 页有一个 {_subtype(annot)} 注释未展平："
                    f"{_REASON_TEXT.get(reason, reason)}"
                )
                keep.append(annot)
                continue
            if reason == "link_kept":
                counts["not_applicable"] += 1
            else:  # pragma: no cover - 目前只有链接走 keep
                counts["not_applicable"] += 1
            keep.append(annot)

        if len(keep) == len(entries):
            continue
        if keep:
            page.obj["/Annots"] = Array(keep)
        else:
            del page.obj["/Annots"]

    after_total = count_annotations(pdf)
    outcome = OpOutcome(
        before=before,
        after={"annotations": after_total},
        counts=counts,
        warnings=warnings,
    )
    removed = before_total - after_total
    if counts["failed"]:
        outcome.reason_code = "annotation_flatten_partial"
        outcome.message = (
            f"已展平 {removed} 个注释，另有 {counts['failed']} 个无法可靠展平，已保留原样"
        )
        outcome.next_action = "请人工检查提示中列出的注释，必要时在源文件中删除后重新处理"
    elif counts["modified"]:
        outcome.reason_code = "annotation_flattened"
        outcome.message = f"已展平 {removed} 个注释，链接注释保持原样"
    elif counts["exempt"]:
        outcome.reason_code = "all_exempt"
        outcome.message = f"注释都在豁免页上，未做展平"
    elif counts["checked"] == 0:
        outcome.reason_code = "no_annotation"
        outcome.message = "文档没有注释，无需展平"
    else:
        outcome.reason_code = "only_links"
        outcome.message = "只有链接注释，按规则保留"
    return outcome


def _subtype(annot: Any) -> str:
    value = annot.get("/Subtype")
    return str(value).lstrip("/") if value is not None else "未知"


def _handle(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    annot: Any,
    siblings: list[Any],
) -> tuple[str, str]:
    """处理单个注释，返回 `(动作, 原因)`，动作取 `keep` / `remove` / `failed`。"""
    subtype = annot.get("/Subtype")
    subtype_name = str(subtype) if subtype is not None else ""

    if subtype_name == "/Link":
        return "keep", "link_kept"
    if subtype_name == "/Popup":
        _strip_popup_reference(siblings, annot)
        return "remove", "popup_removed"

    flags = annot.get("/F")
    if flags is not None:
        try:
            if int(flags) & 0b11:
                return "failed", "annotation_hidden"
        except (TypeError, ValueError):  # pragma: no cover - 畸形 /F
            pass

    stream, reason = _appearance_stream(annot)
    if stream is None:
        return "failed", reason

    rect = _rect(annot)
    if rect is None:
        return "failed", "annotation_bad_rect"

    transform = _transform_for(stream, rect)
    if transform is None:
        bbox = _numbers(stream.get("/BBox"), 4)
        if bbox is None or bbox[2] - bbox[0] == 0 or bbox[3] - bbox[1] == 0:
            return "failed", "annotation_bad_bbox"
        return "failed", "annotation_transform_failed"

    if subtype_name == "/Widget":
        try:
            status = _remove_widget(pdf, annot)
        except Exception:  # noqa: BLE001
            return "failed", "widget_removal_failed"
        # 摘不掉就保留原对象：留下一个还挂在字段树上的注释，
        # 比「画面上有了、表单里还在」的重复更安全。
        if status == "untracked":
            return "failed", "widget_removal_failed"

    name = _register_xobject(pdf, page, stream)
    page.contents_add(_ops(name, transform, rect), prepend=False)
    return "remove", "annotation_flattened"
