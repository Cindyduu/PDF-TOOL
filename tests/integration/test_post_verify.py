"""M5-4 验收：后置校验接入 worker（R27）与处理日志生成（R28）。

这里只证明「处理完之后那一步」：

1. 处理成功的文件有完整 9 行后置校验（AC-R27-N1），且**不受前置勾选影响**；
2. 处理失败的文件不跑后置校验，`post_verify` 停在 `None`（AC-R27-N2）；
3. 分支 B（跳过前置校验）同样有完整后置（AC-R27-N3）；
4. 后置校验里命名项恒为 `skipped`、文案固定（AC-R27-N4）；
5. 产物读不出时整块 9 项记 `fail`，终态降级为 `completed_with_warnings`，
   但产物仍然发布可下载（AC-R27-E1）；
6. 勾了「生成处理日志」才落日志、才给下载；日志生成失败不影响处理结果
   （AC-R28-N1 / N2 / E1）。

这些断言全部落在**磁盘与 job.json 的真实内容**上，不看接口自报的乐观状态。
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest

from app.core import worker as worker_mod
from app.pdf.checks.naming import POST_SUMMARY
from app.pdf.pipeline import PipelineError
from app.schemas import VERIFY_KEYS, validate_verify_result
from app.services.jobs import PROCESS_LOG_FILE_NAME
from app.services.verify import PHASE_POST
from tests.integration.test_upload import job_dir, upload_path

JOBS = "/api/jobs"
BATCH = "/api/jobs/batch"

SUCCESS_STATUSES = ("completed", "completed_with_warnings")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def upload(client, fixture_pdfs: Path, name: str = "text_blocks.pdf") -> dict:
    response = upload_path(client, fixture_pdfs / name)
    assert response.status_code == 201, response.text
    return response.json()


def upload_batch(client, fixture_pdfs: Path, names: list[str]) -> dict:
    files = [
        ("files", (name, (fixture_pdfs / name).read_bytes(), "application/pdf"))
        for name in names
    ]
    response = client.post(BATCH, files=files)
    assert response.status_code == 201, response.text
    return response.json()


def drain(client) -> None:
    client.app.state.ctx.worker.drain()


def read_job(client, job_id: str) -> dict:
    return json.loads((job_dir(client, job_id) / "job.json").read_text("utf-8"))


def verify_then_start(client, job: dict) -> None:
    """分支 A：整批校验跑完，再开始处理，最后把处理也跑完。"""
    response = client.post(f"{BATCH}/{job['batch_id']}/verify", json={})
    assert response.status_code == 202, response.text
    drain(client)
    assert client.post(f"{JOBS}/{job['id']}/start").status_code == 200
    drain(client)


def page_count(path: Path) -> int:
    with pymupdf.open(str(path)) as document:
        return document.page_count


def event_types(client, job_id: str) -> list[str]:
    response = client.get(f"{JOBS}/{job_id}/events")
    assert response.status_code == 200, response.text
    return [record["type"] for record in response.json()["events"]]


# --------------------------------------------------------------------------- #
# AC-R27-N1：处理成功 → 完整 9 行后置校验
# --------------------------------------------------------------------------- #


def test_successful_processing_leaves_a_full_nine_row_post_verify(
    client, fixture_pdfs: Path
):
    job = upload(client, fixture_pdfs)
    verify_then_start(client, job)

    done = read_job(client, job["id"])
    assert done["status"] in SUCCESS_STATUSES

    post = done["post_verify"]
    assert isinstance(post, dict), "处理完成后必须有后置校验结果"
    validate_verify_result(post)

    assert post["phase"] == PHASE_POST
    assert post["job_id"] == job["id"]
    assert [item["key"] for item in post["items"]] == list(VERIFY_KEYS)
    assert sum(post["summary"].values()) == len(VERIFY_KEYS)


def test_post_verify_describes_the_output_file_not_the_source(
    client, fixture_pdfs: Path
):
    """后置校验看的是产物：页数应与源一致，名字是用户看到的原名。"""
    job = upload(client, fixture_pdfs)
    verify_then_start(client, job)

    done = read_job(client, job["id"])
    post = done["post_verify"]
    assert post["file"]["name"] == "text_blocks.pdf"
    assert post["file"]["page_count"] == page_count(fixture_pdfs / "text_blocks.pdf")
    assert post["file"]["size_bytes"] == done["output"]["size"]


def test_post_verify_runs_even_when_precheck_only_ticked_two_items(
    client, fixture_pdfs: Path
):
    """AC-R27-N1 的「完整」二字：后置校验是固定动作，不跟着那次勾选走。

    用户在准备页只勾两项，是「这次只想看这两项」，不是「产物只该过这两关」。
    """
    job = upload(client, fixture_pdfs)
    picked = ["filename_naming", "file_size"]
    patched = client.patch(f"{JOBS}/{job['id']}/config", json={"verify_items": picked})
    assert patched.status_code == 200, patched.text

    verify_then_start(client, job)

    done = read_job(client, job["id"])
    assert [item["key"] for item in done["verify"]["items"]] == picked
    assert [item["key"] for item in done["post_verify"]["items"]] == list(VERIFY_KEYS)


def test_post_verify_progress_events_do_not_mix_with_precheck(client, fixture_pdfs: Path):
    """两段校验共用一条时间线，事件名必须能区分「哪一轮」（§9.1）。"""
    job = upload(client, fixture_pdfs)
    verify_then_start(client, job)

    types = event_types(client, job["id"])
    assert types.count("verify_progress") == len(VERIFY_KEYS)
    assert types.count("post_verify_progress") == len(VERIFY_KEYS)

    response = client.get(f"{JOBS}/{job['id']}/events")
    post_items = [
        record["item"]
        for record in response.json()["events"]
        if record["type"] == "post_verify_progress"
    ]
    assert [item["index"] for item in post_items] == list(
        range(1, len(VERIFY_KEYS) + 1)
    )
    assert {item["total"] for item in post_items} == {len(VERIFY_KEYS)}


def test_status_goes_through_post_verifying_instead_of_jumping_to_completed(
    client, fixture_pdfs: Path
):
    """R27：`processing` 没有直达终态的边，中间必须经过 `post_verifying`。"""
    job = upload(client, fixture_pdfs)
    verify_then_start(client, job)

    response = client.get(f"{JOBS}/{job['id']}/events")
    statuses = [
        record["status"]
        for record in response.json()["events"]
        if record["type"] == "status"
    ]
    assert "processing" in statuses
    assert "post_verifying" in statuses
    assert statuses.index("post_verifying") > statuses.index("processing")
    assert statuses[-1] in SUCCESS_STATUSES


# --------------------------------------------------------------------------- #
# AC-R27-N2：处理失败不跑后置校验
# --------------------------------------------------------------------------- #


def test_failed_processing_never_runs_post_verify(client, fixture_pdfs: Path, monkeypatch):
    """没有产物就没有后置校验 —— 拿什么去查？"""

    def boom(*args, **kwargs):
        raise PipelineError("生成的 PDF 无法通过结构自检", reason_code="output_corrupt")

    monkeypatch.setattr(worker_mod, "run_pipeline", boom)

    job = upload(client, fixture_pdfs)
    verify_then_start(client, job)

    done = read_job(client, job["id"])
    assert done["status"] == "failed"
    assert done["post_verify"] is None
    assert done["output"] is None
    assert "post_verifying" not in event_types(client, job["id"])


# --------------------------------------------------------------------------- #
# AC-R27-N3：分支 B 也有完整后置校验
# --------------------------------------------------------------------------- #


def test_branch_b_still_gets_a_full_post_verify(client, fixture_pdfs: Path):
    """跳过前置校验不等于跳过一切：产物该过的关一样要过。"""
    job = upload(client, fixture_pdfs)
    patched = client.patch(f"{JOBS}/{job['id']}/config", json={"skip_precheck": True})
    assert patched.status_code == 200, patched.text

    assert client.post(f"{JOBS}/{job['id']}/start").status_code == 200
    drain(client)

    done = read_job(client, job["id"])
    assert done["status"] in SUCCESS_STATUSES
    assert done["verify"] is None, "分支 B 不许用空结果冒充「校验通过」"
    assert [item["key"] for item in done["post_verify"]["items"]] == list(VERIFY_KEYS)
    assert done["post_verify"]["phase"] == PHASE_POST


# --------------------------------------------------------------------------- #
# AC-R27-N4：后置校验里命名项恒为 skipped
# --------------------------------------------------------------------------- #


def test_naming_row_is_skipped_with_the_fixed_reason(client, fixture_pdfs: Path):
    """工具自己拼的产物名（含中文「已处理」）不该被用户命名规范判不合规。"""
    job = upload(client, fixture_pdfs)
    verify_then_start(client, job)

    post = read_job(client, job["id"])["post_verify"]
    row = next(item for item in post["items"] if item["key"] == "filename_naming")
    assert row["status"] == "skipped"
    assert row["summary"] == POST_SUMMARY
    assert row["findings"] == []
    # 同一份文件在前置阶段仍然按真实规则判定，不是一律放过
    pre = read_job(client, job["id"])["verify"]
    pre_row = next(item for item in pre["items"] if item["key"] == "filename_naming")
    assert pre_row["status"] != "skipped"


# --------------------------------------------------------------------------- #
# AC-R27-E1：产物读不出 → 整块 fail + 降级终态 + 仍然可下载
# --------------------------------------------------------------------------- #


def test_post_verify_failure_degrades_the_job_but_keeps_the_output(
    client, fixture_pdfs: Path, monkeypatch
):
    """后置校验自己崩了，不能把已经处理好的产物一起丢掉。"""
    real = worker_mod.build_verify_result

    def flaky(job, *, path, filename, phase="precheck", on_item=None):
        if phase == PHASE_POST:
            raise RuntimeError("无法打开 PDF：FileDataError: cannot open broken document")
        return real(job, path=path, filename=filename, phase=phase, on_item=on_item)

    monkeypatch.setattr(worker_mod, "build_verify_result", flaky)

    job = upload(client, fixture_pdfs)
    verify_then_start(client, job)

    done = read_job(client, job["id"])
    assert done["status"] == "completed_with_warnings", (
        "没通过最终校验的文件不能顶着 completed 出去"
    )

    post = done["post_verify"]
    validate_verify_result(post)
    assert [item["key"] for item in post["items"]] == list(VERIFY_KEYS)
    assert {item["status"] for item in post["items"]} == {"fail"}
    assert post["summary"]["fail"] == len(VERIFY_KEYS)
    assert all("后置校验未能执行" in item["summary"] for item in post["items"])

    # 产物照样发布、照样能下载
    assert done["output"] is not None
    output = job_dir(client, job["id"]) / done["output"]["file_name"]
    assert output.is_file()
    download = client.get(f"{JOBS}/{job['id']}/file", params={"kind": "output"})
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"


# --------------------------------------------------------------------------- #
# AC-R28-N1 / N2：日志只在勾选时生成
# --------------------------------------------------------------------------- #


def test_process_log_is_written_when_ticked(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    patched = client.patch(
        f"{JOBS}/{job['id']}/config", json={"write_process_log": True}
    )
    assert patched.status_code == 200, patched.text

    verify_then_start(client, job)

    done = read_job(client, job["id"])
    assert done["status"] in SUCCESS_STATUSES
    assert done["process_log_path"] == PROCESS_LOG_FILE_NAME

    log = job_dir(client, job["id"]) / PROCESS_LOG_FILE_NAME
    assert log.is_file()
    document = log.read_text("utf-8")
    # 四栏齐全：处理前 / 处理后 / 计数 / 原因
    for column in ("处理前", "处理后", "计数", "原因", "处理项"):
        assert column in document
    assert "text_blocks.pdf" in document
    assert job["batch_id"] in document

    served = client.get(f"{BATCH}/{job['batch_id']}/process-log.html")
    assert served.status_code == 200
    assert served.headers["content-type"] == "text/html; charset=utf-8"
    assert served.text == document, "导出必须给原件，不能现渲染"


def test_process_log_is_absent_when_not_ticked(client, fixture_pdfs: Path):
    """默认不勾：既不该有文件，也不该有一个点了就 404 的按钮亮着。"""
    job = upload(client, fixture_pdfs)
    verify_then_start(client, job)

    done = read_job(client, job["id"])
    assert done["process_log_path"] is None
    assert not (job_dir(client, job["id"]) / PROCESS_LOG_FILE_NAME).exists()

    served = client.get(f"{BATCH}/{job['batch_id']}/process-log.html")
    assert served.status_code == 404
    assert served.json()["code"] == "process_log_disabled"


def test_process_log_covers_every_file_of_the_batch(client, fixture_pdfs: Path):
    """日志是「整批」的凭证：一个任务收尾时把整批现状重渲染一遍。"""
    payload = upload_batch(client, fixture_pdfs, ["untagged.pdf", "text_blocks.pdf"])
    batch_id = payload["batch_id"]
    names = ["untagged.pdf", "text_blocks.pdf"]

    patched = client.patch(
        f"{BATCH}/{batch_id}/config", json={"write_process_log": True}
    )
    assert patched.status_code == 200, patched.text

    assert client.post(f"{BATCH}/{batch_id}/verify", json={}).status_code == 202
    drain(client)
    assert client.post(f"{BATCH}/{batch_id}/start").status_code == 200
    drain(client)

    for entry in payload["jobs"]:
        done = read_job(client, entry["id"])
        assert done["status"] in SUCCESS_STATUSES
        assert done["process_log_path"] == PROCESS_LOG_FILE_NAME

    served = client.get(f"{BATCH}/{batch_id}/process-log.html")
    assert served.status_code == 200
    for name in names:
        assert name in served.text
    assert served.text.count("逐文件处理记录") == 1


# --------------------------------------------------------------------------- #
# AC-R28-E1：日志生成失败不影响处理结果
# --------------------------------------------------------------------------- #


def test_process_log_failure_does_not_change_the_result(
    client, fixture_pdfs: Path, monkeypatch
):
    """日志是「附加凭证」，不是处理结果的一部分。"""

    def boom(**kwargs):
        raise RuntimeError("磁盘写满")

    monkeypatch.setattr(worker_mod, "render_process_log", boom)

    job = upload(client, fixture_pdfs)
    patched = client.patch(
        f"{JOBS}/{job['id']}/config", json={"write_process_log": True}
    )
    assert patched.status_code == 200, patched.text

    verify_then_start(client, job)

    done = read_job(client, job["id"])
    assert done["status"] in SUCCESS_STATUSES, "日志失败不能把任务拖成失败"
    assert done["output"] is not None
    assert done["process_log_path"] is None, "页面据此把导出按钮置灰"

    # 处理结果照常可下载
    download = client.get(f"{JOBS}/{job['id']}/file", params={"kind": "output"})
    assert download.status_code == 200

    served = client.get(f"{BATCH}/{job['batch_id']}/process-log.html")
    assert served.status_code == 404
    assert served.json()["code"] == "process_log_disabled"


def test_post_verify_failure_does_not_swallow_the_process_log(
    client, fixture_pdfs: Path, monkeypatch
):
    """两件附加动作互不牵连：后置校验整块失败时，日志仍然照常生成。"""
    real = worker_mod.build_verify_result

    def flaky(job, *, path, filename, phase="precheck", on_item=None):
        if phase == PHASE_POST:
            raise RuntimeError("无法打开 PDF")
        return real(job, path=path, filename=filename, phase=phase, on_item=on_item)

    monkeypatch.setattr(worker_mod, "build_verify_result", flaky)

    job = upload(client, fixture_pdfs)
    assert (
        client.patch(
            f"{JOBS}/{job['id']}/config", json={"write_process_log": True}
        ).status_code
        == 200
    )

    verify_then_start(client, job)

    done = read_job(client, job["id"])
    assert done["status"] == "completed_with_warnings"
    assert done["process_log_path"] == PROCESS_LOG_FILE_NAME
    assert (job_dir(client, job["id"]) / PROCESS_LOG_FILE_NAME).is_file()


@pytest.mark.parametrize("ticked", [True, False])
def test_report_json_agrees_with_job_json_about_the_log(
    client, fixture_pdfs: Path, ticked: bool
):
    """两个消费者（页面读 job.json，日志/报告读 report.json）不能各说一套。"""
    job = upload(client, fixture_pdfs)
    assert (
        client.patch(
            f"{JOBS}/{job['id']}/config", json={"write_process_log": ticked}
        ).status_code
        == 200
    )

    verify_then_start(client, job)

    done = read_job(client, job["id"])
    report = client.get(f"{JOBS}/{job['id']}/report").json()
    assert report["process_log_path"] == done["process_log_path"]
