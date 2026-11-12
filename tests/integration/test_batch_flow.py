"""批量处理的完整链路：建批次 → 统一配置 → 全部开始 → 跑完 → 逐个下载。

这里断言的是**磁盘上的真实结果**，不是接口自报的状态：
每个任务的输出文件都要能打开、页数要和源文件一致，
批次里的坏配置只能拖住它自己。
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf

from app.errors import ErrorCode

BATCH = "/api/jobs/batch"
JOBS = "/api/jobs"

# 流水线对"某项不适用/有警告"的文档会落到 completed_with_warnings，
# 这仍然是成功终态：产物已经过最终验证并发布。
SUCCESS_STATUSES = ("completed", "completed_with_warnings")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def upload_batch(client, fixture_pdfs: Path, names: list[str]):
    files = [
        ("files", (name, (fixture_pdfs / name).read_bytes(), "application/pdf"))
        for name in names
    ]
    return client.post(BATCH, files=files)


def create_batch(client, fixture_pdfs: Path, names: list[str]) -> dict:
    response = upload_batch(client, fixture_pdfs, names)
    assert response.status_code == 201, response.text
    return response.json()


def job_dir(client, job_id: str) -> Path:
    ctx = client.app.state.ctx
    return ctx.storage.job_dir(ctx.generation_id, job_id)


def read_job(client, job_id: str) -> dict:
    return json.loads((job_dir(client, job_id) / "job.json").read_text("utf-8"))


def page_count(path: Path) -> int:
    with pymupdf.open(str(path)) as document:
        return document.page_count


def verify_batch(client, batch_id: str):
    return client.post(f"{BATCH}/{batch_id}/verify", json={})


def run_batch(client, batch_id: str) -> dict:
    """整批跑到底：先前置校验（R13），再开始处理。

    v4.0 的 `POST /batch/{id}/start` 语义变成「开始处理」，没校验过的任务会被
    `verify_not_ready` 拦下。所以批量跑到底现在是两次提交。
    """
    verified = verify_batch(client, batch_id)
    assert verified.status_code == 202, verified.text
    client.app.state.ctx.worker.drain()

    response = client.post(f"{BATCH}/{batch_id}/start")
    assert response.status_code == 200, response.text
    client.app.state.ctx.worker.drain()
    return response.json()


def get_batch(client, batch_id: str) -> dict:
    response = client.get(f"{BATCH}/{batch_id}")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 1. 三个文件跑到底
# --------------------------------------------------------------------------- #


def test_batch_runs_all_jobs_to_completion(client, fixture_pdfs: Path):
    payload = create_batch(
        client, fixture_pdfs, ["untagged.pdf", "text_blocks.pdf", "exempt_mix.pdf"]
    )
    batch_id = payload["batch_id"]
    job_ids = [job["id"] for job in payload["jobs"]]

    # 统一配置：这批文件都不豁免任何页
    patched = client.patch(f"{BATCH}/{batch_id}/config", json={"exempt_pages_raw": ""})
    assert patched.status_code == 200, patched.text
    assert sorted(patched.json()["updated"]) == sorted(job_ids)
    assert patched.json()["invalid"] == []

    started = run_batch(client, batch_id)
    assert sorted(started["started"]) == sorted(job_ids)
    assert started["skipped"] == []

    detail = get_batch(client, batch_id)
    assert detail["total"] == 3
    for job in detail["jobs"]:
        assert job["status"] in SUCCESS_STATUSES, job
        assert job["finished_at"]
        assert job["output"]["file_name"]

        # 输出必须真的存在、能打开、页数与源一致
        output = job_dir(client, job["id"]) / job["output"]["file_name"]
        assert output.is_file()
        assert page_count(output) == page_count(fixture_pdfs / job["original_name"])

        # 下载端点也必须给得出来
        download = client.get(f"{JOBS}/{job['id']}/file", params={"kind": "output"})
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/pdf"

    # 每个任务都有自己的报告，互不串台
    for job_id in job_ids:
        report = client.get(f"{JOBS}/{job_id}/report")
        assert report.status_code == 200, report.text
        assert report.json()["job_id"] == job_id


# --------------------------------------------------------------------------- #
# 2. 一个任务配置非法，只跳过它
# --------------------------------------------------------------------------- #


def test_batch_start_skips_only_the_invalid_job(client, fixture_pdfs: Path):
    payload = create_batch(
        client, fixture_pdfs, ["untagged.pdf", "text_blocks.pdf", "exempt_mix.pdf"]
    )
    batch_id = payload["batch_id"]
    broken_id = payload["jobs"][2]["id"]  # exempt_mix.pdf，5 页
    healthy_ids = [job["id"] for job in payload["jobs"][:2]]

    # 模拟"配置文件被手工改坏"：5 页的文件要求豁免第 99 页。
    # 批量启动前的校验必须重新解析，不能信任落盘的 exempt_pages。
    path = job_dir(client, broken_id) / "job.json"
    broken = json.loads(path.read_text("utf-8"))
    broken["config"]["exempt_pages_raw"] = "99"
    broken["config"]["exempt_pages"] = [99]
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

    result = run_batch(client, batch_id)

    assert sorted(result["started"]) == sorted(healthy_ids)
    assert len(result["skipped"]) == 1
    skipped = result["skipped"][0]
    assert skipped["id"] == broken_id
    assert skipped["code"] == ErrorCode.INVALID_PAGES
    assert skipped["message"]
    assert skipped["next_action"]

    # 好的两个跑完了，坏的那个停在「校验已过、处理被拦」——它确实没被处理，
    # 也没产出任何文件；拦住它的是 `POST /start` 对页码的重新解析。
    for job_id in healthy_ids:
        assert read_job(client, job_id)["status"] in SUCCESS_STATUSES
    broken_after = read_job(client, broken_id)
    assert broken_after["status"] == "verified"
    assert broken_after["output"] is None


# --------------------------------------------------------------------------- #
# 3. 统一配置要按各文件页数逐个校验
# --------------------------------------------------------------------------- #


def test_batch_config_rejects_pages_beyond_each_file(client, fixture_pdfs: Path):
    payload = create_batch(client, fixture_pdfs, ["exempt_mix.pdf", "untagged.pdf"])
    batch_id = payload["batch_id"]
    wide_id = payload["jobs"][0]["id"]  # 5 页
    narrow_id = payload["jobs"][1]["id"]  # 1 页

    response = client.patch(
        f"{BATCH}/{batch_id}/config", json={"exempt_pages_raw": "1-3"}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["updated"] == [wide_id]
    assert len(body["invalid"]) == 1
    assert body["invalid"][0]["id"] == narrow_id
    assert body["invalid"][0]["code"] == ErrorCode.INVALID_PAGES

    # 越界的配置不能写进那个文件
    narrow = read_job(client, narrow_id)
    assert narrow["config"]["exempt_pages"] == []
    assert narrow["config"]["exempt_pages_raw"] == ""


# --------------------------------------------------------------------------- #
# 4. 历史按 batch_id 过滤
# --------------------------------------------------------------------------- #


def test_history_filters_by_batch_id(client, fixture_pdfs: Path):
    first = create_batch(client, fixture_pdfs, ["untagged.pdf", "text_blocks.pdf"])
    second = create_batch(client, fixture_pdfs, ["exempt_mix.pdf"])

    response = client.get(JOBS, params={"batch_id": first["batch_id"]})
    assert response.status_code == 200, response.text
    body = response.json()

    ids = {entry["id"] for entry in body["jobs"]}
    assert ids == {job["id"] for job in first["jobs"]}
    assert body["count"] == 2
    # total 是筛选前的总数：三个任务都还在
    assert body["total"] == 3

    other = client.get(JOBS, params={"batch_id": second["batch_id"]}).json()
    assert {entry["id"] for entry in other["jobs"]} == {
        job["id"] for job in second["jobs"]
    }


def test_batch_endpoints_reject_malformed_ids(client):
    response = client.get(f"{BATCH}/not-a-batch")
    assert response.status_code == 400, response.text
    assert response.json()["code"] == ErrorCode.INVALID_ID

    response = client.post(f"{BATCH}/bat-0000000000000000/start")
    assert response.status_code == 404, response.text
    assert response.json()["code"] == ErrorCode.JOB_NOT_FOUND
