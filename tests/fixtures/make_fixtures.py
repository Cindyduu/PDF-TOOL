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


def _helvetica_widths() -> Array:
    """Helvetica 的标准字宽，`/FirstChar 32` 起共 95 个。

    用 PyMuPDF 内置的 AFM 度量算，不手抄这 95 个数字——抄错一个就会让
    `content_color` 推算的文字位置整体偏移，症状是「有时着色、有时不着色」，
    比报错难查得多。
    """
    import fitz

    font = fitz.Font("helv")
    return Array([round(font.glyph_advance(code) * 1000) for code in range(32, 127)])


#: 带 `/Widths` 的 Helvetica。
#:
#: 标准 14 字体省略 `/Widths` 是合法的，但省略之后「一行里第 2 个 `Tj`
#: 画在哪儿」就推算不出来，`content_color` 只能放弃着色并记一条警告。
#: 真实文件两种写法都有，所以样本也要有带宽度的一份。
HELVETICA_SIZED = Dictionary(
    Type=Name("/Font"),
    Subtype=Name("/Type1"),
    BaseFont=Name("/Helvetica"),
    Encoding=Name("/WinAnsiEncoding"),
    FirstChar=32,
    LastChar=126,
    Widths=_helvetica_widths(),
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
    *,
    embed_font: bool = True,
) -> pikepdf.Object:
    """追加一页并返回页对象（pikepdf.Object）。

    `embed_font=True`（默认）用**真嵌入**的 Helvetica 排正文，让样本在
    `font_embedding` 项上是干净的——每个样本只应该测一个缺陷，字体项不能
    在所有样本上一起红。要造「未嵌入」样本时传 `embed_font=False`。
    """
    size = (PAGE_W, PAGE_H)
    pdf.add_blank_page(page_size=size)
    page = pdf.pages[-1].obj
    page.Contents = Stream(pdf, content)
    font = embedded_helvetica(pdf) if embed_font else HELVETICA
    page.Resources = Dictionary(Font=Dictionary(F1=font))
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


def raw_pdf(objects: list[bytes]) -> bytes:
    """按对象体列表手写一份最小 PDF（xref 偏移现算）。

    有些"结构损坏"样本 pikepdf 造不出来：保存时它会主动纠正不合规的键，
    比如把不是数组的 `/Annots` 直接删掉。测试需要的是**原始损坏状态**，
    所以这里绕过 pikepdf，自己拼字节。
    """
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def raw_stream(data: bytes) -> bytes:
    """手写一个内容流对象体。"""
    return b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


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


def make_smoke_links(path: Path) -> Path:
    """冒烟专用样本：链接矩形**正好压在文字上**，字体带 `/Widths`。

    `exempt_mix.pdf` 拿来当冒烟源有两处不合适，而这两处都不影响它在单测里
    的用途（那边查的是 `/C` 与矩形读取），所以不改它，另造一份：

    1. 它的 Helvetica 没有 `/Widths`。标准 14 字体省略 `/Widths` 是合法的，
       但省略之后「一行里第 2 个 `Tj` 画在哪儿」推算不出来，`content_color`
       只能放弃着色并记警告——R35 在这份样本上永远验证不了。
    2. 它的链接矩形悬在空白处（文字基线 760，矩形 600–620），就算着色
       成功也没有文字可改。

    其余形状照 `exempt_mix` 来：5 页、4 个书签、p2/p4 各有高亮与内链，
    外加 DocInfo + XMP，让「元数据清除」「注释展平」两条断言不是空断言。
    """
    pdf = new_pdf()
    pages = []
    for index in range(5):
        # 文字基线与下面的链接矩形对齐：14pt 字，基线 700 → 字身约 686–703。
        page = add_page(pdf, b"BT /F1 14 Tf 72 700 Td (Page %d) Tj ET" % (index + 1))
        page.Resources = Dictionary(Font=Dictionary(F1=HELVETICA_SIZED))
        pages.append(page)
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
            (72, 640, 300, 660),
            appearance=b"1 1 0 rg 0 0 100 20 re f",
            extra={"QuadPoints": Array([72, 660, 300, 660, 72, 640, 300, 640])},
        )
        hl.P = page
        link = make_annot(
            pdf,
            "Link",
            # 「Page 2」约 6 字符 × 7.8pt ≈ 47pt 宽，矩形给到 240 足够覆盖。
            (66, 686, 240, 706),
            appearance=None,
            extra={"Dest": dest_xyz(pages[0], 3.0), "Border": Array([0, 0, 0])},
        )
        link.P = page
        attach_annots(pdf, page, [hl, link])
    with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
        meta["dc:title"] = "Smoke Sample"
        meta["dc:creator"] = ["fixture-generator"]
    pdf.docinfo = pdf.make_indirect(
        Dictionary(
            Title=String("Smoke Sample"),
            Author=String("fixture-generator"),
            Subject=String("smoke"),
            Keywords=String("smoke, links"),
            Creator=String("fixture-generator"),
            Producer=String("pikepdf"),
        )
    )
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
# v4.0 校验样本（R14–R22）
#
# 每个 check 至少一个正样本 + 一个负样本。有几条样本刻意做成"几乎合规"：
# 校验最怕的是假阳性，一份正常文档被判出十条问题，用户就再也不会看报告了。
# --------------------------------------------------------------------------- #

