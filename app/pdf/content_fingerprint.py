"""R36 内容一致性护栏：证明「只改格式、不改内容」。

**为什么不用整文件 sha256**

书签、元数据、注释、线性化、内容流着色，每一样都会改字节，sha256 必然变。
反过来，sha256 没变也只说明「文件没被就地改过」，不说明内容对。
所以整文件哈希既会误报（正常处理被判成篡改），又不够（证明不了内容等价）。

**采用的办法：内容指纹 + 白名单式差异**

每页算一份指纹：

- 页宽高与 `/Rotate` —— 版式骨架
- 文本层哈希（`get_text("text")` 归一化）—— 读者能读到的字
- 内容流哈希（解码后字节）—— 页面怎么画
- 图片资源摘要 —— 图有没有被换掉
- 字体资源摘要（字体名 + 是否嵌入）—— 字体有没有被换掉

比对时分两档：

**硬门禁（零容忍）**：页数、每页尺寸与旋转、图片摘要、字体摘要。
任何处理项都不该改这些，改了就是内容被破坏，直接判违规。

**软门禁（差异必须能归因）**：文本层与内容流。
「扁平化」会把注释外观画进内容流（文本层和内容流都变），「内链颜色」会插入
颜色操作符（只有内容流变）。差异页集合非空时，检查**已勾选的处理项**里有没有
能解释这种变化的那一项；没有就判违规。这样既不会误伤已知的正常变化，
又能抓住「某个处理器偷偷删了一页文字」这类问题。

**护栏自己坏了怎么办**：指纹计算抛异常时**不阻断处理**（AC-R36-E2），
只记一条 warning 让结果页显示「内容一致性未能核对」。一个用来兜底的检查，
不该成为新的故障点。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Stream

__all__ = [
    "CONTENT_CHANGING_OPERATIONS",
    "TEXT_CHANGING_OPERATIONS",
    "DocumentFingerprint",
    "IntegrityReport",
    "PageFingerprint",
    "compare",
    "describe_integrity",
    "fingerprint_file",
    "fingerprint_pdf",
    "fingerprint_pymupdf",
]

#: 会改内容流字节的处理项。新增处理器时如果它写 `/Contents`，必须登记在这里，
#: 否则护栏会把正常处理判成违规。
CONTENT_CHANGING_OPERATIONS = frozenset({"flatten_annotations", "link_color"})

#: 会改文本层哈希的处理项。扁平化把注释外观（可能是 FreeText 文字）画进内容流，
#: 文本层因此变化；删除注释不写 `/Contents`，但个别解析器会把 `FreeText` 注释里
#: 的字算进提取结果，所以一并登记为「可能改文本」。**它不属于
#: `CONTENT_CHANGING_OPERATIONS`**——摘 `/Annots` 不动内容流字节，这是两张表
#: 必须分开、不能互相推导的原因。
TEXT_CHANGING_OPERATIONS = frozenset({"flatten_annotations", "remove_annotations"})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_text(text: str) -> str:
    """归一化空白后再哈希。

    换行、连续空格、行尾空白在不同 PDF 生成器/解析器版本间不稳定，
    但「有没有这几个字」是稳定的。护栏要抓的是文字被改，不是排版细节。
    """
    return " ".join(text.split())


@dataclass(frozen=True)
class PageFingerprint:
    number: int
    width: float
    height: float
    rotate: int
    text_hash: str
    content_hash: str
    image_digest: str
    font_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "width": self.width,
            "height": self.height,
            "rotate": self.rotate,
            "text_hash": self.text_hash,
            "content_hash": self.content_hash,
            "image_digest": self.image_digest,
            "font_digest": self.font_digest,
        }


@dataclass(frozen=True)
class DocumentFingerprint:
    page_count: int
    pages: tuple[PageFingerprint, ...] = ()
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_count": self.page_count,
            "error": self.error,
            "pages": [page.as_dict() for page in self.pages],
        }


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #


def _page_box(page: Any) -> tuple[float, float]:
    box = page.obj.get("/MediaBox")
    values = [float(item) for item in box] if isinstance(box, Array) and len(box) == 4 else []
    if len(values) != 4:
        return 0.0, 0.0
    return abs(values[2] - values[0]), abs(values[3] - values[1])


def _page_rotate(page: Any) -> int:
    raw = page.obj.get("/Rotate")
    try:
        return int(raw) % 360 if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _content_bytes(page: Any) -> bytes:
    """解码后的内容流字节。`/Contents` 可能是单个流，也可能是流数组。"""
    contents = page.obj.get("/Contents")
    if contents is None:
        return b""
    if isinstance(contents, Stream):
        try:
            return contents.read_bytes()
        except Exception:  # noqa: BLE001 - 坏流按空处理，硬门禁另有兜底
            return b""
    if isinstance(contents, Array):
        chunks: list[bytes] = []
        for item in contents:
            if isinstance(item, Stream):
                try:
                    chunks.append(item.read_bytes())
                except Exception:  # noqa: BLE001
                    continue
        return b"\n".join(chunks)
    return b""


def _resources(page: Any) -> Any:
    try:
        return page.resources
    except Exception:  # noqa: BLE001
        return None


def _image_digest(page: Any) -> str:
    """图片摘要：只看 `/Subtype /Image` 的像素与尺寸，**不看资源名**。

    不看名字是有意的：扁平化会给页面加 `/FlatAnnot1` 这类表单 XObject，
    名字一变摘要就变，护栏会把正常处理判成违规。图片的宽高 + 像素哈希
    足以抓住「图被换掉了」。
    """
    resources = _resources(page)
    if not isinstance(resources, Dictionary):
        return _sha256(b"")
    xobjects = resources.get("/XObject")
    if not isinstance(xobjects, Dictionary):
        return _sha256(b"")

    entries: list[str] = []
    for _name, obj in xobjects.items():
        if not isinstance(obj, Stream):
            continue
        if str(obj.get("/Subtype") or "") != "/Image":
            continue
        try:
            width = int(obj.get("/Width") or 0)
            height = int(obj.get("/Height") or 0)
            payload = _sha256(obj.read_bytes())
        except Exception:  # noqa: BLE001
            continue
        entries.append(f"{width}x{height}:{payload}")
    entries.sort()
    return _sha256("\n".join(entries).encode("ascii"))


_EMBED_KEYS = ("/FontFile", "/FontFile2", "/FontFile3")


def _font_digest(page: Any) -> str:
    """字体摘要：字体名 + 是否嵌入。资源名参与，因为字体资源被换名会影响渲染。"""
    resources = _resources(page)
    if not isinstance(resources, Dictionary):
        return _sha256(b"")
    fonts = resources.get("/Font")
    if not isinstance(fonts, Dictionary):
        return _sha256(b"")

    entries: list[str] = []
    for name, font in fonts.items():
        if not isinstance(font, Dictionary):
            continue
        base = str(font.get("/BaseFont") or "?")
        descriptor = font.get("/FontDescriptor")
        embedded = False
        if isinstance(descriptor, Dictionary):
            embedded = any(key in descriptor for key in _EMBED_KEYS)
        entries.append(f"{name}|{base}|{int(embedded)}")
    entries.sort()
    return _sha256("\n".join(entries).encode("utf-8"))


def fingerprint_pdf(pdf: pikepdf.Pdf, texts: Iterable[str] | None = None) -> DocumentFingerprint:
    """对一个已打开的 pikepdf 文档取指纹。

    `texts` 允许调用方传外部解析器算好的每页文本（`fingerprint_file` 里
    PyMuPDF 已经打开过一次，不必再开）。缺省时文本层哈希留空，
    只比结构——这种模式给单测用。
    """
    text_list = list(texts) if texts is not None else []
    pages: list[PageFingerprint] = []
    for index, page in enumerate(pdf.pages, start=1):
        width, height = _page_box(page)
        text = text_list[index - 1] if index - 1 < len(text_list) else ""
        pages.append(
            PageFingerprint(
                number=index,
                width=round(width, 3),
                height=round(height, 3),
                rotate=_page_rotate(page),
                text_hash=_sha256(_normalize_text(text).encode("utf-8")),
                content_hash=_sha256(_content_bytes(page)),
                image_digest=_image_digest(page),
                font_digest=_font_digest(page),
            )
        )
    return DocumentFingerprint(page_count=len(pages), pages=tuple(pages))


def fingerprint_pymupdf(doc: pymupdf.Document) -> tuple[str, ...]:
    """每页文本层（原始文本，未归一化）。"""
    return tuple(page.get_text("text") for page in doc)


def fingerprint_file(path: Path | str) -> DocumentFingerprint:
    """对一个文件取指纹。任何异常都收进 `error`，不抛出去。"""
    target = Path(path)
    try:
        with pymupdf.open(str(target)) as doc:
            texts = fingerprint_pymupdf(doc)
        with pikepdf.open(target) as pdf:
            return fingerprint_pdf(pdf, texts)
    except Exception as exc:  # noqa: BLE001 - 护栏不能成为新的故障点
        return DocumentFingerprint(
            page_count=0,
            pages=(),
            error=f"{type(exc).__name__}: {exc}",
        )


# --------------------------------------------------------------------------- #
# 比对
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IntegrityReport:
    """内容一致性结论。字段名与结果页展示逐项对应。"""

    ok: bool = True
    page_count_before: int = 0
    page_count_after: int = 0
    changed_content_pages: tuple[int, ...] = ()
    changed_text_pages: tuple[int, ...] = ()
    attributed_to: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "page_count": {
                "before": self.page_count_before,
                "after": self.page_count_after,
            },
            "changed_pages": {
                "content": list(self.changed_content_pages),
                "text": list(self.changed_text_pages),
            },
            "attributed_to": list(self.attributed_to),
            "problems": list(self.problems),
            "warning": self.warning,
        }

    def summary(self) -> str:
        return describe_integrity(self.as_dict())


def describe_integrity(data: dict[str, Any] | None) -> str:
    """把 `IntegrityReport.as_dict()` 说成一句人话。

    和 `IntegrityReport.summary()` 共用这一份实现：报告与日志是从 job.json
    现场推导的（§R28「一份数据源」），那时对象已经没了，只剩字典。各写一份
    的后果是「结果页说 3 页变化、日志说 2 页」——两套账，且没人能说清哪个对。

    `data` 为 `None` 表示这一步根本没跑（老任务，或护栏在建指纹前就抛了）。
    """
    if not isinstance(data, dict):
        return "内容一致性未核对"
    warning = data.get("warning")
    if warning:
        return str(warning)
    problems = list(data.get("problems") or [])
    if data.get("ok") is False:
        return "内容一致性未通过：" + "；".join(str(p) for p in problems)
    changed = data.get("changed_pages") or {}
    content_pages = list(changed.get("content") or [])
    text_pages = list(changed.get("text") or [])
    if not content_pages and not text_pages:
        return "内容与版式均未变化"
    parts: list[str] = []
    if content_pages:
        parts.append(f"{len(content_pages)} 页内容流变化")
    if text_pages:
        parts.append(f"{len(text_pages)} 页文本层变化")
    attributed = [str(item) for item in (data.get("attributed_to") or [])]
    return f"{'、'.join(parts)}，已归因到：{'、'.join(attributed) or '无'}"


def _geometry_problems(
    before: DocumentFingerprint, after: DocumentFingerprint
) -> list[str]:
    problems: list[str] = []
    if before.page_count != after.page_count:
        problems.append(f"页数变化：{before.page_count} → {after.page_count}")
        return problems

    for old, new in zip(before.pages, after.pages):
        if old.width != new.width or old.height != new.height:
            problems.append(
                f"第 {old.number} 页尺寸变化："
                f"{old.width}×{old.height} → {new.width}×{new.height}"
            )
        if old.rotate != new.rotate:
            problems.append(f"第 {old.number} 页旋转角变化：{old.rotate} → {new.rotate}")
        if old.image_digest != new.image_digest:
            problems.append(f"第 {old.number} 页图片资源变化")
        if old.font_digest != new.font_digest:
            problems.append(f"第 {old.number} 页字体资源变化")
    return problems


def compare(
    before: DocumentFingerprint,
    after: DocumentFingerprint,
    selected_operations: Iterable[str],
) -> IntegrityReport:
    """比对两次指纹，按白名单判定差异是否可归因。

    指纹本身出错（`error` 非空）时返回 `ok=True` + `warning`：
    护栏坏了不该阻断正常处理，但必须在结果页说清楚「没核对成」。
    """
    selected = tuple(selected_operations)

    if before.error or after.error:
        detail = before.error or after.error or ""
        return IntegrityReport(
            ok=True,
            page_count_before=before.page_count,
            page_count_after=after.page_count,
            warning=f"内容一致性未能核对（指纹计算失败：{detail}）",
        )

    problems = _geometry_problems(before, after)
    if problems:
        return IntegrityReport(
            ok=False,
            page_count_before=before.page_count,
            page_count_after=after.page_count,
            problems=tuple(problems),
        )

    changed_content = tuple(
        old.number
        for old, new in zip(before.pages, after.pages)
        if old.content_hash != new.content_hash
    )
    changed_text = tuple(
        old.number
        for old, new in zip(before.pages, after.pages)
        if old.text_hash != new.text_hash
    )

    attributed: list[str] = []
    if changed_content:
        owners = [key for key in selected if key in CONTENT_CHANGING_OPERATIONS]
        if not owners:
            problems.append(
                f"{len(changed_content)} 页内容流发生变化，但没有任何已勾选的处理项"
                f"会改动内容流（页号：{_pages_text(changed_content)}）"
            )
        else:
            attributed.extend(owners)
    if changed_text:
        owners = [key for key in selected if key in TEXT_CHANGING_OPERATIONS]
        if not owners:
            problems.append(
                f"{len(changed_text)} 页文本层发生变化，但没有任何已勾选的处理项"
                f"会改动文本（页号：{_pages_text(changed_text)}）"
            )
        else:
            attributed.extend(owners)

    return IntegrityReport(
        ok=not problems,
        page_count_before=before.page_count,
        page_count_after=after.page_count,
        changed_content_pages=changed_content,
        changed_text_pages=changed_text,
        attributed_to=tuple(dict.fromkeys(attributed)),
        problems=tuple(problems),
    )


def _pages_text(pages: tuple[int, ...], limit: int = 10) -> str:
    shown = "、".join(str(page) for page in pages[:limit])
    if len(pages) > limit:
        shown += f" 等 {len(pages)} 页"
    return shown
