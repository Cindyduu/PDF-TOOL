"""R33 页码校验（v4.1 从 R21 拆出）。

用户要求：「每个文件应有单独页码，从第一页开始」。所以这一项只回答一个问题：
**这份文件的页码是不是从 1 开始、逐页连续**。

两种证据，按可信度依次尝试：

1. **`/PageLabels`** —— PDF 的逻辑页码。首段起始值必须是 1，且必须从第 1 页
   开始（前面几页没有逻辑页码，读者看到的就是"没有页码"）。
2. **页脚可见数字** —— 没有 `/PageLabels` 时，读每页底部一小条区域里的文字，
   找出纯数字。全部页都能读到一个数、且正好构成 1、2、…、N，才判 pass。

`view_labels.py` 只管展示用文案（`layout_label` / `mode_label` 等），
没有 `/PageLabels` 的解析能力，所以这里自己读结构：

```
/Root /PageLabels << /Nums [0 << /S /D /St 1 >> 3 << /S /r >>] >>
```

`/Nums` 是「起始页索引（0 基）+ 标签字典」的扁平数组，`/Kids` 可以再嵌一层。
首段的起始值 = 它的 `/St`（缺省为 1）。

## 为什么 v4.1 要加页脚兜底

v4.0 只有 `/PageLabels` 一条路：没设逻辑页码就一律 warning「请人工复核」。
但真实 eCTD 文件的页码绝大多数是排版软件直接印在页脚上的，根本不写
`/PageLabels`——按老逻辑，绝大多数**合规**文件都会被判「无法核对」，
这条校验等于白做，报告里还多一条噪声。

## 为什么页脚判定要这么保守

只认「整行就是一个数字」（允许两侧破折号、空格），并且要求：

- 全部页都能读到一个数；
- 相邻页的读数正好差 1（构成公差为 1 的等差数列）。

两条同时满足才下结论。任何一条不满足就退到 warning 让人工看一眼——
**宁可漏判，也不能把文档编号当成页码给出一个假的 fail**。假阳性一旦出现，
用户就会学会无视所有告警。

刻意**不**按「数字不能超过总页数」去过滤：那样会把「页码整体偏移」这种真实
缺陷（6 页文档页脚印 3–8）误判成「读不到页码」，把 fail 降级成 warning。
量级上限挡不住年份和批号，真正管用的是「必须逐页差 1」——一串偶然读到的
数字不可能刚好构成等差数列。

## 措辞红线

任何结论都不许写「目录页码正确」：这一项现在只管页码，目录由 `toc` 项负责，
两者混在一句话里正是 v4.0 报告含糊的根源。
"""

from __future__ import annotations

import re
from typing import Any

import pymupdf
from pikepdf import Array, Dictionary

from app.pdf.checks import CheckContext, CheckResult, Finding, make_result

__all__ = ["HUMAN_NOTE", "FOOTER_BAND", "run"]

#: 结论固定附注。pass 也必须带——工具核对的是「编号从 1 起、逐页连续」，
#: 不是「每页都看得见印出来的数字」。
HUMAN_NOTE = "页面是否逐页可见地印有页码需人工确认"

_MANUAL_NOTE = "未设置逻辑页码，页脚也未发现页码，请人工复核"

#: 页脚判定带：页面高度下 12%。再大就会把正文最后几行捞进来。
FOOTER_BAND = 0.12

#: 整行就是一个页码。允许两侧破折号/空格（`- 3 -`、`— 3 —`）。
#: **不认**「第 3 页 / 共 8 页」——多一种写法就多一类误判。
_PAGE_NUMBER = re.compile(r"^[-\u2013\u2014\s]*(\d{1,4})[-\u2013\u2014\s]*$")


def _segments(node: Any, depth: int = 0) -> list[tuple[int, Any]]:
    """把 `/PageLabels` 展平成 `[(起始页索引, 标签字典)]`。"""
    if not isinstance(node, Dictionary) or depth > 8:
        return []

    found: list[tuple[int, Any]] = []
    nums = node.get("/Nums")
    if isinstance(nums, Array):
        # 成对出现；奇数长度说明结构坏了，丢掉最后那个孤项
        for offset in range(0, len(nums) - 1, 2):
            try:
                index = int(nums[offset])
            except (TypeError, ValueError):
                continue
            found.append((index, nums[offset + 1]))

    kids = node.get("/Kids")
    if isinstance(kids, Array):
        for kid in kids:
            found.extend(_segments(kid, depth + 1))
    return found


# --------------------------------------------------------------------------- #
# 页脚兜底
# --------------------------------------------------------------------------- #


