"""校验报告 HTML 渲染（R23）。

两个入口，一份实现：

- `render_batch_report(report)`：批次级，入参是 `JobService.batch_verify_report()`
  的返回（§8.4）；
- `render_file_report(result)`：单文件，入参是一条 §8.3 `VerifyResult`。

**纯函数**：不读盘、不写盘、不查数据库。R23 明确要求「报告不落盘为持久文件，
按需生成」，所以这里只负责把已经算好的数据排版，端点每次请求现渲染。

渲染出来的必须是**单文件、无外链**：`<style>` 内联、无 `<script>`、无 `<img>`、
无 `@import`。这样用户把 .html 拷到断网机器上双击也能看（AC-R23-N4）。
"""

from __future__ import annotations

from typing import Any, Sequence

from app.config import APP_NAME, APP_VERSION
from app.report.base import (
    REPORT_DISCLAIMER,
    escape,
    format_bytes,
    render_document,
    text,
    value_text,
)
from app.schemas import VERIFY_STATUS_LABELS, VERIFY_TITLES

__all__ = [
    "PHASE_TITLES",
    "render_batch_report",
    "render_file_report",
]

PHASE_TITLES: dict[str, str] = {
    "precheck": "前置校验",
    "post": "后置校验",
}

# 报告里 `skipped` 不写成「—」：破折号在表格里等于空单元格，
# 用户会以为这一项漏了。报告是给人读的，宁可多写两个字。
_TAG_TEXT: dict[str, str] = {
    "pass": VERIFY_STATUS_LABELS["pass"],
    "warning": VERIFY_STATUS_LABELS["warning"],
    "fail": VERIFY_STATUS_LABELS["fail"],
    "skipped": "跳过",
}

_STATUS_ORDER = ("pass", "warning", "fail", "skipped")


# --------------------------------------------------------------------------- #
# 单文件区块
# --------------------------------------------------------------------------- #


def _finding_html(finding: dict[str, Any]) -> str:
    level = str(finding.get("level") or "")
    message = finding.get("message")
    detail = finding.get("detail")
    page = finding.get("page")
    parts: list[str] = []
    if level:
        parts.append(f'<span class="tag {escape(level)}">{escape(_TAG_TEXT.get(level, level))}</span>')
    if isinstance(page, int) and not isinstance(page, bool):
        parts.append(f'<span class="dim">第 {page} 页</span>')
    parts.append(f"<span>{value_text(message)}</span>")
    if detail:
        parts.append(f'<span class="dim">{value_text(detail)}</span>')
    return "<li>" + " ".join(parts) + "</li>"


def _item_row(index: int, item: dict[str, Any]) -> str:
    key = str(item.get("key") or "")
    status = str(item.get("status") or "")
    title = VERIFY_TITLES.get(key, key or "未知校验项")
    findings = [f for f in (item.get("findings") or []) if isinstance(f, dict)]
    findings_html = (
        '<ul class="findings">' + "".join(_finding_html(f) for f in findings) + "</ul>"
        if findings
        else ""
    )
    duration = item.get("duration_ms")
    duration_text = (
        f"{int(duration)} ms"
        if isinstance(duration, int) and not isinstance(duration, bool)
        else "—"
    )
    return (
        "<tr>"
        f'<td class="num">{index}</td>'
        f"<td>{text(title)}</td>"
        f'<td><span class="tag {escape(status)}">{escape(_TAG_TEXT.get(status, status))}</span></td>'
        f"<td>{value_text(item.get('summary'))}{findings_html}</td>"
        f'<td class="num dim">{escape(duration_text)}</td>'
        "</tr>"
    )


