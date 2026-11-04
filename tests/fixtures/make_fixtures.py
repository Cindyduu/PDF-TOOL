"""生成阶段 B 的 PDF 样本库。

样本全部用 pikepdf 从零构造，保证结构可控、可重复生成、不依赖外部下载。
运行方式：

    python -m tests.fixtures.make_fixtures          # 生成到 tests/fixtures/pdfs/
    python -m tests.fixtures.make_fixtures --out D:/tmp/pdfs

每个样本都对应 PRD 中至少一条验收条目，命名见 SAMPLE_SPECS。
"""

from __future__ import annotations

import argparse
import io
import shutil
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, Stream, String

FIXTURE_DIR = Path(__file__).resolve().parent / "pdfs"

PAGE_W, PAGE_H = 595, 842

HELVETICA = Dictionary(
    Type=Name("/Font"),
    Subtype=Name("/Type1"),
    BaseFont=Name("/Helvetica"),
    Encoding=Name("/WinAnsiEncoding"),
)

TEXT_CONTENT = b"""BT /F1 18 Tf 72 760 Td (Quarterly Report) Tj ET
BT /F1 12 Tf 72 720 Td (This document is a synthetic fixture.) Tj ET
"""


# --------------------------------------------------------------------------- #
# 基础构造工具
# --------------------------------------------------------------------------- #


def new_pdf() -> pikepdf.Pdf:
    pdf = pikepdf.Pdf.new()
    pdf.Root.Type = Name("/Catalog")
    return pdf


def add_page(
    pdf: pikepdf.Pdf,
    content: bytes = TEXT_CONTENT,
    rotate: int = 0,
    mediabox: tuple[int, int, int, int] | None = None,
) -> pikepdf.Object:
    """追加一页并返回页对象（pikepdf.Object）。"""
    size = (PAGE_W, PAGE_H)
    pdf.add_blank_page(page_size=size)
    page = pdf.pages[-1].obj
    page.Contents = Stream(pdf, content)
    page.Resources = Dictionary(Font=Dictionary(F1=HELVETICA))
    if rotate:
        page.Rotate = rotate
    if mediabox is not None:
        page.MediaBox = Array(list(mediabox))
    return page


def form_xobject(
    pdf: pikepdf.Pdf,
    ops: bytes,
    bbox: tuple[float, float, float, float] = (0, 0, 100, 100),
) -> Stream:
    # 外观流自带字体资源：展平时把外观画进页面内容流后，文字仍然能渲染出来。
    # 否则"展平前后像素对比"会因为两边都缺字体而失去区分度。
    return Stream(
        pdf,
        ops,
        Type=Name("/XObject"),
        Subtype=Name("/Form"),
        BBox=Array(list(bbox)),
        Resources=Dictionary(Font=Dictionary(F1=HELVETICA)),
    )


def make_annot(
    pdf: pikepdf.Pdf,
    subtype: str,
    rect: tuple[float, float, float, float],
    appearance: bytes | None = b"1 0 0 RG 2 w 4 4 92 92 re S",
    extra: dict | None = None,
) -> Dictionary:
    annot = Dictionary(
        Type=Name("/Annot"),
        Subtype=Name("/" + subtype),
        Rect=Array(list(rect)),
        P=pdf.pages[0].obj,
        F=4,
    )
    if appearance is not None:
        annot.AP = Dictionary(
            N=form_xobject(pdf, appearance, (0, 0, 100, 100))
        )
    if extra:
        for key, value in extra.items():
            annot[Name("/" + key)] = value
    return annot


def attach_annots(pdf: pikepdf.Pdf, page: pikepdf.Object, annots: list) -> None:
    arr = Array()
    for annot in annots:
        arr.append(annot)
    page.Annots = arr


def dest_xyz(page: pikepdf.Object, zoom: float | None, top: float = 800) -> Array:
    return Array([page, Name("/XYZ"), 0, top, zoom])


