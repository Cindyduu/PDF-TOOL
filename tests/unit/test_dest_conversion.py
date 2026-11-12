"""P3 目的地转换：任意 fit 类型 → 承前缩放。

PDF 的页面目的地有 8 种 fit 类型，只有 `/XYZ` 的第 5 项是缩放因子，其余
类型用「适合页面 / 适合宽度 / 适合矩形」这类**视图模式**表达缩放语义。
Acrobat 的「缩放比率 = 承前缩放」在文件里的等价写法只有一个：

    [页对象 /XYZ 左 上 null]

所以非 `/XYZ` 的目标必须**换类型**，而不是删参数——这也是第一版整批跳过
`/Fit`、`/FitR` 的根因（当时的判断是「没有 zoom 位就没法改」，其实是要改写）。

转换规则（来自 v2.1 计划 4.2）：

| 原目的地        | 转换后                    | 保留的语义     |
|----------------|--------------------------|---------------|
| `[p /XYZ l t z]` | `[p /XYZ l t null]`      | 位置           |
| `[p /Fit]`       | `[p /XYZ null null null]`| 仅页码         |
| `[p /FitH t]`    | `[p /XYZ null t null]`   | 纵向位置       |
| `[p /FitV l]`    | `[p /XYZ l null null]`   | 横向位置       |
| `[p /FitB]`      | `[p /XYZ null null null]`| 仅页码         |
| `[p /FitBH t]`   | `[p /XYZ null t null]`   | 纵向位置       |
| `[p /FitBV l]`   | `[p /XYZ l null null]`   | 横向位置       |
| `[p /FitR l b r t]` | `[p /XYZ null null null]` | 矩形丢弃（报告计数）|

畸形输入（缺参数）一律补 `null`，绝不抛异常：真实文档里 `[p /FitH]`、
`[p /XYZ]` 这种半截数组并不罕见，一条坏目标不能拖垮整项。
"""

from __future__ import annotations

import pytest
from pikepdf import Array, Dictionary, Name, String

from app.pdf.destinations import (
    PageResolver,
    collect_named_dests,
    resolve_dest,
    to_inherit_xyz,
    write_dest,
    write_inherit_zoom,
)
from tests.fixtures.make_fixtures import add_page, new_pdf


def tail(array) -> list:
    """去掉第 0 项（页对象），把剩下的参数转成可比较的普通值。"""
    values: list = []
    for value in list(array)[1:]:
        if value is None:
            values.append(None)
        elif isinstance(value, Name):
            values.append(str(value))
        else:
            values.append(float(value))
    return values


def convert(pdf, dest):
    resolved = resolve_dest(dest, collect_named_dests(pdf), PageResolver(pdf))
    assert resolved is not None, f"目标解析失败：{dest}"
    return to_inherit_xyz(resolved)


# --------------------------------------------------------------------------- #
# 8 种 fit 类型
# --------------------------------------------------------------------------- #


@pytest.fixture
def pages():
    pdf = new_pdf()
    first = add_page(pdf)
    second = add_page(pdf)
    return pdf, first, second


@pytest.mark.parametrize(
    ("name", "dest_tail", "expected"),
    [
        ("XYZ 保留位置、缩放清零", ["/XYZ", 0, 800, 2.0], ["/XYZ", 0.0, 800.0, None]),
        ("Fit 只保留页码", ["/Fit"], ["/XYZ", None, None, None]),
        ("FitH 保留纵向位置", ["/FitH", 700], ["/XYZ", None, 700.0, None]),
        ("FitV 保留横向位置", ["/FitV", 30], ["/XYZ", 30.0, None, None]),
        ("FitB 只保留页码", ["/FitB"], ["/XYZ", None, None, None]),
        ("FitBH 保留纵向位置", ["/FitBH", 700], ["/XYZ", None, 700.0, None]),
        ("FitBV 保留横向位置", ["/FitBV", 30], ["/XYZ", 30.0, None, None]),
        ("FitR 丢弃矩形", ["/FitR", 10, 20, 300, 700], ["/XYZ", None, None, None]),
    ],
)
def test_every_fit_type_becomes_inherit_xyz(pages, name, dest_tail, expected):
    pdf, first, _second = pages
    dest = Array([first, *dest_tail])

    converted = convert(pdf, dest)

    assert tail(converted) == expected, name
    # 页对象必须原样指回同一页，不能因为改写目标而换了页
    assert converted[0].objgen == first.objgen


