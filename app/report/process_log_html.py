"""处理日志 HTML 渲染（R28）。

与校验报告的分工：校验报告回答「文件现在符不符合」，日志回答「工具做了什么」。
所以这里的行是**处理项**，列是「处理前 / 处理后 / 计数 / 原因」。

数据来源只有一份：`JobService.build_report()` 的产物（也就是 report.json）。
R28 明确要求「JSON 是机器数据源，HTML 是人读视图，HTML 从 JSON 渲染」——
所以这里不重新读 job.json，也不重新算计数，避免出现「日志说改了 12 处、
结果页说改了 11 处」这种两套账。
"""

from __future__ import annotations

from typing import Any, Sequence

from app.config import APP_NAME, APP_VERSION
from app.pdf.content_fingerprint import describe_integrity
from app.report.base import (
    REPORT_DISCLAIMER,
    escape,
    format_bytes,
    format_duration,
    parse_iso,
    render_document,
    text,
    value_text,
)
from app.schemas import COUNT_KEYS, COUNT_LABELS, RESULT_STATUS_LABELS

__all__ = ["render_process_log"]

_JOB_STATUS_LABELS: dict[str, str] = {
    "configuring": "待配置",
    "verify_queued": "校验排队中",
    "verifying": "校验中",
    "verified": "校验完成",
    "queued": "排队中",
    "running": "处理中",
    "process_queued": "处理排队中",
    "processing": "处理中",
    "post_verifying": "后置校验中",
    "completed": "已完成",
    "completed_with_warnings": "已完成（有提示）",
    "failed": "失败",
    "interrupted": "已中断",
}


def _counts_text(counts: Any) -> str:
    """六元组计数只显示非零项。全零时给破折号，不给六个 0。"""
    if not isinstance(counts, dict):
        return "—"
    parts = [
        f"{COUNT_LABELS[key]} {int(counts.get(key) or 0)}"
        for key in COUNT_KEYS
        if isinstance(counts.get(key), int) and int(counts.get(key) or 0)
    ]
    return escape(" · ".join(parts)) if parts else "—"


def _reason_text(item: dict[str, Any]) -> str:
    """原因列：message 为主，reason_code / warnings / next_action 依次补充。"""
    chunks: list[str] = []
    message = item.get("message")
    if message:
        chunks.append(f"<span>{value_text(message)}</span>")
    code = item.get("reason_code")
    if code:
        chunks.append(f'<span class="dim">原因码 {escape(code)}</span>')
    for warning in item.get("warnings") or []:
        chunks.append(f'<span class="dim">提示：{value_text(warning)}</span>')
    next_action = item.get("next_action")
    if next_action:
        chunks.append(f'<span class="dim">下一步：{value_text(next_action)}</span>')
    return "<br>".join(chunks) if chunks else "—"


