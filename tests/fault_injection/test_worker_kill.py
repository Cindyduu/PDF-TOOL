"""阶段 G 故障注入：进程被杀、任务卡在 running、执行期抛错。

真实现场是这样的：用户点了"开始处理"，机器被强制重启（或任务管理器结束进程）。
此时 job.json 里留下一个永远不动的 `running`，界面上就一直显示"处理中"。
启动时的 `reconcile_interrupted()` 必须把这类残留如实改成"已中断"，
并把半成品输出文件丢掉——不能假装处理成功，也不能假装还在处理。

同时要保证：单个任务执行期抛任何异常，都不能让后台线程死掉，
也不能把任务永远留在 `running`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import load_settings
from app.context import AppContext
from app.core.storage import utcnow_iso
from app.errors import AppError
from app.pdf.pipeline import PipelineError
from tests.integration.test_upload import assert_error, job_dir, upload_path

ENDPOINT = "/api/jobs"
PENDING = ".pending-output.pdf"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def read_job(client, job_id: str) -> dict:
    return json.loads((job_dir(client, job_id) / "job.json").read_text("utf-8"))


def write_job_file(client, job: dict) -> None:
    (job_dir(client, job["id"]) / "job.json").write_text(
        json.dumps(job, ensure_ascii=False), "utf-8"
    )


def leave_running(client, fixture_pdfs: Path, *, status: str = "running") -> dict:
    """模拟"进程被杀"：磁盘上留下一个卡在 running/queued 的任务。

    刻意走一遍真实上传，这样 original.pdf 是真的，reprocess 才有东西可复制。
    同时留下一个半成品输出文件，验证回收时会被清掉。
    """
    job = upload_path(client, fixture_pdfs / "text_blocks.pdf").json()
    stuck = read_job(client, job["id"])
    stuck["status"] = status
    stuck["started_at"] = utcnow_iso() if status == "running" else None
    stuck["current_stage"] = "tagged_pdf" if status == "running" else None
    write_job_file(client, stuck)
    (job_dir(client, job["id"]) / PENDING).write_bytes(b"%PDF-1.7\n%half done\n")
    return stuck


def restart(var_dir: Path) -> AppContext:
    """按"重新打开应用"的方式建一个上下文并跑完整启动流程。"""
    ctx = AppContext(load_settings(var_dir))
    ctx.start()
    return ctx


# --------------------------------------------------------------------------- #
# 残留状态回收
# --------------------------------------------------------------------------- #


def test_running_job_is_marked_interrupted_on_restart(client, fixture_pdfs: Path):
    stuck = leave_running(client, fixture_pdfs)
    var_dir = client.app.state.ctx.settings.var_dir

    ctx = restart(var_dir)
    try:
        job = ctx.jobs.get_job(stuck["id"])
        assert job["status"] == "interrupted"
        assert job["finished_at"]
        assert job["current_stage"] is None
        assert job["last_error"]["code"] == "interrupted"
        assert job["last_error"]["details"]["previous_status"] == "running"
        assert job["last_error"]["retryable"] is True
        assert job["last_error"]["next_action"]
        # 半成品必须被清掉，不能让人误以为有可下载的结果
        assert job["output"] is None
        assert not (ctx.storage.job_dir(ctx.generation_id, stuck["id"]) / PENDING).exists()
        # 中断也要进事件流，时间线才完整
        events = ctx.jobs.list_events(stuck["id"])["events"]
        assert events[-1]["status"] == "interrupted"
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    finally:
        ctx.stop()


def test_queued_job_is_marked_interrupted_on_restart(client, fixture_pdfs: Path):
    stuck = leave_running(client, fixture_pdfs, status="queued")
    var_dir = client.app.state.ctx.settings.var_dir

    ctx = restart(var_dir)
    try:
        job = ctx.jobs.get_job(stuck["id"])
        assert job["status"] == "interrupted"
        assert job["last_error"]["details"]["previous_status"] == "queued"
        assert job["started_at"] is None
    finally:
        ctx.stop()


def test_terminal_jobs_are_never_touched_by_reconcile(client, fixture_pdfs: Path):
    job = upload_path(client, fixture_pdfs / "text_blocks.pdf").json()
    done = read_job(client, job["id"])
    done["status"] = "completed"
    done["finished_at"] = "2026-01-01T00:00:00Z"
    write_job_file(client, done)

    ctx = client.app.state.ctx
    assert ctx.jobs.reconcile_interrupted() == []
    assert ctx.jobs.get_job(job["id"]) == done


def test_reconcile_is_idempotent(client, fixture_pdfs: Path):
    stuck = leave_running(client, fixture_pdfs)
    ctx = client.app.state.ctx

    assert ctx.jobs.reconcile_interrupted() == [stuck["id"]]
    first = ctx.jobs.get_job(stuck["id"])
    assert ctx.jobs.reconcile_interrupted() == []
    assert ctx.jobs.get_job(stuck["id"]) == first


def test_interrupted_job_can_be_reprocessed(client, fixture_pdfs: Path):
    stuck = leave_running(client, fixture_pdfs)
    ctx = client.app.state.ctx
    ctx.jobs.reconcile_interrupted()

    response = client.post(f"{ENDPOINT}/{stuck['id']}/reprocess")
    assert response.status_code == 201, response.text
    fresh = response.json()
    assert fresh["id"] != stuck["id"]
    assert fresh["status"] == "configuring"
    assert fresh["reprocessed_from"] == stuck["id"]
    assert (job_dir(client, fresh["id"]) / "original.pdf").is_file()

    # 新任务能真的跑起来
    assert client.post(f"{ENDPOINT}/{fresh['id']}/start").status_code == 200
    ctx.worker.drain()
    finished = client.get(f"{ENDPOINT}/{fresh['id']}").json()
    assert finished["status"] in ("completed", "completed_with_warnings")
    assert finished["output"]["verified"] is True


# --------------------------------------------------------------------------- #
# 执行期异常
# --------------------------------------------------------------------------- #


def test_unexpected_exception_marks_job_failed_and_keeps_worker_alive(
    client, fixture_pdfs: Path, monkeypatch
):
    import app.core.worker as worker_module

    def boom(*args, **kwargs):
        raise RuntimeError("模拟执行期崩溃")

    monkeypatch.setattr(worker_module, "run_pipeline", boom)

    job = upload_path(client, fixture_pdfs / "text_blocks.pdf").json()
    assert client.post(f"{ENDPOINT}/{job['id']}/start").status_code == 200
    client.app.state.ctx.worker.drain()

    failed = client.get(f"{ENDPOINT}/{job['id']}").json()
    assert failed["status"] == "failed"
    assert failed["current_stage"] is None
    assert failed["finished_at"]
    assert failed["output"] is None
    assert failed["last_error"]["code"] == "internal_error"
    assert failed["last_error"]["retryable"] is True
    assert "模拟执行期崩溃" in failed["last_error"]["details"]["message"]
    # 半成品不能留下来
    assert not (job_dir(client, job["id"]) / PENDING).exists()
    # 失败也要有报告可读
    report = client.get(f"{ENDPOINT}/{job['id']}/report").json()
    assert report["status"] == "failed"
    assert report["last_error"]["code"] == "internal_error"

    # 线程没死：换回真实现，下一个任务照常跑完
    monkeypatch.undo()
    second = upload_path(client, fixture_pdfs / "text_blocks.pdf").json()
    client.patch(f"{ENDPOINT}/{second['id']}/config", json={"tag_choice": "skip"})
    assert client.post(f"{ENDPOINT}/{second['id']}/start").status_code == 200
    client.app.state.ctx.worker.drain()
    ok = client.get(f"{ENDPOINT}/{second['id']}").json()
    assert ok["status"] == "completed"
    assert ok["output"]["verified"] is True


def test_pipeline_error_keeps_its_reason_code(client, fixture_pdfs: Path, monkeypatch):
    import app.core.worker as worker_module

    def refuse(*args, **kwargs):
        raise PipelineError(
            "输出文件未通过最终验证",
            reason_code="output_verification_failed",
            problems=("page_count_changed",),
        )

    monkeypatch.setattr(worker_module, "run_pipeline", refuse)

    job = upload_path(client, fixture_pdfs / "text_blocks.pdf").json()
    client.post(f"{ENDPOINT}/{job['id']}/start")
    client.app.state.ctx.worker.drain()

    failed = client.get(f"{ENDPOINT}/{job['id']}").json()
    assert failed["status"] == "failed"
    assert failed["last_error"]["code"] == "output_verification_failed"
    assert failed["last_error"]["details"]["problems"] == ["page_count_changed"]
    assert failed["output"] is None
    assert_error(
        client.get(f"{ENDPOINT}/{job['id']}/file?kind=output"),
        "output_not_ready",
        status=409,
    )


def test_app_error_from_worker_is_recorded_verbatim(client, fixture_pdfs: Path, monkeypatch):
    import app.core.worker as worker_module

    def refuse(*args, **kwargs):
        raise AppError(
            "source_missing",
            "原始文件已不存在",
            details={"job_id": "x"},
            next_action="请重新上传该文件",
            http_status=404,
        )

    monkeypatch.setattr(worker_module, "run_pipeline", refuse)

    job = upload_path(client, fixture_pdfs / "text_blocks.pdf").json()
    client.post(f"{ENDPOINT}/{job['id']}/start")
    client.app.state.ctx.worker.drain()

    failed = client.get(f"{ENDPOINT}/{job['id']}").json()
    assert failed["status"] == "failed"
    assert failed["last_error"]["code"] == "source_missing"
    assert failed["last_error"]["next_action"] == "请重新上传该文件"


def test_job_already_interrupted_is_not_overwritten_by_a_late_failure(
    client, fixture_pdfs: Path, monkeypatch
):
    """边跑边被回收：worker 的收尾不能覆盖"已中断"这个真实终态。"""
    import app.core.worker as worker_module

    def interrupt_then_fail(*args, **kwargs):
        # 模拟"跑到一半，另一个进程重启把任务标成中断"
        ctx = client.app.state.ctx
        stuck = read_job(client, job["id"])
        stuck["status"] = "interrupted"
        stuck["finished_at"] = utcnow_iso()
        write_job_file(client, stuck)
        raise RuntimeError("迟到的失败")

    monkeypatch.setattr(worker_module, "run_pipeline", interrupt_then_fail)

    job = upload_path(client, fixture_pdfs / "text_blocks.pdf").json()
    client.post(f"{ENDPOINT}/{job['id']}/start")
    client.app.state.ctx.worker.drain()

    final = client.get(f"{ENDPOINT}/{job['id']}").json()
    assert final["status"] == "interrupted"
    assert final["last_error"] is None  # 不是 failed 的 last_error
