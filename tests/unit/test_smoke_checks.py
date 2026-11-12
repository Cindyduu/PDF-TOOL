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

import fitz
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
    link_color: tuple[float, float, float] | None = (0.0, 0.0, 1.0),
) -> Path:
    """按参数造一份「产物」，用来逐条检验校验函数是否抓得住。

    `link_color` 默认是纯蓝：真实处理会给**每一条** Link 注释写
    `/C = [0,0,1]`（`app/pdf/link_color.py`），造产物时不写就成了
    「一个不可能通过校验的样本」，会让「颜色必须是纯蓝」这条断言在
    正常产物上误报。要验反面时传别的颜色，或传 `None` 模拟漏写。
    """
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

        # 链接改色（R25）：每条 Link 注释都要有 `/C = [0,0,1]`
        for page in pdf.pages:
            for annot in page.get("/Annots", []):
                if not isinstance(annot, Dictionary):
                    continue
                if str(annot.get("/Subtype", "")) != "/Link":
                    continue
                if link_color is None:
                    if "/C" in annot:
                        del annot["/C"]
                else:
                    annot[Name("/C")] = Array(list(link_color))

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
# 链接颜色（R25）：产物说了算
# --------------------------------------------------------------------------- #


def test_wrong_link_color_is_reported(tmp_path, smoke):
    """链接还是别的颜色，等于 R25 没生效，必须点名。

    这一条对应 AC-R25-N1：产物里每一条 Link 的 `/C` 都必须是 `[0, 0, 1]`。
    只验「链接还能点」是不够的——颜色没改的话，用户看到的和没处理一样。
    """
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", link_color=(1.0, 0.0, 0.0))

    problems = smoke.check_output(source, output, exempt=[])

    assert_problem(problems, "链接颜色不是 [0, 0, 1]")
    # 两条链接都没改色，所以两条都要报；只报一条说明循环提前退出了
    assert len(problems) == 2, problems


def test_missing_link_color_is_reported(tmp_path, smoke):
    """连 `/C` 都没有：阅读器按默认黑色画，同样算没改色。"""
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", link_color=None)

    problems = smoke.check_output(source, output, exempt=[])

    assert_problem(problems, "链接颜色不是 [0, 0, 1]")


def test_link_count_change_is_reported(tmp_path, smoke):
    """改色只该动 `/C`，链接对象一个都不能少（AC-R25-N4）。"""
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf")

    with pikepdf.open(output) as pdf:
        kept = [
            annot
            for annot in pdf.pages[0].get("/Annots", [])
            if str(annot.get("/Subtype", "")) == "/Link"
        ][:1]
        pdf.pages[0].Annots = Array(kept)
        pdf.save(tmp_path / "fewer.pdf", linearize=True)

    problems = smoke.check_output(source, tmp_path / "fewer.pdf", exempt=[])

    assert_problem(problems, "链接数变了")


# --------------------------------------------------------------------------- #
# 注释展平：豁免页保留不算缺陷，没勾扁平化也不能"替用户展平"
# --------------------------------------------------------------------------- #


def test_exempt_page_annotation_is_not_a_defect(tmp_path, smoke):
    """豁免页本来就不展平（`ctx.is_exempt`），不能报成残留。"""
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", add_square=True)

    problems = smoke.check_output(source, output, exempt=[1])

    assert not any("未展平" in problem for problem in problems), problems


def test_no_flatten_must_keep_annotations(tmp_path, smoke):
    """没勾扁平化时，注释应当还在；一个不剩说明被误改了。"""
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf", add_square=True)

    problems = smoke.check_output(source, output, exempt=[], flatten=False)

    assert problems == [], f"没勾扁平化却被判成缺陷：{problems}"


def with_square(source: Path, path: Path) -> Path:
    """复制一份源文件并挂一个非链接注释（`/Square`）。

    「没勾扁平化，注释却没了」这条要成立，源文件里必须**本来就有**注释。
    源文件本来没有注释的文档很常见，那种情况不算被误改。
    """
    with pikepdf.open(source) as pdf:
        page = pdf.pages[0]
        annots = list(page.get("/Annots", []))
        annots.append(
            Dictionary(
                Type=Name("/Annot"),
                Subtype=Name("/Square"),
                Rect=Array([20, 20, 120, 120]),
            )
        )
        page.Annots = Array(annots)
        pdf.save(path, linearize=True)
    return path


