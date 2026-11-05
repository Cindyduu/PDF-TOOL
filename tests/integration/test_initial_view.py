"""阶段 P2 集成验收：初始视图三项（R3 / R4 / R5）。

单测验证的是「处理器说自己改了什么」，这里验证的是**用户拿到手的那个 PDF**：

- 重新打开输出文件，按 Acrobat 的读法读三个键，应当正好是
  「书签面板和页面 / 默认 / 默认」；
- 报告里同时给出**原始键值**与**Acrobat 文字**，两边必须指向同一个事实；
- 源文件本来就符合时，如实报「已符合」，不能为了显得干过活而重写；
- `/OpenAction` 是脚本行为时一个字节都不许动；
- 打开动作指向命名目标、而该目标同时被书签引用时，只摘 Root 上的键，
  共享目标本身保持原值。

这些用例都走完整流水线（含展平、元数据、线性化），因为「处理器改对了但
最后一步把文件写回去了」这类问题只有跑完才看得见。
"""

from __future__ import annotations

import json
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Dictionary, Name, String

from app.pdf import view_labels
from app.pdf.pipeline import run_pipeline
from tests.fixtures.make_fixtures import (
    add_named_dests,
    add_page,
    build_outline,
    dest_xyz,
    new_pdf,
    save,
)

VIEW_KEYS = ("bookmark_panel", "initial_layout", "initial_zoom")

TARGET_MODE = "书签面板和页面"
TARGET_LAYOUT = "默认"
TARGET_ZOOM = "默认"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def build_source(
    path: Path,
    *,
    layout: str | None = None,
    mode: str | None = None,
    open_action: str | None = None,
    outline: bool = True,
    named_dest: bool = False,
    named_zoom: float = 2.5,
    bookmark_named: bool = False,
) -> Path:
    """按需拼一个最小文档。

    `open_action` 取值：None（无键）/ "array" / "named" / "javascript"。
    `mode` 必须晚于 `build_outline` 赋值——后者会把 PageMode 设成 /UseNone。
    """
    pdf = new_pdf()
    first = add_page(pdf)
    add_page(pdf)
    if named_dest:
        add_named_dests(pdf, {"go": dest_xyz(first, named_zoom)})
    if outline:
        build_outline(
            pdf,
            [
                {
                    "title": "One",
                    "dest": String("go") if bookmark_named else dest_xyz(first, 2.0),
                }
            ],
        )
    if layout is not None:
        pdf.Root.PageLayout = Name(layout)
    if mode is not None:
        pdf.Root.PageMode = Name(mode)

    if open_action == "array":
        pdf.Root.OpenAction = dest_xyz(first, 2.0)
    elif open_action == "named":
        pdf.Root.OpenAction = String("go")
    elif open_action == "javascript":
        pdf.Root.OpenAction = Dictionary(
            S=Name("/JavaScript"), JS=String("app.alert('hello')")
        )
    elif open_action is not None:  # pragma: no cover - 防手误
        raise AssertionError(f"未知的 open_action：{open_action}")
    return save(pdf, path)


def view_of(path: Path) -> dict:
    """按 Acrobat 的读法读出三项（不含打开动作解析，足够覆盖本文件用途）。"""
    with pikepdf.open(path) as pdf:
        layout = pdf.Root.get("/PageLayout")
        mode = pdf.Root.get("/PageMode")
        action = pdf.Root.get("/OpenAction")
        return {
            "layout_raw": None if layout is None else str(layout),
            "layout_label": view_labels.layout_label(layout),
            "mode_raw": None if mode is None else str(mode),
            "mode_label": view_labels.mode_label(mode),
            "open_action": None if action is None else str(action),
        }


def named_dest_zoom(path: Path, name: str = "go") -> float:
    with pikepdf.open(path) as pdf:
        pairs = pdf.Root.Names.Dests.Names
        for index in range(0, len(pairs) - 1, 2):
            if str(pairs[index]) == name:
                return float(pairs[index + 1][4])
    raise AssertionError(f"输出里找不到命名目标 {name}")


