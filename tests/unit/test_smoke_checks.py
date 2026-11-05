"""冒烟脚本的独立校验逻辑：`check_output()` 必须真的会报错。

`live_smoke.py` 的价值全在「不信应用报告、自己重新打开产物查」。
如果校验函数只会返回空列表，那脚本永远输出「✓ 全部检查通过」，
等于给用户一个假的安心——这种测试通过比不测更糟。

所以这里逐条造「坏产物」，确认每一种缺陷都能被点名：

- 书签目的地还是 `/Fit*`（P3 之前的典型症状）
- 书签缩放位还是具体数字，不是 null
- 页数变了
- 注释没展平
- DocInfo / XMP 没删
- 没线性化
- 导览标签没设成「书签面板和页面」
- 命名目标式链接（`/Dest` 直接挂名字）没转换
- 命名目标在名字树里找不到

外加两条「不该报」的：URI 外链不归本项管、豁免页应当跳过。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name, Stream, String

from tests.fixtures.make_fixtures import (
    add_named_dests,
    add_page,
    build_outline,
    dest_xyz,
    new_pdf,
    save,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live_smoke.py"


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("live_smoke_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 造样本
# --------------------------------------------------------------------------- #


def _outline_nodes(pdf: pikepdf.Pdf):
    outlines = pdf.Root.get("/Outlines")
    if not isinstance(outlines, Dictionary):
        return
    stack = [outlines.get("/First")]
    while stack:
        node = stack.pop()
        while node is not None and isinstance(node, Dictionary):
            yield node
            child = node.get("/First")
            if isinstance(child, Dictionary):
                stack.append(child)
            node = node.get("/Next")


def _slot(node):
    if "/Dest" in node:
        return node, "/Dest"
    action = node.get("/A")
    if isinstance(action, Dictionary) and str(action.get("/S", "")) == "/GoTo":
        if "/D" in action:
            return action, "/D"
    return None, None


def build_source(path: Path) -> Path:
    """源文件：一个 `/FitH` 书签、一个带显式缩放的书签、一条 URI 外链、
    一条用命名目标（`/Dest` 直接挂名字）的页面内链接。

    最后那种写法是真实文件 RECIST 的形状：链接目的地不是 `/A /GoTo`，
    而是注释自己的 `/Dest` 指向名字树里的 `lbib1`。校验只认 `/A` 的话
    会一条都查不到却报「通过」。
    """
    pdf = new_pdf()
    pages = [add_page(pdf) for _ in range(2)]
    build_outline(
        pdf,
        [
            {"title": "FitH", "dest": Array([pages[0], Name("/FitH"), 700])},
            {"title": "Zoom", "dest": dest_xyz(pages[1], 2.0)},
        ],
    )
    add_named_dests(pdf, {"lbib1": Array([pages[1], Name("/FitR"), 0, 0, 595, 842])})
    pdf.pages[0].Annots = Array(
        [
            Dictionary(
                Type=Name("/Annot"),
                Subtype=Name("/Link"),
                Rect=Array([0, 0, 100, 40]),
                A=Dictionary(S=Name("/URI"), URI=String("https://example.com")),
            ),
            Dictionary(
                Type=Name("/Annot"),
                Subtype=Name("/Link"),
                Rect=Array([0, 60, 100, 100]),
                Dest=String("lbib1"),
            ),
        ]
    )
    return save(pdf, path)


def build_output(
    source: Path,
    path: Path,
    *,
    fit: str = "/XYZ",
    zoom: float | None = None,
    page_mode: str = "/UseOutlines",
    add_square: bool = False,
    add_page_extra: bool = False,
    keep_info: bool = False,
    keep_metadata: bool = False,
    linearize: bool = True,
    convert_links: bool = True,
    broken_link: bool = False,
) -> Path:
    """按参数造一份「产物」，用来逐条检验校验函数是否抓得住。"""
    with pikepdf.open(source) as pdf:
        if add_page_extra:
            pdf.add_blank_page(page_size=(595, 842))
        for node in _outline_nodes(pdf):
            slot, key = _slot(node)
            if slot is None:
                continue
            dest = slot[key]
            slot[key] = Array([dest[0], Name(fit), None, 700, zoom])

        if convert_links:
            # 命名目标式的链接：把名字换成数组，模拟应用的处理结果
            for page in pdf.pages:
                for annot in page.get("/Annots", []):
                    if str(annot.get("/Subtype", "")) != "/Link":
                        continue
                    if "/Dest" in annot:
                        annot[Name("/Dest")] = Array(
                            [pdf.pages[1].obj, Name("/XYZ"), None, 700, zoom]
                        )

        if broken_link:
            # 指向名字树里不存在的名字：解析不出来必须报，不能当成「外链」静默跳过
            for page in pdf.pages:
                for annot in page.get("/Annots", []):
                    if str(annot.get("/Subtype", "")) == "/Link" and "/Dest" in annot:
                        annot[Name("/Dest")] = String("missing-target")

        pdf.Root.PageMode = Name(page_mode)
        if add_square:
            # 非链接注释：这种才是应该被展平的
            page = pdf.pages[0]
            existing = list(page.get("/Annots", []))
            existing.append(
                Dictionary(
                    Type=Name("/Annot"),
                    Subtype=Name("/Square"),
                    Rect=Array([20, 20, 120, 120]),
                )
            )
            page.Annots = Array(existing)
        if keep_info:
            pdf.docinfo["/Producer"] = "pikepdf"
        elif "/Info" in pdf.trailer:
            del pdf.trailer["/Info"]
        if keep_metadata:
            pdf.Root.Metadata = Stream(
                pdf, b"<x:xmpmeta/>", Type=Name("/Metadata"), Subtype=Name("/XML")
            )
        elif "/Metadata" in pdf.Root:
            del pdf.Root["/Metadata"]
        pdf.save(path, linearize=linearize)
    return path


def assert_problem(problems: list[str], needle: str) -> None:
    assert any(needle in problem for problem in problems), (
        f"没报出「{needle}」；实际报的是：{problems}"
    )


# --------------------------------------------------------------------------- #
# 正常产物：一条都不该报
# --------------------------------------------------------------------------- #


def test_clean_output_reports_nothing(tmp_path, smoke):
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf")

    problems = smoke.check_output(source, output, exempt=[])
    assert problems == [], f"正常产物被误报：{problems}"


def test_kept_link_annotation_is_not_a_defect(tmp_path, smoke):
    """链接注释必须留着，否则书签和链接就点不动了。

    这一条是脚本第一版的真 bug：把所有注释都算成「未展平」，
    结果正常产物（链接被有意保留）被判成失败。
    """
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf")

    problems = smoke.check_output(source, output, exempt=[])
    assert problems == [], f"保留下来的链接注释被误判成未展平：{problems}"


# --------------------------------------------------------------------------- #
# 命名目标式链接：必须真的查得到，不能「一条没查却报通过」
# --------------------------------------------------------------------------- #


def test_named_dest_link_is_actually_checked(tmp_path, smoke, capsys):
    """RECIST 那种形状：链接目的地是注释自己的 `/Dest`，值是名字 `lbib1`。

    第一版校验只认 `/A /GoTo /D`，遇到这种链接一条都取不到，
    于是「受检 0 条」照样打印「✓ 全部检查通过」——假的安心。
    这里连受检条数一起断言，防止再退回去。
    """
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf")

    problems = smoke.check_output(source, output, exempt=[])
    printed = capsys.readouterr().out

    assert problems == [], f"正常产物被误报：{problems}"
    assert "产物受检 1 条" in printed, f"命名目标式链接没被查：{printed}"
    assert "另有 1 条外链跳过" in printed, f"外链计数不对：{printed}"


def test_named_dest_link_not_converted_is_reported(tmp_path, smoke):
    """名字树里的目的地还是 `/FitR`，等于链接没改，必须报出来。"""
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", convert_links=False)

    problems = smoke.check_output(source, output, exempt=[])
    assert_problem(problems, "链接目的地类型仍是 /FitR")
    assert len(problems) == 1, f"只该报这一条，实际：{problems}"


def test_unresolvable_named_dest_is_reported(tmp_path, smoke):
    """名字树里找不到的名字：属于「本该是页面跳转却解析不出」，
    必须报，不能当成外链静默跳过。"""
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", broken_link=True)

    problems = smoke.check_output(source, output, exempt=[])
    assert_problem(problems, "在名字树里找不到")


# --------------------------------------------------------------------------- #
# 各种坏产物：必须点名
# --------------------------------------------------------------------------- #


def test_unchanged_fit_destination_is_reported(tmp_path, smoke):
    """P3 之前的典型症状：目的地还是 /FitH，等于一条都没改。"""
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", fit="/FitH")

    problems = smoke.check_output(source, output, exempt=[])
    assert_problem(problems, "仍是 /FitH")


def test_explicit_zoom_is_reported(tmp_path, smoke):
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", zoom=1.5)

    problems = smoke.check_output(source, output, exempt=[])
    assert_problem(problems, "缩放位不是 null")


def test_page_count_change_is_reported(tmp_path, smoke):
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", add_page_extra=True)

    problems = smoke.check_output(source, output, exempt=[])
    assert_problem(problems, "页数变了")


def test_leftover_non_link_annotations_are_reported(tmp_path, smoke):
    """非链接注释没展平才是缺陷。"""
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", add_square=True)

    problems = smoke.check_output(source, output, exempt=[])
    assert_problem(problems, "非链接注释未展平")


def test_kept_docinfo_is_reported(tmp_path, smoke):
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", keep_info=True)

    problems = smoke.check_output(source, output, exempt=[])
    assert_problem(problems, "DocInfo")


def test_kept_metadata_stream_is_reported(tmp_path, smoke):
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", keep_metadata=True)

    problems = smoke.check_output(source, output, exempt=[])
    assert_problem(problems, "XMP")


def test_missing_linearization_is_reported(tmp_path, smoke):
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", linearize=False)

    problems = smoke.check_output(source, output, exempt=[])
    assert_problem(problems, "未线性化")


def test_wrong_page_mode_is_reported(tmp_path, smoke):
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", page_mode="/UseNone")

    problems = smoke.check_output(source, output, exempt=[])
    assert_problem(problems, "书签面板和页面")


# --------------------------------------------------------------------------- #
# 豁免页
# --------------------------------------------------------------------------- #


def test_exempt_page_is_skipped(tmp_path, smoke):
    """豁免页上的书签本来就该保持原样，校验不能把它算成缺陷。"""
    source = build_source(tmp_path / "src.pdf")
    # 两个书签分别指向第 1、2 页；只豁免第 1 页，第 2 页那个仍要报
    output = build_output(source, tmp_path / "out.pdf", fit="/FitH")

    all_problems = smoke.check_output(source, output, exempt=[])
    only_page_one = smoke.check_output(source, output, exempt=[1])

    assert len(all_problems) == 2, all_problems
    assert len(only_page_one) == 1, only_page_one
    assert_problem(only_page_one, "仍是 /FitH")
