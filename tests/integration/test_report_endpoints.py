"""M5 验收：三条 HTML 端点（§9.1、R23、R28）。

这里验的是**协议层**，不是渲染内容（渲染细节归 `tests/unit/test_report_*.py`）：

1. `GET /batch/{id}/verify-report.html` 返回可离线打开的 HTML 文件；
2. `GET /{job_id}/verify-report.html` 分得清前置 / 后置，没结果就 404；
3. `GET /batch/{id}/process-log.html` 发的是**已生成**的那份原件，
   没生成就 `process_log_disabled`，且不接受越界路径；
4. 三个端点的响应头都带 `filename*=UTF-8''`，回退名保留 `.html`；
5. 未知 `phase` 一律 400，不让它掉进「当作 post」的默认分支。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.integration.test_upload import assert_error, job_dir, upload_path

JOBS = "/api/jobs"
BATCH = "/api/jobs/batch"

BATCH_HTML = f"{BATCH}/{{batch_id}}/verify-report.html"
FILE_HTML = f"{JOBS}/{{job_id}}/verify-report.html"
LOG_HTML = f"{BATCH}/{{batch_id}}/process-log.html"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def upload(client, fixture_pdfs: Path, name: str = "text_blocks.pdf") -> dict:
    response = upload_path(client, fixture_pdfs / name)
    assert response.status_code == 201, response.text
    return response.json()


def verify_and_drain(client, batch_id: str) -> None:
    response = client.post(f"{BATCH}/{batch_id}/verify", json={})
    assert response.status_code == 202, response.text
    client.app.state.ctx.worker.drain()


def disposition_of(response) -> str:
    return response.headers["content-disposition"]


def ascii_fallback(header: str) -> str:
    return header.split('filename="')[1].split('"')[0]


def assert_offline_readable(document: str) -> None:
    """AC-R23-N4 / AC-R28-N1：断网双击也能看，所以不许有任何外部引用。"""
    assert document.startswith("<!DOCTYPE html>")
    lowered = document.lower()
    for banned in ("<link", "<script", "src=", "@import", "http://", "https://", "url("):
        assert banned not in lowered, banned


# --------------------------------------------------------------------------- #
# 批次校验报告
# --------------------------------------------------------------------------- #


def test_batch_verify_report_html_is_downloadable(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    response = client.get(BATCH_HTML.format(batch_id=job["batch_id"]))
    assert response.status_code == 200, response.text

    # 少一个 charset，整页中文会按 latin-1 解码成乱码。
    assert response.headers["content-type"] == "text/html; charset=utf-8"

    header = disposition_of(response)
    assert header.startswith("attachment; ")
    assert "filename*=UTF-8''" in header
    # 回退名必须是 .html —— 退回成 .pdf，系统会拿 PDF 阅读器打开一个网页。
    assert ascii_fallback(header).lower().endswith(".html")

    document = response.text
    assert_offline_readable(document)
    assert "校验报告" in document
    assert job["batch_id"] in document


def test_batch_verify_report_html_rejects_unknown_phase(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    response = client.get(
        BATCH_HTML.format(batch_id=job["batch_id"]), params={"phase": "postcheck"}
    )
    payload = assert_error(response, "invalid_config", 400)
    assert payload["details"]["phase"] == "postcheck"


def test_batch_verify_report_html_is_404_before_any_verify(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)

    response = client.get(BATCH_HTML.format(batch_id=job["batch_id"]))
    assert_error(response, "verify_report_missing", 404)


def test_batch_verify_report_html_escapes_file_name(client, fixture_pdfs: Path):
    """AC-R23-E1：文件名里的 `&` 必须变成实体，不能原样进 HTML。

    上传时 `<` `>` `"` 会被 `sanitize_filename` 换成下划线，所以这里挑的是
    能穿过清洗、又确实需要转义的两个字符：`&` 和 `'`。
    """
    hostile = "AT&T's report.pdf"
    response = upload_path(client, fixture_pdfs / "text_blocks.pdf", name=hostile)
    assert response.status_code == 201, response.text
    job = response.json()
    verify_and_drain(client, job["batch_id"])

    document = client.get(BATCH_HTML.format(batch_id=job["batch_id"])).text
    assert "AT&amp;T" in document
    assert "AT&T" not in document
    assert "&#x27;" in document


# --------------------------------------------------------------------------- #
# 单文件校验报告
# --------------------------------------------------------------------------- #


def test_file_verify_report_html_serves_precheck(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    response = client.get(FILE_HTML.format(job_id=job["id"]))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert ascii_fallback(disposition_of(response)).lower().endswith(".html")

    document = response.text
    assert_offline_readable(document)
    assert "前置校验" in document
    assert job["id"] in document


def test_file_verify_report_html_post_is_404_until_processing_finishes(
    client, fixture_pdfs: Path
):
    """后置校验属于处理之后（R27），这里只验「还没到就明确说没有」。"""
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    response = client.get(FILE_HTML.format(job_id=job["id"]), params={"phase": "post"})
    payload = assert_error(response, "verify_report_missing", 404)
    assert payload["details"]["phase"] == "post"


def test_file_verify_report_html_rejects_unknown_phase(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    response = client.get(
        FILE_HTML.format(job_id=job["id"]), params={"phase": "before"}
    )
    assert_error(response, "invalid_config", 400)


def test_file_verify_report_html_unknown_job_is_404(client):
    response = client.get(FILE_HTML.format(job_id="job-0000000000000000"))
    assert_error(response, "job_not_found", 404)


# --------------------------------------------------------------------------- #
# 处理日志
# --------------------------------------------------------------------------- #


def test_process_log_is_disabled_until_generated(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    response = client.get(LOG_HTML.format(batch_id=job["batch_id"]))
    payload = assert_error(response, "process_log_disabled", 404)
    assert payload["details"]["batch_id"] == job["batch_id"]


def test_process_log_serves_the_generated_file_verbatim(client, fixture_pdfs: Path):
    """R28 发的是原件：日志是「当时发生了什么」的凭证，不能现渲染。"""
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    stored = "<!DOCTYPE html><html lang=\"zh-CN\"><body><p>当时的日志</p></body></html>"
    (job_dir(client, job["id"]) / "process-log.html").write_text(stored, "utf-8")
    client.app.state.ctx.jobs.record_process_log(job["id"], "process-log.html")

    response = client.get(LOG_HTML.format(batch_id=job["batch_id"]))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert ascii_fallback(disposition_of(response)).lower().endswith(".html")
    assert response.text == stored


def test_process_log_ignores_path_outside_the_generation(client, fixture_pdfs: Path, tmp_path: Path):
    """job.json 是磁盘上的普通文件，被改过也不能让它吐出任意路径的内容。"""
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    outside = tmp_path / "outside.html"
    outside.write_text("<!DOCTYPE html><html><body>不该被读到</body></html>", "utf-8")
    client.app.state.ctx.jobs.record_process_log(job["id"], str(outside))

    response = client.get(LOG_HTML.format(batch_id=job["batch_id"]))
    assert_error(response, "process_log_disabled", 404)
    assert "不该被读到" not in response.text


def test_process_log_prefers_the_newest_file_in_the_batch(client, fixture_pdfs: Path):
    """一个任务一个任务收尾，越晚写的那份越接近「批次完成」。"""
    created = client.post(
        f"{BATCH}",
        files=[
            ("files", ("first.pdf", (fixture_pdfs / "text_blocks.pdf").read_bytes(), "application/pdf")),
            ("files", ("second.pdf", (fixture_pdfs / "untagged.pdf").read_bytes(), "application/pdf")),
        ],
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    batch_id = payload["batch_id"]
    assert len(payload["jobs"]) == 2
    verify_and_drain(client, batch_id)

    for index, job in enumerate(payload["jobs"]):
        path = job_dir(client, job["id"]) / "process-log.html"
        path.write_text(f"<!DOCTYPE html><html><body>第 {index} 份</body></html>", "utf-8")
        client.app.state.ctx.jobs.record_process_log(job["id"], "process-log.html")
        # mtime 在 Windows 上精度足够，但同一瞬间两次写入可能打平；
        # 显式错开一秒，让「取最新」这件事有确定的答案可断言。
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))

    response = client.get(LOG_HTML.format(batch_id=batch_id))
    assert response.status_code == 200, response.text
    assert "第 1 份" in response.text


def test_process_log_is_404_for_unknown_batch(client):
    response = client.get(LOG_HTML.format(batch_id="bat-0000000000000000"))
    assert_error(response, "job_not_found", 404)


# --------------------------------------------------------------------------- #
# 与 JSON 端点的一致性
# --------------------------------------------------------------------------- #

def test_html_and_json_reports_agree_on_totals(client, fixture_pdfs: Path):
    """同一份结果，JSON 与 HTML 的计数必须一模一样。

    页面上的数字来自 JSON，用户导出的报告来自 HTML —— 两处对不上，
    用户就不知道该信哪一个。
    """
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    payload = client.get(
        f"{BATCH}/{job['batch_id']}/verify-report", params={"phase": "precheck"}
    ).json()
    document = client.get(BATCH_HTML.format(batch_id=job["batch_id"])).text

    counts = {status: 0 for status in ("pass", "warning", "fail", "skipped")}
    for item in payload["files"]:
        for status, count in (item.get("summary") or {}).items():
            counts[status] = counts.get(status, 0) + int(count)

    assert sum(counts.values()) == len(payload["files"]) * len(
        payload["files"][0]["items"]
    )
    # 只对「非零项」断言：报告里零计数的色块仍然会画出来，
    # 断言「0 不出现」等于要求它别画，那是另一件事。
    for status, count in counts.items():
        if count:
            assert f">{count}<" in document


def test_report_endpoints_do_not_write_anything(client, fixture_pdfs: Path):
    """R23：校验报告按需渲染、不落盘。导出两次，任务目录不该多出文件。"""
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])
    before = {p.name for p in job_dir(client, job["id"]).iterdir()}

    client.get(BATCH_HTML.format(batch_id=job["batch_id"]))
    client.get(FILE_HTML.format(job_id=job["id"]))

    after = {p.name for p in job_dir(client, job["id"]).iterdir()}
    assert after == before


def test_batch_verify_report_json_still_works(client, fixture_pdfs: Path):
    """重构 phase 校验不能碰坏原有的 JSON 端点。"""
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    response = client.get(f"{BATCH}/{job['batch_id']}/verify-report")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["phase"] == "precheck"
    assert json.dumps(payload)  # 可序列化，没混进 Path 之类的对象