def items_of(result) -> dict[str, dict]:
    return {key: result.result_for(key) for key in VIEW_KEYS}


# --------------------------------------------------------------------------- #
# 三种源状态：无键 / 有键值 / 已符合
# --------------------------------------------------------------------------- #


def test_explicit_values_are_fixed_on_disk(tmp_path):
    source = build_source(
        tmp_path / "src.pdf",
        layout="/TwoColumnLeft",
        mode="/UseNone",
        open_action="array",
    )
    output = tmp_path / "out.pdf"

    result = run_pipeline(source, output)
    items = items_of(result)

    # 文件层面：三项都真的变成了目标值
    after = view_of(output)
    assert after["layout_raw"] is None, "PageLayout 必须被删除，而不是换成别的值"
    assert after["layout_label"] == TARGET_LAYOUT
    assert after["mode_raw"] == "/UseOutlines"
    assert after["mode_label"] == TARGET_MODE
    assert after["open_action"] is None, "打开动作必须被移除"

    # 报告层面：三项都报「已调整」，并给出改前/改后的原始键值
    assert {key: item["status"] for key, item in items.items()} == {
        "bookmark_panel": "adjusted",
        "initial_layout": "adjusted",
        "initial_zoom": "adjusted",
    }
    assert items["initial_layout"]["before"]["page_layout"] == "/TwoColumnLeft"
    assert items["initial_layout"]["before"]["page_layout_label"] == "双联连续（奇数页在左）"
    assert items["initial_layout"]["after"]["page_layout_label"] == TARGET_LAYOUT
    assert items["bookmark_panel"]["before"]["page_mode_label"] == "仅页面"
    assert items["initial_zoom"]["before"]["open_page"] == 1
    assert items["initial_zoom"]["before"]["zoom_label"] == "200%"
    assert items["initial_zoom"]["after"]["zoom_label"] == TARGET_ZOOM

    for item in items.values():
        assert item["counts"]["checked"] == 1
        assert item["counts"]["modified"] == 1


def test_document_without_keys_is_reported_compliant(tmp_path):
    """三项键都不存在：既已符合，就不能谎称改过。"""
    source = build_source(tmp_path / "src.pdf", mode="/UseOutlines")
    output = tmp_path / "out.pdf"

    result = run_pipeline(source, output)
    items = items_of(result)

    assert items["initial_layout"]["reason_code"] == "no_page_layout"
    assert items["initial_zoom"]["reason_code"] == "no_open_action"
    assert items["bookmark_panel"]["reason_code"] == "already_use_outlines"
    for item in items.values():
        assert item["status"] == "unchanged"
        assert item["counts"]["compliant"] == 1
        assert item["counts"]["modified"] == 0

    # 报告里的「处理后」必须等于 Acrobat 的默认显示，而不是空白
    assert items["initial_layout"]["after"]["page_layout_label"] == TARGET_LAYOUT
    assert items["initial_zoom"]["after"]["zoom_label"] == TARGET_ZOOM
    assert items["bookmark_panel"]["after"]["page_mode_label"] == TARGET_MODE

    after = view_of(output)
    assert after["layout_raw"] is None
    assert after["mode_raw"] == "/UseOutlines"
    assert after["open_action"] is None


def test_other_page_mode_is_overwritten_on_disk(tmp_path):
    source = build_source(tmp_path / "src.pdf", mode="/UseThumbs")
    output = tmp_path / "out.pdf"

    result = run_pipeline(source, output)
    item = result.result_for("bookmark_panel")

    assert item["before"]["page_mode"] == "/UseThumbs"
    assert item["before"]["page_mode_label"] == "缩略图面板和页面"
    assert item["after"]["page_mode_label"] == TARGET_MODE
    assert view_of(output)["mode_raw"] == "/UseOutlines"


# --------------------------------------------------------------------------- #
# 非视图的打开动作
# --------------------------------------------------------------------------- #


