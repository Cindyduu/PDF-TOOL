"""标签状态检测：真实标签 / 无标签 / 仅标记位 / 检测失败。

判定原则：**不轻信 `/MarkInfo /Marked`**。只有同时具备
`/StructTreeRoot`、可用的 `/ParentTree`、每页 `/StructParents` 与内容流里的
`/MCID` 时才算"已有真实标签"。结构残缺一律按"仅有标记位"处理——这类文件
恰恰是最需要重新生成标签的。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pikepdf

from app.core.storage import utcnow_iso
from app.schemas import default_tag_detection

MCID_MARK = b"/MCID"


def _page_content_bytes(page: pikepdf.Object) -> bytes:
    contents = page.get("/Contents")
    if contents is None:
        return b""
    streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
    chunks: list[bytes] = []
    for stream in streams:
        try:
            chunks.append(stream.read_bytes())
        except Exception:  # pragma: no cover - 损坏的流不影响判定
            continue
    return b"".join(chunks)


def _parent_tree_keys(tree: pikepdf.Object) -> tuple[bool, set[int]]:
    """返回 (结构是否可用, ParentTree 里出现的 key 集合)。"""
    parent_tree = tree.get("/ParentTree")
    if parent_tree is None:
        return False, set()
    nums = parent_tree.get("/Nums")
    if not isinstance(nums, pikepdf.Array) or len(nums) == 0 or len(nums) % 2 != 0:
        return False, set()
    keys: set[int] = set()
    for index in range(0, len(nums), 2):
        try:
            keys.add(int(nums[index]))
        except (TypeError, ValueError):
            return False, set()
    return True, keys


def _analyse(pdf: pikepdf.Pdf) -> dict[str, Any]:
    root = pdf.Root
    tree = root.get("/StructTreeRoot")
    markinfo = root.get("/MarkInfo")
    has_struct_tree = tree is not None
    has_markinfo = markinfo is not None
    marked = bool(markinfo.get("/Marked")) if has_markinfo else False

    parent_tree_ok = False
    struct_parents_ok = False
    mcid_count = 0
    keys: set[int] = set()

    if has_struct_tree:
        parent_tree_ok, keys = _parent_tree_keys(tree)

        page_keys: list[int] = []
        for page in pdf.pages:
            obj = page.obj
            value = obj.get("/StructParents")
            if value is not None:
                try:
                    page_keys.append(int(value))
                except (TypeError, ValueError):
                    page_keys = []
                    break
            mcid_count += _page_content_bytes(obj).count(MCID_MARK)

        struct_parents_ok = bool(page_keys) and all(key in keys for key in page_keys)

    if not has_struct_tree and not marked:
        status = "untagged"
        message = "文档没有标签结构，需要生成"
    elif has_struct_tree and parent_tree_ok and struct_parents_ok and mcid_count > 0:
        status = "tagged"
        message = "文档已有可用的标签结构"
    else:
        status = "markinfo_only"
        message = "文档只有标记位，结构不完整，建议重新生成标签"

    payload = default_tag_detection()
    payload.update(
        status=status,
        checked_at=utcnow_iso(),
        has_struct_tree=has_struct_tree,
        has_markinfo=has_markinfo,
        marked=marked,
        parent_tree_ok=parent_tree_ok,
        struct_parents_ok=struct_parents_ok,
        mcid_count=mcid_count,
        message=message,
    )
    return payload


def analyse(pdf: pikepdf.Pdf) -> dict[str, Any]:
    """对**已打开**的文档做同一套判定。

    处理器在内存里改文档，没法再走 `detect_tags(path)` 的读文件路径；
    让它复用这份实现，才能保证「检测规则」与「生成前判断」是同一个标准。
    """
    return _analyse(pdf)


def detect_tags(path: Path) -> dict[str, Any]:
    """永不抛异常：任何读取失败都落到 `detection_failed`，让用户可重试。"""
    payload = default_tag_detection()
    payload["checked_at"] = utcnow_iso()
    try:
        pdf = pikepdf.open(path)
    except Exception as exc:
        payload.update(
            status="detection_failed",
            error=f"{type(exc).__name__}: {exc}",
            message="无法读取标签信息，请重试检测",
        )
        return payload

    try:
        with pdf:
            return _analyse(pdf)
    except Exception as exc:
        payload.update(
            status="detection_failed",
            error=f"{type(exc).__name__}: {exc}",
            message="标签检测过程中出错，请重试检测",
        )
        return payload
