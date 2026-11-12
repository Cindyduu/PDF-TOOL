"""R16 外部链接校验。

**不自动删除**（§1.3 第 9 条）——外链可能是文档故意留的参考资料，
工具只负责把它捞出来给人看。

遍历复用 `app/pdf/links.py`：链接的判定（`/Subtype /Link`、动作类型）
在 R9 里已经有一份实现，这里再写一遍迟早会两边不一致。

`/Annots` 结构损坏时跳过该页并记明细（AC-R16-E1）——一页坏掉不该让
整份文档的链接结论变成"未知"，用户至少还能看到其余页的结果。
"""

from __future__ import annotations

from typing import Any

import pikepdf
from pikepdf import Array, Dictionary

from app.pdf.checks import CheckContext, CheckResult, Finding, make_result
from app.pdf.checks._common import truncate
from app.pdf.links import _action_kind, iter_links

__all__ = ["run", "classify_scheme"]

#: scheme -> 展示用类别。没列到的都算「其他协议」，同样告警。
_SCHEME_GROUPS: dict[str, str] = {
    "http": "网页链接",
    "https": "网页链接",
    "mailto": "邮件链接",
    "file": "本地文件",
    "ftp": "FTP 链接",
    "ftps": "FTP 链接",
}


def classify_scheme(uri: str) -> tuple[str, str]:
    """返回 `(scheme, 类别)`。scheme 统一小写；取不到就记空串。"""
    head, sep, _rest = (uri or "").partition(":")
    if not sep or not head:
        return "", "其他协议"
    # `C:\docs\a.pdf` 的 "C" 也是纯字母，光靠字符集判断会把它当成协议。
    # 单字母前缀一律按盘符处理——现实里没有单字母的 URI scheme。
    if len(head) == 1:
        return "", "其他协议"
    if not head.replace("+", "").replace("-", "").replace(".", "").isalnum():
        return "", "其他协议"
    scheme = head.lower()
    return scheme, _SCHEME_GROUPS.get(scheme, "其他协议")


def _broken_pages(pdf: pikepdf.Pdf) -> list[int]:
    """找出 `/Annots` 读不干净的页（结构损坏，链接结论不可信）。

    两种坏法：

    - `/Annots` 存在但不是数组；
    - 数组里混进了非字典项——悬空引用读出来是 `None`，坏值读出来是数字。
      这种最危险：`iter_links` 会**静默跳过**这些项，结果就是这页明明有
      链接，报告却说"没有"。跳过一页的坏注释可以，但不能连"这页有问题"
      都不说（AC-R16-E1）。
    """
    broken: list[int] = []
    for index, page in enumerate(pdf.pages, start=1):
        try:
            annots = page.obj.get("/Annots")
        except Exception:  # noqa: BLE001 - 读不出来正是要报的情况
            broken.append(index)
            continue
        if annots is None:
            continue
        if not isinstance(annots, Array):
            broken.append(index)
            continue
        if any(not isinstance(item, Dictionary) for item in annots):
            broken.append(index)
    return broken


def _uri_of(annot: Any) -> str:
    action = annot.get("/A")
    if not isinstance(action, Dictionary):
        return ""
    value = action.get("/URI")
    return "" if value is None else str(value)


def run(ctx: CheckContext) -> CheckResult:
    findings: list[Finding] = []
    broken = _broken_pages(ctx.pdf)
    for page_number in broken:
        findings.append(
            Finding(
                level="warning",
                message=f"第 {page_number} 页链接读取失败",
                page=page_number,
                detail="/Annots 结构异常，该页链接未参与校验；其余页面不受影响",
            )
        )

    external: list[tuple[int, str, str, str]] = []
    for page_number, annot in iter_links(ctx.pdf):
        if _action_kind(annot) != "/URI":
            continue
        uri = _uri_of(annot)
        scheme, group = classify_scheme(uri)
        external.append((page_number, scheme, group, uri))

    for page_number, scheme, group, uri in external:
        label = f"{scheme}://" if scheme else ""
        findings.append(
            Finding(
                level="warning",
                message=f"第 {page_number} 页：{truncate(uri)}",
                page=page_number,
                detail=f"类别：{group}" + (f"（协议 {label.rstrip(':/')}）" if label else ""),
            )
        )

    if not external and not broken:
        return make_result(
            "external_links",
            "pass",
            "未发现外部链接",
            [Finding(level="pass", message="文档内没有指向外部地址的链接")],
        )

    groups: dict[str, int] = {}
    for _page, _scheme, group, _uri in external:
        groups[group] = groups.get(group, 0) + 1
    breakdown = "、".join(f"{name} {count} 个" for name, count in groups.items())

    if external:
        summary = f"发现 {len(external)} 个外部链接"
        if breakdown:
            summary += f"（{breakdown}）"
    else:
        # 有页读不出来时不能说"没有外链"——那几页根本没看过。
        summary = "未发现可读取的外部链接" if broken else "未发现外部链接"
    if broken:
        summary += f"，{len(broken)} 页链接读取失败"
    return make_result("external_links", "warning", summary, findings)