def test_javascript_open_action_survives_the_whole_pipeline(tmp_path):
    """R4 只处理「打开时跳转」，脚本行为在最终文件里必须原样还在。"""
    source = build_source(
        tmp_path / "src.pdf", mode="/UseOutlines", open_action="javascript"
    )
    output = tmp_path / "out.pdf"

    result = run_pipeline(source, output)
    item = result.result_for("initial_zoom")

    assert item["status"] == "not_applicable"
    assert item["reason_code"] == "open_action_not_view"
    assert item["counts"]["not_applicable"] == 1
    assert item["counts"]["modified"] == 0
    # 警告里必须点名是哪种动作（处理器统一转成小写子类型名）
    assert any("javascript" in warning.lower() for warning in item["warnings"])

    with pikepdf.open(output) as pdf:
        action = pdf.Root.get("/OpenAction")
        assert action is not None, "脚本型打开动作被误删了"
        assert str(action.get("/S")) == "/JavaScript"
        assert "app.alert" in str(action.get("/JS"))


# --------------------------------------------------------------------------- #
# 命名目标被共享
# --------------------------------------------------------------------------- #


def test_shared_named_dest_is_not_touched_when_open_action_is_removed(tmp_path):
    """打开动作指向的命名目标同时被书签引用：只摘 Root 的键，目标保持原值。"""
    source = build_source(
        tmp_path / "src.pdf",
        mode="/UseOutlines",
        open_action="named",
        named_dest=True,
        bookmark_named=True,
        named_zoom=2.5,
    )
    output = tmp_path / "out.pdf"

    result = run_pipeline(source, output)

    assert result.result_for("initial_zoom")["status"] == "adjusted"
    assert result.result_for("initial_zoom")["before"]["zoom_label"] == "250%"

    with pikepdf.open(output) as pdf:
        assert pdf.Root.get("/OpenAction") is None
    assert named_dest_zoom(output) == pytest.approx(2.5), (
        "共享命名目标被就地改写，等于把书签的跳转也一起改了"
    )


# --------------------------------------------------------------------------- #
# 报告能逐字对号
# --------------------------------------------------------------------------- #


def test_report_contains_both_acrobat_text_and_raw_keys(tmp_path):
    source = build_source(
        tmp_path / "src.pdf",
        layout="/TwoPageRight",
        mode="/UseNone",
        open_action="array",
    )
    result = run_pipeline(source, tmp_path / "out.pdf")

    raw_fields = {
        "initial_layout": ("page_layout", "page_layout_label"),
        "bookmark_panel": ("page_mode", "page_mode_label"),
        "initial_zoom": ("fit_type", "zoom_label"),
    }
    for key, (raw_field, label_field) in raw_fields.items():
        item = result.result_for(key)
        for side in ("before", "after"):
            view = item[side]
            assert raw_field in view, f"{key}.{side} 缺原始键值"
            assert view[label_field], f"{key}.{side} 缺 Acrobat 文字"
            # 原始键值必须是 JSON 安全的标量，不能是 pikepdf 对象
            assert view[raw_field] is None or isinstance(view[raw_field], (str, int, float))

    # 整份报告要能直接落盘成 JSON（Job 就是这么存的）
    payload = json.dumps(
        {key: result.result_for(key) for key in VIEW_KEYS}, ensure_ascii=False
    )
    assert TARGET_MODE in payload


def test_meta_endpoint_publishes_the_same_labels(client):
    """前端不自己维护中文表：文字必须由后端一处给出。"""
    meta = client.get("/api/meta").json()
    labels = meta["view_labels"]

    assert labels["targets"] == {
        "page_layout": TARGET_LAYOUT,
        "page_mode": TARGET_MODE,
        "zoom": TARGET_ZOOM,
    }
    assert labels["page_mode"]["/UseOutlines"] == TARGET_MODE
    assert labels["page_layout"]["/TwoColumnLeft"] == "双联连续（奇数页在左）"
    assert labels["page_layout"][""] == TARGET_LAYOUT