def build_outline(pdf: pikepdf.Pdf, items: list[dict]) -> None:
    """items: [{'title': str, 'dest': Array|Dictionary|None, 'action': Dictionary|None}]

    注意：书签节点与 /Outlines 必须都是**间接对象**。Parent/First/Next 互相引用，
    若保持直接对象，qpdf 在序列化时会顺着直接对象图无限展开，直接把进程打爆
    （Windows 上表现为 stack overflow 而非 Python 异常）。
    """
    if not items:
        return
    outlines = pdf.make_indirect(Dictionary(Type=Name("/Outlines")))
    nodes: list[pikepdf.Object] = []
    for spec in items:
        node = Dictionary(Title=String(spec["title"]))
        if spec.get("dest") is not None:
            node.Dest = spec["dest"]
        if spec.get("action") is not None:
            node.A = spec["action"]
        nodes.append(pdf.make_indirect(node))

    for index, node in enumerate(nodes):
        node.Parent = outlines
        if index > 0:
            node.Prev = nodes[index - 1]
            nodes[index - 1].Next = node

    outlines.First = nodes[0]
    outlines.Last = nodes[-1]
    outlines.Count = len(nodes)
    pdf.Root.Outlines = outlines
    pdf.Root.PageMode = Name("/UseNone")


def add_named_dests(pdf: pikepdf.Pdf, mapping: dict[str, Array]) -> None:
    names = Array()
    for key in sorted(mapping):
        names.append(String(key))
        names.append(mapping[key])
    pdf.Root.Names = Dictionary(
        Dests=Dictionary(Names=names),
    )


def save(pdf: pikepdf.Pdf, path: Path, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(path, **kwargs)
    pdf.close()
    return path


# --------------------------------------------------------------------------- #
# 各样本
# --------------------------------------------------------------------------- #


def make_zoom_explicit(path: Path) -> Path:
    """书签与链接都带显式缩放倍数（需要被改为承前缩放）。"""
    pdf = new_pdf()
    p1 = add_page(pdf)
    p2 = add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Chapter Two) Tj ET")
    p3 = add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Chapter Three) Tj ET")
    build_outline(
        pdf,
        [
            {"title": "Chapter 1", "dest": dest_xyz(p1, 2.0)},
            {"title": "Chapter 2", "dest": dest_xyz(p2, 1.5)},
            {"title": "Chapter 3", "dest": dest_xyz(p3, 0.75)},
        ],
    )
    link1 = make_annot(
        pdf,
        "Link",
        (72, 600, 300, 620),
        appearance=None,
        extra={"Dest": dest_xyz(p2, 3.0), "Border": Array([0, 0, 0])},
    )
    link2 = make_annot(
        pdf,
        "Link",
        (72, 560, 300, 580),
        appearance=None,
        extra={"Dest": dest_xyz(p3, 1.25), "Border": Array([0, 0, 0])},
    )
    attach_annots(pdf, p1, [link1, link2])
    return save(pdf, path)


def make_zoom_inherit(path: Path) -> Path:
    """书签与链接已经是承前缩放（应全部记为无需修改）。"""
    pdf = new_pdf()
    p1 = add_page(pdf)
    p2 = add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Second) Tj ET")
    build_outline(
        pdf,
        [
            {"title": "One", "dest": dest_xyz(p1, None)},
            {"title": "Two", "dest": dest_xyz(p2, None)},
        ],
    )
    link = make_annot(
        pdf,
        "Link",
        (72, 600, 300, 620),
        appearance=None,
        extra={"Dest": dest_xyz(p2, None), "Border": Array([0, 0, 0])},
    )
    attach_annots(pdf, p1, [link])
    return save(pdf, path)


