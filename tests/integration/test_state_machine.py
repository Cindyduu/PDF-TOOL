"""阶段 G 集成验收：持久任务状态机与完整流水线。

这里要证明的是"状态是真的、进度是真的、产物是真的"：

1. 非法迁移一律抛错，且不改动 job 字典；
2. 重复 `POST /start` 返回 409，队列里不会多出第二个任务；
3. 事件 seq 从 1 起严格递增，内容只有阶段名，**没有任何百分比**；
4. 报告落盘，重启后仍能读到同一份；
5. 进程"重启"（换一个 AppContext 读同一份数据）不丢状态；
6. `completed_with_warnings` 与 `completed` 的判定符合 PRD；
7. 输出文件在通过最终验证之前一律 409，发布之后可下载，文件被移走后 404。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import load_settings
from app.context import AppContext
from app.core import state_machine
from app.core.state_machine import TRANSITIONS, transition
from app.errors import AppError
from app.schemas import (
    JOB_STATUSES,
    OPERATION_KEYS,
    TERMINAL_JOB_STATUSES,
    default_config,
    expand_process_groups,
    validate_job,
)
from tests.integration.test_upload import assert_error, job_dir, upload_path

ENDPOINT = "/api/jobs"
FORBIDDEN = ("完全合规", "已通过认证", "已合规")

LEGAL = {(src, dst) for src, targets in TRANSITIONS.items() for dst in targets}

# 默认配置只勾了 g1–g5，不含 g6（扁平化不可逆，默认不做），
# 所以阶段序列里不会出现 `flatten_annotations`。
EXPECTED_STAGES = (
    ("preparing",)
    + tuple(
        key
        for key in OPERATION_KEYS
        if key in expand_process_groups(default_config()["process_items"])
    )
    + ("verifying", "post_verifying", "publishing")
)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def read_job(client, job_id: str) -> dict:
    return json.loads((job_dir(client, job_id) / "job.json").read_text("utf-8"))


def write_job_file(client, job: dict) -> None:
    (job_dir(client, job["id"]) / "job.json").write_text(
        json.dumps(job, ensure_ascii=False), "utf-8"
    )


def start(client, job_id: str):
    return client.post(f"{ENDPOINT}/{job_id}/start")


def verify(client, batch_id: str, body: dict | None = None):
    return client.post(f"{ENDPOINT}/batch/{batch_id}/verify", json=body or {})


def upload(client, fixture_pdfs: Path, name: str = "text_blocks.pdf") -> dict:
    return upload_path(client, fixture_pdfs / name).json()


def run_to_end(
    client,
    fixture_pdfs: Path,
    *,
    name: str = "text_blocks.pdf",
    patch: dict | None = None,
    verify_first: bool = True,
) -> dict:
    """上传 → （可选）改配置 → （默认）前置校验 → 开始 → 同步跑完 → 读回终态。

    v4.0 把动线拆成两段（R13）：不校验就走 `POST /start` 会拿到
    `verify_not_ready`。所以这里默认先跑一遍前置校验，让「跑到底」仍然是一个
    动作；需要走分支 B（不校验直接处理）的用例显式传 `verify_first=False`
    并在 patch 里带 `skip_precheck: True`。
    """
    job = upload(client, fixture_pdfs, name)
    if patch:
        client.patch(f"{ENDPOINT}/{job['id']}/config", json=patch)
    if verify_first:
        response = verify(client, job["batch_id"])
        assert response.status_code == 202, response.text
        client.app.state.ctx.worker.drain()
    response = start(client, job["id"])
    assert response.status_code == 200, response.text
    client.app.state.ctx.worker.drain()
    return client.get(f"{ENDPOINT}/{job['id']}").json()


# --------------------------------------------------------------------------- #
# 白名单转移表
# --------------------------------------------------------------------------- #


def test_every_transition_outside_the_whitelist_is_rejected():
    """64 种组合里只有白名单上的几条能通过，其余全部抛错且不留痕。"""
    checked = 0
    for source in JOB_STATUSES:
        for target in JOB_STATUSES:
            if (source, target) in LEGAL:
                continue
            job = {"status": source, "finished_at": None, "current_stage": "x"}
            with pytest.raises(AppError) as info:
                transition(job, target)
            assert info.value.code == "invalid_state_transition"
            assert info.value.http_status == 409
            assert info.value.next_action
            assert info.value.details == {
                "from": source,
                "to": target,
                "allowed": list(TRANSITIONS[source]),
            }
            # 迁移失败必须一个字都没改
            assert job == {"status": source, "finished_at": None, "current_stage": "x"}
            checked += 1
    assert checked == len(JOB_STATUSES) ** 2 - len(LEGAL)
    assert checked > 0


def test_terminal_statuses_have_no_outgoing_edges():
    for status in TERMINAL_JOB_STATUSES:
        assert TRANSITIONS[status] == ()
        assert state_machine.is_terminal(status)
        for target in JOB_STATUSES:
            assert not state_machine.can_transition(status, target)


def test_unknown_states_are_rejected():
    with pytest.raises(AppError) as info:
        transition({"status": "started"}, "running")
    assert info.value.code == "invalid_state_transition"

    with pytest.raises(AppError) as info:
        transition({"status": "queued"}, "done")
    assert info.value.code == "invalid_state_transition"
    assert info.value.details["to"] == "done"
    assert info.value.details["known"] == list(JOB_STATUSES)


def test_legal_transitions_stamp_times():
    job = {"status": "configuring", "started_at": None, "finished_at": None, "current_stage": None}

    transition(job, "process_queued", at="2026-01-01T00:00:00Z")
    assert job["status"] == "process_queued"
    assert job["started_at"] is None  # 排队还没开始

    transition(job, "processing", at="2026-01-01T00:00:01Z")
    assert job["started_at"] == "2026-01-01T00:00:01Z"

    job["current_stage"] = "link_color"
    transition(job, "post_verifying", at="2026-01-01T00:00:08Z")
    assert job["current_stage"] == "link_color"  # 还没结束，阶段不清空

    transition(job, "completed_with_warnings", at="2026-01-01T00:00:09Z")
    assert job["finished_at"] == "2026-01-01T00:00:09Z"
    assert job["current_stage"] is None
    assert job["started_at"] == "2026-01-01T00:00:01Z"


def test_processing_keeps_the_first_started_at():
    job = {"status": "process_queued", "started_at": "2026-01-01T00:00:00Z", "finished_at": None}
    transition(job, "processing", at="2026-01-02T00:00:00Z")
    assert job["started_at"] == "2026-01-01T00:00:00Z"


def test_processing_cannot_jump_straight_to_completed():
    """R27：后置校验是强制的，处理完必须经过 `post_verifying`。"""
    job = {"status": "processing", "started_at": None, "finished_at": None, "current_stage": None}
    with pytest.raises(AppError) as info:
        transition(job, "completed")
    assert info.value.code == "invalid_state_transition"
    assert "post_verifying" in info.value.details["allowed"]


# --------------------------------------------------------------------------- #
# 入队与重复开始
# --------------------------------------------------------------------------- #


def test_duplicate_start_is_409_and_creates_no_second_job(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    ctx = client.app.state.ctx
    before = ctx.storage.list_job_ids(ctx.generation_id)

    # 先跑前置校验，让 `POST /start` 落到「开始处理」这一道闸门
    assert verify(client, job["batch_id"]).status_code == 202
    ctx.worker.drain()
    pending_after_verify = ctx.worker.pending()

    assert start(client, job["id"]).status_code == 200
    payload = assert_error(start(client, job["id"]), "job_already_started", status=409)
    assert payload["details"]["status"] == "process_queued"

    # 队列里只多了这一条，磁盘上也没有第二个任务目录
    assert ctx.worker.pending() == pending_after_verify + 1
    assert ctx.storage.list_job_ids(ctx.generation_id) == before
    assert read_job(client, job["id"])["status"] == "process_queued"


def test_start_from_terminal_status_is_409(client, fixture_pdfs: Path):
    job = run_to_end(client, fixture_pdfs)
    assert job["status"] == "completed"
    assert_error(start(client, job["id"]), "job_already_started", status=409)


# --------------------------------------------------------------------------- #
# 事件流
# --------------------------------------------------------------------------- #


def test_events_are_monotonic_and_contain_only_stage_names(client, fixture_pdfs: Path):
    job = run_to_end(client, fixture_pdfs)
    payload = client.get(f"{ENDPOINT}/{job['id']}/events").json()
    events = payload["events"]

    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert payload["last_seq"] == events[-1]["seq"]

    # 事件里只允许这几个字段：没有 percent / progress / ratio 之类的东西。
    # `item` 是 v4.0 新增的：校验进度只报「哪一项 / 什么状态 / 第几项共几项」，
    # 同样不含任何百分比。
    #
    # 四种类型（§9.1）：阶段、状态，以及两段校验各自的进度。后置校验用
    # `post_verify_progress` 而不是复用 `verify_progress`——两轮校验会先后
    # 出现在同一条时间线上，共用一个名字前端就没法区分是哪一轮。
    allowed_types = ("stage", "status", "verify_progress", "post_verify_progress")
    for event in events:
        assert set(event) <= {"seq", "type", "at", "stage", "label", "status", "item"}
        assert event["type"] in allowed_types
        # `type` 本身带着 progress 这个词（`verify_progress`），所以把它排除掉再看
        blob = json.dumps(
            {k: v for k, v in event.items() if k != "type"}, ensure_ascii=False
        )
        for word in ("percent", "progress", "ratio", "%"):
            assert word not in blob

    # 四种类型都真的出现过：只允许不等于实际发生过
    assert {event["type"] for event in events} == set(allowed_types)

    stages = [event["stage"] for event in events if event["type"] == "stage"]
    assert stages == ["precheck", *EXPECTED_STAGES], (
        "阶段顺序必须与流水线一致：前置校验 → 处理 → 后置校验 → 发布"
    )
    statuses = [event["status"] for event in events if event["type"] == "status"]
    assert statuses[:4] == ["verify_queued", "verifying", "verified", "process_queued"]
    assert statuses[4] == "processing"
    assert statuses[5] == "post_verifying"
    assert statuses[-1] == job["status"]


def test_events_after_seq_is_incremental(client, fixture_pdfs: Path):
    job = run_to_end(client, fixture_pdfs)
    full = client.get(f"{ENDPOINT}/{job['id']}/events").json()["events"]

    tail = client.get(f"{ENDPOINT}/{job['id']}/events?after=5").json()["events"]
    assert [event["seq"] for event in tail] == [event["seq"] for event in full[5:]]
    assert all(event["seq"] > 5 for event in tail)

    assert client.get(f"{ENDPOINT}/{job['id']}/events?after=999").json()["events"] == []


def test_negative_after_is_rejected(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    ctx = client.app.state.ctx
    with pytest.raises(AppError) as info:
        ctx.jobs.list_events(job["id"], after=-1)
    assert info.value.code == "invalid_config"

    # HTTP 层由 FastAPI 的 ge=0 拦住
    assert client.get(f"{ENDPOINT}/{job['id']}/events?after=-1").status_code == 422


def test_unknown_stage_name_is_refused(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    ctx = client.app.state.ctx
    with pytest.raises(AppError) as info:
        ctx.jobs.record_stage(job["id"], "45%")
    assert info.value.code == "internal_error"


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #


def test_report_is_persisted_and_schema_complete(client, fixture_pdfs: Path):
    job = run_to_end(client, fixture_pdfs)
    path = job_dir(client, job["id"]) / "report.json"
    assert path.is_file(), "终态必须落盘 report.json"

    stored = json.loads(path.read_text("utf-8"))
    served = client.get(f"{ENDPOINT}/{job['id']}/report").json()
    assert served["job_id"] == job["id"]
    assert served["status"] == job["status"]
    assert stored["generated_at"] == served["generated_at"]

    # 报告里的项数由**配置展开**决定，不是写死的 9：
    # 默认只勾 g1–g5（扁平化不可逆，默认不做），展开后是 8 项。
    expected_keys = expand_process_groups(job["config"]["process_items"])
    assert [item["key"] for item in served["results"]] == list(expected_keys)
    assert {item["key"] for item in served["results"]} == {
        item["key"] for item in job["results"]
    }
    for item in served["results"]:
        assert item["status_label"]
        assert item["status_icon"]
        assert set(item["counts"]) == {
            "checked",
            "modified",
            "compliant",
            "exempt",
            "not_applicable",
            "failed",
        }
    assert sum(served["summary"].values()) == len(expected_keys)

    blob = json.dumps(served, ensure_ascii=False)
    for phrase in FORBIDDEN:
        assert phrase not in blob
    # v4.0 起不再有标签检测项（R30），报告里不该再出现标签相关的「待人工复核」；
    # 取而代之的是统一的免责声明：结论只供人工复核，不构成合规判定。
    assert "待人工复核" not in blob
    assert served["disclaimer"]


def test_report_is_rebuilt_when_file_is_missing(client, fixture_pdfs: Path):
    job = run_to_end(client, fixture_pdfs)
    path = job_dir(client, job["id"]) / "report.json"
    path.unlink()

    rebuilt = client.get(f"{ENDPOINT}/{job['id']}/report").json()
    assert rebuilt["job_id"] == job["id"]
    assert path.is_file(), "重建后必须重新落盘"


# --------------------------------------------------------------------------- #
# 结果判定
# --------------------------------------------------------------------------- #


def test_clean_run_completes_without_warnings(client, fixture_pdfs: Path):
    job = run_to_end(client, fixture_pdfs)
    assert job["status"] == "completed"
    assert job["last_error"] is None
    assert not any(item["status"] == "failed" for item in job["results"])


def _write_broken_link_color_pdf(path: Path) -> Path:
    """造一个 `/C` 不是数组的链接注释：单项失败，其余项照常。

    这里**必须有文字层**。v4.1 的 R35 先看这一页有没有文字：没有文字层（典型是
    扫描页）时整页原样保留，连 `/C` 都不动，状态记 `not_applicable`——那条路径
    是「无可着色文字」的诚实结论，不是失败。早先这个样本用的是一张空白页，
    于是它压根走不到 `/C` 分支，用例从「验状态机降级」变成了「验空白页被跳过」，
    红在 `completed != completed_with_warnings` 上。加上一行文字，`/C` 结构异常
    才会真的被碰到，`failed` 才会真的记 1。
    """
    import pikepdf

    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(200, 200))
        page.obj["/Contents"] = pdf.make_stream(b"BT /F1 12 Tf 10 100 Td (Hello) Tj ET")
        page.obj["/Resources"] = pikepdf.Dictionary(
            Font=pikepdf.Dictionary(
                F1=pikepdf.Dictionary(
                    Type=pikepdf.Name("/Font"),
                    Subtype=pikepdf.Name("/Type1"),
                    BaseFont=pikepdf.Name("/Helvetica"),
                    Encoding=pikepdf.Name("/WinAnsiEncoding"),
                )
            )
        )
        annot = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name("/Annot"),
                Subtype=pikepdf.Name("/Link"),
                # 矩形压在「Hello」的起点 (10, 100) 上，否则没有文字可着色
                Rect=pikepdf.Array([8, 96, 60, 112]),
                Border=pikepdf.Array([0, 0, 1]),
                # 故意给一个名字而不是颜色数组
                C=pikepdf.Name("/Blue"),
                A=pikepdf.Dictionary(
                    S=pikepdf.Name("/URI"), URI="https://example.com/broken"
                ),
            )
        )
        page.Annots = pikepdf.Array([annot])
        pdf.save(path)
    return path


def test_failed_operation_makes_the_job_completed_with_warnings(client, tmp_path: Path):
    """单项失败不掩盖其余项：整体降级为 `completed_with_warnings`，输出仍可用。"""
    source = _write_broken_link_color_pdf(tmp_path / "broken_link_color.pdf")
    job = upload_path(client, source).json()
    client.patch(
        f"{ENDPOINT}/{job['id']}/config", json={"process_items": ["g4_link_color"]}
    )
    assert verify(client, job["batch_id"]).status_code == 202
    client.app.state.ctx.worker.drain()
    assert start(client, job["id"]).status_code == 200
    client.app.state.ctx.worker.drain()
    done = client.get(f"{ENDPOINT}/{job['id']}").json()

    assert done["status"] == "completed_with_warnings"
    failed = [item for item in done["results"] if item["status"] == "failed"]
    assert [item["key"] for item in failed] == ["link_color"]
    assert failed[0]["next_action"]
    assert failed[0]["counts"]["failed"] == 1
    # 输出文件真实存在，只是结果里带着一条需要人工看的记录
    assert done["output"] and (
        job_dir(client, job["id"]) / done["output"]["file_name"]
    ).is_file()


# --------------------------------------------------------------------------- #
# 刷新 / 重启不丢状态
# --------------------------------------------------------------------------- #


def test_state_survives_a_restart(client, fixture_pdfs: Path):
    job = run_to_end(client, fixture_pdfs)
    var_dir = client.app.state.ctx.settings.var_dir

    # 换一个 AppContext 读同一份数据，等价于关掉应用再打开（或浏览器刷新后重查）
    fresh_ctx = AppContext(load_settings(var_dir))
    reloaded = fresh_ctx.jobs.get_job(job["id"])

    assert reloaded["status"] == job["status"]
    assert reloaded["output"] == job["output"]
    assert reloaded["results"] == job["results"]
    assert reloaded["current_stage"] is None
    validate_job(reloaded)

    events = fresh_ctx.jobs.list_events(job["id"])["events"]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert fresh_ctx.jobs.get_report(job["id"])["job_id"] == job["id"]


# --------------------------------------------------------------------------- #
# 输出文件
# --------------------------------------------------------------------------- #


def test_output_is_409_before_final_verification(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    # v4.0 的 `POST /start` 前面多了一道前置校验（R13）。本用例要看的是
    # 「处理还没跑完时输出不可下载」，所以先按正手动线把校验跑掉，再让它停在
    # `process_queued`——这里不 drain，任务就停在队列里没被执行。
    assert verify(client, job["batch_id"]).status_code == 202
    client.app.state.ctx.worker.drain()
    assert start(client, job["id"]).status_code == 200  # 停在 process_queued，未执行

    payload = assert_error(
        client.get(f"{ENDPOINT}/{job['id']}/file?kind=output"),
        "output_not_ready",
        status=409,
    )
    assert payload["retryable"] is True
    assert "等待" in payload["next_action"]

    # 处理中（processing）同样不可下载
    running = read_job(client, job["id"])
    running["status"] = "processing"
    running["current_stage"] = "link_color"
    write_job_file(client, running)
    assert_error(
        client.get(f"{ENDPOINT}/{job['id']}/file?kind=output"),
        "output_not_ready",
        status=409,
    )


def test_output_is_downloadable_after_verification(client, fixture_pdfs: Path):
    job = run_to_end(client, fixture_pdfs)
    response = client.get(f"{ENDPOINT}/{job['id']}/file?kind=output")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")
    assert len(response.content) == job["output"]["size"]

    source = client.get(f"{ENDPOINT}/{job['id']}/file?kind=source")
    assert source.status_code == 200
    assert source.content == (fixture_pdfs / "text_blocks.pdf").read_bytes()


def test_output_missing_after_file_is_moved(client, fixture_pdfs: Path):
    job = run_to_end(client, fixture_pdfs)
    (job_dir(client, job["id"]) / job["output"]["file_name"]).unlink()

    assert_error(
        client.get(f"{ENDPOINT}/{job['id']}/file?kind=output"),
        "output_missing",
        status=404,
    )
    # 记录本身还在，历史不会因为文件被移走就消失
    assert read_job(client, job["id"])["status"] == "completed"


def test_unknown_file_kind_is_rejected(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    assert_error(
        client.get(f"{ENDPOINT}/{job['id']}/file?kind=backup"),
        "invalid_config",
        status=400,
    )


# --------------------------------------------------------------------------- #
# 重新处理
# --------------------------------------------------------------------------- #


def test_reprocess_copies_config_into_a_brand_new_job(client, fixture_pdfs: Path):
    job = run_to_end(client, fixture_pdfs, patch={"exempt_pages_raw": "1"})

    response = client.post(f"{ENDPOINT}/{job['id']}/reprocess")
    assert response.status_code == 201, response.text
    fresh = response.json()

    assert fresh["id"] != job["id"]
    assert fresh["config"] == job["config"]
    assert fresh["reprocessed_from"] == job["id"]
    assert fresh["status"] == "configuring"
    assert fresh["output"] is None
    assert fresh["results"] == []
    assert (job_dir(client, fresh["id"]) / "original.pdf").is_file()
    # 原任务的产物与状态一个字都没被改动
    assert read_job(client, job["id"]) == job


def test_reprocess_rejects_missing_source(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    (job_dir(client, job["id"]) / "original.pdf").unlink()
    assert_error(
        client.post(f"{ENDPOINT}/{job['id']}/reprocess"), "source_missing", status=404
    )
