"""M5-3/M5-5 验收：处理日志 HTML 渲染（R28）。

处理日志与校验报告是两份不同的东西：校验报告回答「文件现在符不符合」，
日志回答「工具做了什么」。所以这里盯的是 R28 点名的那些列：

- 抬头：批次 ID、开始/结束时间、耗时、文件数；
- 逐文件：原文件名 → 输出文件名、页数、字节数；
- 逐处理项：状态 + 四栏（处理前 / 处理后 / 计数 / 原因）；
- 失败项：错误码 + `next_action`；
- 页脚：生成时间 + 应用版本。

同样要求「可离线、全转义、不下结论」——和校验报告一个标准。
"""

from __future__ import annotations

import pytest

from app.config import APP_NAME, APP_VERSION
from app.pdf.content_fingerprint import describe_integrity
from app.report.base import REPORT_DISCLAIMER
from app.report.process_log_html import render_process_log
from app.schemas import COUNT_KEYS, COUNT_LABELS, RESULT_STATUS_LABELS

BATCH_ID = "bat-0000000000000002"
GENERATED_AT = "2026-03-01T08:10:00.000Z"


# --------------------------------------------------------------------------- #
# 夹具构造（形状与 JobService.build_report() 一致）
# --------------------------------------------------------------------------- #


def make_result(
    key: str = "link_color",
    title: str = "内链颜色修正",
    status: str = "adjusted",
    before: object = "12 个链接，颜色未设置",
    after: object = "12 个链接，颜色为 RGB(0,0,255)",
    counts: dict | None = None,
    message: str = "已调整 12 个链接",
    reason_code: str | None = None,
    next_action: str | None = None,
    warnings: list[str] | None = None,
) -> dict:
    payload = {key_: 0 for key_ in COUNT_KEYS}
    payload.update({"checked": 12, "modified": 12})
    payload.update(counts or {})
    return {
        "key": key,
        "title": title,
        "status": status,
        "status_label": RESULT_STATUS_LABELS.get(status, status),
        "status_icon": "",
        "before": before,
        "after": after,
        "counts": payload,
        "reason_code": reason_code,
        "message": message,
        "warnings": warnings or [],
        "next_action": next_action,
    }


def make_report(
    *,
    job_id: str = "job-0000000000000001",
    original_name: str = "示例文档.pdf",
    output_name: str | None = "示例文档_已处理_20260301-080000_000001.pdf",
    status: str = "completed",
    results: list[dict] | None = None,
    last_error: dict | None = None,
    started_at: str = "2026-03-01T08:00:00.000Z",
    finished_at: str = "2026-03-01T08:00:30.000Z",
    content_integrity: dict | None = None,
) -> dict:
    return {
        "schema_version": 2,
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "generation_id": "gen-0000000000000001",
        "job_id": job_id,
        "generated_at": GENERATED_AT,
        "original_name": original_name,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "source": {"size": 4096, "page_count": 12},
        "output": (
            None
            if output_name is None
            else {"file_name": output_name, "size": 8192, "page_count": 12}
        ),
        "config": {},
        "tag_detection": None,
        "verify": None,
        "post_verify": None,
        "process_log_path": None,
        "last_error": last_error,
        "summary": {"adjusted": 0, "unchanged": 0, "not_applicable": 0, "review_required": 0, "failed": 0},
        "results": results if results is not None else [make_result()],
        "content_integrity": content_integrity,
        "content_integrity_summary": describe_integrity(content_integrity),
        "warnings": [],
        "disclaimer": "本报告由程序自动生成，结果供人工复核，不构成合规结论。",
    }


def render(*reports: dict) -> str:
    return render_process_log(
        batch_id=BATCH_ID,
        reports=list(reports) or [make_report()],
        generated_at=GENERATED_AT,
    )


# --------------------------------------------------------------------------- #
# 抬头
# --------------------------------------------------------------------------- #