def make_named_dest_shared(path: Path) -> Path:
    """同一个命名目标被两处引用，而这两处的豁免状态相反。

    结构：
      /Names/Dests/sharedTarget -> 第 2 页 /XYZ zoom 2.5   （间接对象，唯一一份）
      - 书签「Named shared」用它，目标页第 2 页（非豁免）→ 需要改
      - 第 4 页（豁免页）上的链接也用它 → 链接豁免，一个字节都不许变

    正确处理方式只能是「复制目标对象再改」：原地改写会让那条豁免链接的
    实际缩放跟着变，等于偷偷改了豁免对象。用来验证「不得跨范围误改」。
    """
    pdf = new_pdf()
    p1 = add_page(pdf)
    p2 = add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Target A) Tj ET")
    p3 = add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Middle) Tj ET")
    p4 = add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Reference 2.7.5) Tj ET")
    # 共享目标必须是**间接对象**，否则 /Names 里存的和书签里存的各是各的副本，
    # 「共享」这件事根本不存在，测试也就测不出误改。
    shared = pdf.make_indirect(dest_xyz(p2, 2.5))
    add_named_dests(
        pdf,
        {"sharedTarget": shared, "otherTarget": pdf.make_indirect(dest_xyz(p3, 1.5))},
    )
    build_outline(
        pdf,
        [
            {"title": "Non exempt", "dest": dest_xyz(p2, 2.5)},
            {"title": "Exempt chapter", "dest": dest_xyz(p4, 2.5)},
            {"title": "Named shared", "dest": String("sharedTarget")},
        ],
    )
    # 豁免页上的链接复用同一个命名目标：与上面的书签共享同一份目标对象
    exempt_link = make_annot(
        pdf,
        "Link",
        (72, 600, 300, 620),
        appearance=None,
        extra={"Dest": String("sharedTarget"), "Border": Array([0, 0, 0])},
    )
    exempt_link.P = p4
    attach_annots(pdf, p4, [exempt_link])
    return save(pdf, path)


def make_links_mixed(path: Path) -> Path:
    """内部 GoTo、URI、命名目标三种链接混合。"""
    pdf = new_pdf()
    p1 = add_page(pdf)
    p2 = add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Page Two) Tj ET")
    add_named_dests(pdf, {"goNamed": dest_xyz(p2, 1.8)})
    internal = make_annot(
        pdf,
        "Link",
        (72, 640, 300, 660),
        appearance=None,
        extra={"Dest": dest_xyz(p2, 2.0), "Border": Array([0, 0, 0])},
    )
    uri = make_annot(
        pdf,
        "Link",
        (72, 600, 300, 620),
        appearance=None,
        extra={
            "A": Dictionary(S=Name("/URI"), URI=String("https://example.com")),
            "Border": Array([0, 0, 0]),
        },
    )
    named = make_annot(
        pdf,
        "Link",
        (72, 560, 300, 580),
        appearance=None,
        extra={"Dest": String("goNamed"), "Border": Array([0, 0, 0])},
    )
    attach_annots(pdf, p1, [internal, uri, named])
    return save(pdf, path)


def make_annots_mixed(path: Path) -> Path:
    """各种注释混合，含旋转页、无外观注释、隐藏注释、Widget、Popup、Link。"""
    pdf = new_pdf()
    p1 = add_page(pdf)
    p2 = add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Rotated page) Tj ET", rotate=90)

    highlight = make_annot(
        pdf,
        "Highlight",
        (72, 700, 300, 720),
        appearance=b"1 1 0 rg 0 0 100 20 re f",
        extra={"QuadPoints": Array([72, 720, 300, 720, 72, 700, 300, 700])},
    )
    freetext = make_annot(
        pdf,
        "FreeText",
        (72, 640, 300, 690),
        appearance=b"0 0 1 RG 1 w 0 0 100 50 re S BT /F1 10 Tf 4 20 Td (Note) Tj ET",
        extra={"DA": String("/F1 10 Tf 0 0 1 rg"), "Contents": String("Reviewer note")},
    )
    stamp = make_annot(
        pdf,
        "Stamp",
        (330, 640, 500, 700),
        appearance=b"1 0 0 RG 3 w 0 0 100 60 re S",
        extra={"Name": Name("/Approved")},
    )
    no_appearance = make_annot(
        pdf,
        "Square",
        (72, 560, 300, 600),
        appearance=None,
        extra={"IC": Array([1, 0, 0]), "C": Array([1, 0, 0])},
    )
    hidden = make_annot(
        pdf,
        "Square",
        (72, 500, 300, 540),
        appearance=b"0 1 0 RG 2 w 0 0 100 40 re S",
        extra={"F": 2},
    )
    link = make_annot(
        pdf,
        "Link",
        (72, 460, 300, 480),
        appearance=None,
        extra={
            "A": Dictionary(S=Name("/URI"), URI=String("https://example.com")),
            "Border": Array([0, 0, 0]),
        },
    )
    popup = make_annot(
        pdf,
        "Popup",
        (330, 560, 520, 640),
        appearance=None,
        extra={"Open": False},
    )

    widget = make_annot(
        pdf,
        "Widget",
        (72, 380, 300, 410),
        appearance=b"0 0 0 RG 1 w 0 0 100 30 re S",
        extra={"FT": Name("/Tx"), "T": String("fieldA"), "Ff": 0, "V": String("x")},
    )
    # 同一个注释既在 Annots 又在 AcroForm/Fields 里，必须是同一个间接对象
    widget = pdf.make_indirect(widget)
    widget.F = 4
    widget.P = p1

    attach_annots(pdf, p1, [highlight, freetext, stamp, no_appearance, hidden, link, popup, widget])

    rot_highlight = make_annot(
        pdf,
        "Highlight",
        (100, 400, 400, 440),
        appearance=b"0 1 1 rg 0 0 100 20 re f",
        extra={"QuadPoints": Array([100, 440, 400, 440, 100, 400, 400, 400])},
    )
    rot_highlight.P = p2
    attach_annots(pdf, p2, [rot_highlight])

    pdf.Root.AcroForm = Dictionary(
        Fields=Array([widget]),
        NeedAppearances=False,
    )
    pdf.Root.PageLayout = Name("/TwoColumnLeft")
    pdf.Root.PageMode = Name("/UseNone")
    pdf.Root.OpenAction = dest_xyz(p1, 2.0)
    return save(pdf, path)