def _page_footer_number(page: Any) -> int | None:
    """读一页页脚里的页码；读不到或有歧义返回 None。

    用 `clip` 只解析底部那一条，而不是 `get_text("dict")` 解析整页——
    几千页的文档上，整页解析会把这一项直接顶到 60 秒超时。
    """
    try:
        rect = page.rect
        clip = pymupdf.Rect(
            rect.x0,
            rect.y0 + rect.height * (1.0 - FOOTER_BAND),
            rect.x1,
            rect.y1,
        )
        text = page.get_text("text", clip=clip) or ""
    except Exception:  # noqa: BLE001 - 读不到就走「无法核对」
        return None

    found: set[int] = set()
    for line in text.splitlines():
        match = _PAGE_NUMBER.match(line.strip())
        if not match:
            continue
        found.add(int(match.group(1)))

    # 一行里出现两个不同的候选（页脚同时印了别的东西）→ 不敢认
    if len(found) == 1:
        return next(iter(found))
    return None


def _from_footer(ctx: CheckContext) -> CheckResult:
    """没有 `/PageLabels` 时，改用页脚数字判定。"""
    total = int(ctx.page_count)
    numbers = [_page_footer_number(ctx.doc[index]) for index in range(total)]

    missing = [index + 1 for index, value in enumerate(numbers) if value is None]
    if missing:
        listed = "、".join(str(item) for item in missing[:10])
        more = "…" if len(missing) > 10 else ""
        return make_result(
            "page_labels",
            "warning",
            _MANUAL_NOTE,
            [
                Finding(
                    level="warning",
                    message="未设置逻辑页码（/PageLabels），页脚也未读到页码",
                    page=missing[0],
                    detail=f"以下页面底部没有可识别的页码：{listed}{more}；"
                    "无法自动核对，请人工复核",
                )
            ],
        )

    expected = list(range(1, total + 1))
    if numbers == expected:
        return make_result(
            "page_labels",
            "pass",
            f"页脚页码从 1 开始连续（共 {total} 页）",
            [
                Finding(
                    level="pass",
                    message=f"页脚页码 1–{total} 逐页连续",
                    detail=HUMAN_NOTE,
                )
            ],
        )

    # 只有构成「公差为 1 的等差数列」时才敢下 fail 的结论：
    # 这说明页脚确实是一套页码，只是起点不对。否则多半是我们读错了东西。
    first = numbers[0] if numbers else 0
    arithmetic = all(value == first + index for index, value in enumerate(numbers))
    if arithmetic:
        return make_result(
            "page_labels",
            "fail",
            f"页码未从 1 开始（第 1 页为 {first}）",
            [
                Finding(
                    level="fail",
                    message=f"页脚页码从 {first} 开始，不是 1",
                    page=1,
                    detail=f"共 {total} 页，页脚编号为 {first}–{numbers[-1]}；"
                    "每个文件都应独立编号，从第一页的 1 开始",
                )
            ],
        )

    return make_result(
        "page_labels",
        "warning",
        "页脚页码不连续，无法自动核对",
        [
            Finding(
                level="warning",
                message="页脚读到的数字不构成连续页码",
                detail=f"第 1 页读到 {first}，第 {total} 页读到 {numbers[-1]}；"
                "可能是页码被拆分或页脚另有其他数字，请人工复核",
            )
        ],
    )


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def _from_labels(ctx: CheckContext, segments: list[tuple[int, Any]]) -> CheckResult:
    first_index, first = segments[0]
    start = 1
    if isinstance(first, Dictionary):
        raw = first.get("/St")
        if raw is not None:
            try:
                start = int(raw)
            except (TypeError, ValueError):
                start = 1

    if start != 1:
        return make_result(
            "page_labels",
            "fail",
            f"逻辑页码未从 1 开始（起始为 {start}）",
            [
                Finding(
                    level="fail",
                    message=f"逻辑页码未从 1 开始（起始为 {start}）",
                    page=1,
                    detail=f"第 1 段从第 {first_index + 1} 页开始，起始编号为 {start}",
                )
            ],
        )

    findings = [
        Finding(
            level="pass",
            message=f"逻辑页码从 1 开始（共 {len(segments)} 段）",
            detail=HUMAN_NOTE,
        )
    ]

    # 首段不从第 1 页开始：前几页没有逻辑页码，读者看到的就是"没有页码"。
    if first_index != 0:
        return make_result(
            "page_labels",
            "warning",
            f"前 {first_index} 页未设置逻辑页码",
            findings
            + [
                Finding(
                    level="warning",
                    message=f"第 1-{first_index} 页未设置逻辑页码",
                    detail="逻辑页码从第 "
                    f"{first_index + 1} 页才开始；这几页在阅读器里看不到页码，"
                    "请人工复核",
                )
            ],
        )

    return make_result("page_labels", "pass", "逻辑页码从 1 开始", findings)


def run(ctx: CheckContext) -> CheckResult:
    try:
        labels = ctx.pdf.Root.get("/PageLabels")
    except Exception:  # noqa: BLE001 - 结构坏了按「没设置」处理更安全
        labels = None

    segments = sorted(_segments(labels), key=lambda item: item[0])
    if segments:
        return _from_labels(ctx, segments)
    return _from_footer(ctx)
