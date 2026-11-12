"""R15 文件大小校验。

只做判定、**不自动拆分**（§1.3 第 1 条）。所以这一项的全部价值都在
"报告里那个数字准不准"上：用户拿它去对照资源管理器，一对照就得对上。

因此验收重点不是"超没超"，而是**字节数与 MB 换算同时出现在摘要里**，
且换算用 1024 进制（AC-R15-N1 的 `4404019 字节（4.20 MB）`）。
"""

from __future__ import annotations

import pytest

from app.pdf.checks import size


def _run(ctx, size_bytes: int):
    return size.run(ctx("check_clean.pdf", size_bytes=size_bytes))


def test_exact_bytes_and_megabytes_are_both_reported(check_ctx):
    result = _run(check_ctx, 4404019)
    # AC-R15-N1 的原文示例，一个字符都不许差
    assert "4404019 字节（4.20 MB）" in result.summary
    assert result.status == "pass"


def test_small_file_passes(check_ctx):
    result = _run(check_ctx, 1024)
    assert result.status == "pass"
    assert result.findings[0].level == "pass"
    assert "1024 字节（0.00 MB）" in result.summary


def test_conversion_uses_1024_not_1000(check_ctx):
    # 用 1000 进制会把 4.20 MB 写成 4.40 MB，用户一对照就发现工具在胡说
    result = _run(check_ctx, 4404019)
    assert "4.40 MB" not in result.summary
    assert "4.20 MB" in result.summary


def test_boundary_at_100mb_is_still_pass(check_ctx):
    assert _run(check_ctx, size.PASS_LIMIT_BYTES).status == "pass"


def test_one_byte_over_100mb_warns(check_ctx):
    result = _run(check_ctx, size.PASS_LIMIT_BYTES + 1)
    assert result.status == "warning"
    assert "接近 200 MB 上限" in result.summary
    assert result.findings[0].level == "warning"


def test_boundary_at_200mb_still_warns_not_fails(check_ctx):
    assert _run(check_ctx, size.WARN_LIMIT_BYTES).status == "warning"


def test_one_byte_over_200mb_fails(check_ctx):
    result = _run(check_ctx, size.WARN_LIMIT_BYTES + 1)
    assert result.status == "fail"
    assert "超过 200 MB 上限" in result.summary


def test_fail_message_says_the_tool_will_not_split(check_ctx):
    result = _run(check_ctx, size.WARN_LIMIT_BYTES + 1)
    # §1.3 第 1 条：不自动拆分。文案必须把下一步交给用户。
    assert "不会自动拆分" in result.findings[0].detail


def test_warning_message_suggests_compressing(check_ctx):
    result = _run(check_ctx, 150 * size.MB)
    assert "压缩" in result.findings[0].detail


@pytest.mark.parametrize("size_bytes", [0, 1, 999, 100 * size.MB])
def test_pass_level_never_degrades(check_ctx, size_bytes):
    result = _run(check_ctx, size_bytes)
    assert result.status == "pass"
    assert all(item.level == "pass" for item in result.findings)


def test_limits_are_100_and_200_megabytes():
    assert size.MB == 1024 * 1024
    assert size.PASS_LIMIT_BYTES == 100 * 1024 * 1024
    assert size.WARN_LIMIT_BYTES == 200 * 1024 * 1024


def test_real_fixture_size_matches_the_file_on_disk(check_ctx):
    ctx = check_ctx("check_clean.pdf")
    result = size.run(ctx)
    assert f"{ctx.size_bytes} 字节" in result.summary