def test_no_flatten_but_nothing_left_is_reported(tmp_path, smoke):
    """源文件有注释、没勾扁平化，产物却一个不剩：说明被误改了。"""
    plain = build_source(tmp_path / "src.pdf")
    source = with_square(plain, tmp_path / "src_sq.pdf")
    # 产物从「没有 Square 的那份」造出来，等于模拟"不该展平却展平了"
    output = build_output(plain, tmp_path / "out.pdf")

    problems = smoke.check_output(source, output, exempt=[], flatten=False)

    assert_problem(problems, "产物被误改")
    assert len(problems) == 1, problems


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


# --------------------------------------------------------------------------- #
# 链接文字颜色（R35）
# --------------------------------------------------------------------------- #

#: 只挑 R35 相关的问题，避免被「没线性化」「DocInfo 还在」这类无关项干扰。
def _r35(problems: list[str]) -> list[str]:
    return [problem for problem in problems if "R35" in problem]


def _text_link_pdf(path: Path, color: tuple[float, float, float]) -> Path:
    """造一份「一段文字 + 一个盖住它的页内链接」的最小 PDF。

    用 fitz 造而不是 pikepdf：这里要的是**渲染层能读到的 span 颜色**，
    手写内容流很容易写出「pikepdf 解析得动、MuPDF 却不认」的字节。
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 700), "Chapter One", fontsize=12, color=color)
    page.insert_link(
        {
            "kind": fitz.LINK_GOTO,
            "from": fitz.Rect(66, 688, 220, 706),
            "page": 0,
            "to": fitz.Point(72, 700),
        }
    )
    doc.save(str(path))
    doc.close()
    return path


def test_link_covered_black_text_is_reported(tmp_path, smoke):
    """`/C` 是蓝的、文字还是黑的——正是 v4.0 的现场，必须报出来。

    这条挡的是「只查 `/C` 就宣布通过」：用户看不到链接边框，颜色改没改
    对他而言完全没区别。
    """
    source = _text_link_pdf(tmp_path / "src.pdf", (0.0, 0.0, 0.0))
    output = _text_link_pdf(tmp_path / "out.pdf", (0.0, 0.0, 0.0))

    problems = _r35(smoke.check_output(source, output, exempt=[]))

    assert problems, "链接覆盖的文字仍是黑色，冒烟必须报 R35"
    assert "一段都没变蓝" in problems[0]
    assert "Chapter" in problems[0], "报错要指出是哪段文字"


def test_link_covered_blue_text_passes(tmp_path, smoke):
    """文字真的变蓝时不报——否则这条断言只会制造噪音。"""
    source = _text_link_pdf(tmp_path / "src.pdf", (0.0, 0.0, 0.0))
    output = _text_link_pdf(tmp_path / "out.pdf", (0.0, 0.0, 1.0))

    assert not _r35(smoke.check_output(source, output, exempt=[]))


def test_links_without_any_text_do_not_trip_the_text_color_check(tmp_path, smoke):
    """有链接但整页没有文字层时，覆盖段数为 0，不能算失败。

    纯图 PDF 加链接是常见形态（扫描件上加的目录跳转）。把「一段蓝色文字都
    没有」当失败，会让这类文件永远过不了冒烟。
    """
    source = build_source(tmp_path / "src.pdf")
    output = build_output(source, tmp_path / "out.pdf")

    covered, blue, samples = smoke._link_text_color(output)

    assert (covered, blue, samples) == (0, 0, [])
    assert not _r35(smoke.check_output(source, output, exempt=[]))


def test_link_text_color_counts_only_spans_inside_link_rects(tmp_path, smoke):
    """蓝色文字落在链接矩形外面时不能充数。

    源文件常有蓝色的标题或强调句；如果只数「文件里有没有蓝色文字」，那种
    文件会把「链接一个字都没改」判成通过。
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    # 蓝色文字在页面顶部，链接矩形在底部，两者不相交
    page.insert_text((72, 100), "Blue Heading", fontsize=12, color=(0.0, 0.0, 1.0))
    page.insert_text((72, 700), "Black Body", fontsize=12, color=(0.0, 0.0, 0.0))
    page.insert_link(
        {
            "kind": fitz.LINK_GOTO,
            "from": fitz.Rect(66, 688, 220, 706),
            "page": 0,
            "to": fitz.Point(72, 100),
        }
    )
    path = tmp_path / "mixed.pdf"
    doc.save(str(path))
    doc.close()

    covered, blue, _ = smoke._link_text_color(path)

    assert covered == 1, "只有链接矩形里那一段算数"
    assert blue == 0, "蓝色标题在矩形外面，不能算成已着色"
