"""校验模块的纯函数工具（`_common.py`）。

这两支函数被 9 个 check 共用，写错一次会污染一大片报告文案：
页码压缩漏掉一页，用户就少核对一页；截断多留一个字符，明细就会换行。
"""

from __future__ import annotations

import pytest

from app.pdf.checks._common import URI_LIMIT, page_ranges, truncate


@pytest.mark.parametrize(
    ("numbers", "expected"),
    [
        ([], ""),
        ([1], "1"),
        ([1, 2, 3], "1-3"),
        ([1, 3], "1、3"),
        ([1, 2, 3, 8, 10, 11, 12], "1-3、8、10-12"),
        ([5, 4, 3], "3-5"),  # 乱序输入也要能压
        ([2, 2, 2], "2"),  # 去重
        ([1, 2, 4, 5], "1-2、4-5"),
        ([3, 1, 2, 7, 6], "1-3、6-7"),
    ],
)
def test_page_ranges_compresses_but_never_drops_a_page(numbers, expected):
    assert page_ranges(numbers) == expected


def test_page_ranges_handles_a_generator():
    assert page_ranges(index for index in (1, 2, 3, 9)) == "1-3、9"


def test_page_ranges_keeps_every_page_of_a_long_run():
    result = page_ranges(range(1, 41))
    assert result == "1-40"


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short", 120, "short"),
        ("abcdef", 6, "abcdef"),
        ("abcdefg", 6, "abcde…"),
        ("", 5, ""),
    ],
)
def test_truncate_never_exceeds_the_limit(text, limit, expected):
    result = truncate(text, limit)
    assert result == expected
    assert len(result) <= limit


def test_truncate_defaults_to_the_uri_limit():
    long_uri = "https://example.com/" + "a" * 500
    result = truncate(long_uri)
    assert len(result) == URI_LIMIT
    assert result.endswith("…")


def test_truncate_is_idempotent_on_short_text():
    assert truncate("https://a.example/x") == "https://a.example/x"