#: 正常横排文本。所有"合规基线"页都用它。
CLEAN_TEXT = b"""BT /F1 16 Tf 72 770 Td (Clinical Study Report) Tj ET
BT /F1 11 Tf 72 740 Td (This page contains ordinary horizontal text lines.) Tj ET
BT /F1 11 Tf 72 720 Td (Every line is wider than three times its height.) Tj ET
"""

#: 90° 旋转的文本行：模拟"横向表格塞进纵向页面"。
ROTATED_TEXT = b"""BT /F1 11 Tf 0 1 -1 0 120 120 Tm (Rotated table column one) Tj ET
BT /F1 11 Tf 0 1 -1 0 150 120 Tm (Rotated table column two) Tj ET
"""

#: 横向页面里的正文。坐标要落在 595 高的可视区内，否则文本被裁掉，
#: `text_layer` 会把它当成扫描件——那样样本就同时踩了两个问题，测不准。
LANDSCAPE_TEXT = b"""BT /F1 16 Tf 72 520 Td (Landscape page heading) Tj ET
BT /F1 11 Tf 72 490 Td (This page is wider than it is tall.) Tj ET
"""


def _font_program_bytes() -> bytes:
    """PyMuPDF 内置 Helvetica 的真实 CFF 程序（约 33 KB）。

    v4.3 起校验项会**校验字体程序结构**（长度 + 魔数），手搓的假流会被判
    「嵌入程序无效」。要造「真嵌入」样本就必须拿真程序。PyMuPDF 自带 URW 系的
    Base14 替代字体，取它的字节即可——不依赖外部文件，样本仍然可重复生成。
    """
    global _FONT_PROGRAM_BYTES
    if _FONT_PROGRAM_BYTES is None:
        import fitz

        _FONT_PROGRAM_BYTES = fitz.Font("helv").buffer
    return _FONT_PROGRAM_BYTES


#: 33 KB 的程序读一次就够，别在每个样本里重复解析字体。
_FONT_PROGRAM_BYTES: bytes | None = None


def _shared_font_stream(pdf: pikepdf.Pdf) -> Stream:
    """同一个 PDF 内共用一个 `/FontFile3` 流。

    每页都新建一份的话，8 页样本会从 8 KB 涨到 450 KB（一份 33 KB 复制八遍），
    整个样本目录从 300 KB 涨到 8 MB。pikepdf 允许在 `Pdf` 对象上挂 Python
    属性，用它做一次性的 per-PDF 缓存最省事。
    """
    cached = getattr(pdf, "_pdftool_font_stream", None)
    if cached is None:
        cached = pdf.make_indirect(
            Stream(pdf, _font_program_bytes(), Subtype=Name("/Type1C"))
        )
        pdf._pdftool_font_stream = cached
    return cached


def _embedded_descriptor(pdf: pikepdf.Pdf, base_font: str) -> Dictionary:
    """带真实 `/FontFile3`（Type1C）的字体描述符。"""
    return pdf.make_indirect(
        Dictionary(
            Type=Name("/FontDescriptor"),
            FontName=Name("/" + base_font),
            Flags=32,
            FontBBox=Array([-166, -225, 1000, 931]),
            ItalicAngle=0,
            Ascent=718,
            Descent=-207,
            CapHeight=718,
            StemV=88,
            FontFile3=_shared_font_stream(pdf),
        )
    )


def embedded_helvetica(pdf: pikepdf.Pdf) -> Dictionary:
    """**真正嵌入**了 CFF 程序的 Helvetica，用来排正文。

    带 `/Widths`：`content_color` 要靠字宽推算「一行里第 2 个 `Tj` 画在哪儿」，
    没有字宽就只能放弃着色并记一条警告。
    """
    return Dictionary(
        Type=Name("/Font"),
        Subtype=Name("/Type1"),
        BaseFont=Name("/Helvetica"),
        Encoding=Name("/WinAnsiEncoding"),
        FirstChar=32,
        LastChar=126,
        Widths=_helvetica_widths(),
        FontDescriptor=_embedded_descriptor(pdf, "Helvetica"),
    )


def embedded_font(pdf: pikepdf.Pdf) -> Dictionary:
    """另一个真嵌入字体（NimbusSans），挂在资源里但不被内容流使用。

    它存在的意义是钉住「**未被使用的字体也要检查**」：只看被 `Tf` 选中的字体，
    会把「资源里躺着十个没嵌入的字体」这种真实情况整片漏掉。
    """
    return Dictionary(
        Type=Name("/Font"),
        Subtype=Name("/Type1"),
        BaseFont=Name("/NimbusSans"),
        Encoding=Name("/WinAnsiEncoding"),
        FirstChar=32,
        LastChar=126,
        Widths=_helvetica_widths(),
        FontDescriptor=_embedded_descriptor(pdf, "NimbusSans"),
    )


