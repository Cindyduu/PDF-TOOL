"""豁免页规格解析：`1,3,8-12`。

验收点：
- 合法输入得到"去重 + 升序 + 相邻合并"的规范化字符串与页码集合；
- 非法输入一律 `invalid_pages` + 400 + 可执行的 next_action，且 details 里带原始输入；
- 提供页数时越界必须报出"文档共 N 页"，而不是笼统的"页码不合法"。
"""

from __future__ import annotations

import pytest

from app.core.pagespec import PageSpec, parse_page_spec
from app.errors import AppError, ErrorCode

VALID_CASES = [
    # (输入, 页数, 期望页码, 期望规范化字符串)
    (None, None, (), ""),
    ("", None, (), ""),
    ("   ", None, (), ""),
    ("1", None, (1,), "1"),
    ("1,3", None, (1, 3), "1,3"),
    ("8-12", None, tuple(range(8, 13)), "8-12"),
    (" 1 , 3 ", None, (1, 3), "1,3"),
    ("1，3、5", None, (1, 3, 5), "1,3,5"),
    ("3,1,2", None, (1, 2, 3), "1-3"),
    ("2,2,2", None, (2,), "2"),
    ("1-3,5-6", None, (1, 2, 3, 5, 6), "1-3,5-6"),
    ("1-2,3-4", None, (1, 2, 3, 4), "1-4"),
    ("5", 5, (5,), "5"),
    ("1-5", 5, (1, 2, 3, 4, 5), "1-5"),
    ("10", 20, (10,), "10"),
    ("1,3-4,9", 10, (1, 3, 4, 9), "1,3-4,9"),
    ("  7  ", 10, (7,), "7"),
    ("2-4,1", 10, (1, 2, 3, 4), "1-4"),
]

INVALID_CASES = [
    ("0", None),
    ("0-3", None),
    ("3-0", None),
    ("5-3", None),
    ("-1", None),
    ("-", None),
    ("1,,2", None),
    ("1,", None),
    (",1", None),
    ("a", None),
    ("1.5", None),
    ("1-2-3", None),
    ("1 3", None),
    ("1;3", None),
    ("999", 10),
    ("1-11", 10),
    ("12", 5),
]


@pytest.mark.parametrize("raw,page_count,pages,normalized", VALID_CASES)
def test_parse_valid(raw, page_count, pages, normalized):
    spec = parse_page_spec(raw, page_count)
    assert isinstance(spec, PageSpec)
    assert spec.pages == pages
    assert spec.as_list() == list(pages)
    assert spec.normalized == normalized


@pytest.mark.parametrize("raw,page_count,pages,normalized", VALID_CASES)
def test_valid_result_is_sorted_and_unique(raw, page_count, pages, normalized):
    spec = parse_page_spec(raw, page_count)
    assert list(spec.pages) == sorted(set(spec.pages))


@pytest.mark.parametrize("raw,page_count", INVALID_CASES)
def test_parse_invalid_reports_invalid_pages(raw, page_count):
    with pytest.raises(AppError) as excinfo:
        parse_page_spec(raw, page_count)
    error = excinfo.value
    assert error.code == ErrorCode.INVALID_PAGES
    assert error.http_status == 400
    assert error.retryable is False
    assert error.next_action
    assert "1,3,8-12" in error.next_action
    assert error.details.get("raw") == raw


def test_zero_page_is_rejected_with_reason():
    with pytest.raises(AppError) as excinfo:
        parse_page_spec("0")
    assert "从 1 开始" in excinfo.value.message


def test_reversed_range_mentions_token():
    with pytest.raises(AppError) as excinfo:
        parse_page_spec("9-4")
    error = excinfo.value
    assert "9-4" in error.message
    assert error.details["token"] == "9-4"


def test_out_of_range_reports_page_count():
    with pytest.raises(AppError) as excinfo:
        parse_page_spec("999", 12)
    error = excinfo.value
    assert "12" in error.message
    assert error.details["page_count"] == 12


def test_empty_piece_mentions_comma():
    with pytest.raises(AppError) as excinfo:
        parse_page_spec("1,,2")
    assert "逗号" in excinfo.value.message


def test_pagespec_bool_and_membership():
    spec = parse_page_spec("1,3-5")
    assert bool(spec) is True
    assert 1 in spec
    assert 4 in spec
    assert 2 not in spec
    assert 6 not in spec


def test_empty_pagespec_is_falsy():
    spec = parse_page_spec("")
    assert bool(spec) is False
    assert spec.pages == ()
    assert spec.normalized == ""
    assert 1 not in spec


def test_pagespec_is_immutable():
    spec = parse_page_spec("1,2")
    with pytest.raises(Exception):
        spec.pages = (3,)  # type: ignore[misc]
    listed = spec.as_list()
    listed.append(99)
    assert spec.pages == (1, 2)


def test_page_count_none_skips_range_check():
    spec = parse_page_spec("1000")
    assert spec.pages == (1000,)


def test_full_width_comma_and_ideographic_comma_are_equivalent():
    assert parse_page_spec("1，3").pages == parse_page_spec("1、3").pages == (1, 3)
