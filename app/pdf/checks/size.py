"""R15 文件大小校验。

只做判定，**不自动拆分**（§1.3 第 1 条）。

报告必须写精确字节数（AC-R15-N1：`4404019 字节（4.20 MB）`），
因为用户下一步要么去压缩、要么去拆卷，他需要一个能对得上的数字。
换算用 1024 进制——用 1000 进制会把 4.20 MB 的文件写成 4.40 MB，
用户拿资源管理器一对照就发现工具在胡说。
"""

from __future__ import annotations

from app.pdf.checks import CheckContext, CheckResult, Finding, make_result

__all__ = ["run", "PASS_LIMIT_BYTES", "WARN_LIMIT_BYTES", "MB"]

MB = 1024 * 1024

#: ≤ 100 MB 通过。
PASS_LIMIT_BYTES = 100 * MB
#: (100 MB, 200 MB] 告警；超过 200 MB 失败。
WARN_LIMIT_BYTES = 200 * MB


def _describe(size: int) -> str:
    return f"{size} 字节（{size / MB:.2f} MB）"


def run(ctx: CheckContext) -> CheckResult:
    size = int(ctx.size_bytes)
    measured = _describe(size)

    if size <= PASS_LIMIT_BYTES:
        return make_result(
            "file_size",
            "pass",
            f"文件大小 {measured}，未超过 100 MB",
            [Finding(level="pass", message=measured, detail="在 100 MB 以内")],
        )

    if size <= WARN_LIMIT_BYTES:
        return make_result(
            "file_size",
            "warning",
            f"文件大小 {measured}，已接近 200 MB 上限",
            [
                Finding(
                    level="warning",
                    message=measured,
                    detail="超过 100 MB，接近 200 MB 上限；建议压缩后再提交",
                )
            ],
        )

    return make_result(
        "file_size",
        "fail",
        f"文件大小 {measured}，超过 200 MB 上限",
        [
            Finding(
                level="fail",
                message=measured,
                detail="超过 200 MB 上限；本工具不会自动拆分，请手工拆分或压缩后重新上传",
            )
        ],
    )