def placeholder_font(pdf: pikepdf.Pdf, name: str = "/ArialMT") -> Dictionary:
    """有 `/FontFile2` 键、但流只有 26 字节——**看起来嵌入了，其实没有**。

    v4.3 之前这种字体判 pass（只查键在不在）。AC-R41-N3 用钉住它必须判
    「嵌入程序无效」。
    """
    program = Stream(pdf, b"\x00\x01\x00\x00synthetic-font-program")
    descriptor = pdf.make_indirect(
        Dictionary(
            Type=Name("/FontDescriptor"),
            FontName=Name(name),
            Flags=4,
            FontFile2=program,
        )
    )
    return Dictionary(
        Type=Name("/Font"),
        Subtype=Name("/TrueType"),
        BaseFont=Name(name),
        FontDescriptor=descriptor,
    )


def unembedded_font(name: str = "/Arial") -> Dictionary:
    """非标准 14、又没有 `/FontDescriptor` 的字体 → 必须报错。"""
    return Dictionary(
        Type=Name("/Font"),
        Subtype=Name("/TrueType"),
        BaseFont=Name(name),
        Encoding=Name("/WinAnsiEncoding"),
    )


def _font_resources(
    pdf: pikepdf.Pdf,
    extra: dict[str, Dictionary],
    *,
    embed_font: bool = True,
) -> Dictionary:
    fonts = {"F1": embedded_helvetica(pdf) if embed_font else HELVETICA}
    fonts.update(extra)
    return Dictionary(Font=Dictionary(**fonts))


def _add_pages(
    pdf: pikepdf.Pdf,
    count: int,
    content: bytes = CLEAN_TEXT,
    *,
    embed_font: bool = True,
) -> list:
    return [add_page(pdf, content, embed_font=embed_font) for _ in range(count)]


def _outline(pdf: pikepdf.Pdf, pages: list, count: int = 3) -> None:
    """给前 `count` 页各挂一个书签。"""
    items = [
        {"title": f"Chapter {index + 1}", "dest": dest_xyz(pages[index], None)}
        for index in range(min(count, len(pages)))
    ]
    build_outline(pdf, items)


def _page_labels(pdf: pikepdf.Pdf, start: int = 1, first_index: int = 0) -> None:
    pdf.Root.PageLabels = Dictionary(
        Nums=Array([first_index, Dictionary(S=Name("/D"), St=start)])
    )


# --------------------------------------------------------------------------- #
# v4.1 目录样本（R32）
#
# 目录首页的版式刻意做成「标题黑色 + 条目蓝色」：用户的原话是「内链均为蓝色
# （RGB 0 0 255；比如目录，但需要注意，目录 2 字仍为黑色）」。所以标题不挂在
# 任何链接矩形里，校验只看得到条目的颜色——这正是期望的行为。
#
# 行距 28、链接矩形只包住一行。`toc` 用的是「span 中心点落在矩形内」判据，
# 矩形一旦跨行，相邻行的颜色就会被一起捞进来，样本自己就测不准了。
# --------------------------------------------------------------------------- #

TOC_TITLE_Y = 780
TOC_FIRST_Y = 730
TOC_LINE_GAP = 28


def _toc_content(count: int, black_rows: tuple[int, ...] = ()) -> bytes:
    """目录首页内容流。`black_rows` 里的条目刻意写成黑色，用来造「非蓝」样本。"""
    ops = [b"BT /F1 14 Tf 72 %d Td (Table of Contents) Tj ET" % TOC_TITLE_Y]
    for index in range(count):
        y = TOC_FIRST_Y - index * TOC_LINE_GAP
        color = b"0 0 0 rg" if index in black_rows else b"0 0 1 rg"
        ops.append(
            b"BT /F1 12 Tf " + color + b" 72 %d Td (Chapter " % y
            + str(index + 1).encode()
            + b" ........ "
            + str(index + 2).encode()
            + b") Tj ET"
        )
    return b"\n".join(ops) + b"\n"


def _toc_rect(index: int) -> tuple[float, float, float, float]:
    """第 `index` 条目（0 基）的链接矩形，PDF 用户空间。"""
    y = TOC_FIRST_Y - index * TOC_LINE_GAP
    return (68.0, float(y - 5), 300.0, float(y + 15))


def _toc_links(
    pdf: pikepdf.Pdf,
    page,
    targets: list,
    broken: tuple[int, ...] = (),
) -> None:
    """给首页挂 `len(targets)` 条内链；`broken` 里的序号写成悬空命名目标。"""
    annots = []
    for index, target in enumerate(targets):
        annot = Dictionary(
            Type=Name("/Annot"),
            Subtype=Name("/Link"),
            Rect=Array(list(_toc_rect(index))),
            P=page,
            F=4,
            Border=Array([0, 0, 0]),
        )
        # 悬空命名目标：`/Dest` 是个名字，但名字树里没有它 → 解析不出页。
        # 这是真实文档里最常见的一种「点了没反应」。
        annot.Dest = String("missing-dest") if index in broken else dest_xyz(target, None)
        annots.append(annot)
    attach_annots(pdf, page, annots)


