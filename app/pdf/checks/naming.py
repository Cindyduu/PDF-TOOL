"""R14 文件命名规范校验。

规则只有一条但很容易写歪：**扩展名 `.pdf` 不计入判定**。
所以 `report-2024_v1.pdf` 合规，而 `report-2024_v1.PDF` 的扩展名部分
按同一规则也放过——用户上传时的后缀大小写不该成为失败理由。

违规定位到「第几个字符」，是因为用户要回去改名：只说"含非法字符"
等于让他自己数一遍。位置按**去掉扩展名之后**的字符串从 1 开始数。

后置校验（R27）是个例外：那里的文件名是工具自己按 `build_output_name()`
拼出来的（含中文「已处理」、时间戳、任务短 ID），拿用户上传名才适用的
规范去卡它，等于让工具判自己的产物不合规。所以 AC-R27-N4 要求这一项在
后置校验中恒为 `skipped`，原因文案固定——用户看到的是一个明确结论，
而不是一行莫名其妙的 `fail`。
"""

from __future__ import annotations

from app.pdf.checks import (
    PHASE_POST,
    CheckContext,
    CheckResult,
    Finding,
    make_result,
)

__all__ = ["run", "ALLOWED", "MAX_NAME_BYTES", "POST_SUMMARY"]

#: 后置校验里的固定结论。文案被 AC-R27-N4 钉死，改动会破坏验收。
POST_SUMMARY = "工具生成名，不适用命名规范校验"

#: 允许的字符集：小写字母、数字、连字符、下划线。
ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")

#: Windows 单个文件名上限（字节）。超过不判失败，但要提示风险。
MAX_NAME_BYTES = 255


def _stem(filename: str) -> str:
    """去掉 `.pdf` 后缀；大小写不敏感。"""
    if filename.lower().endswith(".pdf"):
        return filename[:-4]
    return filename


def _classify(char: str) -> str | None:
    """返回违规类别；合规返回 `None`。"""
    if char in ALLOWED:
        return None
    if not char.isascii():
        # 中文、全角括号、其他 Unicode 符号都归到这里：用户看得懂"非 ASCII"
        return "非 ASCII 字符"
    if char.isupper():
        return "大写字母"
    if char == " ":
        return "空格"
    if char == ".":
        return "点号"
    if char in "()[]{}":
        return "括号"
    return "非法符号"


def _label(category: str, char: str) -> str:
    # 空格直接说"空格"，写成「空格「 」」会让报告出现一对空引号，很难看
    return category if char == " " else f"{category}「{char}」"


def run(ctx: CheckContext) -> CheckResult:
    # 后置校验：文件名是工具生成的，这一项没有判断余地（AC-R27-N4）。
    # 返回 `skipped` 而不是 `pass`：我们并没有"检查过并且合规"，
    # 只是"这项对产物没有意义"。这两种说法在报告里必须能区分开。
    if ctx.phase == PHASE_POST:
        return make_result("filename_naming", "skipped", POST_SUMMARY)

    stem = _stem(ctx.filename)
    findings: list[Finding] = []

    # (类别, 字符) -> 位置列表。同一个字符出现多次只占一行，位置全列出来。
    groups: dict[tuple[str, str], list[int]] = {}
    for position, char in enumerate(stem, start=1):
        category = _classify(char)
        if category is None:
            continue
        groups.setdefault((category, char), []).append(position)

    for (category, char), positions in groups.items():
        findings.append(
            Finding(
                level="fail",
                message=f"文件名含{_label(category, char)}",
                detail="位置：" + "、".join(f"第 {item} 个字符" for item in positions),
            )
        )

    # 长度风险不改变结论：命名规则本身没被违反，只是这个文件在 Windows 上
    # 可能存不下（AC-R14-E1 要求"仍能完成校验并额外提示"，不是判失败）。
    total_bytes = len(ctx.filename.encode("utf-8"))
    if total_bytes > MAX_NAME_BYTES:
        findings.append(
            Finding(
                level="warning",
                message=f"文件名长度 {total_bytes} 字节，超过 Windows 单文件名上限 {MAX_NAME_BYTES} 字节",
                detail="保存或复制到某些目录时可能失败，建议缩短文件名",
            )
        )

    if groups:
        return make_result(
            "filename_naming",
            "fail",
            f"文件名含 {len(groups)} 处不合规字符",
            findings,
        )

    summary = "文件名符合命名规范"
    if total_bytes > MAX_NAME_BYTES:
        summary += "（但长度偏长）"
    return make_result("filename_naming", "pass", summary, findings)