def test_header_lists_batch_span_and_file_count() -> None:
    document = render(
        make_report(job_id="job-0000000000000001"),
        make_report(job_id="job-0000000000000002", original_name="第二份.pdf"),
    )
    assert BATCH_ID in document
    assert "<dt>文件数</dt><dd>2</dd>" in document
    # 开始取最早、结束取最晚
    assert "2026-03-01T08:00:00.000Z" in document
    assert "2026-03-01T08:00:30.000Z" in document
    assert "<dt>耗时</dt><dd>30.0 秒</dd>" in document


def test_duration_is_dash_when_timestamps_are_missing() -> None:
    document = render(make_report(started_at=None, finished_at=None))
    assert "<dt>耗时</dt><dd>—</dd>" in document


# --------------------------------------------------------------------------- #
# 逐文件
# --------------------------------------------------------------------------- #


def test_each_file_shows_source_name_output_name_pages_and_size() -> None:
    document = render()
    assert "示例文档.pdf" in document
    assert "示例文档_已处理_20260301-080000_000001.pdf" in document
    assert "页数 12" in document
    assert "8.0 KB" in document


def test_failed_file_says_it_has_no_output() -> None:
    document = render(make_report(output_name=None, status="failed"))
    assert "（无产物）" in document
    assert "失败" in document


def test_file_without_output_does_not_invent_one() -> None:
    document = render(make_report(output_name=None, status="interrupted"))
    assert "已处理_" not in document


# --------------------------------------------------------------------------- #
# 逐处理项：四栏
# --------------------------------------------------------------------------- #


def test_operation_table_has_the_four_required_columns() -> None:
    document = render()
    for header in ("处理前", "处理后", "计数", "原因"):
        assert f"<th" in document and header in document
    assert "处理项" in document and "状态" in document


def test_operation_row_carries_before_after_counts_and_reason() -> None:
    document = render()
    assert "12 个链接，颜色未设置" in document
    assert "12 个链接，颜色为 RGB(0,0,255)" in document
    assert "已调整 12 个链接" in document
    # 计数只列非零项，不摆六个 0
    assert f"{COUNT_LABELS['checked']} 12" in document
    assert f"{COUNT_LABELS['modified']} 12" in document
    assert f"{COUNT_LABELS['exempt']} 0" not in document


def test_all_zero_counts_render_as_a_dash() -> None:
    report = make_report(
        results=[make_result(status="not_applicable", counts={key: 0 for key in COUNT_KEYS})]
    )
    document = render(report)
    assert f"{COUNT_LABELS['checked']} 0" not in document


def test_status_label_comes_from_the_shared_contract() -> None:
    document = render(
        make_report(
            results=[
                make_result(key="metadata", title="清除文档描述", status="unchanged"),
                make_result(key="fast_web_view", title="Fast Web View", status="failed"),
            ]
        )
    )
    for status in ("unchanged", "failed"):
        assert f'<span class="tag {status}">{RESULT_STATUS_LABELS[status]}</span>' in document


def test_warnings_and_next_action_are_shown_as_reason() -> None:
    document = render(
        make_report(
            results=[
                make_result(
                    status="review_required",
                    message="有 2 处注释无法判断",
                    warnings=["第 3 页注释无外观"],
                    next_action="请人工确认第 3 页的批注是否保留",
                )
            ]
        )
    )
    assert "提示：第 3 页注释无外观" in document
    assert "下一步：请人工确认第 3 页的批注是否保留" in document


def test_no_operations_renders_an_empty_state() -> None:
    document = render(make_report(results=[]))
    assert "本次没有执行任何处理项" in document


# --------------------------------------------------------------------------- #
# 失败项：错误码 + next_action
# --------------------------------------------------------------------------- #


def test_failed_file_reports_error_code_and_next_action() -> None:
    document = render(
        make_report(
            status="failed",
            output_name=None,
            results=[],
            last_error={
                "code": "corrupt_pdf",
                "message": "文件结构损坏，无法解析",
                "details": {"page": 3},
                "retryable": False,
                "next_action": "请用阅读器另存一份后重新上传",
                "stage": "preparing",
            },
        )
    )
    assert "corrupt_pdf" in document
    assert "文件结构损坏，无法解析" in document
    assert "请用阅读器另存一份后重新上传" in document


# --------------------------------------------------------------------------- #
# 内容一致性（R36）
# --------------------------------------------------------------------------- #