def _toc_document(
    pdf: pikepdf.Pdf,
    page_count: int,
    content: bytes,
    entry_count: int,
    broken: tuple[int, ...] = (),
) -> list:
    """组装一份「首页目录 + 其余正文」的合规文档骨架（字体、书签、页码齐备）。

    `entry_count` 条内链指向第 2 页起的正文页；页码不够就循环复用，
    这样任何 `page_count` 都能造出合法目标。
    """
    pages = _add_pages(pdf, page_count)
    pages[0].Contents = Stream(pdf, content)
    for page in pages:
        page.Resources = _font_resources(pdf, {"F2": embedded_font(pdf)})
    _outline(pdf, pages, 3)
    _page_labels(pdf, 1)
    # v4.2 起「合规骨架」还要满足初始视图的规定值：导览标签 = 书签面板和页面。
    # `build_outline` 为了别的样本的语义把 PageMode 设成 `/UseNone`，这里改回来；
    # 不加这一句，`check_clean` 会在新增的 `initial_view` 项上 fail，
    # 把「无假阳性」那条硬验收直接打破。
    pdf.Root.PageMode = Name("/UseOutlines")
    if entry_count:
        span = max(page_count - 1, 1)
        targets = [pages[1 + index % span] for index in range(entry_count)]
        _toc_links(pdf, pages[0], targets, broken)
    return pages


def make_check_clean(path: Path) -> Path:
    """合规基线：10 项校验对这份文件必须全部 pass（无假阳性）。

    刻意把"容易被误判"的几件事都摆齐：字体里有一个未使用的假嵌入字体、
    逻辑页码从 1 开始、8 页且有书签、首页有 4 条蓝色目录内链、没有外链。

    v4.1 起首页必须带目录，否则新增的 `toc` 项会 fail——那会把「无假阳性」
    这条硬验收直接打破。所以基线的首页内容换成了目录版式。
    """
    pdf = new_pdf()
    _toc_document(pdf, 8, _toc_content(4), 4)
    return save(pdf, path)


# ---- R32 目录：正样本与各类负样本 ----


def make_check_toc_ok(path: Path) -> Path:
    """首页 4 条蓝色内链 → toc PASS。"""
    pdf = new_pdf()
    _toc_document(pdf, 8, _toc_content(4), 4)
    return save(pdf, path)


def make_check_toc_partial_blue(path: Path) -> Path:
    """首页 4 条内链，第 2 条是黑色 → toc WARNING「1 条非蓝」。"""
    pdf = new_pdf()
    _toc_document(pdf, 8, _toc_content(4, black_rows=(1,)), 4)
    return save(pdf, path)


def make_check_toc_title_only(path: Path) -> Path:
    """首页有「Table of Contents」标题但一条内链都没有 → toc WARNING。"""
    pdf = new_pdf()
    _toc_document(pdf, 8, _toc_content(0), 0)
    return save(pdf, path)


