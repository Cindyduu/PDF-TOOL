"""R7 生成带标签的 PDF：真的结构树，不是标记位。

生成顺序是刻意的，反过来就会产出「自称已加标签、实际没有结构」的坏文件：

1. 先清掉旧的标签痕迹（结构树、`/StructParents`、内容流里的旧 `/MCID`），
   否则重新生成会得到两套互相打架的标记；
2. 再按页把文本块对应到内容流指令区间，插入 `BDC`/`EMC`；
3. 然后才构造 `/StructElem` → `/ParentTree` → `/StructTreeRoot`；
4. **最后**才写 `/MarkInfo /Marked true`；
5. 收尾跑一遍 `verify.inspect_structure`，账对不上就当失败处理，
   绝不把一棵残缺的树交给流水线保存。

两条硬底线：

- **扫描件不生成空树**。没有文本层就如实报失败，生成一棵空树等于骗人；
- **只做保守推断**。字号明显偏大且行数少的块才算标题，其余一律段落。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Callable

import pikepdf
from pikepdf import Array, Dictionary, Name, Operator

from app.pdf.context import OpContext
from app.pdf.outcome import OpOutcome, new_counts
from app.pdf.tags import analyze, content_mapping, detect, verify

__all__ = [
    "process",
    "build_processor",
    "MCID_TOLERANCE",
    "ALLOWED_INSIDE_TAG",
]

# 匹配文本块与内容流文本点时允许的几何误差（pt）。
# 基线点算出来会和块矩形的边缘差一点点，1~2pt 足够覆盖渲染舍入。
MCID_TOLERANCE = 2.0

# 允许被同一段标记跨过的指令：文本状态类 + 绘制文字类。
# 中间夹了图形操作就断开——把无关绘图圈进结构元素会让阅读顺序错乱。
ALLOWED_INSIDE_TAG = content_mapping.TEXT_STATE_OPS | content_mapping.SHOW_TEXT_OPS

_CHOICE_SKIP = frozenset({"skip", "keep"})


# --------------------------------------------------------------------------- #
# 内容流改写
# --------------------------------------------------------------------------- #


def _strip_mcid_marks(instructions: list[Any]) -> tuple[list[Any], int]:
    """移除内容流里已有的 `/MCID` 标记对，返回 (新指令表, 移除对数)。

    重新生成时必须先清旧标记：留着它们，新标记会套在旧标记里，
    MCID 数量翻倍，ParentTree 立刻对不上账。
    """
    drop: set[int] = set()
    stack: list[tuple[int, bool]] = []
    removed = 0
    for index, instruction in enumerate(instructions):
        operator = str(instruction.operator)
        if operator in ("BDC", "BMC"):
            stack.append((index, content_mapping.mcid_of(instruction) is not None))
        elif operator == "EMC" and stack:
            start, is_mcid = stack.pop()
            if is_mcid:
                drop.add(start)
                drop.add(index)
                removed += 1
    if not drop:
        return list(instructions), 0
    return [item for index, item in enumerate(instructions) if index not in drop], removed


def _wrap(role: str, mcid: int) -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction(
        [Name(role), Dictionary(MCID=mcid)], Operator("BDC")
    )


def _close() -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction([], Operator("EMC"))


# --------------------------------------------------------------------------- #
# 块与内容流的对应
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Span:
    """一个文本块对应到的内容流区间（指令下标，闭区间）。"""

    block: analyze.TextBlock
    first: int
    last: int


def _inside(
    bbox: tuple[float, float, float, float],
    x: float,
    y: float,
    tolerance: float = MCID_TOLERANCE,
) -> bool:
    x0, y0, x1, y1 = bbox
    return x0 - tolerance <= x <= x1 + tolerance and y0 - tolerance <= y <= y1 + tolerance


def _clusters(
    indices: list[int],
    instructions: list[Any],
    foreign: set[int],
) -> list[list[int]]:
    """把同一个块的多条文本指令切成「内容流上连续」的若干段。"""
    groups: list[list[int]] = [[indices[0]]]
    for nxt in indices[1:]:
        previous = groups[-1][-1]
        blocked = False
        for position in range(previous + 1, nxt):
            if position in foreign or str(instructions[position].operator) not in ALLOWED_INSIDE_TAG:
                blocked = True
                break
        if blocked:
            groups.append([nxt])
        else:
            groups[-1].append(nxt)
    return groups


def match_spans(
    blocks: tuple[analyze.TextBlock, ...],
    runs: list[content_mapping.TextRun],
    instructions: list[Any],
    *,
    crop: tuple[float, float, float, float],
) -> tuple[list[Span], int]:
    """把文本块对应到内容流区间。返回 (区间列表, 有文字但没对上的块数)。"""
    if not blocks or not runs:
        return [], len(blocks)

    assigned: dict[int, list[int]] = {}
    for run in runs:
        x, y = content_mapping.user_to_text_space(run.x, run.y, crop)
        for block_index, block in enumerate(blocks):
            if _inside(block.bbox, x, y):
                assigned.setdefault(block_index, []).append(run.index)
                break

    all_runs = {run.index for run in runs}
    spans: list[Span] = []
    unmatched = 0
    for block_index, block in enumerate(blocks):
        indices = sorted(assigned.get(block_index, ()))
        if not indices:
            unmatched += 1
            continue
        foreign = all_runs - set(indices)
        groups = _clusters(indices, instructions, foreign)
        best = max(groups, key=len)
        if len(best) < len(indices):
            unmatched += 1  # 部分对上的块也记为「没完全对上」，如实上报
        spans.append(Span(block=block, first=best[0], last=best[-1]))

    spans.sort(key=lambda span: span.first)
    return spans, unmatched


# --------------------------------------------------------------------------- #
# 结构树
# --------------------------------------------------------------------------- #


def _strip_existing(pdf: pikepdf.Pdf) -> bool:
    """清掉旧的标签痕迹，返回是否清掉了东西。"""
    removed = False
    root = pdf.Root
    for key in ("/StructTreeRoot", "/MarkInfo"):
        if root.get(key) is not None:
            del root[key]
            removed = True
    for page in pdf.pages:
        if page.obj.get("/StructParents") is not None:
            del page.obj["/StructParents"]
            removed = True
    return removed


def _build_tree(
    pdf: pikepdf.Pdf,
    per_page: list[tuple[pikepdf.Object, list[tuple[analyze.TextBlock, int]]]],
) -> tuple[int, int, int]:
    """构造结构树。返回 (结构元素总数, MCID 总数, 页数)。"""
    root = pdf.make_indirect(Dictionary(Type=Name("/StructTreeRoot")))
    document = pdf.make_indirect(
        Dictionary(Type=Name("/StructElem"), S=Name("/Document"), P=root)
    )
    nums = Array()
    children = Array()
    mcid_total = 0
    key = 0

    for page_obj, entries in per_page:
        if not entries:
            continue
        page_obj.StructParents = key
        elements = Array()
        for mcid, (block, value) in enumerate(entries):
            element = pdf.make_indirect(
                Dictionary(
                    Type=Name("/StructElem"),
                    S=Name(block.tag),
                    P=document,
                    Pg=page_obj,
                    K=value,
                )
            )
            elements.append(element)
            children.append(element)
            mcid_total += 1
        nums.append(key)
        nums.append(elements)
        key += 1

    document.K = children
    root.K = Array([document])
    parent_tree = pdf.make_indirect(Dictionary(Nums=nums))
    root.ParentTree = parent_tree
    root.ParentTreeNextKey = key
    pdf.Root.StructTreeRoot = root
    return len(children) + 1, mcid_total, key


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def _snapshot(pdf: pikepdf.Pdf, detection: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": detection.get("status"),
        "has_struct_tree": detection.get("has_struct_tree"),
        "marked": detection.get("marked"),
        "mcid_count": detection.get("mcid_count"),
    }


def _fail(
    before: dict[str, Any],
    *,
    reason: str,
    message: str,
    next_action: str | None = None,
    warnings: tuple[str, ...] = (),
) -> OpOutcome:
    counts = new_counts(checked=1, failed=1)
    outcome = OpOutcome(
        before=before,
        after=before,
        counts=counts,
        reason_code=reason,
        message=message,
        next_action=next_action,
    )
    for warning in warnings:
        outcome.warn(warning)
    return outcome


def process(
    pdf: pikepdf.Pdf,
    ctx: OpContext,
    *,
    choice: str | None = None,
) -> OpOutcome:
    """生成标签结构。`choice` 为 `skip`/`keep` 时如实说明没做。

    `choice` 为 `None` 表示用户还没做选择。这时**不擅自生成**：
    按 PRD 第 5 节，无标签文档必须由用户明确选「生成」或「跳过」，
    默认替用户决定会产出他并不想要的结构。
    """
    detection = detect.analyse(pdf)
    before = _snapshot(pdf, detection)
    already_tagged = detection.get("status") == "tagged"

    if choice is None:
        return OpOutcome(
            before=before,
            after=before,
            counts=new_counts(checked=1, not_applicable=1),
            reason_code="tag_choice_missing",
            message="尚未选择标签处理方式，本次未生成标签结构",
            next_action="如需标签，请在配置中选择「生成标签」后重新处理",
        )

    if choice == "skip":
        return OpOutcome(
            before=before,
            after=before,
            counts=new_counts(checked=1, not_applicable=1),
            reason_code="tag_skipped",
            message="按你的选择跳过了标签处理，其他处理项不受影响",
        )

    if choice == "keep":
        if already_tagged:
            return OpOutcome(
                before=before,
                after=before,
                counts=new_counts(checked=1, compliant=1),
                reason_code="tag_kept",
                message="按你的选择保留了原有标签结构",
            )
        return OpOutcome(
            before=before,
            after=before,
            counts=new_counts(checked=1, not_applicable=1),
            reason_code="tag_skipped",
            message="文档没有真实标签结构，按你的选择保持原样",
        )

    if pdf.is_encrypted:
        return _fail(
            before,
            reason="tag_unsupported",
            message="文档已加密，无法生成标签结构",
            next_action="请先解除加密后重新上传",
        )

    try:
        buffer = io.BytesIO()
        pdf.save(buffer)
        text = analyze.analyze_document(buffer.getvalue())
    except Exception as exc:  # noqa: BLE001 - 分析失败要如实上报，不能装作成功
        return _fail(
            before,
            reason="tag_analysis_failed",
            message=f"读取文本结构失败：{type(exc).__name__}: {exc}",
            next_action="请确认源文件完整后重试，或跳过标签处理",
        )

    if text.errors:
        return _fail(
            before,
            reason="tag_analysis_failed",
            message="读取文本结构失败：" + "；".join(text.errors),
            next_action="请确认源文件完整后重试，或跳过标签处理",
        )

    if not text.has_text:
        return _fail(
            before,
            reason="tag_no_text",
            message="文档没有可提取的文字（可能是扫描件），未生成标签结构",
            next_action="如需可访问性支持，请先对文档做 OCR 后重新上传",
            warnings=("扫描件不生成空标签树，避免输出「自称已加标签」的文件",),
        )

    if text.page_count != len(pdf.pages):
        return _fail(
            before,
            reason="tag_analysis_failed",
            message="文本分析与页面数量不一致，已放弃生成标签结构",
            next_action="请确认源文件完整后重试",
        )

    if len(pdf.pages) != ctx.page_count:
        return _fail(
            before,
            reason="tag_analysis_failed",
            message="页面数量与上下文不一致，已放弃生成标签结构",
            next_action="请确认源文件完整后重试",
        )

    _strip_existing(pdf)

    per_page: list[tuple[pikepdf.Object, list[tuple[analyze.TextBlock, int]]]] = []
    partial = 0
    tagged_pages = 0
    for page_index, page in enumerate(pdf.pages):
        page_obj = page.obj
        instructions, _ = _strip_mcid_marks(content_mapping.page_instructions(page_obj))
        runs = content_mapping.runs_from_instructions(instructions)
        page_text = text.pages[page_index]
        spans, unmatched = match_spans(
            page_text.blocks,
            runs,
            instructions,
            crop=content_mapping.page_crop_box(page_obj),
        )
        partial += unmatched
        if not spans:
            per_page.append((page_obj, []))
            continue

        entries: list[tuple[analyze.TextBlock, int]] = []
        starts: dict[int, tuple[str, int]] = {}
        ends: dict[int, int] = {}
        for mcid, span in enumerate(spans):
            entries.append((span.block, mcid))
            starts[span.first] = (span.block.tag, mcid)
            ends[span.last] = mcid

        rebuilt: list[Any] = []
        for index, instruction in enumerate(instructions):
            opening = starts.get(index)
            if opening is not None:
                rebuilt.append(_wrap(opening[0], opening[1]))
            rebuilt.append(instruction)
            if index in ends:
                rebuilt.append(_close())

        page_obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(rebuilt))
        per_page.append((page_obj, entries))
        tagged_pages += 1

    if not any(entries for _, entries in per_page):
        return _fail(
            before,
            reason="tag_content_mapping_failed",
            message="无法把页面文字与内容流对应起来，未生成标签结构",
            next_action="该文档版式超出自动推断范围，可跳过标签处理",
        )

    _, mcid_total, _ = _build_tree(pdf, per_page)

    # 结构树齐了才写标记位——顺序反过来就是「自称已加标签」。
    pdf.Root.MarkInfo = Dictionary(Marked=True)

    report = verify.inspect_structure(pdf)
    if not report.ok:
        return _fail(
            before,
            reason="tag_structure_invalid",
            message="生成的标签结构未通过自检，已放弃本项处理",
            next_action="请重试，或跳过标签处理",
            warnings=tuple(report.problems),
        )

    counts = new_counts(checked=1, modified=1)
    after = {
        "status": "tagged",
        "struct_elements": report.struct_elements,
        "mcids": report.mcids,
        "headings": report.headings,
        "paragraphs": report.paragraphs,
        "pages_tagged": tagged_pages,
        "marked": True,
    }
    outcome = OpOutcome(
        before=before,
        after=after,
        counts=counts,
        status="review_required",
        reason_code="tag_regenerated" if already_tagged else "tag_generated",
        message=(
            f"已生成 {report.headings} 个标题、{report.paragraphs} 个段落，"
            f"共 {report.mcids} 个内容标记；结果待人工复核"
        ),
        next_action="标题层级与阅读顺序由程序推断，请人工复核后再对外使用",
    )
    if partial:
        outcome.warn(f"{partial} 个文本块未能完整对应到内容流，未纳入标签结构")
    if mcid_total == 0:  # pragma: no cover - 上面已拦住
        outcome.warn("没有生成任何内容标记")
    return outcome


def build_processor(choice: str | None) -> Callable[[pikepdf.Pdf, OpContext], OpOutcome]:
    """把用户的选择绑进处理器，交给 `run_pipeline(tag_processor=...)`。"""

    def _handler(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
        return process(pdf, ctx, choice=choice)

    return _handler
