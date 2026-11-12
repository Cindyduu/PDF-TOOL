"""R14 文件命名校验。

这条规则最容易写错的地方是**扩展名不参与判定**：`Report.PDF` 的 `.PDF`
既不该因为"点号"失败，也不该因为"大写"失败。用户改的是文件主名，
后缀是上传时系统给的。

第二个坑是**位置要能对上**：报告说"第 3 个字符"，用户回去数的时候
必须正好数到那个字符。所以位置一律按去掉 `.pdf` 之后的字符串从 1 开始。
"""

from __future__ import annotations

import pytest

from app.pdf.checks import naming


def _run(ctx, filename: str):
    # 命名校验只看 `filename` 字符串，不读 PDF 内容；借用同一份样本文件即可。
    return naming.run(ctx("check_clean.pdf", filename=filename))


@pytest.mark.parametrize(
    "filename",
    [
        "report-2024_v1.pdf",
        "a.pdf",
        "study-0001-v2.pdf",
        "0123456789.pdf",
        "x_y-z.pdf",
    ],
)
def test_lowercase_letters_digits_and_separators_pass(check_ctx, filename):
    result = _run(check_ctx, filename)
    assert result.status == "pass"
    assert result.summary == "文件名符合命名规范"
    assert result.findings == []


@pytest.mark.parametrize("filename", ["report-2024_v1.PDF", "REPORT.pdf".lower(), "x.Pdf"])
def test_extension_case_is_ignored(check_ctx, filename):
    assert _run(check_ctx, filename).status == "pass"


def test_uppercase_extension_alone_does_not_fail(check_ctx):
    # `.PDF` 里的 P/D/F 属于扩展名，不参与判定。
    assert _run(check_ctx, "report.PDF").status == "pass"


def test_uppercase_in_stem_fails_and_points_at_the_position(check_ctx):
    result = _run(check_ctx, "Report.pdf")
    assert result.status == "fail"
    assert result.findings[0].message == "文件名含大写字母「R」"
    assert result.findings[0].detail == "位置：第 1 个字符"


def test_non_ascii_characters_fail(check_ctx):
    result = _run(check_ctx, "报告.pdf")
    assert result.status == "fail"
    # 分组键是「(类别, 字符)」，两个不同的汉字各占一条
    assert [item.message for item in result.findings] == [
        "文件名含非 ASCII 字符「报」",
        "文件名含非 ASCII 字符「告」",
    ]
    assert [item.detail for item in result.findings] == [
        "位置：第 1 个字符",
        "位置：第 2 个字符",
    ]


def test_space_is_reported_without_a_quote_pair(check_ctx):
    result = _run(check_ctx, "a b.pdf")
    assert result.status == "fail"
    # 「空格「 」」会让报告出现一对空引号，很难看
    assert result.findings[0].message == "文件名含空格"


def test_inner_dot_is_reported_as_a_dot(check_ctx):
    result = _run(check_ctx, "a.b.pdf")
    assert result.status == "fail"
    assert result.findings[0].message == "文件名含点号「.」"
    assert result.findings[0].detail == "位置：第 2 个字符"


@pytest.mark.parametrize(
    ("filename", "message"),
    [
        ("a(1).pdf", "文件名含括号「(」"),
        ("a[1].pdf", "文件名含括号「[」"),
        ("a@b.pdf", "文件名含非法符号「@」"),
        ("a+b.pdf", "文件名含非法符号「+」"),
        ("a#b.pdf", "文件名含非法符号「#」"),
    ],
)
def test_brackets_and_symbols_are_classified_separately(check_ctx, filename, message):
    result = _run(check_ctx, filename)
    assert result.status == "fail"
    assert result.findings[0].message == message


def test_same_character_at_several_positions_is_one_finding(check_ctx):
    result = _run(check_ctx, "aA-aA.pdf")
    assert result.status == "fail"
    # 大写 A 出现两次，只占一行，位置全列出来
    assert len(result.findings) == 1
    assert result.findings[0].detail == "位置：第 2 个字符、第 5 个字符"


def test_summary_counts_distinct_violations(check_ctx):
    result = _run(check_ctx, "A b.pdf")
    assert result.status == "fail"
    assert result.summary == "文件名含 2 处不合规字符"


def test_every_finding_is_level_fail(check_ctx):
    result = _run(check_ctx, "A B.pdf")
    assert {item.level for item in result.findings} == {"fail"}


# --------------------------------------------------------------------------- #
# AC-R14-E1：超长文件名不改变结论，只额外提示
# --------------------------------------------------------------------------- #


def test_overlong_name_still_passes_but_warns(check_ctx):
    filename = "a" * 256 + ".pdf"
    result = _run(check_ctx, filename)
    # 命名规则本身没被违反，只是这个文件在 Windows 上可能存不下
    assert result.status == "pass"
    assert "长度偏长" in result.summary
    assert [item.level for item in result.findings] == ["warning"]
    assert "255 字节" in result.findings[0].message


def test_name_exactly_at_the_limit_does_not_warn(check_ctx):
    filename = "a" * 251 + ".pdf"  # 251 + 4 = 255
    result = _run(check_ctx, filename)
    assert result.status == "pass"
    assert result.findings == []
    assert result.summary == "文件名符合命名规范"


def test_overlong_name_with_illegal_chars_is_still_fail(check_ctx):
    filename = "A" * 300 + ".pdf"
    result = _run(check_ctx, filename)
    assert result.status == "fail"
    levels = [item.level for item in result.findings]
    assert levels.count("fail") == 1
    assert levels.count("warning") == 1


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a.pdf", "a"),
        ("a.PDF", "a"),
        ("a.Pdf", "a"),
        ("a", "a"),
        ("a.pdf.pdf", "a.pdf"),
    ],
)
def test_stem_strips_only_the_final_extension(raw, expected):
    assert naming._stem(raw) == expected


def test_allowed_charset_is_exactly_the_documented_one():
    assert naming.ALLOWED == frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")


# --------------------------------------------------------------------------- #
# AC-R27-N4：后置校验里这一项恒为 skipped，文案固定
# --------------------------------------------------------------------------- #


def test_post_phase_always_skips_with_the_fixed_summary(check_ctx):
    """后置校验面对的是工具自己拼的产物名，命名规范对它没有判断余地。

    这里特意用一个**前置阶段必然 fail** 的名字（大写 + 空格 + 中文），
    证明 skipped 不是因为"名字恰好合规"，而是因为阶段不同。
    """
    result = naming.run(check_ctx("check_clean.pdf", filename="A B 报告.pdf", phase="post"))
    assert result.status == "skipped"
    assert result.key == "filename_naming"
    assert result.summary == naming.POST_SUMMARY
    assert result.findings == []


def test_post_summary_text_is_pinned_by_the_acceptance_criteria():
    """文案被 PRD AC-R27-N4 钉死，改一个字就破坏验收。"""
    assert naming.POST_SUMMARY == "工具生成名，不适用命名规范校验"


def test_same_name_is_fail_in_precheck_but_skipped_in_post(check_ctx):
    """同一个名字在两个阶段的结论必须不同——这正是 phase 存在的理由。"""
    name = "A B 报告.pdf"
    assert naming.run(check_ctx("check_clean.pdf", filename=name)).status == "fail"
    assert (
        naming.run(check_ctx("check_clean.pdf", filename=name, phase="post")).status
        == "skipped"
    )


def test_default_phase_is_precheck(check_ctx):
    """不传 phase 的老调用点行为不变：仍按前置规则判定。"""
    assert check_ctx("check_clean.pdf").phase == "precheck"