def make_check_toc_none(path: Path) -> Path:
    """首页既无目录标题也无内链 → toc FAIL。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, 8)
    _page_labels(pdf, 1)
    _outline(pdf, pages, 3)
    return save(pdf, path)


def make_check_toc_single_page(path: Path) -> Path:
    """单页文档 → toc SKIPPED（一页的文件不存在目录）。"""
    pdf = new_pdf()
    _add_pages(pdf, 1)
    _page_labels(pdf, 1)
    return save(pdf, path)


def make_check_toc_broken_dest(path: Path) -> Path:
    """首页 4 条内链，第 3 条 `/Dest` 指向不存在的命名目标 → toc FAIL。

    其余 3 条照常判定颜色：结论里必须同时出现「跳不过去」和颜色情况，
    否则用户只知道坏了、不知道还差什么。
    """
    pdf = new_pdf()
    _toc_document(pdf, 8, _toc_content(4), 4, broken=(2,))
    return save(pdf, path)


# ---- R33 页码：页脚兜底样本（不写 /PageLabels，页码只印在页脚） ----


def _footer_content(number: int | None) -> bytes:
    """正文 + 页脚数字。`None` 表示这一页忘了印页码。"""
    if number is None:
        return CLEAN_TEXT
    return CLEAN_TEXT + b"BT /F1 10 Tf 290 30 Td (" + str(number).encode() + b") Tj ET\n"


def _footer_document(pdf: pikepdf.Pdf, numbers: list[int | None]) -> list:
    pages = _add_pages(pdf, len(numbers))
    for page, number in zip(pages, numbers):
        page.Contents = Stream(pdf, _footer_content(number))
    _outline(pdf, pages, 3)
    return pages


def make_check_footer_ok(path: Path) -> Path:
    """6 页，页脚 1–6 连续，无 `/PageLabels` → page_labels PASS（走页脚兜底）。"""
    pdf = new_pdf()
    _footer_document(pdf, [1, 2, 3, 4, 5, 6])
    return save(pdf, path)


def make_check_footer_from3(path: Path) -> Path:
    """6 页，页脚 3–8 连续 → page_labels FAIL「未从 1 开始」。"""
    pdf = new_pdf()
    _footer_document(pdf, [3, 4, 5, 6, 7, 8])
    return save(pdf, path)


def make_check_footer_gap(path: Path) -> Path:
    """6 页，第 3 页漏印页码 → page_labels WARNING（不敢下结论）。"""
    pdf = new_pdf()
    _footer_document(pdf, [1, 2, None, 4, 5, 6])
    return save(pdf, path)


# --------------------------------------------------------------------------- #
# v4.2 新增样本（R37 / R38 / R39）
#
# 造样本的原则与前面一致：**一个样本只坏一处**，其余全部沿用 `_toc_document`
# 的合规骨架。这样状态矩阵里那一位非 pass 就精确指向被测缺陷，不会被别的
# 项搅浑；也让「改完这一处就转 PASS」的闭环用例有干净的起点。
# --------------------------------------------------------------------------- #

#: 文档级 XMP 元数据流。内容是合法 XMP 包，够让 `/Root /Metadata` 存在即可。
XMP_PACKET = (
    b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
    b"<dc:title><rdf:Alt><rdf:li xml:lang=\"x-default\">Draft</rdf:li></rdf:Alt></dc:title>"
    b"</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>"
)


def make_check_bookmark_fith(path: Path) -> Path:
    """书签齐全但全部用 `/FitH` → `bookmarks` WARNING（R37）。

    `/FitH` 只给「顶边位置」，没有缩放位，Acrobat 里显示「适合宽度」而不是
    「默认」——它**不是**承前缩放。这是最容易和 `/XYZ null` 混淆的一种写法，
    所以单独留一个样本。
    """
    pdf = new_pdf()
    pages = _toc_document(pdf, 8, _toc_content(4), 4)
    build_outline(
        pdf,
        [
            {"title": f"Chapter {index + 1}", "dest": Array([pages[index], Name("/FitH"), 800])}
            for index in range(6)
        ],
    )
    # `build_outline` 会把 PageMode 打回 `/UseNone`，这里改回来，让本样本只坏 R37。
    pdf.Root.PageMode = Name("/UseOutlines")
    return save(pdf, path)


def make_check_view_bad(path: Path) -> Path:
    """初始视图三项全不合规 → `initial_view` FAIL（R38）。

    导览标签 `/UseNone`（仅页面）、页面布局 `/OneColumn`（单页连续）、
    放大率 `/XYZ 0 0 1.5`（150%），三种错法一次摆齐。
    """
    pdf = new_pdf()
    pages = _toc_document(pdf, 8, _toc_content(4), 4)
    pdf.Root.PageMode = Name("/UseNone")
    pdf.Root.PageLayout = Name("/OneColumn")
    pdf.Root.OpenAction = dest_xyz(pages[1], 1.5)
    return save(pdf, path)


def make_check_meta_dirty(path: Path) -> Path:
    """DocInfo 三个字段 + 文档级 XMP 都在 → `metadata_security` WARNING（R39）。"""
    pdf = new_pdf()
    _toc_document(pdf, 8, _toc_content(4), 4)
    pdf.docinfo = pdf.make_indirect(
        Dictionary(
            Title=String("Quarterly Report"),
            Author=String("Alice"),
            Producer=String("fixture-generator"),
        )
    )
    pdf.Root.Metadata = pdf.make_stream(XMP_PACKET)
    return save(pdf, path)


def make_check_meta_encrypted(path: Path) -> Path:
    """仅权限密码（能打开、不能改）→ `metadata_security` FAIL（R39 / D-9）。

    `user=""` 意味着打开不需要密码，所以它过得来上传与校验；工具不解密，
    这一项只能报 FAIL 让用户自己去解除保护。
    """
    pdf = new_pdf()
    _toc_document(pdf, 8, _toc_content(4), 4)
    return save(
        pdf,
        path,
        encryption=pikepdf.Encryption(owner="owner-pass", user="", R=6),
    )


# --------------------------------------------------------------------------- #
# v4.1 清除高亮与批注样本（R34）
#
# 白名单删除的关键是「删得对、留得对」：样本里必须同时摆着该删的和该留的，
# 否则「多删一个」和「少删一个」都断言不出来。
#
# 两种外观各带一个可搜索的填充色标记，用来证明「删了就是真没了」：
# 只删注释时标记不在内容流里，一旦被先压平再删，标记就会留在页面上。
# --------------------------------------------------------------------------- #

#: 高亮外观的标记（黄底填充）。
HIGHLIGHT_AP = b"1 1 0 rg 0 0 100 20 re f"
#: 图章外观的标记（红框 + 红底），与高亮不共用任何字节。
STAMP_AP = b"0.9 0.1 0.1 rg 0 0 100 60 re f"


def _highlight(
    pdf: pikepdf.Pdf, page, rect: tuple[float, float, float, float]
) -> Dictionary:
    annot = make_annot(
        pdf,
        "Highlight",
        rect,
        appearance=HIGHLIGHT_AP,
        extra={
            "QuadPoints": Array([rect[0], rect[3], rect[2], rect[3], rect[0], rect[1], rect[2], rect[1]])
        },
    )
    annot.P = page
    return annot


def _stamp(
    pdf: pikepdf.Pdf, page, rect: tuple[float, float, float, float]
) -> Dictionary:
    annot = make_annot(
        pdf, "Stamp", rect, appearance=STAMP_AP, extra={"Name": Name("/Approved")}
    )
    annot.P = page
    return annot


def _internal_link(
    pdf: pikepdf.Pdf, page, target, rect: tuple[float, float, float, float]
) -> Dictionary:
    annot = make_annot(
        pdf,
        "Link",
        rect,
        appearance=None,
        extra={"Dest": dest_xyz(target, None), "Border": Array([0, 0, 0])},
    )
    annot.P = page
    return annot


def make_remove_annots_mixed(path: Path) -> Path:
    """AC-R34-N1：第 1 页 3 个高亮 + 2 个图章 + 4 个内链。

    期望：删 3 个高亮，图章与内链一条不少。链接指向不同的正文页，
    「链接签名前后一致」这条比对才有区分度。
    """
    pdf = new_pdf()
    pages = [
        add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Page %d) Tj ET" % (index + 1))
        for index in range(4)
    ]
    first = pages[0]
    annots = [
        _highlight(pdf, first, (72, 700, 300, 720)),
        _highlight(pdf, first, (72, 670, 300, 690)),
        _highlight(pdf, first, (72, 640, 300, 660)),
        _stamp(pdf, first, (330, 700, 500, 760)),
        _stamp(pdf, first, (330, 620, 500, 680)),
        _internal_link(pdf, first, pages[1], (72, 600, 300, 620)),
        _internal_link(pdf, first, pages[2], (72, 570, 300, 590)),
        _internal_link(pdf, first, pages[3], (72, 540, 300, 560)),
        _internal_link(pdf, first, pages[1], (72, 510, 300, 530)),
    ]
    attach_annots(pdf, first, annots)
    return save(pdf, path)


def make_remove_annots_links_only(path: Path) -> Path:
    """AC-R34-N2：只有 Link 注释 → 一个字节都不该动。"""
    pdf = new_pdf()
    pages = [
        add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Page %d) Tj ET" % (index + 1))
        for index in range(3)
    ]
    attach_annots(
        pdf,
        pages[0],
        [
            _internal_link(pdf, pages[0], pages[1], (72, 600, 300, 620)),
            _internal_link(pdf, pages[0], pages[2], (72, 560, 300, 580)),
        ],
    )
    return save(pdf, path)


def make_remove_annots_exempt(path: Path) -> Path:
    """AC-R34-N3：第 1、4 页各一个高亮 + 一个内链；第 4 页作为豁免页。"""
    pdf = new_pdf()
    pages = [
        add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Page %d) Tj ET" % (index + 1))
        for index in range(5)
    ]
    for index in (0, 3):
        page = pages[index]
        attach_annots(
            pdf,
            page,
            [
                _highlight(pdf, page, (72, 700, 300, 720)),
                _internal_link(pdf, page, pages[0], (72, 600, 300, 620)),
            ],
        )
    return save(pdf, path)


def make_remove_annots_form(path: Path) -> Path:
    """AC-R34-E2：带表单域的文档，删批注不得动字段树。

    字段树做成「父域 + 一个 Kid」，这样字段节点数（2）与页面表单域数（1）
    不相等——只看 `len(/Fields)` 的实现会被这种结构骗过去。
    """
    pdf = new_pdf()
    pages = [
        add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Page %d) Tj ET" % (index + 1))
        for index in range(2)
    ]
    page = pages[0]
    widget = make_annot(
        pdf,
        "Widget",
        (72, 380, 300, 410),
        appearance=b"0 0 0 RG 1 w 0 0 100 30 re S",
        extra={"FT": Name("/Tx"), "T": String("fieldA"), "Ff": 0, "V": String("x")},
    )
    widget = pdf.make_indirect(widget)
    widget.P = page
    parent = pdf.make_indirect(
        Dictionary(FT=Name("/Tx"), T=String("group"), Kids=Array([widget]))
    )
    widget.Parent = parent
    attach_annots(
        pdf,
        page,
        [_highlight(pdf, page, (72, 700, 300, 720)), widget],
    )
    pdf.Root.AcroForm = Dictionary(Fields=Array([parent]), NeedAppearances=False)
    return save(pdf, path)


def make_remove_annots_popup(path: Path) -> Path:
    """删 `Popup` 后不得留下悬空引用；`/Annots` 变空要整键删除。

    第 1 页：图章（保留）的 `/Popup` 指向一个 `Popup` 注释（删除）。
            删除后图章上的 `/Popup` 必须一并消失，否则指向一个不存在的对象。
    第 2 页：只有一个高亮。删完 `/Annots` 变成空数组，
            留个空数组会让部分阅读器报警告，必须整键删掉。
    """
    pdf = new_pdf()
    pages = [
        add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Page %d) Tj ET" % (index + 1))
        for index in range(2)
    ]
    first = pages[0]
    popup = make_annot(
        pdf, "Popup", (330, 600, 520, 680), appearance=None, extra={"Open": False}
    )
    popup = pdf.make_indirect(popup)
    popup.P = first
    stamp = _stamp(pdf, first, (330, 700, 500, 760))
    stamp.Popup = popup
    note = make_annot(
        pdf,
        "Text",
        (72, 700, 92, 720),
        appearance=None,
        extra={"Contents": String("sticky note"), "Popup": popup},
    )
    note.P = first
    attach_annots(pdf, first, [stamp, note, popup])
    attach_annots(pdf, pages[1], [_highlight(pdf, pages[1], (72, 700, 300, 720))])
    return save(pdf, path)


def make_check_no_outline(path: Path) -> Path:
    """8 页、无书签 → bookmarks FAIL。"""
    pdf = new_pdf()
    _add_pages(pdf, 8)
    _page_labels(pdf, 1)
    return save(pdf, path)


def make_check_empty_outline(path: Path) -> Path:
    """8 页、有 `/Outlines` 但一个条目都没有 → bookmarks FAIL「存在空书签树」。"""
    pdf = new_pdf()
    _add_pages(pdf, 8)
    _page_labels(pdf, 1)
    pdf.Root.Outlines = pdf.make_indirect(Dictionary(Type=Name("/Outlines"), Count=0))
    return save(pdf, path)


def make_check_short(path: Path) -> Path:
    """3 页无书签 → bookmarks SKIPPED（页数 ≤5）。"""
    pdf = new_pdf()
    _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    return save(pdf, path)


def make_check_links_http(path: Path) -> Path:
    """第 1、2 页各一个 https 外链，第 3 页一个 mailto → WARNING，明细 3 条。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    _outline(pdf, pages, 2)

    def uri_annot(page, rect, target: str) -> Dictionary:
        return Dictionary(
            Type=Name("/Annot"),
            Subtype=Name("/Link"),
            Rect=Array(list(rect)),
            P=page,
            F=4,
            Border=Array([0, 0, 0]),
            A=Dictionary(S=Name("/URI"), URI=String(target)),
        )

    attach_annots(pdf, pages[0], [uri_annot(pages[0], (72, 600, 300, 620), "https://example.org/a")])
    attach_annots(
        pdf, pages[1], [uri_annot(pages[1], (72, 600, 300, 620), "https://example.org/b")]
    )
    attach_annots(
        pdf, pages[2], [uri_annot(pages[2], (72, 600, 300, 620), "mailto:review@example.org")]
    )
    return save(pdf, path)


