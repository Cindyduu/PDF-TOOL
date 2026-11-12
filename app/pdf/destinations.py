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

## 为什么必须换类型而不是删参数

PDF 的页面目的地有 8 种 fit 类型，但只有 `/XYZ` 的第 5 项是缩放因子：

| 类型    | 参数            | 有缩放位？ |
|---------|-----------------|-----------|
| `/XYZ`  | 左 上 缩放      | 有        |
| `/Fit`  | —               | 无        |
| `/FitH` | 上              | 无        |
| `/FitV` | 左              | 无        |
| `/FitR` | 左 下 右 上     | 无        |
| `/FitB` | —               | 无        |
| `/FitBH`| 上              | 无        |
| `/FitBV`| 左              | 无        |

「承前缩放」在文件里只有一种等价写法：`[页 /XYZ 左 上 null]`。
所以 `/Fit`、`/FitR` 这类目标**必须换类型**，把能保留的位置参数搬到
`/XYZ` 的「左」「上」槽位，其余补 `null`。第一版把这些目标记成
「不适用」整批跳过，是把「没有缩放位」误当成了「不用改」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, String

__all__ = [
    "XYZ",
    "FIT_LABELS",
    "UNKNOWN_FIT",
    "UNKNOWN_FIT_LABEL",
    "PageResolver",
    "ResolvedDest",
    "collect_named_dests",
    "fit_breakdown",
    "fit_name",
    "resolve_dest",
    "to_inherit_xyz",
    "write_dest",
    "write_inherit_zoom",
]

XYZ = "/XYZ"

_MAX_TREE_DEPTH = 32

# Acrobat「缩放比率」下拉框里对每种目的地的称呼，供报告文案使用。
FIT_LABELS: dict[str, str] = {
    XYZ: "指定位置与缩放",
    "/Fit": "适合页面",
    "/FitH": "适合宽度",
    "/FitV": "适合高度",
    "/FitB": "适合可见区域",
    "/FitBH": "适合可见宽度",
    "/FitBV": "适合可见高度",
    "/FitR": "适合矩形区域",
}

UNKNOWN_FIT = "unknown"
UNKNOWN_FIT_LABEL = "未知目标类型"


def fit_name(fit_type: str | None) -> str:
    """fit 类型 → Acrobat 中文称呼；未知值降级，绝不抛异常。"""
    return FIT_LABELS.get(fit_type or "", UNKNOWN_FIT_LABEL)


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

    @property
    def fit_label(self) -> str:
        """Acrobat 口径的中文称呼；未知类型降级不抛异常。"""
        return fit_name(self.fit_type)

    @property
    def already_inherit(self) -> bool:
        """是否已经是承前缩放：只有 `/XYZ` 且缩放位为 `null` 才算。"""
        return self.fit_type == XYZ and self.zoom is None


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

    旧入口，保留为薄封装；新代码直接用 :func:`write_dest`。
    """
    return write_dest(slot, key, to_inherit_xyz(resolved), resolved)


# --------------------------------------------------------------------------- #
# 目标改写：任意 fit 类型 → 承前缩放
# --------------------------------------------------------------------------- #


def _at(array: Any, index: int) -> Any:
    """越界取参数返回 None：真实文档里 `[p /FitH]` 这种半截数组并不罕见。"""
    try:
        return array[index] if len(array) > index else None
    except (TypeError, KeyError):  # pragma: no cover - 防御
        return None


def _position(resolved: ResolvedDest) -> tuple[Any, Any]:
    """挑出能保留的位置语义，返回 `(左, 上)`。

    - `/XYZ` 两个都保留；
    - `/FitH`、`/FitBH` 只有纵向（上）；
    - `/FitV`、`/FitBV` 只有横向（左）；
    - `/Fit`、`/FitB`、`/FitR` 没有可搬的单值位置，全丢（`/FitR` 的矩形另行计数）。
    """
    fit = resolved.fit_type
    array = resolved.array
    if fit == XYZ:
        return _at(array, 2), _at(array, 3)
    if fit in ("/FitV", "/FitBV"):
        return _at(array, 2), None
    if fit in ("/FitH", "/FitBH"):
        return None, _at(array, 2)
    return None, None


def to_inherit_xyz(resolved: ResolvedDest) -> Array:
    """构造 `[页 /XYZ 左 上 null]`。

    第 0 项**原样搬运**：可能是页对象引用，也可能是少数老文档用的 0 基页序号，
    换掉它等于把书签跳到别的页。
    """
    left, top = _position(resolved)
    return Array([resolved.array[0], Name(XYZ), left, top, None])


def write_dest(slot: Any, key: str, array: Array, resolved: ResolvedDest) -> str:
    """把改写后的目标写回槽位。返回 `"copied"` 或 `"in_place"`。

    - 共享目标（命名目标 / 间接对象）：写回新数组，原件保持不动；
    - 独占目标：就地替换数组内容，不新建对象。
    """
    if resolved.shared:
        slot[Name(key)] = array
        return "copied"
    _replace_contents(resolved.array, array)
    return "in_place"


def _replace_contents(target: Any, source: Any) -> None:
    """就地替换数组内容：长度对齐后逐项写入。

    pikepdf 的 `Array` 不支持整体赋值，只能 `pop` / `append` / 按索引写。
    """
    items = list(source)
    while len(target) > len(items):
        target.pop()
    for index, value in enumerate(items):
        if index < len(target):
            target[index] = value
        else:
            target.append(value)


def fit_breakdown(items: list[Any], slot_getter: Any, ctx: Any) -> dict[str, int]:
    """统计各 fit 类型的条数，用于 before/after 实测值。"""
    breakdown: dict[str, int] = {}
    for item in items:
        slot, key = slot_getter(item)
        if slot is None:
            continue
        resolved = resolve_dest(slot[key], ctx.named, ctx.resolver)
        if resolved is None:
            continue
        fit = resolved.fit_type or UNKNOWN_FIT
        breakdown[fit] = breakdown.get(fit, 0) + 1
    return breakdown