def make_meta_full(path: Path) -> Path:
    """含 DocInfo 与 XMP 元数据。"""
    pdf = new_pdf()
    add_page(pdf)
    with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
        meta["dc:title"] = "Quarterly Report"
        meta["dc:creator"] = ["Alice"]
        meta["pdf:Keywords"] = "internal, draft"
        meta["xmp:CreatorTool"] = "fixture-generator"
    pdf.docinfo = pdf.make_indirect(
        Dictionary(
            Title=String("Quarterly Report"),
            Author=String("Alice"),
            Subject=String("Draft"),
            Keywords=String("internal, draft"),
            Creator=String("fixture-generator"),
            Producer=String("pikepdf"),
        )
    )
    return save(pdf, path)


def make_meta_clean(path: Path) -> Path:
    pdf = new_pdf()
    add_page(pdf)
    pdf.docinfo = pdf.make_indirect(Dictionary())
    return save(pdf, path, object_stream_mode=pikepdf.ObjectStreamMode.disable)


def _build_tagged(pdf: pikepdf.Pdf, page: pikepdf.Object) -> None:
    """在给定页面上写入 MCID 并构造真实结构树。

    结构树是 P/K 双向引用的图，所有节点必须是间接对象；直接对象会形成
    直接对象环，qpdf 序列化时会无限展开并让进程直接崩溃。
    """
    content = (
        b"/P <</MCID 0>> BDC BT /F1 18 Tf 72 760 Td (Tagged Heading) Tj ET EMC\n"
        b"/P <</MCID 1>> BDC BT /F1 12 Tf 72 720 Td (Tagged paragraph body.) Tj ET EMC\n"
    )
    page.Contents = Stream(pdf, content)
    page.StructParents = 0

    root = pdf.make_indirect(Dictionary(Type=Name("/StructTreeRoot")))
    doc_elem = pdf.make_indirect(
        Dictionary(Type=Name("/StructElem"), S=Name("/Document"), P=root)
    )
    p_elem0 = pdf.make_indirect(
        Dictionary(Type=Name("/StructElem"), S=Name("/P"), P=doc_elem, Pg=page)
    )
    p_elem1 = pdf.make_indirect(
        Dictionary(Type=Name("/StructElem"), S=Name("/P"), P=doc_elem, Pg=page)
    )
    doc_elem.K = Array([p_elem0, p_elem1])
    root.K = Array([doc_elem])

    # ParentTree：StructParents 0 -> [p_elem0, p_elem1]
    root.ParentTree = pdf.make_indirect(
        Dictionary(Nums=Array([0, Array([p_elem0, p_elem1])]))
    )
    root.ParentTreeNextKey = 1
    pdf.Root.StructTreeRoot = root
    pdf.Root.MarkInfo = Dictionary(Marked=True, Suspects=False)
    pdf.Root.Lang = String("en-US")