def make_check_links_internal(path: Path) -> Path:
    """只有 `/GoTo` 内链 → external_links PASS（内链不算外链）。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    _outline(pdf, pages, 2)
    link = Dictionary(
        Type=Name("/Annot"),
        Subtype=Name("/Link"),
        Rect=Array([72, 600, 300, 620]),
        P=pages[0],
        F=4,
        A=Dictionary(S=Name("/GoTo"), D=dest_xyz(pages[2], None)),
    )
    attach_annots(pdf, pages[0], [link])
    return save(pdf, path)


def make_check_links_broken_annots(path: Path) -> Path:
    """第 1 页 `/Annots` 里是悬空引用，第 2 页有一个正常外链。

    这种文件 pikepdf 造不出来——它保存时会把不合规的 `/Annots` 直接删掉，
    测试就永远拿不到"结构坏了"的样本。所以这里手写字节。

    第 1 页引用对象 9，但全文只有 8 个对象；pikepdf 读出来是 `[None]`。
    期望：第 1 页记一条「链接读取失败」，第 2 页的外链照常被捞出来。
    """
    page1 = b"BT /F1 12 Tf 72 700 Td (Broken annots page) Tj ET"
    page2 = b"BT /F1 12 Tf 72 700 Td (Healthy page with one link) Tj ET"
    resources = b"<< /Font << /F1 7 0 R >> >>"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources " + resources + b" /Contents 4 0 R /Annots [9 0 R] >>",
        raw_stream(page1),
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources " + resources + b" /Contents 6 0 R /Annots [8 0 R] >>",
        raw_stream(page2),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Annot /Subtype /Link /Rect [72 690 320 710] /Border [0 0 0] "
        b"/A << /S /URI /URI (https://example.com/broken-annots-test) >> >>",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw_pdf(objects))
    return path


def make_check_rotated_text(path: Path) -> Path:
    """纵向页里塞了旋转 90° 的文本 → page_orientation WARNING。"""
    pdf = new_pdf()
    page = add_page(pdf, ROTATED_TEXT)
    _add_pages(pdf, 2)
    _page_labels(pdf, 1)
    assert page is not None
    _outline(pdf, [page], 1)
    return save(pdf, path)


def make_check_landscape(path: Path) -> Path:
    """横向页面（宽 > 高）→ 该页跳过方向判定，文件级 PASS。"""
    pdf = new_pdf()
    page = add_page(pdf, LANDSCAPE_TEXT, mediabox=(0, 0, PAGE_H, PAGE_W))
    _page_labels(pdf, 1)
    _outline(pdf, [page], 1)
    return save(pdf, path)


def make_check_mixed_text(path: Path) -> Path:
    """前 2 页无文本层、后 4 页正常 → text_layer WARNING。"""
    pdf = new_pdf()
    for _ in range(2):
        page = add_page(pdf, b"0.9 0.9 0.9 rg 40 40 515 760 re f")
        page.Contents = Stream(pdf, b"0.9 0.9 0.9 rg 40 40 515 760 re f")
    _add_pages(pdf, 4)
    _page_labels(pdf, 1)
    _outline(pdf, pdf.pages[0:1] and [pdf.pages[0].obj], 1)
    return save(pdf, path)


def make_check_highlights(path: Path) -> Path:
    """第 2、5、7 页各一处高亮 → annotations WARNING「高亮 3 处」。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, 7)
    _page_labels(pdf, 1)
    _outline(pdf, pages, 3)
    for index in (1, 4, 6):
        page = pages[index]
        annot = Dictionary(
            Type=Name("/Annot"),
            Subtype=Name("/Highlight"),
            Rect=Array([72, 600, 300, 620]),
            P=page,
            F=4,
        )
        attach_annots(pdf, page, [annot])
    return save(pdf, path)