def test_integrity_block_reports_verdict_pages_and_attribution() -> None:
    """一致时也要把「哪几页变了、归因到谁」写出来。

    只写一句「一致」等于让用户无凭据地相信工具。页号与归因是这句话的证据：
    用户拿 Acrobat 翻到第 1、2 页，能自己确认那两页确实只是链接变蓝了。
    """
    document = render(
        make_report(
            content_integrity={
                "ok": True,
                "page_count": {"before": 12, "after": 12},
                "changed_pages": {"content": [1, 2], "text": []},
                "attributed_to": ["link_color"],
                "problems": [],
                "warning": None,
            }
        )
    )
    assert "内容一致性" in document
    assert "2 页内容流变化" in document
    assert "1、2" in document
    assert "link_color" in document
    # 页数前后相等时也要写出来：12 → 12 本身就是「没丢页」的证据
    assert "12 → 12" in document
    assert "未核对" not in document


def test_integrity_block_says_unverified_when_data_is_missing() -> None:
    """`content_integrity=None` 时不许整块消失。

    这一行的存在本身就是结论：用户需要知道**这件事没被验证过**，
    而不是从「没有这一行」去猜它验过了（AC-R36-E2）。
    """
    document = render(make_report(content_integrity=None))
    assert "内容一致性" in document
    assert "内容一致性未核对" in document


def test_integrity_block_lists_problems_when_not_ok() -> None:
    document = render(
        make_report(
            status="failed",
            content_integrity={
                "ok": False,
                "page_count": {"before": 12, "after": 11},
                "changed_pages": {"content": [3], "text": [3]},
                "attributed_to": [],
                "problems": ["页数从 12 变成 11"],
                "warning": None,
            },
        )
    )
    assert "内容一致性未通过" in document
    assert "页数从 12 变成 11" in document
    assert "12 → 11" in document


def test_integrity_block_prefers_the_recorded_warning() -> None:
    """护栏自己坏了（指纹算不出来）时，原样展示那条警告，不另编一句话。

    `ok=True` + `warning` 是「没核对成、但放行」的唯一合法组合（AC-R36-E2）。
    这里如果把 warning 丢掉、按 `ok` 渲染成「一致」，就等于替工具撒谎。
    """
    warning = "内容一致性未能核对（指纹计算失败：RuntimeError: boom）"
    document = render(
        make_report(
            content_integrity={
                "ok": True,
                "page_count": {"before": None, "after": None},
                "changed_pages": {"content": [], "text": []},
                "attributed_to": [],
                "problems": [],
                "warning": warning,
            }
        )
    )
    assert warning in document
    assert "内容与版式均未变化" not in document


# --------------------------------------------------------------------------- #
# 页脚与转义
# --------------------------------------------------------------------------- #


def test_footer_carries_version_time_and_disclaimer() -> None:
    document = render()
    assert APP_VERSION in document
    assert GENERATED_AT in document
    assert document.count(REPORT_DISCLAIMER) == 1


def test_log_makes_no_conclusion_outside_the_disclaimer() -> None:
    document = render()
    body = document.replace(REPORT_DISCLAIMER, "")
    for word in ("完全合规", "已通过", "100% 合规"):
        assert word not in body


@pytest.mark.parametrize(
    "raw",
    ["<script>alert(1)</script>", 'a"b&c<d>.pdf', "<iframe src=//evil></iframe>"],
)
def test_file_names_are_escaped(raw: str) -> None:
    document = render(make_report(original_name=raw, output_name=f"{raw}_已处理.pdf"))
    assert raw not in document
    lowered = document.lower()
    assert "<script" not in lowered
    assert "<iframe" not in lowered


def test_document_has_no_external_reference() -> None:
    document = render()
    assert document.startswith("<!DOCTYPE html>")
    assert document.count("<style>") == 1
    lowered = document.lower()
    for forbidden in ("<link", "<script", "src=", "@import", "http://", "https://", "url("):
        assert forbidden not in lowered, f"导出文件含外部依赖：{forbidden}"


def test_render_is_pure() -> None:
    assert render() == render()
