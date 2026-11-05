"""P3 集成验收（书签）：8 种 fit 类型全部转成承前缩放。

第一版的盲区在这里：现有测试只造了 `/XYZ` 书签，`/Fit`、`/FitH`、`/FitR`
一条都没进过流水线，于是「整批跳过」这种缺陷全绿通过。

这个文件同时钉四件事：

1. 产物里**所有非豁免书签**都是 `[p /XYZ l t null]`；
2. 共用命名目标被复制，原件签名不变；
3. 豁免页书签一个字节不动；
4. 幂等——对产物再跑一遍，全部「已符合」，`modified == 0`。

外加真实文件回归：`QLM3003-301_ACRF(1).pdf` 的 60 个书签（58 `/XYZ`、
1 `/Fit`、1 `/FitH`）处理后必须全部承前缩放。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name, String

from app.pdf import bookmarks
from app.pdf.pipeline import run_pipeline
from tests.fixtures.make_fixtures import (
    add_named_dests,
    add_page,
    build_outline,
    dest_xyz,
    new_pdf,
    save,
)
from tests.pdf_ops import object_signature

REAL_FILE = Path.home() / "Downloads" / "QLM3003-301_ACRF(1).pdf"

FIT_TITLES = ("Fit", "FitH", "FitV", "FitB", "FitBH", "FitBV", "FitR")


def build_source(path: Path) -> Path:
    """8 种 fit + 2 个共用命名目标 + 1 个指向豁免页的书签。"""
    pdf = new_pdf()
    pages = [add_page(pdf) for _ in range(4)]
    add_named_dests(pdf, {"shared": dest_xyz(pages[1], 1.5)})

    build_outline(
        pdf,
        [
            {"title": "XYZ", "dest": dest_xyz(pages[0], 2.0)},
            {"title": "Fit", "dest": Array([pages[1], Name("/Fit")])},
            {"title": "FitH", "dest": Array([pages[2], Name("/FitH"), 700])},
            {"title": "FitV", "dest": Array([pages[0], Name("/FitV"), 30])},
            {"title": "FitB", "dest": Array([pages[1], Name("/FitB")])},
            {"title": "FitBH", "dest": Array([pages[2], Name("/FitBH"), 700])},
            {"title": "FitBV", "dest": Array([pages[0], Name("/FitBV"), 30])},
            {"title": "FitR", "dest": Array([pages[1], Name("/FitR"), 10, 20, 300, 700])},
            {"title": "NamedA", "dest": String("shared")},
            {"title": "NamedB", "dest": String("shared")},
            {"title": "Exempt", "dest": dest_xyz(pages[3], 3.0)},
        ],
    )
    return save(pdf, path)


def bookmark_arrays(pdf: pikepdf.Pdf) -> dict[str, object]:
    views = {}
    for item in bookmarks.iter_outline_items(pdf):
        slot, key = bookmarks.dest_slot(item)
        views[str(item.get("/Title"))] = None if slot is None else slot[key]
    return views


def test_every_bookmark_target_type_becomes_inherit_zoom(tmp_path):
    source = build_source(tmp_path / "src.pdf")
    output = tmp_path / "out.pdf"

    result = run_pipeline(source, output, exempt_pages=(4,))
    item = result.result_for("bookmarks_inherit_zoom")

    with pikepdf.open(output) as pdf:
        views = bookmark_arrays(pdf)

        for title in ("XYZ", *FIT_TITLES, "NamedA", "NamedB"):
            array = views[title]
            assert str(array[1]) == "/XYZ", f"{title} 没有换成 /XYZ"
            assert array[4] is None, f"{title} 的缩放位不是承前缩放"

        # 位置语义：该保留的保留，该丢的丢
        assert float(views["XYZ"][2]) == 0 and float(views["XYZ"][3]) == 800
        assert views["FitH"][2] is None and float(views["FitH"][3]) == 700
        assert float(views["FitV"][2]) == 30 and views["FitV"][3] is None
        assert views["Fit"][2] is None and views["Fit"][3] is None
        assert views["FitR"][2] is None and views["FitR"][3] is None

        # 豁免页书签原样：还是 /XYZ 3.0
        assert str(views["Exempt"][1]) == "/XYZ"
        assert float(views["Exempt"][4]) == 3.0

        # 共用命名目标：书签拿到复制品，原件仍是 1.5
        assert views["NamedA"] is not views["NamedB"]
        assert float(pdf.Root.Names.Dests.Names[1][4]) == 1.5

    assert item["status"] == "adjusted"
    assert item["counts"]["checked"] == 11
    assert item["counts"]["modified"] == 10
    assert item["counts"]["exempt"] == 1
    assert item["counts"]["not_applicable"] == 0
    assert item["reason_code"] == "fit_converted"
    assert any("矩形" in warning for warning in item["warnings"])
    # 报告文案用 Acrobat 口径点名被改写的类型，用户不查文件也知道改了什么
    assert "适合矩形区域 1 个" in item["message"]
    assert "适合宽度 1 个" in item["message"]


def test_fit_breakdown_matches_source_and_result(tmp_path):
    source = build_source(tmp_path / "src.pdf")
    result = run_pipeline(source, tmp_path / "out.pdf", exempt_pages=(4,))
    item = result.result_for("bookmarks_inherit_zoom")

    assert item["before"]["fit_breakdown"] == {
        "/XYZ": 4,  # XYZ 书签 + 豁免书签 + 两个共用命名目标
        "/Fit": 1,
        "/FitH": 1,
        "/FitV": 1,
        "/FitB": 1,
        "/FitBH": 1,
        "/FitBV": 1,
        "/FitR": 1,
    }
    assert item["after"]["fit_breakdown"] == {"/XYZ": 11}


def test_shared_named_target_signature_is_unchanged(tmp_path):
    source = build_source(tmp_path / "src.pdf")
    with pikepdf.open(source) as pdf:
        before = object_signature(pdf, pdf.Root.Names.Dests.Names[1])

    run_pipeline(source, tmp_path / "out.pdf", exempt_pages=(4,))

    with pikepdf.open(tmp_path / "out.pdf") as pdf:
        after = object_signature(pdf, pdf.Root.Names.Dests.Names[1])
    assert after == before, "共享命名目标被就地改写，等于改了所有引用方"


def test_action_based_bookmark_is_converted_in_place(tmp_path):
    """书签目的地也可以挂在 `/A` 动作里，而不是 `/Dest` 键上。

    真实样本 `附录16-2-8…-71.pdf` 就是这种形状：`/A {/S /GoTo, /D [页 /FitBH 595]}`。
    这条路径此前没有任何测试覆盖——`dest_slot()` 的 `/A` 分支一旦退化，
    动作式书签会静默落进「不适用」，而所有 `/Dest` 用例照样全绿。

    要点：改的是动作字典里的 `/D`，不能新建一个 `/Dest` 把动作架空。
    """
    pdf = new_pdf()
    pages = [add_page(pdf) for _ in range(2)]
    build_outline(
        pdf,
        [
            {
                "title": "FitBH-action",
                "action": Dictionary(S=Name("/GoTo"), D=Array([pages[0], Name("/FitBH"), 595])),
            },
            {
                "title": "XYZ-zoom-action",
                "action": Dictionary(S=Name("/GoTo"), D=Array([pages[1], Name("/XYZ"), 100, 700, 2.0])),
            },
        ],
    )
    source = save(pdf, tmp_path / "src.pdf")

    result = run_pipeline(source, tmp_path / "out.pdf")
    item = result.result_for("bookmarks_inherit_zoom")
    assert item["counts"]["checked"] == 2
    assert item["counts"]["modified"] == 2
    assert item["counts"]["not_applicable"] == 0, "动作式目的地被当成没有目的地了"
    assert item["reason_code"] == "fit_converted"

    with pikepdf.open(tmp_path / "out.pdf") as out:
        nodes = {str(node.get("/Title")): node for node in bookmarks.iter_outline_items(out)}
        assert len(nodes) == 2

        for title in nodes:
            assert "/Dest" not in nodes[title], f"{title} 被挪到 /Dest，原动作成了空壳"
            action = nodes[title].get("/A")
            assert isinstance(action, Dictionary)
            assert str(action.get("/S")) == "/GoTo"

        # /FitBH 595 的纵向位置必须搬过来，否则书签跳过去会停在页顶
        fitbh = nodes["FitBH-action"].get("/A").get("/D")
        assert str(fitbh[1]) == "/XYZ"
        assert fitbh[2] is None
        assert float(fitbh[3]) == 595
        assert fitbh[4] is None

        # /XYZ 带显式缩放 2.0：位置保留，缩放清空
        xyz = nodes["XYZ-zoom-action"].get("/A").get("/D")
        assert str(xyz[1]) == "/XYZ"
        assert float(xyz[2]) == 100
        assert float(xyz[3]) == 700
        assert xyz[4] is None


def test_second_pass_is_idempotent(tmp_path):
    """同样的参数再跑一遍：10 个已符合、1 个仍豁免、一个都不改。"""
    source = build_source(tmp_path / "src.pdf")
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"

    run_pipeline(source, first, exempt_pages=(4,))
    result = run_pipeline(first, second, exempt_pages=(4,))

    item = result.result_for("bookmarks_inherit_zoom")
    assert item["counts"]["compliant"] == 10
    assert item["counts"]["exempt"] == 1
    assert item["counts"]["modified"] == 0
    assert item["status"] == "unchanged"
    assert item["reason_code"] == "already_inherit"


@pytest.mark.skipif(
    not REAL_FILE.is_file(), reason=f"真实样本不在本机：{REAL_FILE}"
)
def test_real_qlm_file_all_sixty_bookmarks_become_inherit(tmp_path):
    """真实文件回归：60 个书签里有 1 个 /Fit、1 个 /FitH，第一版永远跳过。"""
    source = tmp_path / "qlm.pdf"
    shutil.copyfile(REAL_FILE, source)

    result = run_pipeline(source, tmp_path / "out.pdf")
    item = result.result_for("bookmarks_inherit_zoom")

    assert item["counts"]["checked"] == 60
    assert item["counts"]["modified"] == 60
    assert item["counts"]["not_applicable"] == 0
    assert item["status"] == "adjusted"

    with pikepdf.open(tmp_path / "out.pdf") as pdf:
        assert len(pdf.pages) == 68, "页数不能因为改书签而变"
        seen = 0
        for node in bookmarks.iter_outline_items(pdf):
            slot, key = bookmarks.dest_slot(node)
            if slot is None:
                continue
            seen += 1
            assert str(slot[key][1]) == "/XYZ"
            assert slot[key][4] is None
        assert seen == 60