def test_xyz_without_zoom_already_is_inherit(pages):
    pdf, first, _second = pages
    converted = convert(pdf, Array([first, Name("/XYZ"), 0, 800, None]))
    assert tail(converted) == ["/XYZ", 0.0, 800.0, None]


def test_page_index_style_destination_keeps_the_index(pages):
    """少数老文档第 0 项写的是 0 基页序号而不是页对象，改写时不能丢。"""
    pdf, _first, _second = pages
    converted = convert(pdf, Array([1, Name("/Fit")]))
    assert float(converted[0]) == 1.0
    assert tail(converted) == ["/XYZ", None, None, None]


# --------------------------------------------------------------------------- #
# 畸形输入
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "dest_tail", "expected"),
    [
        ("FitH 缺 top", ["/FitH"], ["/XYZ", None, None, None]),
        ("XYZ 缺全部参数", ["/XYZ"], ["/XYZ", None, None, None]),
        ("XYZ 只有左", ["/XYZ", 12], ["/XYZ", 12.0, None, None]),
        ("FitR 矩形不全", ["/FitR", 10, 20], ["/XYZ", None, None, None]),
        ("未知 fit 类型", ["/Whatever", 1, 2], ["/XYZ", None, None, None]),
    ],
)
def test_malformed_destinations_do_not_raise(pages, name, dest_tail, expected):
    pdf, first, _second = pages
    converted = convert(pdf, Array([first, *dest_tail]))
    assert tail(converted) == expected, name


def test_empty_array_is_not_resolvable(pages):
    pdf, _first, _second = pages
    assert resolve_dest(Array([]), collect_named_dests(pdf), PageResolver(pdf)) is None


# --------------------------------------------------------------------------- #
# 写回槽位
# --------------------------------------------------------------------------- #


def test_shared_target_is_replaced_by_a_copy(pages):
    """命名目标被多处引用：写回的是新数组，原件一个字节不动。"""
    pdf, first, _second = pages
    pdf.Root.Names = Dictionary(
        Dests=Dictionary(Names=Array([String("shared"), Array([first, Name("/FitH"), 700])]))
    )
    named = collect_named_dests(pdf)
    resolved = resolve_dest(String("shared"), named, PageResolver(pdf))
    assert resolved is not None and resolved.shared

    slot = Dictionary()
    mode = write_dest(slot, "/Dest", to_inherit_xyz(resolved), resolved)

    assert mode == "copied"
    assert tail(slot.Dest) == ["/XYZ", None, 700.0, None]
    assert tail(named["shared"]) == ["/FitH", 700.0], "命名目标原件被就地改写"


def test_private_target_is_written_back(pages):
    pdf, first, _second = pages
    slot = Dictionary(Dest=Array([first, Name("/Fit")]))
    resolved = resolve_dest(slot.Dest, collect_named_dests(pdf), PageResolver(pdf))
    assert resolved is not None and not resolved.shared

    mode = write_dest(slot, "/Dest", to_inherit_xyz(resolved), resolved)

    assert mode == "in_place"
    assert tail(slot.Dest) == ["/XYZ", None, None, None]


def test_write_inherit_zoom_still_works_for_xyz(pages):
    """旧入口保留为薄封装：已有调用方和测试都还在用它。"""
    pdf, first, _second = pages
    slot = Dictionary(Dest=Array([first, Name("/XYZ"), 0, 800, 2.0]))
    resolved = resolve_dest(slot.Dest, collect_named_dests(pdf), PageResolver(pdf))
    assert resolved is not None

    assert write_inherit_zoom(slot, "/Dest", resolved) == "in_place"
    assert tail(slot.Dest) == ["/XYZ", 0.0, 800.0, None]


# --------------------------------------------------------------------------- #
# 中文口径
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("dest_tail", "label"),
    [
        (["/XYZ", 0, 800, 2.0], "指定位置与缩放"),
        (["/Fit"], "适合页面"),
        (["/FitH", 700], "适合宽度"),
        (["/FitV", 30], "适合高度"),
        (["/FitB"], "适合可见区域"),
        (["/FitBH", 700], "适合可见宽度"),
        (["/FitBV", 30], "适合可见高度"),
        (["/FitR", 10, 20, 300, 700], "适合矩形区域"),
    ],
)
def test_fit_label_matches_acrobat_wording(pages, dest_tail, label):
    pdf, first, _second = pages
    resolved = resolve_dest(Array([first, *dest_tail]), {}, PageResolver(pdf))
    assert resolved is not None
    assert resolved.fit_label == label
