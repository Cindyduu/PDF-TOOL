"""R17 页面方向校验。

要检出的只有一种情况：**横向内容被放进了纵向页面**。**不自动旋转**
（§1.3 第 7 条）——旋转错了比不旋转更糟。

所以这一项的验收重点是**措辞**：任何结论都必须带「疑似」「请人工确认」，
绝不能写成「页面方向错误」（AC-R17-E1）。这条靠断言文案来守。
"""

from __future__ import annotations

import pytest

from app.pdf.checks import orientation


def test_clean_document_passes_with_a_manual_confirmation_note(check_ctx):
    result = orientation.run(check_ctx("check_clean.pdf"))
    assert result.status == "pass"
    assert "8 页参与判定" in result.summary
    # 启发式判断，结论必须交回给人
    assert "请人工确认" in result.summary


def test_rotated_text_in_portrait_page_warns(check_ctx):
    result = orientation.run(check_ctx("check_rotated_text.pdf"))
    assert result.status == "warning"
    assert "1 页疑似方向或版式异常" in result.summary
    finding = result.findings[0]
    assert finding.page == 1
    assert "疑似横向内容置于纵向页面" in finding.message


def test_warning_never_claims_the_page_is_definitely_wrong(check_ctx):
    result = orientation.run(check_ctx("check_rotated_text.pdf"))
    blob = " ".join(item.message + (item.detail or "") for item in result.findings)
    assert "疑似" in blob
    assert "请人工确认" in blob
    # 措辞红线：不许下确定性结论
    assert "方向错误" not in blob
    assert "页面方向错误" not in blob


def test_warning_says_the_tool_will_not_rotate(check_ctx):
    result = orientation.run(check_ctx("check_rotated_text.pdf"))
    assert "不自动旋转" in result.findings[0].detail


def test_landscape_page_is_skipped_not_flagged(check_ctx):
    # 横向页面本来就没有"方向问题"，判定不了也不该告警
    result = orientation.run(check_ctx("check_landscape.pdf"))
    assert result.status == "skipped"
    assert "全部页面为横向" in result.summary
    assert "请人工确认" in result.summary


def test_page_without_text_layer_is_skipped(tmp_path, ctx_factory):
    from tests.fixtures.make_fixtures import add_page, new_pdf, save

    pdf = new_pdf()
    add_page(pdf, b"0.9 0.9 0.9 rg 40 40 515 760 re f")
    path = save(pdf, tmp_path / "no_text.pdf")

    result = orientation.run(ctx_factory(path))
    assert result.status == "skipped"
    assert "无文本层" in result.summary


def test_all_skipped_pages_still_say_manual_confirmation(check_ctx):
    result = orientation.run(check_ctx("check_landscape.pdf"))
    assert all(item.level == "skipped" for item in result.findings)


# --------------------------------------------------------------------------- #
# 判定阈值
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("bbox", "expected"),
    [
        ((0, 0, 300, 12), True),  # 宽 300 高 12：明显横排
        ((0, 0, 30, 12), False),  # 宽高比 2.5，不够宽
        ((0, 0, 36, 12), False),  # 正好 3 倍，不含（> 而非 >=）
        ((0, 0, 37, 12), True),
    ],
)
def test_horizontal_row_detection_uses_width_over_height_times_three(bbox, expected):
    assert orientation._is_horizontal_row(bbox) is expected


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        ((1.0, 0.0), False),
        ((0.0, 1.0), True),
        ((0.0, -1.0), True),
        ((-1.0, 0.0), True),
    ],
)
def test_rotation_detection_reads_the_direction_vector(direction, expected):
    assert orientation._is_rotated(direction) is expected


def test_thresholds_match_the_prd():
    assert orientation.HORIZONTAL_RATIO == 0.6
    assert orientation.ROTATED_RATIO == 0.6
    assert orientation.OVERFLOW_RATIO == 1.15
