"""M2 验收：前置校验这一段动线（R13、§8.3、§8.4）。

这里只证明 M2 自己的东西，不重复 M3–M5 的处理细节：

1. 分支 A 全链路：上传 → 校验 → `verified` → 开始处理 → 终态；
2. 分支 B 全链路：`skip_precheck` → 直接处理 → 终态，且 `verify` 保持 `None`；
3. 两道闸门各挡各的：未校验不许处理（`verify_not_ready`），
   处理中不许校验（`job_already_started`）；
4. 零写入：校验前后源文件 sha256 与上传时记录值一致（AC-R13-N4）；
5. 结果落盘：换一个 `AppContext` 读同一份数据，结果还在且不重跑（AC-R13-E3）；
6. 单项异常不拖垮整份结论（AC-R13-E2）；
7. 进度事件只报「第几项 / 共几项」，没有任何百分比（M2-4）；
8. 批次汇总（§8.4）与未知阶段拒绝。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.config import load_settings
from app.context import AppContext
from app.errors import AppError, ErrorCode
from app.pdf import checks as checks_pkg
from app.schemas import VERIFY_KEYS, validate_verify_result
from tests.integration.test_upload import assert_error, job_dir, upload_path

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


def verify(client, batch_id: str, body: dict | None = None):
    return client.post(f"{BATCH}/{batch_id}/verify", json=body or {})


def start(client, job_id: str):
    return client.post(f"{JOBS}/{job_id}/start")


def read_job(client, job_id: str) -> dict:
    return json.loads((job_dir(client, job_id) / "job.json").read_text("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_and_drain(client, batch_id: str, body: dict | None = None) -> dict:
    """提交校验并同步跑完，返回提交响应体（含 `started` / `skipped`）。"""
    response = verify(client, batch_id, body)
    assert response.status_code == 202, response.text
    client.app.state.ctx.worker.drain()
    return response.json()


# --------------------------------------------------------------------------- #
# 分支 A：先校验再处理
# --------------------------------------------------------------------------- #


def test_branch_a_runs_verify_then_process(client, fixture_pdfs: Path):
    """AC-R13-N2 的后端部分：校验 → verified → 处理 → 终态。"""
    job = upload(client, fixture_pdfs)

    submitted = verify_and_drain(client, job["batch_id"])
    assert submitted["started"] == [job["id"]]
    assert submitted["skipped"] == []

    verified = client.get(f"{JOBS}/{job['id']}").json()
    assert verified["status"] == "verified"
    assert verified["phase"] == "precheck"
    assert verified["current_stage"] is None
    assert verified["output"] is None, "校验阶段不允许产出任何文件"
    assert verified["post_verify"] is None, "后置校验属于处理之后"

    result = verified["verify"]
    validate_verify_result(result)
    assert result["job_id"] == job["id"]
    assert result["phase"] == "precheck"
    assert result["file"]["name"] == "text_blocks.pdf"
    assert [item["key"] for item in result["items"]] == list(VERIFY_KEYS)
    # 十二项全跑完，四态计数必须加起来正好十二项
    assert len(result["items"]) == len(VERIFY_KEYS) == 12
    assert sum(result["summary"].values()) == len(VERIFY_KEYS)
    assert set(result["summary"]) == {"pass", "warning", "fail", "skipped"}
    # v4.2 的三项新校验必须真的出现在结果里，而不是注册表里有、跑起来没有
    assert {"bookmarks", "initial_view", "metadata_security"} <= {
        item["key"] for item in result["items"]
    }

    assert start(client, job["id"]).status_code == 200
    client.app.state.ctx.worker.drain()

    done = client.get(f"{JOBS}/{job['id']}").json()
    assert done["status"] in SUCCESS_STATUSES
    assert done["phase"] == "process"
    assert done["output"] is not None
    # 校验结果在进入处理之后仍然读得到，不被处理阶段冲掉
    assert done["verify"]["generated_at"] == result["generated_at"]


def test_verify_applies_config_sent_with_the_request(client, fixture_pdfs: Path):
    """准备页上「勾选 → 点校验」是一个动作，配置随请求一起到。

    单独再发一次 PATCH 会让「勾了但没保存」变成静默丢配置，
    所以校验入口必须先落配置、再入队。
    """
    job = upload(client, fixture_pdfs)
    picked = ["bookmarks", "filename_naming"]

    verify_and_drain(client, job["batch_id"], {"verify_items": picked})

    fresh = client.get(f"{JOBS}/{job['id']}").json()
    # 配置本身也被归一化成声明序：前端重新渲染勾选状态时不必再排一次序，
    # 也不会出现「配置里是 A,B、报告里是 B,A」这种对不上的情况。
    assert fresh["config"]["verify_items"] == ["filename_naming", "bookmarks"]
    assert set(fresh["config"]["verify_items"]) == set(picked)
    assert [item["key"] for item in fresh["verify"]["items"]] == [
        "filename_naming",
        "bookmarks",
    ]
    assert sum(fresh["verify"]["summary"].values()) == 2


def test_verify_report_matches_the_job_result(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    response = client.get(f"{BATCH}/{job['batch_id']}/verify-report")
    assert response.status_code == 200, response.text
    report = response.json()

    assert report["batch_id"] == job["batch_id"]
    assert report["phase"] == "precheck"
    assert [item["job_id"] for item in report["files"]] == [job["id"]]
    assert report["summary"]["files"] == 1
    # 批次汇总的计数是逐文件相加，不是「有几个文件」
    assert report["summary"]["pass"] == report["files"][0]["summary"]["pass"]
    assert sum(
        report["summary"][key] for key in ("pass", "warning", "fail", "skipped")
    ) == len(VERIFY_KEYS)


# --------------------------------------------------------------------------- #
# 分支 B：跳过前置校验
# --------------------------------------------------------------------------- #


def test_branch_b_skips_verify_and_goes_straight_to_process(
    client, fixture_pdfs: Path
):
    """AC-R13-N3 的后端部分：不校验也能处理，但 `verify` 必须诚实地留在 `None`。

    这里不允许拿一个空结果冒充「校验通过」——那会让结果页看起来像跑过校验。
    """
    job = upload(client, fixture_pdfs)
    patched = client.patch(
        f"{JOBS}/{job['id']}/config", json={"skip_precheck": True}
    )
    assert patched.status_code == 200, patched.text

    assert start(client, job["id"]).status_code == 200
    client.app.state.ctx.worker.drain()

    done = client.get(f"{JOBS}/{job['id']}").json()
    assert done["status"] in SUCCESS_STATUSES
    assert done["verify"] is None
    assert done["output"] is not None


# --------------------------------------------------------------------------- #
# 两道闸门
# --------------------------------------------------------------------------- #


def test_start_without_verify_is_rejected(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    payload = assert_error(start(client, job["id"]), "verify_not_ready", status=409)
    assert payload["details"]["job_id"] == job["id"]
    assert payload["details"]["skip_precheck"] is False
    assert "不执行前置校验" in payload["next_action"]

    # 被拦下之后任务还停在 configuring，没有偷偷入队
    assert client.app.state.ctx.worker.pending() == 0
    assert read_job(client, job["id"])["status"] == "configuring"


def test_start_while_verifying_is_rejected(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    # 只提交、不 drain：任务停在 `verify_queued`，正好是「校验还没跑完」
    assert verify(client, job["batch_id"]).status_code == 202
    assert read_job(client, job["id"])["status"] == "verify_queued"

    payload = assert_error(start(client, job["id"]), "job_already_started", status=409)
    assert payload["details"]["status"] == "verify_queued"
    assert "校验" in payload["message"]

    client.app.state.ctx.worker.drain()
    assert client.get(f"{JOBS}/{job['id']}").json()["status"] == "verified"


def test_verify_while_processing_is_skipped(client, fixture_pdfs: Path):
    """处理中再提交校验：整批入口不 4xx，而是把这一条列进 `skipped`。

    与 `POST /batch/{id}/start` 同一取舍：一个任务状态不对，不该让同批
    另外十九个也提交不了。所以断言落在 `skipped` 的内容上，而不是 HTTP 码。
    """
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])
    assert start(client, job["id"]).status_code == 200  # 停在 process_queued

    submitted = verify_and_drain(client, job["batch_id"])
    assert submitted["started"] == []
    assert len(submitted["skipped"]) == 1
    entry = submitted["skipped"][0]
    assert entry["id"] == job["id"]
    assert entry["code"] == ErrorCode.JOB_ALREADY_STARTED
    assert entry["message"]
    assert entry["next_action"]

    client.app.state.ctx.worker.drain()
    assert client.get(f"{JOBS}/{job['id']}").json()["status"] in SUCCESS_STATUSES


def test_verify_without_any_item_is_rejected_at_the_service_layer(
    client, fixture_pdfs: Path
):
    """AC-R13-E1：一项都不勾时不允许校验。

    这个错误在批次入口会被折进 `skipped`（同上一条的理由），
    所以真正的判定要在服务层验——HTTP 上只能看到「这一条没被提交」。
    """
    job = upload(client, fixture_pdfs)
    assert (
        client.patch(
            f"{JOBS}/{job['id']}/config", json={"verify_items": []}
        ).status_code
        == 200
    )

    ctx = client.app.state.ctx
    with pytest.raises(AppError) as caught:
        ctx.jobs.assert_verifiable(job["id"])
    assert caught.value.code == ErrorCode.NO_VERIFY_ITEMS
    assert caught.value.http_status == 400
    assert "不执行前置校验" in caught.value.next_action

    submitted = verify_and_drain(client, job["batch_id"])
    assert submitted["started"] == []
    assert submitted["skipped"][0]["code"] == ErrorCode.NO_VERIFY_ITEMS


# --------------------------------------------------------------------------- #
# 只读：AC-R13-N4
# --------------------------------------------------------------------------- #


def test_verify_does_not_touch_the_source_file(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    stored = job_dir(client, job["id"]) / "original.pdf"
    before = sha256_file(stored)
    assert before == job["source"]["sha256"]

    verify_and_drain(client, job["batch_id"])

    after = sha256_file(stored)
    assert after == before, "前置校验改动了原始文件"
    # 目录里也不该多出任何东西：校验连临时文件都不写
    assert sorted(path.name for path in job_dir(client, job["id"]).iterdir()) == [
        "events.jsonl",
        "job.json",
        "original.pdf",
    ]


# --------------------------------------------------------------------------- #
# 落盘可恢复：AC-R13-E3
# --------------------------------------------------------------------------- #


def test_verify_result_survives_a_restart_without_rerunning(
    client, fixture_pdfs: Path
):
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])
    recorded = client.get(f"{JOBS}/{job['id']}").json()["verify"]

    var_dir = client.app.state.ctx.settings.var_dir
    fresh = AppContext(load_settings(var_dir))
    try:
        reloaded = fresh.jobs.get_job(job["id"])
        assert reloaded["status"] == "verified"
        assert reloaded["verify"] == recorded
        validate_verify_result(reloaded["verify"])
        # 恢复靠的是读 job.json，不是「再跑一遍」：队列里空空如也
        assert fresh.worker.pending() == 0
    finally:
        fresh.worker.stop()


# --------------------------------------------------------------------------- #
# 单项隔离：AC-R13-E2
# --------------------------------------------------------------------------- #


def test_one_broken_check_does_not_fail_the_whole_result(
    client, fixture_pdfs: Path, monkeypatch: pytest.MonkeyPatch
):
    """某一项抛异常 → 该项记 `fail` + 异常摘要，其余八项照常出结果。"""

    def boom(_ctx):  # noqa: ANN001 - 测试替身
        raise RuntimeError("字体表解析炸了\n第二行不该出现在摘要里")

    monkeypatch.setitem(checks_pkg.CHECKS, "font_embedding", boom)

    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    result = client.get(f"{JOBS}/{job['id']}").json()["verify"]
    validate_verify_result(result)
    by_key = {item["key"]: item for item in result["items"]}

    assert len(by_key) == len(VERIFY_KEYS), "一项坏掉不能让别的项消失"
    broken = by_key["font_embedding"]
    assert broken["status"] == "fail"
    assert "RuntimeError" in broken["summary"]
    # 异常摘要只取首行：整段栈进报告会把十二项结果挤成一屏日志
    assert "第二行" not in broken["summary"]
    assert result["summary"]["fail"] >= 1

    # 校验结论里出现 fail 也不阻止处理——决定权在用户，报告只负责说清楚
    assert client.get(f"{JOBS}/{job['id']}").json()["status"] == "verified"
    assert start(client, job["id"]).status_code == 200


# --------------------------------------------------------------------------- #
# 进度事件：M2-4
# --------------------------------------------------------------------------- #


def test_verify_progress_events_report_items_without_percentages(
    client, fixture_pdfs: Path
):
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    events = client.get(f"{JOBS}/{job['id']}/events").json()["events"]
    progress = [event for event in events if event["type"] == "verify_progress"]

    assert len(progress) == len(VERIFY_KEYS), "每一项完成都要有一条事件"
    assert [event["item"]["index"] for event in progress] == list(
        range(1, len(VERIFY_KEYS) + 1)
    )
    assert {event["item"]["total"] for event in progress} == {len(VERIFY_KEYS)}
    assert [event["item"]["key"] for event in progress] == list(VERIFY_KEYS)
    for event in progress:
        assert event["item"]["status"] in ("pass", "warning", "fail", "skipped")
        assert set(event["item"]) == {"key", "status", "index", "total"}

    # 与处理阶段的进度口径一致：界面显示真实发生过的事，不显示推算出来的百分比
    for event in progress:
        blob = json.dumps(event["item"], ensure_ascii=False)
        for word in ("percent", "progress", "ratio", "%"):
            assert word not in blob

    # 阶段事件：前置校验只有一个阶段名，且排在最前
    stages = [event["stage"] for event in events if event["type"] == "stage"]
    assert stages[0] == "precheck"
    assert stages.count("precheck") == 1
    assert "verifying" not in stages, "前置校验不能复用后置校验的阶段名"


# --------------------------------------------------------------------------- #
# 批次汇总的边界
# --------------------------------------------------------------------------- #


def test_verify_report_is_404_before_any_verify(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    payload = assert_error(
        client.get(f"{BATCH}/{job['batch_id']}/verify-report"),
        "verify_report_missing",
        status=404,
    )
    assert payload["details"]["phase"] == "precheck"


def test_verify_report_rejects_an_unknown_phase(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    payload = assert_error(
        client.get(f"{BATCH}/{job['batch_id']}/verify-report?phase=whatever"),
        "invalid_config",
        status=400,
    )
    assert payload["details"]["allowed"] == ["precheck", "post"]
    assert "precheck" in payload["next_action"]


def test_verify_report_for_post_phase_is_404_until_processing_finishes(
    client, fixture_pdfs: Path
):
    """后置校验是 M3 之后的事；在那之前 `phase=post` 只能是「还没有结果」。"""
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])
    assert_error(
        client.get(f"{BATCH}/{job['batch_id']}/verify-report?phase=post"),
        "verify_report_missing",
        status=404,
    )


def test_verify_skips_a_job_whose_source_is_gone(client, fixture_pdfs: Path):
    """源文件被用户手动删掉后提交校验：服务层给 `source_missing`（404）。"""
    job = upload(client, fixture_pdfs)
    (job_dir(client, job["id"]) / "original.pdf").unlink()

    ctx = client.app.state.ctx
    with pytest.raises(AppError) as caught:
        ctx.jobs.assert_verifiable(job["id"])
    assert caught.value.code == ErrorCode.SOURCE_MISSING
    assert caught.value.http_status == 404

    submitted = verify_and_drain(client, job["batch_id"])
    assert submitted["started"] == []
    assert submitted["skipped"][0]["code"] == ErrorCode.SOURCE_MISSING


def test_both_phases_run_all_twelve_items(client, fixture_pdfs: Path):
    """前置与后置各跑一遍十二项，而且各自独立（§10.3 第 6 条：不缓存）。

    v4.2 的三项新校验必须**两个阶段都在**：前置看到的是原文件的问题，
    后置看到的是工具改完之后的真实状态。少一个阶段，用户要么看不到问题、
    要么看不到问题是否被修好。
    """
    job = upload(client, fixture_pdfs)
    verify_and_drain(client, job["batch_id"])

    pre = client.get(f"{JOBS}/{job['id']}").json()["verify"]
    assert [item["key"] for item in pre["items"]] == list(VERIFY_KEYS)

    assert start(client, job["id"]).status_code == 200
    client.app.state.ctx.worker.drain()

    done = client.get(f"{JOBS}/{job['id']}").json()
    post = done["post_verify"]
    assert post is not None, "处理完成后必须留下后置校验结果"
    validate_verify_result(post)
    assert post["phase"] == "post"
    assert [item["key"] for item in post["items"]] == list(VERIFY_KEYS)
    assert len(post["items"]) == len(VERIFY_KEYS) == 12

    # 两阶段各存一份，不共用同一个对象（否则改一份等于改两份）
    assert pre["generated_at"] != post["generated_at"]

    # 三项新校验在两个阶段都要有**明确结论**：既不能缺项，也不能报「检查异常」。
    for phase, payload in (("precheck", pre), ("post", post)):
        by_key = {item["key"]: item for item in payload["items"]}
        for key in ("initial_view", "metadata_security"):
            assert by_key[key]["status"] in ("pass", "warning", "fail"), (phase, key)
        # 书签项按 R37 的 `MIN_PAGES = 5` 规则判定，而本用例的样本是
        # `text_blocks.pdf`（2 页）——正确行为就是 `skipped`，不是缺陷。
        # 这里断言「按规则跳过」，而不是断言「必须 pass/warning/fail」：
        # 前者能抓住「注册表里有、跑起来却被异常吞掉」的回归，
        # 后者会把合规的跳过误判成失败。
        assert by_key["bookmarks"]["status"] == "skipped", phase
        assert "检查异常" not in by_key["bookmarks"]["summary"]
