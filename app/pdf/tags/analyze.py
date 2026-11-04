"""文本块分析：把页面上的文字分成标题 / 段落，并给出保守的阅读顺序。

两条自我约束：

1. **只做保守推断**。字号明显大于正文、且行数少的块才判为标题；
   其余一律当段落。宁可在复杂版式上判成段落，也不要把正文误判成标题
   ——误判会让阅读顺序乱掉，比「不够精细」糟得多。
2. **没有文本就承认没有**。扫描件、纯图形页在这里就是空列表，
   上层据此判 `failed`，绝不生成一棵空标签树来冒充成功。

坐标一律使用 PyMuPDF 的显示空间（旋转后、y 向下），
因为块与内容流文本行的匹配是在这个空间里做的（见 `content_mapping`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

__all__ = [
    "TextBlock",
    "PageText",
    "DocumentText",
    "analyze_document",
    "classify_blocks",
    "HEADING_SIZES",
]

# 结构标签：H1 对应一级标题，H2 二级标题，P 段落
HEADING_SIZES = {"H1": 1.55, "H2": 1.18}

_MAX_HEADING_CHARS = 120
_MIN_BODY_CHARS = 1


@dataclass(frozen=True)
class TextBlock:
    """一页里的一个文本块。"""

    page_index: int
    kind: str
    bbox: tuple[float, float, float, float]
    text: str
    font_size: float
    bold: bool

    @property
    def tag(self) -> str:
        return f"/{self.kind}"


@dataclass(frozen=True)
class PageText:
    page_index: int
    width: float
    height: float
    rotation: int
    blocks: tuple[TextBlock, ...] = ()
    body_size: float = 0.0

    @property
    def has_text(self) -> bool:
        return bool(self.blocks)

    @property
    def headings(self) -> int:
        return sum(1 for block in self.blocks if block.kind in ("H1", "H2"))

    @property
    def paragraphs(self) -> int:
        return sum(1 for block in self.blocks if block.kind == "P")


@dataclass(frozen=True)
class DocumentText:
    pages: tuple[PageText, ...] = ()
    body_size: float = 0.0
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_text(self) -> bool:
        return any(page.has_text for page in self.pages)

    @property
    def block_count(self) -> int:
        return sum(len(page.blocks) for page in self.pages)

    @property
    def heading_count(self) -> int:
        return sum(page.headings for page in self.pages)

    @property
    def paragraph_count(self) -> int:
        return sum(page.paragraphs for page in self.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


def _spans(block: dict[str, Any]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for line in block.get("lines") or ():
        for span in line.get("spans") or ():
            if span.get("text"):
                spans.append(span)
    return spans


def _block_text(block: dict[str, Any]) -> str:
    parts: list[str] = []
    for line in block.get("lines") or ():
        chunk = "".join(span.get("text", "") for span in line.get("spans") or ())
        if chunk.strip():
            parts.append(chunk.strip())
    return " ".join(parts).strip()


def _weighted_median(values: list[tuple[float, int]]) -> float:
    """按字符数加权的字号中位数——正文占多数，标题不会把基准带偏。"""
    if not values:
        return 0.0
    total = sum(weight for _, weight in values)
    if total <= 0:
        return sorted(value for value, _ in values)[len(values) // 2]
    target = total / 2.0
    running = 0
    for value, weight in sorted(values):
        running += weight
        if running >= target:
            return value
    return sorted(value for value, _ in values)[-1]


def _page_lines(block: dict[str, Any]) -> int:
    return sum(1 for line in block.get("lines") or () if _line_text(line))


def _line_text(line: dict[str, Any]) -> str:
    return "".join(span.get("text", "") for span in line.get("spans") or ()).strip()


def classify_blocks(
    raw_blocks: list[dict[str, Any]],
    *,
    page_index: int,
    body_size: float,
) -> tuple[TextBlock, ...]:
    """把 PyMuPDF 的原始块转成带结构类型的块，并排出阅读顺序。

    阅读顺序沿用解析器给出的文档顺序，只在「同一行」内部按 x 排序。
    这是最保守的选择：多栏排版下强行按 y 排序会把左右栏交错拼在一起，
    而文档顺序至少不会制造出比原文更乱的结果。
    """
    if body_size <= 0:
        return ()

    items: list[TextBlock] = []
    for block in raw_blocks:
        spans = _spans(block)
        text = _block_text(block)
        if not spans or len(text) < _MIN_BODY_CHARS:
            continue
        size = max(float(span.get("size", 0.0)) for span in spans)
        bold = any(bool(int(span.get("flags", 0)) & 2 ** 4) for span in spans)
        bbox = tuple(float(value) for value in block.get("bbox", (0, 0, 0, 0)))
        lines = _page_lines(block)

        kind = "P"
        if size >= body_size * HEADING_SIZES["H1"] and len(text) <= _MAX_HEADING_CHARS and lines <= 2:
            kind = "H1"
        elif size >= body_size * HEADING_SIZES["H2"] and len(text) <= _MAX_HEADING_CHARS and lines <= 3:
            kind = "H2"

        items.append(
            TextBlock(
                page_index=page_index,
                kind=kind,
                bbox=bbox,  # type: ignore[arg-type]
                text=text,
                font_size=size,
                bold=bold,
            )
        )

    items.sort(key=lambda item: (round(item.bbox[1], 1), item.bbox[0]))
    return tuple(items)


def analyze_document(source: Path | str | bytes) -> DocumentText:
    """读取整份文档的文本结构。读不了就返回空结果 + 错误说明，不抛异常。

    接受路径或**内存字节**：标签生成跑在流水线里，那时文档只在内存中，
    落盘的是流水线自己的临时候选，处理器不该为了分析再另存一份文件。
    """
    errors: list[str] = []
    raw_pages: list[tuple[int, dict[str, Any], float, float, int]] = []
    size_samples: list[tuple[float, int]] = []

    try:
        if isinstance(source, bytes):
            doc = pymupdf.open(stream=source, filetype="pdf")
        else:
            doc = pymupdf.open(str(source))
    except Exception as exc:  # noqa: BLE001 - 分析失败要如实上报
        return DocumentText(errors=(f"{type(exc).__name__}: {exc}",))

    try:
        if doc.needs_pass:
            return DocumentText(errors=("文档需要密码，无法分析文本",))
        for index, page in enumerate(doc):
            payload = page.get_text("dict")
            raw_pages.append((index, payload, float(page.rect.width), float(page.rect.height), int(page.rotation)))
            for block in payload.get("blocks") or ():
                for span in _spans(block):
                    text = span.get("text", "")
                    if text.strip():
                        size_samples.append((float(span.get("size", 0.0)), len(text)))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"读取文本层失败：{type(exc).__name__}: {exc}")
        raw_pages = []
    finally:
        doc.close()

    body_size = _weighted_median([sample for sample in size_samples if sample[0] > 0])
    pages: list[PageText] = []
    for index, payload, width, height, rotation in raw_pages:
        blocks = classify_blocks(
            list(payload.get("blocks") or ()),
            page_index=index,
            body_size=body_size,
        )
        pages.append(
            PageText(
                page_index=index,
                width=width,
                height=height,
                rotation=rotation,
                blocks=blocks,
                body_size=body_size,
            )
        )

    return DocumentText(
        pages=tuple(pages),
        body_size=body_size,
        errors=tuple(errors),
    )
