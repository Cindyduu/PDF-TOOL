"""R22 / R37 / R40 书签校验：存在性 + 承前缩放。

**不自动生成书签**（§1.3 第 2 条）——书签的层级和标题是编辑判断，
自动生成的目录树对 eCTD 审阅者没有任何帮助。

v4.3（R40）把**页数门槛与承前缩放判定解耦**。原来第一句就是
`if total <= MIN_PAGES: return skipped`，完全不看书签，后果是「3 页文档、
2 个书签、全是 `/Fit`」被判 `skipped`——明明有缺陷，报告什么都不说。
现在的顺序是**先看有没有书签，再按页数决定「没有书签」算不算问题**：

- 有可用书签 → 无论几页，都走承前缩放判定；
- 没有可用书签 → ≤5 页 `skipped`（短文档不要求书签）、>5 页 `warning`。

`warning` 而不是 `fail`（D-18）：缺书签是内容缺失，工具补不了，但也不该和
「书签树损坏」同级——后者是数据坏了，必须修。

「有 `/Outlines` 但 `get_toc()` 为空」单独成一条（PRD 明确要求）：
空书签树和没有书签树是两回事——前者说明有人动过书签，只是把它清空了。
**结构异常不受页数豁免**：树里确实有条目，只是读不出来，页数少不代表树可以坏。

v4.2（R37）的承前缩放判定必须与处理侧 `app/pdf/bookmarks.py` 逐字一致，
否则会出现「处理说全改完了、校验说还有 3 个没改」。所以这里不新写遍历逻辑，
直接复用 `iter_outline_items()` / `dest_slot()` 与 `destinations.resolve_dest()`
——**同一套函数，同一个结论**。

三种状态的划分（D-14 / D-18）：

- 书签树损坏 / 读取失败 → `fail`：这是数据损坏，工具不敢动；
- 有书签但缩放不合规 → `warning`：处理项 ① 能自动改，判 `fail` 会让
  「缺书签」和「缩放不对」在汇总里同等严重，用户分不清哪个必须手工补；
- 没有书签 → >5 页 `warning`、≤5 页 `skipped`。
"""

from __future__ import annotations

import pikepdf
from pikepdf import Dictionary

from app.pdf.bookmarks import dest_slot, iter_outline_items
from app.pdf.checks import CheckContext, CheckResult, Finding, make_result
from app.pdf.context import OpContext
from app.pdf.destinations import resolve_dest
from app.pdf.view_labels import zoom_label

__all__ = ["run", "MIN_PAGES", "TITLE_LIMIT"]

#: 低于这个页数**不要求**有书签（不是「不检查」）。
#:
#: v4.3 起这个常量只影响「没有书签」这一种情形的结论；只要文档里有书签，
#: 不管几页都要查承前缩放。名称保留是为了兼容既有引用。
MIN_PAGES = 5

#: 明细里书签标题的截断长度。真实书签标题动辄整段句子，全写进报告会把
#: 另外两条明细挤出屏幕；40 字足够认出是哪一条。
TITLE_LIMIT = 40


def _title(item: object) -> str:
    """取书签标题并压成单行；取不到时给一个不会误导的占位。"""
    try:
        raw = item.get("/Title")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 坏对象连取键都可能炸
        return "（无标题）"
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return "（无标题）"
    text = " ".join(text.split())
    return text if len(text) <= TITLE_LIMIT else text[: TITLE_LIMIT - 1] + "…"


def run(ctx: CheckContext) -> CheckResult:
    total = int(ctx.page_count)

    try:
        outlines = ctx.pdf.Root.get("/Outlines")
    except Exception as exc:  # noqa: BLE001
        return _read_failure(exc)

    if outlines is not None and not isinstance(outlines, Dictionary):
        return _read_failure("/Outlines 指向的不是字典对象")

    try:
        toc = ctx.doc.get_toc(simple=True)
    except Exception as exc:  # noqa: BLE001 - AC-R22-E1
        return _read_failure(exc)

    # 有书签：页数不再是门槛，一律查承前缩放（R40 的核心变更）。
    if toc:
        return _check_inherit_zoom(ctx, len(toc))

    # 没有可用条目。先分辨「树坏了」和「确实没书签」——前者与页数无关。
    if outlines is not None:
        # `get_toc()` 读不出条目有两种截然不同的原因，文案不能混：
        # 真的空树（有人清空了书签）和树结构坏了（`/First` 成环、节点互指）。
        # 后者用我们自己的环保护遍历能走通，据此分辨。
        nodes = list(iter_outline_items(ctx.pdf))
        if nodes:
            return make_result(
                "bookmarks",
                "fail",
                "书签树结构异常",
                [
                    Finding(
                        level="fail",
                        message="书签树结构异常",
                        detail=(
                            f"大纲项存在成环或非法链接，无法正常读取"
                            f"（遍历到 {len(nodes)} 个节点）"
                        ),
                    )
                ],
            )
        if total <= MIN_PAGES:
            return _skipped(total, empty_tree=True)
        return make_result(
            "bookmarks",
            "warning",
            "存在空书签树",
            [
                Finding(
                    level="warning",
                    message="存在空书签树",
                    detail=f"/Outlines 存在但没有任何书签条目（共 {total} 页）",
                )
            ],
        )

    if total <= MIN_PAGES:
        return _skipped(total, empty_tree=False)
    return make_result(
        "bookmarks",
        "warning",
        f"{total} 页文档无书签",
        [
            Finding(
                level="warning",
                message=f"{total} 页文档无书签",
                detail="超过 5 页的文档需要书签导航；本工具不自动生成书签",
            )
        ],
    )