def make_check_font_missing(path: Path) -> Path:
    """含一个未嵌入的 Arial（非标准 14）→ font_embedding FAIL。

    正文的 Helvetica 是**真嵌入**的，所以报告里只会出现 Arial 一条——
    这个样本要测的是「非标准 14 未嵌入」，不是「所有字体都没嵌」。
    """
    pdf = new_pdf()
    pages = _add_pages(pdf, 6)
    for page in pages:
        page.Resources = _font_resources(pdf, {"F2": unembedded_font()})
    _outline(pdf, pages, 3)
    _page_labels(pdf, 1)
    return save(pdf, path)


def make_check_font_standard_unembedded(path: Path) -> Path:
    """**只用 Helvetica 且未嵌入** → font_embedding FAIL（AC-R41-N2）。

    v4.3 之前这个样本会判 PASS（标准 14 豁免）。用户的真实文件基本都是这种，
    所以它是这一轮最关键的负样本：工具不能因为「阅读器自带 Helvetica」
    就说文件没问题。
    """
    pdf = new_pdf()
    pages = _add_pages(pdf, 6, embed_font=False)
    _outline(pdf, pages, 3)
    _page_labels(pdf, 1)
    return save(pdf, path)


def make_check_font_placeholder(path: Path) -> Path:
    """有 `/FontFile2` 但只有 26 字节 → font_embedding FAIL「嵌入程序无效」。

    对应「看起来嵌入了」这一类：导出工具写了 `/FontFile2` 键，但流是占位符。
    v4.3 之前只查键在不在，这种字体一律 pass。
    """
    pdf = new_pdf()
    pages = _add_pages(pdf, 6)
    for page in pages:
        page.Resources = _font_resources(pdf, {"F2": placeholder_font(pdf)})
    _outline(pdf, pages, 3)
    _page_labels(pdf, 1)
    return save(pdf, path)


