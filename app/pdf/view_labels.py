"""Acrobat「文档属性 → 初始视图」的取值口径。

工具要能让人**不用打开 Acrobat 就逐字核对**这三项，就必须说 Acrobat 的话：
用户在下拉框里看到的是"默认""书签面板和页面"，报告里写 `/PageLayout: None`
是没法对号的。

三条设计约束：

1. **这是唯一翻译层**。后端报告字段和前端卡片都从这里取，前端不再自己维护
   一份中文表——两份表迟早会分叉，分叉的那天用户就又对不上号了。
2. **绝不抛异常**。真实文档里 `/PageLayout` 可能是间接对象、数字、乱码字符串；
   翻译失败一律降级成 `未知(xxx)`，让"这一项的值很奇怪"变成可见信息，
   而不是让整个处理项失败。
3. **缺失值有确定语义**，且两者不同：`/PageLayout` 缺失 = Acrobat 的"默认"，
   `/PageMode` 缺失按规范等价于 `/UseNone` = "仅页面"。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "TARGET_LAYOUT_LABEL",
    "TARGET_MODE_LABEL",
    "TARGET_ZOOM_LABEL",
    "layout_label",
    "meta_payload",
    "mode_label",
    "zoom_label",
]

DEFAULT_LAYOUT_LABEL = "默认"
DEFAULT_MODE_LABEL = "仅页面"
DEFAULT_ZOOM_LABEL = "默认"
UNKNOWN_TEMPLATE = "未知({value})"

# 产品目标值：处理后产物在 Acrobat 里应当显示成这三个。
TARGET_LAYOUT_LABEL = DEFAULT_LAYOUT_LABEL
TARGET_MODE_LABEL = "书签面板和页面"
TARGET_ZOOM_LABEL = DEFAULT_ZOOM_LABEL

# ISO 32000 `/PageLayout` → Acrobat「页面布局」下拉框。
# `TwoColumn*` 是连续滚动（双联连续），`TwoPage*` 是一次两页（双联），
# 二者的区别只在滚动方式，不在左右——左右由 Left/Right 后缀决定。
PAGE_LAYOUT_LABELS: dict[str, str] = {
    "/SinglePage": "单页",
    "/OneColumn": "单页连续",
    "/TwoColumnLeft": "双联连续（奇数页在左）",
    "/TwoColumnRight": "双联连续（奇数页在右）",
    "/TwoPageLeft": "双联（奇数页在左）",
    "/TwoPageRight": "双联（奇数页在右）",
}

# ISO 32000 `/PageMode` → Acrobat「导览标签」下拉框。
PAGE_MODE_LABELS: dict[str, str] = {
    "/UseNone": "仅页面",
    "/UseOutlines": "书签面板和页面",
    "/UseThumbs": "缩略图面板和页面",
    "/UseOC": "图层面板和页面",
    "/UseAttachments": "附件面板和页面",
    "/FullScreen": "全屏",
}

# 目的地 fit 类型 → Acrobat「放大率」下拉框。
ZOOM_LABELS: dict[str, str] = {
    "/Fit": "适合页面",
    "/FitH": "适合宽度",
    "/FitV": "适合高度",
    "/FitB": "适合可见区域",
    "/FitBH": "适合可见宽度",
    "/FitBV": "适合可见高度",
    "/FitR": "适合矩形区域",
}

_XYZ = "/XYZ"
_ACTUAL_SIZE = 1.0


def _as_text(value: Any) -> str | None:
    """把 PDF 对象转成可比较的文本；转不了返回 None（交给调用方降级）。"""
    if value is None:
        return None
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - 坏对象转字符串也可能炸，一律降级
        return None


def _unknown(value: Any) -> str:
    text = _as_text(value)
    return UNKNOWN_TEMPLATE.format(value="?" if text is None else text)


def layout_label(value: Any) -> str:
    """`/Root /PageLayout` → Acrobat「页面布局」文字。"""
    text = _as_text(value)
    if text is None:
        return DEFAULT_LAYOUT_LABEL
    return PAGE_LAYOUT_LABELS.get(text, _unknown(value))


def mode_label(value: Any) -> str:
    """`/Root /PageMode` → Acrobat「导览标签」文字。

    键缺失时按规范等价于 `/UseNone`，所以显示"仅页面"而不是"默认"——
    Acrobat 的导览标签下拉框里没有"默认"这一项。
    """
    text = _as_text(value)
    if text is None:
        return DEFAULT_MODE_LABEL
    return PAGE_MODE_LABELS.get(text, _unknown(value))


def zoom_label(fit_type: Any, zoom: Any) -> str:
    """目的地 `(fit 类型, 缩放因子)` → Acrobat「放大率」文字。

    - 没有打开动作、或 `/XYZ` 的缩放位是 `null`（承前缩放）→ "默认"；
    - `/XYZ` 缩放位 1.0 → "实际大小"，其余按百分比显示；
    - 其余 fit 类型本身不含缩放因子，按 fit 语义命名。
    """
    text = _as_text(fit_type)
    if text is None:
        return DEFAULT_ZOOM_LABEL
    if text != _XYZ:
        return ZOOM_LABELS.get(text, _unknown(fit_type))

    if zoom is None:
        return DEFAULT_ZOOM_LABEL
    try:
        factor = float(zoom)
    except (TypeError, ValueError):
        return _unknown(zoom)
    if factor == _ACTUAL_SIZE:
        return "实际大小"
    return f"{round(factor * 100)}%"


def meta_payload() -> dict[str, Any]:
    """给 `/api/meta` 的 JSON 安全版本（键值全是字符串）。

    空字符串键代表"文档里没有这个键"，对应 Acrobat 的默认显示。
    """
    return {
        "page_layout": {"": DEFAULT_LAYOUT_LABEL, **PAGE_LAYOUT_LABELS},
        "page_mode": dict(PAGE_MODE_LABELS),
        "zoom": {"": DEFAULT_ZOOM_LABEL, **ZOOM_LABELS},
        "targets": {
            "page_layout": TARGET_LAYOUT_LABEL,
            "page_mode": TARGET_MODE_LABEL,
            "zoom": TARGET_ZOOM_LABEL,
        },
    }
