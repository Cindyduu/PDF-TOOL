"""R38 初始视图校验（只读）。

查「文档属性 → 初始视图」三项是否等于规定值：

| 子项 | 规定值 | 判据 |
| --- | --- | --- |
| 导览标签 | 书签面板和页面 | `/Root /PageMode == /UseOutlines` |
| 页面布局 | 默认 | `/Root /PageLayout` **不存在** |
| 放大率 | 默认 | `/OpenAction` 不是视图跳转，或解析后为 `/XYZ` + `null` |

**三项都是「缺失即合规」**，这与直觉相反但符合 PDF 规范：`/PageLayout` 缺失时
Acrobat 显示「默认」，`/OpenAction` 缺失时放大率也是「默认」。反过来，写成
`/PageLayout /SinglePage` 才是「单页」——多写一个键反而让文件不合规。

**判定口径与处理侧共用同一套解析**：`app/pdf/initial_view.py` 的
`open_action_kind()` / `zoom_view()` 就是处理项 ③ 用来改写和报告的函数。
校验另写一套「打开动作解析」，迟早在「`/S /JavaScript` 算不算视图跳转」
这种边界上分叉，然后处理说改好了、校验说没改好。

**为什么判 `fail` 不判 `warning`**（D-15）：这三项是 eCTD 提交的规定值，
处理项 ③ 一定改得了。判 `warning` 会让「根本没设置」被「预警」这个词稀释掉，
用户扫一眼汇总不会去处理。
"""

from __future__ import annotations

from typing import Any

from app.pdf import view_labels
from app.pdf.checks import CheckContext, CheckResult, Finding, make_result
from app.pdf.initial_view import open_action_kind, zoom_view

__all__ = ["run", "XYZ"]

XYZ = "/XYZ"


def _text(value: Any) -> str | None:
    """PDF 对象转文本；坏对象降级为 None（报告里保持 JSON 安全）。"""
    if value is None:
        return None
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - 坏对象转字符串也可能炸
        return None


def run(ctx: CheckContext) -> CheckResult:
    root = ctx.pdf.Root

    mode_raw = _text(root.get("/PageMode"))
    layout_raw = root.get("/PageLayout")
    action = root.get("/OpenAction")
    kind = open_action_kind(action)
    view = zoom_view(ctx.pdf, action, kind)

    mode_ok = mode_raw == "/UseOutlines"
    layout_ok = layout_raw is None
    # `zoom_view` 对「无动作」「非视图动作」「解析失败」都给 fit_type=None，
    # 三种都属于 Acrobat 的「默认」，所以这一个判据覆盖了全部分支。
    fit_type = view.get("fit_type")
    zoom_ok = fit_type is None or (fit_type == XYZ and view.get("zoom") is None)

    mode_label = view_labels.mode_label(mode_raw)
    layout_label = view_labels.layout_label(layout_raw)
    zoom_text = view.get("zoom_label") or view_labels.DEFAULT_ZOOM_LABEL

    findings = [
        Finding(
            level="pass" if mode_ok else "fail",
            message=f"导览标签：{mode_label}",
            detail=None
            if mode_ok
            else f"应为「{view_labels.TARGET_MODE_LABEL}」"
            f"（/PageMode 缺失等价于「{view_labels.DEFAULT_MODE_LABEL}」）",
        ),
        Finding(
            level="pass" if layout_ok else "fail",
            message=f"页面布局：{layout_label}",
            detail=None if layout_ok else f"应为「{view_labels.TARGET_LAYOUT_LABEL}」",
        ),
        Finding(
            level="pass" if zoom_ok else "fail",
            message=f"放大率：{zoom_text}",
            detail=None if zoom_ok else f"应为「{view_labels.TARGET_ZOOM_LABEL}」",
        ),
    ]

    bad = [
        name
        for name, ok in (
            ("导览标签", mode_ok),
            ("页面布局", layout_ok),
            ("放大率", zoom_ok),
        )
        if not ok
    ]

    if not bad:
        return make_result(
            "initial_view",
            "pass",
            "三项均符合规定值",
            findings,
        )

    return make_result(
        "initial_view",
        "fail",
        f"{len(bad)} 项不符合规定值（{'、'.join(bad)}）",
        findings,
    )