def _operation_rows(results: Sequence[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        title = item.get("title") or item.get("key") or "未知处理项"
        rows.append(
            "<tr>"
            f"<td>{text(title)}</td>"
            f'<td><span class="tag {escape(status)}">'
            f"{escape(RESULT_STATUS_LABELS.get(status, status))}</span></td>"
            f"<td>{value_text(item.get('before'))}</td>"
            f"<td>{value_text(item.get('after'))}</td>"
            f'<td class="num">{_counts_text(item.get("counts"))}</td>'
            f"<td>{_reason_text(item)}</td>"
            "</tr>"
        )
    if not rows:
        return '<p class="empty">本次没有执行任何处理项。</p>'
    return (
        "<table>"
        "<thead><tr>"
        '<th style="width:180px">处理项</th>'
        '<th style="width:96px">状态</th>'
        "<th>处理前</th>"
        "<th>处理后</th>"
        '<th style="width:190px">计数</th>'
        "<th>原因</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _error_block(report: dict[str, Any]) -> str:
    """失败项：错误码 + next_action（R28 明确要求这两样）。"""
    error = report.get("last_error")
    if not isinstance(error, dict) or not error:
        return ""
    rows = [
        ("错误码", error.get("code")),
        ("说明", error.get("message")),
        ("阶段", error.get("stage")),
        ("下一步", error.get("next_action")),
    ]
    cells = "".join(
        f"<tr><th>{text(label)}</th><td>{value_text(value)}</td></tr>"
        for label, value in rows
        if value
    )
    return f'<table><tbody>{cells}</tbody></table>'


def _integrity_block(report: dict[str, Any]) -> str:
    """R36 内容一致性：一行结论 + 变化页号与归因明细。

    结论文案直接用报告里的 `content_integrity_summary`，不在渲染层重算。
    那句话由 `content_fingerprint.describe_integrity` 生成，结果页用的是同一个
    函数——两处各算一遍，迟早会出现「日志说 3 页变化、结果页说 2 页」。

    `content_integrity` 为 `None`（老任务，或护栏在建指纹前就抛了）时照样出这一行，
    写「未核对」。这一行的存在本身就是结论：用户需要知道**这件事没被验证过**，
    而不是从「没有这一行」去猜。
    """
    data = report.get("content_integrity")
    summary = report.get("content_integrity_summary") or describe_integrity(data)
    rows: list[tuple[str, Any]] = [("结论", summary)]
    if isinstance(data, dict):
        counts = data.get("page_count") or {}
        if counts.get("before") is not None:
            rows.append(("页数", f"{counts.get('before')} → {counts.get('after')}"))
        changed = data.get("changed_pages") or {}
        content_pages = list(changed.get("content") or [])
        text_pages = list(changed.get("text") or [])
        if content_pages:
            rows.append(("内容流变化页", _page_list(content_pages)))
        if text_pages:
            rows.append(("文本层变化页", _page_list(text_pages)))
        attributed = [str(item) for item in (data.get("attributed_to") or [])]
        if attributed:
            rows.append(("归因到", "、".join(attributed)))
        problems = [str(item) for item in (data.get("problems") or [])]
        if problems:
            rows.append(("问题", "；".join(problems)))
    cells = "".join(
        f"<tr><th>{text(label)}</th><td>{value_text(value)}</td></tr>"
        for label, value in rows
    )
    # 标题不能省。这张表的首列是「结论 / 页数 / 内容流变化页」，没有标题时
    # 读者只能靠猜知道它在讲什么——而这一块恰恰是用户最需要看懂的地方
    # （「工具到底有没有动我的内容」）。
    return (
        "<h3>内容一致性</h3>"
        f'<table class="integrity"><tbody>{cells}</tbody></table>'
    )


def _page_list(pages: Sequence[Any], limit: int = 10) -> str:
    """页号列表：超过 `limit` 页就折叠，避免 200 页的文件把表格撑爆。"""
    shown = "、".join(str(page) for page in pages[:limit])
    if len(pages) > limit:
        shown += f" 等 {len(pages)} 页"
    return shown


def _file_section(report: dict[str, Any]) -> str:
    source = report.get("source") or {}
    output = report.get("output") or {}
    status = str(report.get("status") or "")
    original = report.get("original_name") or source.get("original_name") or "（未命名文件）"
    produced = output.get("file_name")

    pages = output.get("page_count") or source.get("page_count")
    size = output.get("size") or source.get("size")
    facts = " · ".join(
        part
        for part in (
            f"页数 {pages}" if pages is not None else "",
            f"输出 {format_bytes(size)}" if output else "",
            f"原始 {format_bytes(source.get('size'))}" if source.get("size") else "",
        )
        if part
    )

    lines = [
        '<section class="file-log">',
        "<h2>"
        f'{value_text(original)}'
        f' <span class="tag plain">{escape(_JOB_STATUS_LABELS.get(status, status))}</span>'
        "</h2>",
        f'<p class="dim">'
        f"任务号 {escape(str(report.get('job_id') or '—'))} · "
        f"开始 {escape(str(report.get('started_at') or '—'))} · "
        f"结束 {escape(str(report.get('finished_at') or '—'))} · "
        f"{escape(facts or '无产物信息')}</p>",
        '<p class="dim">'
        f"输出文件 {value_text(produced) if produced else '（无产物）'}"
        "</p>",
    ]
    error_html = _error_block(report)
    if error_html:
        lines.append(error_html)
    lines.append(_operation_rows([item for item in (report.get("results") or []) if isinstance(item, dict)]))
    lines.append(_integrity_block(report))
    lines.append("</section>")
    return "\n".join(lines)


def _batch_span(reports: Sequence[dict[str, Any]]) -> tuple[str, str]:
    """批次的开始/结束：取所有文件里最早开始、最晚结束。"""
    starts = [
        moment
        for moment in (parse_iso(report.get("started_at")) for report in reports)
        if moment is not None
    ]
    ends = [
        moment
        for moment in (parse_iso(report.get("finished_at")) for report in reports)
        if moment is not None
    ]
    start = min(starts).isoformat().replace("+00:00", "Z") if starts else "—"
    end = max(ends).isoformat().replace("+00:00", "Z") if ends else "—"
    return start, end


def render_process_log(
    *,
    batch_id: str,
    reports: Sequence[dict[str, Any]],
    generated_at: str,
) -> str:
    """把一批 `build_report()` 的产物渲染成一份可离线打开的 HTML 日志。"""
    items = [report for report in reports if isinstance(report, dict)]
    start, end = _batch_span(items)

    succeeded = sum(
        1
        for report in items
        if report.get("status") in ("completed", "completed_with_warnings")
    )
    failed = sum(
        1 for report in items if report.get("status") in ("failed", "interrupted")
    )

    meta = [
        ("批次 ID", batch_id),
        ("文件数", len(items)),
        ("开始时间", start),
        ("结束时间", end),
        ("耗时", format_duration(start, end)),
        ("成功 / 失败", f"{succeeded} / {failed}"),
        ("生成时间", generated_at),
    ]

    body: list[str] = ["<h2>逐文件处理记录</h2>"]
    if items:
        body.extend(_file_section(report) for report in items)
    else:
        body.append('<p class="empty">本批次没有可展示的处理记录。</p>')

    return render_document(
        title=f"处理日志 - {APP_NAME}",
        heading="处理日志",
        subtitle=f"{APP_NAME} {APP_VERSION} · 批次 {batch_id}",
        meta=meta,
        body="\n".join(body),
        footer=(
            f"生成时间：{generated_at}",
            f"应用版本：{APP_NAME} {APP_VERSION}",
            REPORT_DISCLAIMER,
        ),
    )
