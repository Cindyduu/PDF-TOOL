"""目的地解析与「承前缩放」改写。

书签（R1）与链接（R9）处理的是同一批 PDF 结构，必须共用同一套解析逻辑，
否则两处会对同一个目标得出不同结论。

三个概念：

- **目的地数组**：`[页对象 /XYZ 左 上 缩放]`。缩放因子为 `null` 即「承前缩放」。
- **命名目标**：`/Names /Dests` 名字树（或旧的 `/Root /Dests` 字典）里的目标。
  同一个名字可能被多处引用，**改它等于改了所有引用方**。
- **槽位**：真正存放 `/Dest` 或 `/A /GoTo /D` 的那个字典。

改写规则只有一条：**绝不原地修改可能被共享的目标对象**。
共享对象（命名目标，或间接对象）一律复制一份再改，把复制品写回槽位；
豁免方仍然指向原件，因此一个字节都不受影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, String

__all__ = [
    "XYZ",
    "PageResolver",
    "ResolvedDest",
    "collect_named_dests",
    "resolve_dest",
    "write_inherit_zoom",
]

XYZ = "/XYZ"

_MAX_TREE_DEPTH = 32


class PageResolver:
    """把「目的地数组第 0 项」翻译成 1 基页码。"""

    def __init__(self, pdf: pikepdf.Pdf) -> None:
        self._by_objgen: dict[tuple[int, int], int] = {}
        self.page_count = len(pdf.pages)
        for index, page in enumerate(pdf.pages, start=1):
            self._by_objgen[page.obj.objgen] = index

    def page_number(self, target: Any) -> int | None:
        """返回 1 基页码；解析不出来返回 None（调用方按失败处理）。"""
        if target is None:
            return None
        try:
            if target.is_indirect:
                return self._by_objgen.get(target.objgen)
        except (AttributeError, TypeError):
            return None
        # 少数老文档直接写 0 基页序号
        try:
            index = int(target)
        except (TypeError, ValueError):
            return None
        if 0 <= index < self.page_count:
            return index + 1
        return None


def collect_named_dests(pdf: pikepdf.Pdf) -> dict[str, Any]:
    """收集全部命名目标：名字 → 目标对象（数组或 `{/D: 数组}`）。"""
    result: dict[str, Any] = {}
    root = pdf.Root
    names = root.get("/Names")
    if names is not None:
        tree = names.get("/Dests")
        if tree is not None:
            _walk_name_tree(tree, result, 0)
    legacy = root.get("/Dests")
    if isinstance(legacy, Dictionary):
        for key, value in legacy.items():
            result[str(key).lstrip("/")] = value
    return result


def _walk_name_tree(node: Any, out: dict[str, Any], depth: int) -> None:
    if depth > _MAX_TREE_DEPTH or not isinstance(node, Dictionary):
        return
    pairs = node.get("/Names")
    if isinstance(pairs, Array):
        for index in range(0, len(pairs) - 1, 2):
            out[str(pairs[index])] = pairs[index + 1]
    kids = node.get("/Kids")
    if isinstance(kids, Array):
        for kid in kids:
            _walk_name_tree(kid, out, depth + 1)


@dataclass(frozen=True)
class ResolvedDest:
    """解析后的目的地。"""

    array: Any
    via_name: str | None
    page_number: int | None
    fit_type: str | None
    zoom: Any
    zoom_index: int | None

    @property
    def shared(self) -> bool:
        """该目标对象是否可能被别的槽位共享。"""
        if self.via_name is not None:
            return True
        try:
            return bool(self.array.is_indirect)
        except AttributeError:  # pragma: no cover - 防御
            return True


def _is_name_ref(value: Any) -> bool:
    return isinstance(value, (String, Name))


def resolve_dest(
    raw: Any,
    named: dict[str, Any],
    resolver: PageResolver,
) -> ResolvedDest | None:
    """把任意形态的目的地解成结构化结果；无法解析返回 None。"""
    if raw is None:
        return None
    value = raw
    via_name: str | None = None
    if _is_name_ref(value):
        via_name = str(value)
        value = named.get(via_name)
        if value is None:
            return None
    if isinstance(value, Dictionary):
        value = value.get("/D")
    if not isinstance(value, Array) or len(value) == 0:
        return None

    fit_type = str(value[1]) if len(value) > 1 else None
    zoom_index = 4 if fit_type == XYZ else None
    zoom = value[4] if zoom_index is not None and len(value) > 4 else None
    return ResolvedDest(
        array=value,
        via_name=via_name,
        page_number=resolver.page_number(value[0]),
        fit_type=fit_type,
        zoom=zoom,
        zoom_index=zoom_index,
    )


def write_inherit_zoom(slot: Any, key: str, resolved: ResolvedDest) -> str:
    """把承前缩放写回槽位。返回 `"copied"` 或 `"in_place"`。

    - 共享目标：复制一份改完写回槽位，原件保持不动；
    - 独占目标：直接改数组第 5 项。
    """
    if resolved.zoom_index is None:  # pragma: no cover - 调用方已过滤
        raise ValueError("目标不是 /XYZ 类型，没有缩放因子可改")
    if resolved.shared:
        duplicate = Array(list(resolved.array))
        duplicate[resolved.zoom_index] = None
        slot[Name(key)] = duplicate
        return "copied"
    resolved.array[resolved.zoom_index] = None
    return "in_place"
