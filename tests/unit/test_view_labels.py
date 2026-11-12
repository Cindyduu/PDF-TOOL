"""Acrobat 口径映射表。

这份表是「报告里的文字」和「Acrobat 文档属性对话框里的文字」之间唯一的翻译层。
它一旦错了，用户就永远对不上号——所以每条都按真实下拉框选项逐字断言。

两条硬约束：
- **绝不抛异常**。坏文档里 `/PageLayout` 可能是间接对象、数字、甚至乱码字符串，
  翻译失败只能降级成"未知(xxx)"，不能让整项处理失败。
- **缺失值有确定语义**。`/PageLayout` 缺失 = Acrobat 的"默认"，
  `/PageMode` 缺失按规范等价于 `/UseNone` = "仅页面"，两者不能混为一谈。
"""

from __future__ import annotations

import pytest

from app.pdf import view_labels


# --------------------------------------------------------------------------- #
# 页面布局
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        (None, "默认"),
        ("/SinglePage", "单页"),
        ("/OneColumn", "单页连续"),
        ("/TwoColumnLeft", "双联连续（奇数页在左）"),
        ("/TwoColumnRight", "双联连续（奇数页在右）"),
        ("/TwoPageLeft", "双联（奇数页在左）"),
        ("/TwoPageRight", "双联（奇数页在右）"),
    ],
)
def test_layout_label_matches_acrobat_dropdown(raw, label):
    assert view_labels.layout_label(raw) == label


def test_layout_label_unknown_value_is_reported_not_raised():
    assert view_labels.layout_label("/Whatever") == "未知(/Whatever)"
    assert view_labels.layout_label(42) == "未知(42)"


# --------------------------------------------------------------------------- #
# 导览标签
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        (None, "仅页面"),
        ("/UseNone", "仅页面"),
        ("/UseOutlines", "书签面板和页面"),
        ("/UseThumbs", "缩略图面板和页面"),
        ("/UseOC", "图层面板和页面"),
        ("/UseAttachments", "附件面板和页面"),
        ("/FullScreen", "全屏"),
    ],
)
def test_mode_label_matches_acrobat_dropdown(raw, label):
    assert view_labels.mode_label(raw) == label


def test_mode_label_unknown_value_is_reported_not_raised():
    assert view_labels.mode_label("/UseNothing") == "未知(/UseNothing)"


# --------------------------------------------------------------------------- #
# 放大率
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fit_type", "zoom", "label"),
    [
        (None, None, "默认"),  # 没有 /OpenAction
        ("/XYZ", None, "默认"),  # 有跳转但缩放位是 null = 承前缩放
        ("/XYZ", 1.0, "实际大小"),
        ("/XYZ", 1.25, "125%"),
        ("/XYZ", 0.5, "50%"),
        ("/Fit", None, "适合页面"),
        ("/FitH", None, "适合宽度"),
        ("/FitV", None, "适合高度"),
        ("/FitB", None, "适合可见区域"),
        ("/FitBH", None, "适合可见宽度"),
        ("/FitBV", None, "适合可见高度"),
        ("/FitR", None, "适合矩形区域"),
    ],
)
def test_zoom_label_matches_acrobat_dropdown(fit_type, zoom, label):
    assert view_labels.zoom_label(fit_type, zoom) == label


def test_zoom_label_unknown_fit_type_is_reported_not_raised():
    assert view_labels.zoom_label("/FitZ", None) == "未知(/FitZ)"


def test_zoom_label_tolerates_garbage_zoom_value():
    """缩放位可能是个名字或字符串（坏文档），不能因此抛异常。"""
    assert view_labels.zoom_label("/XYZ", "/Big") == "未知(/Big)"


# --------------------------------------------------------------------------- #
# 目标值常量
# --------------------------------------------------------------------------- #


def test_target_labels_are_the_three_acrobat_values():
    """产品目标值集中在一处：三个处理项和前端卡片都从这里取。"""
    assert view_labels.TARGET_LAYOUT_LABEL == "默认"
    assert view_labels.TARGET_MODE_LABEL == "书签面板和页面"
    assert view_labels.TARGET_ZOOM_LABEL == "默认"


def test_meta_payload_is_json_safe():
    """这份表要经 /api/meta 吐给前端，键值必须都是字符串。"""
    payload = view_labels.meta_payload()
    assert set(payload) == {"page_layout", "page_mode", "zoom", "targets"}
    for group in ("page_layout", "page_mode", "zoom"):
        assert payload[group], group
        for key, value in payload[group].items():
            assert isinstance(key, str) and isinstance(value, str)
    assert payload["targets"] == {
        "page_layout": "默认",
        "page_mode": "书签面板和页面",
        "zoom": "默认",
    }
