"""R18 文本可提取性校验。

**不做 OCR**（§1.3 第 3 条）。这一项回答的问题只有一个：
「这份 PDF 现在的文本层，下游系统能不能读出来」。

两种不可用，成因完全不同，报告要分开说：

- 提取结果是空 → 这页压根没有文本层（扫描件）；
- 提取结果非空但可打印字符占比 < 30% → 有文本层但编码坏了
  （`/ToUnicode` 缺失时常见），用户看到的是乱码，不是内容。

后者比前者更危险：文件"看起来有字"，下游却读出一堆方块。
"""

from __future__ import annotations

from typing import Any

from app.pdf.checks import CheckContext, CheckResult, Finding, make_result
from app.pdf.checks._common import page_ranges

__all__ = ["run", "PRINTABLE_MIN"]

#: 可打印字符占比低于它就判「内容不可读」。
PRINTABLE_MIN = 0.30

EMPTY = "empty"
UNREADABLE = "unreadable"
OK = "ok"


def _page_state(page: Any) -> tuple[str, float]:
    """返回 `(状态, 可打印字符占比)`。"""
    text = page.get_text("text") or ""
    stripped = "".join(text.split())
    if not stripped:
        return EMPTY, 0.0
    printable = sum(1 for char in stripped if char.isprintable())
    ratio = printable / len(stripped)
    return (OK if ratio >= PRINTABLE_MIN else UNREADABLE), ratio


def run(ctx: CheckContext) -> CheckResult:
    total = int(ctx.page_count)
    empty: list[int] = []
    unreadable: list[tuple[int, float]] = []

    for index, page in enumerate(ctx.doc, start=1):
        state, ratio = _page_state(page)
        if state == EMPTY:
            empty.append(index)
        elif state == UNREADABLE:
            unreadable.append((index, ratio))

    usable = total - len(empty) - len(unreadable)
    findings: list[Finding] = []

    if empty:
        findings.append(
            Finding(
                level="warning",
                message=f"第 {page_ranges(empty)} 页无文本层",
                detail="这些页可能是扫描图片，本工具不做 OCR",
            )
        )
    if unreadable:
        worst = min(ratio for _, ratio in unreadable)
        findings.append(
            Finding(
                level="warning",
                message=f"第 {page_ranges(index for index, _ in unreadable)} 页文本层存在但内容不可读",
                detail=f"可打印字符占比最低 {worst:.0%}（阈值 {PRINTABLE_MIN:.0%}），疑似编码或 ToUnicode 缺失",
            )
        )

    if total == 0:
        return make_result("text_layer", "skipped", "文档没有页面", findings)

    if usable == 0:
        # 「整份都读不出来」有两种成因，给的建议完全不同：扫描件要做 OCR，
        # 编码坏了要重新导出。这里不能合并成一句话，否则用户会照着错的那条去处理。
        if unreadable:
            headline = f"0/{total} 页文本可读，疑似编码损坏"
            detail = (
                "文本层存在但可打印字符占比过低，疑似编码或 ToUnicode 缺失；"
                "请从源文件重新导出 PDF，本工具不做 OCR"
            )
        else:
            headline = f"0/{total} 页可提取文本，疑似扫描件"
            detail = "整份文档没有可用的文本层；本工具不做 OCR，请另做文字识别"
        findings.append(
            Finding(level="fail", message=f"0/{total} 页可提取文本", detail=detail)
        )
        return make_result("text_layer", "fail", headline, findings)

    if usable < total:
        return make_result(
            "text_layer",
            "warning",
            f"{usable}/{total} 页可提取文本，{total - usable} 页不可用",
            findings,
        )

    return make_result(
        "text_layer",
        "pass",
        f"{usable}/{total} 页可提取文本",
        [Finding(level="pass", message=f"{total}/{total} 页可提取文本")],
    )