def _file_block(result: dict[str, Any]) -> str:
    """一个文件的折叠区块：折叠头显示摘要，展开后是十二项明细（AC-R23-N2）。"""
    file_info = result.get("file") or {}
    name = file_info.get("name") or "（未命名文件）"
    items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    summary = result.get("summary") or {}

    counts = " · ".join(
        f"{_TAG_TEXT[status]} {int(summary.get(status) or 0)}"
        for status in _STATUS_ORDER
        if summary.get(status)
    )
    if not counts:
        counts = "无结果"

    if items:
        rows = "".join(_item_row(index, item) for index, item in enumerate(items, start=1))
        table = (
            "<table>"
            "<thead><tr>"
            '<th style="width:48px">序号</th>'
            "<th style=\"width:150px\">校验项</th>"
            '<th style="width:96px">状态</th>'
            "<th>说明与明细</th>"
            '<th style="width:84px">耗时</th>'
            "</tr></thead>"
            f"<tbody>{rows}</tbody>"
            "</table>"
        )
    else:
        table = '<p class="empty">本次未执行任何校验项。</p>'

    facts = " · ".join(
        part
        for part in (
            f"页数 {file_info['page_count']}" if file_info.get("page_count") is not None else "",
            format_bytes(file_info.get("size_bytes")),
        )
        if part
    )
    return (
        '<details class="file">'
        "<summary>"
        f'<span class="fname">{value_text(name)}</span>'
        f'<span class="dim">{escape(facts)}</span>'
        f'<span class="dim">{escape(counts)}</span>'
        "</summary>"
        f'<div class="body-pad">{table}</div>'
        "</details>"
    )


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


def _aggregate(files: Sequence[dict[str, Any]]) -> dict[str, int]:
    """按「文件 × 项」计数，与 §8.4 的 summary 口径一致。

    不取"每个文件的最差项"来汇总：那会让 3 个文件里各有 1 项 FAIL 变成
    「FAIL 1」，用户以为只有一处问题。
    """
    totals = {status: 0 for status in _STATUS_ORDER}
    for result in files:
        for status, count in (result.get("summary") or {}).items():
            if status in totals:
                totals[status] += int(count or 0)
    return totals


def _tiles(totals: dict[str, int]) -> str:
    labels = {
        "pass": "通过",
        "warning": "预警",
        "fail": "不通过",
        "skipped": "跳过",
    }
    cells = "".join(
        f'<div class="tile"><b>{totals[status]}</b>'
        f"<span>{escape(labels[status])}（项）</span></div>"
        for status in _STATUS_ORDER
    )
    return f'<div class="tiles">{cells}</div>'


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


def _render(
    *,
    files: Sequence[dict[str, Any]],
    phase: str,
    batch_id: str | None,
    heading: str,
) -> str:
    phase_title = PHASE_TITLES.get(phase, phase)
    totals = _aggregate(files)

    meta: list[tuple[str, Any]] = []
    if batch_id:
        meta.append(("批次 ID", batch_id))
    if len(files) == 1:
        job_id = files[0].get("job_id")
        if job_id:
            meta.append(("任务号", job_id))
    meta.append(("校验阶段", phase_title))
    meta.append(("文件数", len(files)))
    generated = files[0].get("generated_at") if files else None
    meta.append(("生成时间", generated or "—"))

    body = [_tiles(totals)]
    if files:
        body.append("<h2>逐文件结果</h2>")
        body.extend(_file_block(result) for result in files)
    else:
        body.append('<p class="empty">本次没有可展示的校验结果。</p>')

    return render_document(
        title=f"{phase_title}报告 - {APP_NAME}",
        heading=heading,
        subtitle=f"{APP_NAME} {APP_VERSION} · 校验报告",
        meta=meta,
        body="\n".join(body),
        footer=(
            f"生成时间：{generated or '—'}",
            f"应用版本：{APP_NAME} {APP_VERSION}",
            REPORT_DISCLAIMER,
        ),
    )


def render_batch_report(report: dict[str, Any]) -> str:
    """批次级校验报告。入参是 `batch_verify_report()` 的返回（§8.4）。"""
    files = [item for item in (report.get("files") or []) if isinstance(item, dict)]
    phase = str(report.get("phase") or "precheck")
    batch_id = report.get("batch_id")
    return _render(
        files=files,
        phase=phase,
        batch_id=str(batch_id) if batch_id else None,
        heading=f"{PHASE_TITLES.get(phase, phase)}报告",
    )


def render_file_report(result: dict[str, Any]) -> str:
    """单文件校验报告。入参是一条 §8.3 `VerifyResult`。"""
    files = [result] if isinstance(result, dict) else []
    phase = str(result.get("phase") or "precheck") if isinstance(result, dict) else "precheck"
    file_info = (result.get("file") or {}) if isinstance(result, dict) else {}
    name = file_info.get("name") or "校验报告"
    return _render(
        files=files,
        phase=phase,
        batch_id=None,
        heading=f"{PHASE_TITLES.get(phase, phase)}报告 · {name}",
    )