def _skipped(total: int, *, empty_tree: bool) -> CheckResult:
    """≤5 页且没有可用书签：跳过，但要说清是「没书签」还是「空书签树」。"""
    summary = f"页数 ≤{MIN_PAGES} 且无书签，跳过书签校验"
    detail = f"本文档共 {total} 页，短文档不要求书签"
    if empty_tree:
        detail += "；/Outlines 存在但没有任何条目"
    return make_result(
        "bookmarks",
        "skipped",
        summary,
        [Finding(level="skipped", message=summary, detail=detail)],
    )


def _read_failure(reason: object) -> CheckResult:
    """书签树读不了：记 fail，说清是读取失败而不是「没有书签」。"""
    if isinstance(reason, BaseException):
        detail = f"{type(reason).__name__}：{reason}".strip()
    else:
        detail = str(reason)
    return make_result(
        "bookmarks",
        "fail",
        "书签树读取失败",
        [Finding(level="fail", message="书签树读取失败", detail=detail)],
    )


def _check_inherit_zoom(ctx: CheckContext, toc_count: int) -> CheckResult:
    """逐个书签判定目的地是否为承前缩放（`/XYZ` + `null`）。

    分母只算「有可解析页面目的地」的书签：纯标题节点、`/A /S /URI` 这类
    没有页面跳转的条目，本来就没有缩放可言，算进分母会让百分比永远到不了 100%。
    """
    opctx = OpContext.build(ctx.pdf)
    items = list(iter_outline_items(ctx.pdf))

    inherit = 0
    explicit = 0
    unresolved: list[Finding] = []
    bad: list[Finding] = []

    for item in items:
        slot, key = dest_slot(item)
        if slot is None:
            # 纯标题节点或外部链接动作：没有页面目的地，不参与缩放判定。
            continue

        try:
            resolved = resolve_dest(slot[key], opctx.named, opctx.resolver)
        except Exception as exc:  # noqa: BLE001 - 单个坏目标不拖垮整项
            unresolved.append(
                Finding(
                    level="warning",
                    message=f"书签「{_title(item)}」的目标无法解析",
                    detail=f"{type(exc).__name__}：{exc}".strip(),
                )
            )
            continue

        if resolved is None:
            unresolved.append(
                Finding(
                    level="warning",
                    message=f"书签「{_title(item)}」的目标无法解析",
                    detail="命名目标不存在，或目的地数组结构非法",
                )
            )
            continue

        if resolved.already_inherit:
            inherit += 1
            continue

        explicit += 1
        current = zoom_label(resolved.fit_type, resolved.zoom)
        bad.append(
            Finding(
                level="warning",
                page=resolved.page_number,
                message=f"书签「{_title(item)}」未设为承前缩放",
                detail=f"当前 {current}（{resolved.fit_type or '未知'}），应为 承前缩放（/XYZ null）",
            )
        )

    checked = inherit + explicit

    # 全部书签都没有页面目的地：书签存在但点不动，这是缺陷，不是合规。
    if checked == 0:
        findings = list(unresolved) or [
            Finding(
                level="warning",
                message="书签均无页面跳转目标",
                detail=f"共 {len(items)} 个书签条目，没有一个指向本文档页面",
            )
        ]
        return make_result(
            "bookmarks",
            "warning",
            f"{toc_count} 个书签均无页面跳转目标",
            findings,
        )

    if explicit == 0 and not unresolved:
        return make_result(
            "bookmarks",
            "pass",
            f"{toc_count} 个书签，全部为承前缩放",
            [
                Finding(
                    level="pass",
                    message=f"{toc_count} 个书签，全部为承前缩放",
                    detail=f"{checked} 个指向本文档页面的书签均为 /XYZ null",
                )
            ],
        )

    if explicit == 0:
        return make_result(
            "bookmarks",
            "warning",
            f"{toc_count} 个书签均为承前缩放，另有 {len(unresolved)} 个目标无法解析",
            list(unresolved),
        )

    summary = (
        f"{toc_count} 个书签，其中 {explicit} 个未设为承前缩放"
        if explicit < checked
        else f"{toc_count} 个书签均未设为承前缩放"
    )
    findings = bad + unresolved
    if unresolved:
        summary += f"，另有 {len(unresolved)} 个目标无法解析"
    return make_result("bookmarks", "warning", summary, findings)
