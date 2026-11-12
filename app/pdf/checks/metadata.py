"""R39 元数据与安全校验（只读）。

三个子项：

| 子项 | 判据 | 状态 | 可自动修复？ |
| --- | --- | --- | --- |
| DocInfo 字段 | `/Trailer /Info` 是字典且含 ≥1 个非空字段 | `warning` | 是（处理项 ②） |
| XMP 元数据流 | `/Root /Metadata` 或任一页面 `/Metadata` | `warning` | 是（处理项 ②） |
| 加密 / 权限设置 | `pdf.is_encrypted` | `fail` | 否（D-9：工具不解密） |

**为什么「密码」落在这里**：需要打开密码的文件在上传阶段就被 R29 拒了，
进不到校验；能进来的只有「仅权限密码」（owner password）的文件——它能打开、
能处理，但带着加密字典和权限限制。eCTD 提交前这类文件必须解干净，
而工具不会替用户解密，所以判 `fail` 并说清「需要人工处理」。

**为什么描述信息判 `warning` 而不是 `fail`**（D-17）：处理项 ② 一勾就清掉，
属于「工具能修的问题」。判 `fail` 会让「作者名没删」和「文件加密了」在汇总里
同等严重，而后者必须人工介入——严重度不拉开，用户就没法按顺序处理。

**不挑字段**：处理侧 `metadata.process()` 删的是整个 `/Info`，不是白名单里的
某几个键。校验如果只查 Author/Title 而放过 Producer，就会出现「校验说干净了、
处理却还有东西可删」的矛盾。两边都按「整个 `/Info` 存不存在」算。
"""

from __future__ import annotations

from typing import Any

from pikepdf import Dictionary

from app.pdf.checks import CheckContext, CheckResult, Finding, make_result

__all__ = ["run", "VALUE_LIMIT"]

#: 明细里字段值的截断长度。元数据值动辄整段句子，甚至有人把整篇摘要塞进
#: `/Subject`；不截断会把后面几条明细挤出报告。
VALUE_LIMIT = 60


def _flatten(value: Any) -> str:
    """把 PDF 对象转成单行文本并截断；任何异常都降级成占位。"""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - 坏对象转字符串也可能炸
        return "（无法读取）"
    text = " ".join(text.split())
    if not text:
        return "（空值）"
    return text if len(text) <= VALUE_LIMIT else text[: VALUE_LIMIT - 1] + "…"


def _info_fields(pdf: Any) -> list[tuple[str, str]]:
    """返回 `[(字段名, 值)]`；`/Info` 不是字典时返回空列表并单独标记。"""
    try:
        info = pdf.trailer.get("/Info")
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(info, Dictionary):
        return []
    fields: list[tuple[str, str]] = []
    try:
        keys = list(info.keys())
    except Exception:  # noqa: BLE001 - 坏字典连键都列不出来
        return fields
    for key in keys:
        name = str(key).lstrip("/")
        try:
            raw = info.get(key)
        except Exception:  # noqa: BLE001
            fields.append((name, "（无法读取）"))
            continue
        # 空值不算残留：`/Author ()` 这种空字符串在 Acrobat 里显示为空，
        # 和键不存在没有区别。只有真的写了内容才算「没清干净」。
        if raw is None:
            continue
        if str(raw).strip() == "":
            continue
        fields.append((name, _flatten(raw)))
    return fields


def _info_is_broken(pdf: Any) -> bool:
    """`/Info` 存在但不是字典：结构异常，必须报出来而不是当成「没有」。"""
    try:
        info = pdf.trailer.get("/Info")
    except Exception:  # noqa: BLE001
        return True
    return info is not None and not isinstance(info, Dictionary)


def _pages_with_xmp(pdf: Any) -> int:
    count = 0
    try:
        pages = pdf.pages
    except Exception:  # noqa: BLE001
        return 0
    for page in pages:
        try:
            if page.obj.get("/Metadata") is not None:
                count += 1
        except Exception:  # noqa: BLE001 - 单页坏了不拖垮计数
            continue
    return count


def run(ctx: CheckContext) -> CheckResult:
    pdf = ctx.pdf

    encrypted = False
    try:
        encrypted = bool(pdf.is_encrypted)
    except Exception:  # noqa: BLE001 - 老版本没有这个属性时按未加密处理
        encrypted = False

    broken_info = _info_is_broken(pdf)
    fields = _info_fields(pdf)
    has_doc_xmp = False
    try:
        has_doc_xmp = pdf.Root.get("/Metadata") is not None
    except Exception:  # noqa: BLE001
        has_doc_xmp = False
    page_xmp = _pages_with_xmp(pdf)

    findings: list[Finding] = []

    if encrypted:
        findings.append(
            Finding(
                level="fail",
                message="文档带加密/权限设置",
                detail="工具不自动解密；请用 PDF 软件解除保护后重新上传",
            )
        )

    if broken_info:
        findings.append(
            Finding(
                level="warning",
                message="DocInfo 结构异常",
                detail="/Info 存在但不是字典对象，无法逐字段核对",
            )
        )

    for name, value in fields:
        findings.append(
            Finding(
                level="warning",
                message=f"描述字段残留：{name}",
                detail=f"值：{value}",
            )
        )

    if has_doc_xmp:
        findings.append(
            Finding(
                level="warning",
                message="残留文档级 XMP 元数据流",
                detail="/Root /Metadata 仍然存在",
            )
        )

    if page_xmp:
        findings.append(
            Finding(
                level="warning",
                message=f"残留页面级 XMP 元数据流（{page_xmp} 页）",
                detail="页面 /Metadata 仍然存在",
            )
        )

    if encrypted:
        return make_result(
            "metadata_security",
            "fail",
            "文档带加密/权限设置，工具无法自动解除",
            findings,
        )

    if not findings:
        return make_result(
            "metadata_security",
            "pass",
            "文档描述已清空，无加密设置",
            [
                Finding(
                    level="pass",
                    message="文档描述已清空，无加密设置",
                    detail="无 DocInfo 字段、无 XMP 元数据流、未加密",
                )
            ],
        )

    parts: list[str] = []
    if fields:
        parts.append(f"{len(fields)} 个描述字段")
    if broken_info:
        parts.append("DocInfo 结构异常")
    if has_doc_xmp:
        parts.append("1 个文档级 XMP")
    if page_xmp:
        parts.append(f"{page_xmp} 页页面级 XMP")
    return make_result(
        "metadata_security",
        "warning",
        "残留 " + "、".join(parts),
        findings,
    )