def make_tagged_real(path: Path) -> Path:
    pdf = new_pdf()
    page = add_page(pdf)
    _build_tagged(pdf, page)
    return save(pdf, path)


def make_markinfo_only(path: Path) -> Path:
    """只有 MarkInfo，没有真实结构树——必须被识别为未加标签。"""
    pdf = new_pdf()
    add_page(pdf)
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    pdf.Root.Lang = String("en-US")
    return save(pdf, path)


def make_untagged(path: Path) -> Path:
    pdf = new_pdf()
    add_page(pdf)
    return save(pdf, path)


def make_broken_struct(path: Path) -> Path:
    """结构树存在但 ParentTree 引用指向不存在的对象。"""
    pdf = new_pdf()
    page = add_page(pdf)
    page.StructParents = 0
    root = pdf.make_indirect(Dictionary(Type=Name("/StructTreeRoot")))
    doc_elem = pdf.make_indirect(
        Dictionary(Type=Name("/StructElem"), S=Name("/Document"), P=root)
    )
    root.K = Array([doc_elem])
    # ParentTree 的 Nums 只有键没有值，引用不一致
    root.ParentTree = pdf.make_indirect(Dictionary(Nums=Array([0])))
    pdf.Root.StructTreeRoot = root
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    return save(pdf, path)


def make_encrypted(path: Path) -> Path:
    pdf = new_pdf()
    add_page(pdf)
    return save(
        pdf,
        path,
        encryption=pikepdf.Encryption(owner="owner-pass", user="user-pass", R=6),
    )


def make_signed(path: Path) -> Path:
    """带签名域与签名值的 PDF（用于「默认拒绝处理」的判定）。"""
    pdf = new_pdf()
    page = add_page(pdf)
    widget = make_annot(
        pdf,
        "Widget",
        (330, 100, 520, 160),
        appearance=b"0 0 0 RG 1 w 0 0 100 60 re S",
        extra={
            "FT": Name("/Sig"),
            "T": String("Signature1"),
            "Ff": 1,
            "V": Dictionary(
                Type=Name("/Sig"),
                Filter=Name("/Adobe.PPKLite"),
                SubFilter=Name("/adbe.pkcs7.detached"),
                ByteRange=Array([0, 100, 200, 100]),
                Contents=String("0" * 64),
                M=String("D:20240101000000+08'00'"),
            ),
        },
    )
    widget.F = 132
    widget.P = page
    widget = pdf.make_indirect(widget)
    attach_annots(pdf, page, [widget])
    pdf.Root.AcroForm = Dictionary(
        Fields=Array([widget]),
        SigFlags=3,
    )
    return save(pdf, path)


def make_corrupt(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.7\n" + b"\x00\x11\x22\x33garbage" * 40)
    return path


def make_exempt_mix(path: Path) -> Path:
    """5 页；第 4、5 页作为豁免页。

    - 书签：Ch1->p1, Ch2->p2, Ref2.7.5->p4, Ref3.3->p5
    - 注释：p2 与 p4 各一个高亮
    - 链接：p2 与 p4 各一个内部链接（显式缩放）
    """
    pdf = new_pdf()
    pages = []
    for index in range(5):
        pages.append(
            add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Page %d) Tj ET" % (index + 1))
        )
    build_outline(
        pdf,
        [
            {"title": "Ch1", "dest": dest_xyz(pages[0], 2.0)},
            {"title": "Ch2", "dest": dest_xyz(pages[1], 2.0)},
            {"title": "Ref 2.7.5", "dest": dest_xyz(pages[3], 2.0)},
            {"title": "Ref 3.3", "dest": dest_xyz(pages[4], 2.0)},
        ],
    )
    for page_index in (1, 3):
        page = pages[page_index]
        hl = make_annot(
            pdf,
            "Highlight",
            (72, 700, 300, 720),
            appearance=b"1 1 0 rg 0 0 100 20 re f",
            extra={
                "QuadPoints": Array([72, 720, 300, 720, 72, 700, 300, 700]),
            },
        )
        hl.P = page
        link = make_annot(
            pdf,
            "Link",
            (72, 600, 300, 620),
            appearance=None,
            extra={"Dest": dest_xyz(pages[0], 3.0), "Border": Array([0, 0, 0])},
        )
        link.P = page
        attach_annots(pdf, page, [hl, link])
    return save(pdf, path)


