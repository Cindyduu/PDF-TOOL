"""P3 集成验收（链接）：8 种 fit 类型全部转成承前缩放。

链接侧与书签是同构缺陷（`links.py` 只认 `/XYZ`，其余 7 种记「不适用」跳过），
但链接还多两条必须同时成立的约束：

1. URI 链接没有页面缩放语义，**一个字节都不许动**，且必须仍然可点击（AC-R9-N2）；
2. 豁免按**链接所在页**判断，不是按目标页。

外加一条跨处理器的：书签与链接指向同一个命名目标时，各自拿到自己的复制品，
原件不动——否则改一个会连累另一个。

真实文件回归：`RECIST1.1.pdf` 的 32 条 `/FitR` 链接必须全部转成承前缩放。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name, String

from app.pdf import bookmarks, links
from app.pdf.pipeline import run_pipeline
from tests.fixtures.make_fixtures import (
    add_named_dests,
    add_page,
    attach_annots,
    build_outline,
    dest_xyz,
    make_annot,
    new_pdf,
    save,
)
from tests.pdf_ops import object_signature

REAL_FILE = Path.home() / "Downloads" / "指导文件" / "RECIST1.1.pdf"

FIT_TITLES = ("Fit", "FitH", "FitV", "FitB", "FitBH", "FitBV", "FitR")

URI_TARGET = "https://example.com/n1"


def _link(pdf, index: int, dest) -> Dictionary:
    """造一条指向本文档页面的链接注释；Rect 按序号错开，方便按位置找回来。"""
    top = 620 - 25 * index
    return make_annot(
        pdf,
        "Link",
        (72, top - 20, 300, top),
        appearance=None,
        extra={"Dest": dest, "Border": Array([0, 0, 0])},
    )


def _uri_link(pdf, index: int, target: str = URI_TARGET) -> Dictionary:
    top = 500 - 25 * index
    return make_annot(
        pdf,
        "Link",
        (72, top - 20, 300, top),
        appearance=None,
        extra={
            "A": Dictionary(S=Name("/URI"), URI=String(target)),
            "Border": Array([0, 0, 0]),
        },
    )


def build_source(path: Path) -> Path:
    """9 条页面内链接（8 种 fit + 1 个命名目标）、2 条 URI、1 条豁免页链接。"""
    pdf = new_pdf()
    pages = [add_page(pdf) for _ in range(4)]
    add_named_dests(pdf, {"shared": dest_xyz(pages[1], 1.5)})

    page_dests = [
        ("XYZ", dest_xyz(pages[0], 2.0)),
        ("Fit", Array([pages[1], Name("/Fit")])),
        ("FitH", Array([pages[2], Name("/FitH"), 700])),
        ("FitV", Array([pages[0], Name("/FitV"), 30])),
        ("FitB", Array([pages[1], Name("/FitB")])),
        ("FitBH", Array([pages[2], Name("/FitBH"), 700])),
        ("FitBV", Array([pages[0], Name("/FitBV"), 30])),
        ("FitR", Array([pages[1], Name("/FitR"), 10, 20, 300, 700])),
        ("Named", String("shared")),
    ]
    attach_annots(pdf, pages[0], [_link(pdf, i, dest) for i, (_t, dest) in enumerate(page_dests)])
    attach_annots(pdf, pages[1], [_uri_link(pdf, 0), _uri_link(pdf, 1)])
    # 第 4 页整页豁免，它上面这条链接必须原样保留
    attach_annots(pdf, pages[3], [_link(pdf, 0, dest_xyz(pages[2], 3.0))])

    # 书签也指向同一个命名目标：两个处理器各自复制，互不影响
    build_outline(pdf, [{"title": "Shared bookmark", "dest": String("shared")}])
    return save(pdf, path)


def dests_by_rect(pdf: pikepdf.Pdf) -> dict[tuple[int, float], object]:
    """按 (页码, Rect 上边界) 索引目的地，避免依赖注释顺序、也不会跨页撞键。"""
    found = {}
    for page_number, annot in links.iter_links(pdf):
        slot, key = links.link_dest_slot(annot)
        found[(page_number, round(float(annot.Rect[3])))] = (
            None if slot is None else slot[key]
        )
    return found


def at(page: int, index: int) -> tuple[int, float]:
    return (page, float(620 - 25 * index))


def test_every_link_target_type_becomes_inherit_zoom(tmp_path):
    source = build_source(tmp_path / "src.pdf")
    output = tmp_path / "out.pdf"

    result = run_pipeline(source, output, exempt_pages=(4,))
    item = result.result_for("links_inherit_zoom")

    with pikepdf.open(output) as pdf:
        views = dests_by_rect(pdf)

        for index in range(9):
            array = views[at(1, index)]
            assert str(array[1]) == "/XYZ", f"第 {index} 条链接没有换成 /XYZ"
            assert array[4] is None, f"第 {index} 条链接的缩放位不是承前缩放"

        # 位置语义：该保留的保留，该丢的丢
        assert float(views[at(1, 0)][2]) == 0 and float(views[at(1, 0)][3]) == 800
        assert views[at(1, 2)][2] is None and float(views[at(1, 2)][3]) == 700
        assert float(views[at(1, 3)][2]) == 30 and views[at(1, 3)][3] is None
        assert views[at(1, 1)][2] is None and views[at(1, 1)][3] is None
        assert views[at(1, 7)][2] is None and views[at(1, 7)][3] is None

        # 豁免页那条链接原样：还是 /XYZ 3.0
        exempt = views[at(4, 0)]
        assert str(exempt[1]) == "/XYZ"
        assert float(exempt[4]) == 3.0

        # 命名目标原件仍是 1.5，链接拿到的是复制品
        assert float(pdf.Root.Names.Dests.Names[1][4]) == 1.5

    assert item["status"] == "adjusted"
    assert item["counts"]["checked"] == 12
    assert item["counts"]["modified"] == 9
    assert item["counts"]["not_applicable"] == 2
    assert item["counts"]["exempt"] == 1
    assert item["reason_code"] == "fit_converted"
    assert any("矩形" in warning for warning in item["warnings"])


def test_fit_breakdown_matches_source_and_result(tmp_path):
    source = build_source(tmp_path / "src.pdf")
    result = run_pipeline(source, tmp_path / "out.pdf", exempt_pages=(4,))
    item = result.result_for("links_inherit_zoom")

    assert item["before"]["internal"] == 10
    assert item["before"]["fit_breakdown"] == {
        "/XYZ": 3,  # XYZ 链接 + 命名目标链接 + 豁免页链接
        "/Fit": 1,
        "/FitH": 1,
        "/FitV": 1,
        "/FitB": 1,
        "/FitBH": 1,
        "/FitBV": 1,
        "/FitR": 1,
    }
    assert item["after"]["internal"] == 10
    assert item["after"]["fit_breakdown"] == {"/XYZ": 10}


def test_uri_links_are_untouched_and_still_clickable(tmp_path):
    """URI 链接没有缩放语义：动作字典一个字节不变，仍然指向原网址。"""
    source = build_source(tmp_path / "src.pdf")
    with pikepdf.open(source) as pdf:
        before = [
            object_signature(pdf, annot.A)
            for _page, annot in links.iter_links(pdf)
            if "/A" in annot
        ]
    assert len(before) == 2

    run_pipeline(source, tmp_path / "out.pdf", exempt_pages=(4,))

    with pikepdf.open(tmp_path / "out.pdf") as pdf:
        after = []
        for _page, annot in links.iter_links(pdf):
            if "/A" not in annot:
                continue
            after.append(object_signature(pdf, annot.A))
            assert str(annot.A.S) == "/URI"
            assert str(annot.A.URI) == URI_TARGET
        assert after == before, "URI 链接的动作字典被改动了"


def test_shared_named_target_serves_bookmark_and_link_independently(tmp_path):
    """书签与链接共用一个命名目标：各自复制，原件不动。"""
    source = build_source(tmp_path / "src.pdf")
    run_pipeline(source, tmp_path / "out.pdf", exempt_pages=(4,))

    with pikepdf.open(tmp_path / "out.pdf") as pdf:
        # 原件：命名树里还是 1.5
        assert float(pdf.Root.Names.Dests.Names[1][4]) == 1.5

        # 书签那份
        bookmark_dest = None
        for outline_item in bookmarks.iter_outline_items(pdf):
            slot, key = bookmarks.dest_slot(outline_item)
            if slot is not None:
                bookmark_dest = slot[key]
                break
        assert bookmark_dest is not None
        assert bookmark_dest[4] is None

        # 链接那份
        link_dest = dests_by_rect(pdf)[at(1, 8)]
        assert link_dest[4] is None

        assert bookmark_dest is not link_dest, "两处共享同一个目标对象，改一个会连累另一个"


def test_second_pass_is_idempotent(tmp_path):
    """同样的参数再跑一遍：9 个已符合、1 个仍豁免、一个都不改。"""
    source = build_source(tmp_path / "src.pdf")
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"

    run_pipeline(source, first, exempt_pages=(4,))
    result = run_pipeline(first, second, exempt_pages=(4,))

    item = result.result_for("links_inherit_zoom")
    assert item["counts"]["compliant"] == 9
    assert item["counts"]["exempt"] == 1
    assert item["counts"]["not_applicable"] == 2  # 两条 URI 链接
    assert item["counts"]["modified"] == 0
    # 注：`decide_status` 现有规则是「有 not_applicable 项就不报 unchanged」，
    # 所以这里落到兜底分支。这是既有口径（`test_ops_outcome` 已钉住），
    # 与本次改写无关；幂等的硬指标是 modified == 0。
    assert item["status"] == "not_applicable"
    assert item["reason_code"] == "already_inherit"


def test_single_link_failure_does_not_sink_the_rest(tmp_path, monkeypatch):
    """单条链接写回失败只记一条 failed，其余照常处理（不假成功，也不整批放弃）。"""
    source = build_source(tmp_path / "src.pdf")
    real_write = links.write_dest
    calls = {"n": 0}

    def flaky(slot, key, array, resolved):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("模拟底层对象拒绝写入")
        return real_write(slot, key, array, resolved)

    monkeypatch.setattr(links, "write_dest", flaky)

    result = run_pipeline(source, tmp_path / "out.pdf", exempt_pages=(4,))
    item = result.result_for("links_inherit_zoom")

    assert item["counts"]["failed"] == 1
    assert item["counts"]["modified"] == 8
    assert item["status"] == "failed"
    assert item["reason_code"] == "link_zoom_failed"
    assert any("无法安全拆分共享目标" in warning for warning in item["warnings"])


@pytest.mark.skipif(
    not REAL_FILE.is_file(), reason=f"真实样本不在本机：{REAL_FILE}"
)
def test_real_recist_file_all_fitr_links_become_inherit(tmp_path):
    """真实文件回归：32 条 /FitR 链接 + 1 条 URI，第一版把 32 条全跳过。"""
    source = tmp_path / "recist.pdf"
    shutil.copyfile(REAL_FILE, source)

    result = run_pipeline(source, tmp_path / "out.pdf")
    item = result.result_for("links_inherit_zoom")

    assert item["counts"]["checked"] == 33
    assert item["counts"]["modified"] == 32
    assert item["counts"]["not_applicable"] == 1
    assert item["status"] == "adjusted"

    with pikepdf.open(tmp_path / "out.pdf") as pdf:
        assert len(pdf.pages) == 20, "页数不能因为改链接而变"
        converted = 0
        uri = 0
        for _page, annot in links.iter_links(pdf):
            slot, key = links.link_dest_slot(annot)
            if slot is None:
                uri += 1
                assert str(annot.A.S) == "/URI"
                continue
            converted += 1
            assert str(slot[key][1]) == "/XYZ"
            assert slot[key][4] is None
        assert converted == 32
        assert uri == 1
