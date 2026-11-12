"""处理器测试的公共工具。

处理器测试一律在**内存里**改样本，不落盘：
`pikepdf.open(path)` 默认只读，改动不会写回 `tests/fixtures/pdfs/`，
所以同一份样本可以被多个用例反复使用而互不污染。
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pikepdf

from app.pdf.bookmarks import iter_outline_items
from app.pdf.context import OpContext


@contextmanager
def opened(
    path: Path | str, exempt: tuple[int, ...] = ()
) -> Iterator[tuple[pikepdf.Pdf, OpContext]]:
    """打开样本并现场构造上下文，与流水线里的顺序保持一致。"""
    with pikepdf.open(path) as pdf:
        yield pdf, OpContext.build(pdf, exempt)


def page_content_bytes(page: pikepdf.Page | pikepdf.Object) -> bytes:
    """把一页的内容流拼成字节串（`/Contents` 可能是单个流，也可能是流数组）。

    参数可以是 `pikepdf.Page`，也可以是页对象本身（`add_page` 返回的就是后者）。
    """
    obj = page.obj if isinstance(page, pikepdf.Page) else page
    contents = obj.get("/Contents")
    if contents is None:
        return b""
    if isinstance(contents, pikepdf.Array):
        return b"".join(stream.read_bytes() for stream in contents)
    return contents.read_bytes()


def outline_titles(pdf: pikepdf.Pdf) -> list[str]:
    return [str(item.get("/Title") or "") for item in iter_outline_items(pdf)]


def _page_obj(page: pikepdf.Page | pikepdf.Object) -> pikepdf.Object:
    return page.obj if isinstance(page, pikepdf.Page) else page


def annots_of(page: pikepdf.Page | pikepdf.Object) -> list:
    annots = _page_obj(page).get("/Annots")
    return list(annots) if isinstance(annots, pikepdf.Array) else []


def annot_subtypes(page: pikepdf.Page | pikepdf.Object) -> list[str]:
    return [str(annot.get("/Subtype")).lstrip("/") for annot in annots_of(page)]


# --------------------------------------------------------------------------- #
# 对象内容签名
# --------------------------------------------------------------------------- #


def object_signature(
    pdf: pikepdf.Pdf,
    obj: Any,
    *,
    _pages: dict | None = None,
    _stack: tuple = (),
    skip_keys: frozenset[str] | set[str] | None = None,
) -> Any:
    """把一个对象解成**只反映内容**的规范化签名。

    为什么不直接比 `unparse()`：qpdf 做线性化（R8，流水线最后一步）时必须把
    首页对象排到文件前面，对象编号会被整体重排。于是同一个未改动过的对象，
    保存前后 `unparse()` 结果里的 `9 0 R` 会变成 `6 0 R`——**编号变了，内容没变**。
    拿 `unparse()` 直接比较，比的是 xref 号而不是「对象有没有被改」，
    这条断言在带线性化的流水线上永远不可能成立。

    这里改为解析内容本身：

    - 间接引用 → 展开成被引用对象的内容（页面引用则折算成页码）；
    - 字典 → 键排序后的键值对；数组 → 逐项；
    - 流 → 字典部分 + **解码后字节的 SHA-256**（与压缩方式无关）。

    这样签名只随内容变化，不随编号漂移，比原来那条断言更贴近「零变化」的本意。
    """
    pages = _pages if _pages is not None else {
        page.obj.objgen: index for index, page in enumerate(pdf.pages)
    }

    if isinstance(obj, pikepdf.Object) and obj.is_indirect:
        objgen = obj.objgen
        if objgen in pages:
            return ("page", pages[objgen])
        if objgen in _stack:  # 环形引用（/Parent ↔ /Kids）到此为止
            return ("cycle",)
        return object_signature(
            pdf, obj, _pages=pages, _stack=_stack + (objgen,), skip_keys=skip_keys
        )

    if isinstance(obj, pikepdf.Stream):
        body = hashlib.sha256(obj.read_bytes()).hexdigest()
        skip = {"/Length"} | set(skip_keys or ())
        return ("stream", _dict_signature(pdf, obj, pages, _stack, skip=skip), body)
    if isinstance(obj, pikepdf.Dictionary):
        return ("dict", _dict_signature(pdf, obj, pages, _stack, skip=skip_keys))
    if isinstance(obj, pikepdf.Array):
        return (
            "array",
            tuple(
                object_signature(
                    pdf, item, _pages=pages, _stack=_stack, skip_keys=skip_keys
                )
                for item in obj
            ),
        )
    if isinstance(obj, pikepdf.Name):
        return ("name", str(obj))
    if isinstance(obj, pikepdf.String):
        return ("string", bytes(obj))
    if isinstance(obj, bool):
        return ("bool", obj)
    if isinstance(obj, (int, float)):
        return ("number", float(obj))
    if obj is None:
        return ("null",)
    return ("other", repr(obj))  # pragma: no cover - 不该出现的类型


def _dict_signature(
    pdf: pikepdf.Pdf,
    obj: Any,
    pages: dict,
    stack: tuple,
    *,
    skip: set[str] | None = None,
) -> tuple:
    return tuple(
        sorted(
            (
                str(key),
                object_signature(pdf, value, _pages=pages, _stack=stack),
            )
            for key, value in obj.items()
            if skip is None or str(key) not in skip
        )
    )