def make_text_blocks(path: Path) -> Path:
    """简单文本型 PDF：一个标题 + 三段正文，用于标签生成。"""
    pdf = new_pdf()
    p1 = add_page(pdf)
    p1.Contents = Stream(
        pdf,
        b"""BT /F1 20 Tf 72 770 Td (Study Report) Tj ET
BT /F1 14 Tf 72 720 Td (1. Introduction) Tj ET
BT /F1 11 Tf 72 690 Td (This section introduces the study design and objectives.) Tj ET
BT /F1 11 Tf 72 670 Td (The study was conducted in two phases over twelve months.) Tj ET
BT /F1 11 Tf 72 650 Td (All participants provided written informed consent.) Tj ET
""",
    )
    p2 = add_page(pdf, b"")
    p2.Contents = Stream(
        pdf,
        b"""BT /F1 14 Tf 72 770 Td (2. Methods) Tj ET
BT /F1 11 Tf 72 740 Td (Randomisation used a block design with stratification.) Tj ET
BT /F1 11 Tf 72 720 Td (Adverse events were recorded at every visit.) Tj ET
""",
    )
    return save(pdf, path)


def make_scanned_like(path: Path) -> Path:
    """无文本层，模拟扫描件。"""
    pdf = new_pdf()
    page = add_page(pdf)
    page.Contents = Stream(pdf, b"0.8 0.8 0.8 rg 40 40 515 760 re f")
    return save(pdf, path)


def make_linearized(path: Path) -> Path:
    pdf = new_pdf()
    add_page(pdf)
    return save(pdf, path, linearize=True)


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

SAMPLE_SPECS = {
    "zoom_explicit.pdf": make_zoom_explicit,
    "zoom_inherit.pdf": make_zoom_inherit,
    "named_dest_shared.pdf": make_named_dest_shared,
    "links_mixed.pdf": make_links_mixed,
    "annots_mixed.pdf": make_annots_mixed,
    "meta_full.pdf": make_meta_full,
    "meta_clean.pdf": make_meta_clean,
    "tagged_real.pdf": make_tagged_real,
    "markinfo_only.pdf": make_markinfo_only,
    "untagged.pdf": make_untagged,
    "broken_struct.pdf": make_broken_struct,
    "encrypted.pdf": make_encrypted,
    "signed.pdf": make_signed,
    "corrupt.pdf": make_corrupt,
    "exempt_mix.pdf": make_exempt_mix,
    "text_blocks.pdf": make_text_blocks,
    "scanned_like.pdf": make_scanned_like,
    "linearized.pdf": make_linearized,
}


def build_all(out_dir: Path | None = None, force: bool = True) -> dict[str, Path]:
    out_dir = Path(out_dir or FIXTURE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    built: dict[str, Path] = {}
    for name, builder in SAMPLE_SPECS.items():
        target = out_dir / name
        if target.exists() and not force:
            built[name] = target
            continue
        built[name] = builder(target)
    return built


def ensure_fixtures(out_dir: Path | None = None) -> Path:
    """测试用入口：样本缺失时自动生成。"""
    target = Path(out_dir or FIXTURE_DIR)
    missing = [n for n in SAMPLE_SPECS if not (target / n).is_file()]
    if missing:
        build_all(target, force=False)
    return target


def reset_dir(out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 PDF 测试样本")
    parser.add_argument("--out", default=None, help="输出目录")
    parser.add_argument("--keep", action="store_true", help="已存在的样本不重建")
    args = parser.parse_args()
    built = build_all(Path(args.out) if args.out else None, force=not args.keep)
    for name, path in sorted(built.items()):
        print(f"{name:26s} {path.stat().st_size:>9d} bytes")


if __name__ == "__main__":
    main()