def make_check_font_broken(path: Path) -> Path:
    """`/Font` 指向一个整数而不是字典 → font_embedding FAIL「字体资源读取失败」。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, 6)
    pages[0].Resources = Dictionary(Font=42)
    _outline(pdf, pages, 3)
    _page_labels(pdf, 1)
    return save(pdf, path)


def make_check_labels_none(path: Path) -> Path:
    """没有 `/PageLabels` → page_labels WARNING「请人工复核」。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, 6)
    _outline(pdf, pages, 3)
    return save(pdf, path)


def make_check_labels_from3(path: Path) -> Path:
    """逻辑页码从 3 开始 → page_labels FAIL。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, 6)
    _outline(pdf, pages, 3)
    _page_labels(pdf, 3)
    return save(pdf, path)


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
    # 冒烟专用：链接矩形压在文字上、字体带 /Widths（R35 要能真的着色）
    "smoke_links.pdf": make_smoke_links,
    "text_blocks.pdf": make_text_blocks,
    "scanned_like.pdf": make_scanned_like,
    "linearized.pdf": make_linearized,
    # v4.0 校验样本
    "check_clean.pdf": make_check_clean,
    "check_no_outline.pdf": make_check_no_outline,
    "check_empty_outline.pdf": make_check_empty_outline,
    "check_short.pdf": make_check_short,
    "check_links_http.pdf": make_check_links_http,
    "check_links_internal.pdf": make_check_links_internal,
    "check_links_broken_annots.pdf": make_check_links_broken_annots,
    "check_rotated_text.pdf": make_check_rotated_text,
    "check_landscape.pdf": make_check_landscape,
    "check_mixed_text.pdf": make_check_mixed_text,
    "check_highlights.pdf": make_check_highlights,
    "check_font_missing.pdf": make_check_font_missing,
    "check_font_standard_unembedded.pdf": make_check_font_standard_unembedded,
    "check_font_placeholder.pdf": make_check_font_placeholder,
    "check_font_broken.pdf": make_check_font_broken,
    "check_labels_none.pdf": make_check_labels_none,
    "check_labels_from3.pdf": make_check_labels_from3,
    # v4.1 目录样本（R32）
    "check_toc_ok.pdf": make_check_toc_ok,
    "check_toc_partial_blue.pdf": make_check_toc_partial_blue,
    "check_toc_title_only.pdf": make_check_toc_title_only,
    "check_toc_none.pdf": make_check_toc_none,
    "check_toc_single_page.pdf": make_check_toc_single_page,
    "check_toc_broken_dest.pdf": make_check_toc_broken_dest,
    # v4.1 页码页脚兜底样本（R33）
    "check_footer_ok.pdf": make_check_footer_ok,
    "check_footer_from3.pdf": make_check_footer_from3,
    "check_footer_gap.pdf": make_check_footer_gap,
    # v4.2 新增样本（R37 承前缩放 / R38 初始视图 / R39 元数据与安全）
    "check_bookmark_fith.pdf": make_check_bookmark_fith,
    "check_view_bad.pdf": make_check_view_bad,
    "check_meta_dirty.pdf": make_check_meta_dirty,
    "check_meta_encrypted.pdf": make_check_meta_encrypted,
    # v4.1 清除高亮与批注样本（R34）
    "remove_annots_mixed.pdf": make_remove_annots_mixed,
    "remove_annots_links_only.pdf": make_remove_annots_links_only,
    "remove_annots_exempt.pdf": make_remove_annots_exempt,
    "remove_annots_form.pdf": make_remove_annots_form,
    "remove_annots_popup.pdf": make_remove_annots_popup,
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
