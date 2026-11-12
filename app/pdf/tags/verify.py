"""标签结构校验：只认**真实**结构，不认标记位。

判定「加了标签」的唯一依据是四个东西同时对上账：

1. 内容流里有 `BDC ... /MCID n` 标记；
2. 每个 `/MCID` 能在页面 `/StructParents` 对应的 `/ParentTree` 数组里按**下标**查到；
3. 数组里的每一项确实是一个 `/StructElem`，且它的 `/K` 就是那个 MCID；
4. 结构元素总数 = MCID 总数 = ParentTree 引用总数。

`/MarkInfo /Marked true` 只是附带条件：它证明不了任何结构存在，
所以本模块把它排在最后检查，缺结构时先报结构问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pikepdf

from app.pdf.tags.content_mapping import mcid_of

__all__ = [
    "TagReport",
    "page_mcids",
    "inspect_structure",
    "verify_tagged_file",
]

_ROLE_TAGS = frozenset(
    {"/P", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6", "/Document", "/Span", "/L", "/LI"}
)


@dataclass(frozen=True)
class TagReport:
    """标签结构实测结果。"""

    ok: bool = False
    marked: bool = False
    has_struct_tree: bool = False
    struct_elements: int = 0
    content_elements: int = 0
    mcids: int = 0
    parent_tree_refs: int = 0
    headings: int = 0
    paragraphs: int = 0
    pages_tagged: int = 0
    problems: tuple[str, ...] = field(default_factory=tuple)

    def raise_if_bad(self) -> None:
        if not self.ok:
            raise ValueError("；".join(self.problems) or "标签结构不完整")


# --------------------------------------------------------------------------- #
# 内容流
# --------------------------------------------------------------------------- #


def _mcid_value(instruction: Any) -> int | None:
    return mcid_of(instruction)


def page_mcids(page: pikepdf.Page | pikepdf.Object) -> list[int]:
    """按内容流顺序取出该页所有 `/MCID`。"""
    target = page.obj if isinstance(page, pikepdf.Page) else page
    try:
        instructions = list(pikepdf.parse_content_stream(target))
    except Exception:  # noqa: BLE001 - 流坏了就等于没有标记
        return []
    found: list[int] = []
    for instruction in instructions:
        value = _mcid_value(instruction)
        if value is not None:
            found.append(value)
    return found


# --------------------------------------------------------------------------- #
# 结构树
# --------------------------------------------------------------------------- #


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _collect(tree: pikepdf.Object) -> tuple[list[tuple[pikepdf.Object, int]], int, list[str]]:
    """遍历结构树。返回 (带 MCID 的元素列表, 结构元素总数, 问题列表)。"""
    content: list[tuple[pikepdf.Object, int]] = []
    problems: list[str] = []
    seen: set[tuple[int, int]] = set()
    total = 0
    stack: list[Any] = [tree]

    while stack:
        node = stack.pop()
        if not isinstance(node, pikepdf.Dictionary):
            problems.append("结构树里出现非字典节点")
            continue
        if node.is_indirect:
            objgen = node.objgen
            if objgen in seen:  # 环引用：结构树是图，不是树
                continue
            seen.add(objgen)
        if str(node.get("/Type") or "") == "/StructElem":
            total += 1
            role = str(node.get("/S") or "")
            if role and role not in _ROLE_TAGS:
                problems.append(f"结构元素使用了未知角色 {role}")

        kids = node.get("/K")
        if kids is None:
            continue
        if _is_int(kids):
            content.append((node, int(kids)))
        elif isinstance(kids, pikepdf.Array):
            for item in kids:
                if _is_int(item):
                    content.append((node, int(item)))
                elif isinstance(item, pikepdf.Dictionary):
                    stack.append(item)
                else:
                    problems.append("结构元素的 /K 里有无法识别的项")
        elif isinstance(kids, pikepdf.Dictionary):
            stack.append(kids)
        else:
            problems.append("结构元素的 /K 类型不正确")

    return content, total, problems


def _parent_tree(tree: pikepdf.Object) -> tuple[dict[int, Any], list[str]]:
    problems: list[str] = []
    parent_tree = tree.get("/ParentTree")
    if not isinstance(parent_tree, pikepdf.Dictionary):
        return {}, ["缺少 /ParentTree"]
    nums = parent_tree.get("/Nums")
    if not isinstance(nums, pikepdf.Array) or len(nums) % 2 != 0:
        return {}, ["/ParentTree 的 /Nums 缺失或长度不成对"]
    mapping: dict[int, Any] = {}
    for index in range(0, len(nums), 2):
        key = nums[index]
        if not _is_int(key):
            problems.append("/ParentTree 的键不是整数")
            continue
        mapping[int(key)] = nums[index + 1]
    return mapping, problems


def _inspect(pdf: pikepdf.Pdf) -> TagReport:
    root = pdf.Root
    tree = root.get("/StructTreeRoot")
    markinfo = root.get("/MarkInfo")
    marked = bool(markinfo.get("/Marked")) if isinstance(markinfo, pikepdf.Dictionary) else False

    if not isinstance(tree, pikepdf.Dictionary):
        return TagReport(
            marked=marked,
            problems=("缺少 /StructTreeRoot",),
        )

    problems: list[str] = []
    content, total, walk_problems = _collect(tree)
    problems.extend(walk_problems)
    mapping, tree_problems = _parent_tree(tree)
    problems.extend(tree_problems)

    mcids_total = 0
    refs_total = 0
    pages_tagged = 0
    for page_index, page in enumerate(pdf.pages):
        obj = page.obj
        mcids = page_mcids(obj)
        key_obj = obj.get("/StructParents")
        if key_obj is None:
            if mcids:
                problems.append(f"第 {page_index + 1} 页有 {len(mcids)} 个 MCID，但没有 /StructParents")
            continue
        pages_tagged += 1
        try:
            key = int(key_obj)
        except (TypeError, ValueError):
            problems.append(f"第 {page_index + 1} 页的 /StructParents 不是整数")
            continue
        if key not in mapping:
            problems.append(f"第 {page_index + 1} 页的 /StructParents {key} 在 ParentTree 里不存在")
            continue
        array = mapping[key]
        if not isinstance(array, pikepdf.Array):
            problems.append(f"ParentTree[{key}] 不是数组")
            continue
        refs_total += len(array)
        mcids_total += len(mcids)
        if len(array) != len(mcids):
            problems.append(
                f"第 {page_index + 1} 页 MCID 数（{len(mcids)}）与 ParentTree 引用数（{len(array)}）不一致"
            )
        if mcids and sorted(mcids) != list(range(len(mcids))):
            problems.append(f"第 {page_index + 1} 页的 MCID 不是从 0 开始的连续序列：{sorted(mcids)}")
        for mcid, entry in enumerate(array):
            if not isinstance(entry, pikepdf.Dictionary):
                problems.append(f"ParentTree[{key}][{mcid}] 不是结构元素")
                continue
            kid = entry.get("/K")
            if not _is_int(kid) or int(kid) != mcid:
                problems.append(f"ParentTree[{key}][{mcid}] 指向的结构元素 /K 不匹配")
            page_ref = entry.get("/Pg")
            if page_ref is None:
                problems.append(f"ParentTree[{key}][{mcid}] 的结构元素缺少 /Pg")

    if mcids_total == 0:
        problems.append("内容流里没有任何 MCID")
    if content and len(content) != mcids_total:
        problems.append(
            f"带 MCID 的结构元素数（{len(content)}）与 MCID 总数（{mcids_total}）不一致"
        )
    if refs_total != mcids_total:
        problems.append(f"ParentTree 引用数（{refs_total}）与 MCID 总数（{mcids_total}）不一致")
    if not marked:
        problems.append("缺少 /MarkInfo /Marked true")

    roles = [str(elem.get("/S") or "") for elem, _ in content]
    report = TagReport(
        ok=not problems,
        marked=marked,
        has_struct_tree=True,
        struct_elements=total,
        content_elements=len(content),
        mcids=mcids_total,
        parent_tree_refs=refs_total,
        headings=sum(1 for role in roles if role.startswith("/H")),
        paragraphs=sum(1 for role in roles if role == "/P"),
        pages_tagged=pages_tagged,
        problems=tuple(problems),
    )
    return report


def inspect_structure(pdf: pikepdf.Pdf) -> TagReport:
    """对已打开的文档做结构校验；内部出错也落成问题列表，不抛异常。"""
    try:
        return _inspect(pdf)
    except Exception as exc:  # noqa: BLE001 - 校验本身不能把流水线带崩
        return TagReport(problems=(f"标签结构校验出错：{type(exc).__name__}: {exc}",))


def verify_tagged_file(path: Path | str) -> TagReport:
    """打开磁盘上的文件再验一次——落盘之后才算数。"""
    try:
        with pikepdf.open(path) as pdf:
            return _inspect(pdf)
    except Exception as exc:  # noqa: BLE001
        return TagReport(problems=(f"无法打开文件：{type(exc).__name__}: {exc}",))
