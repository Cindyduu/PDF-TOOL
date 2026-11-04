"""阶段 I 集成验收：历史列表、文件缺失与重新处理。

要证明的是「记录与文件解耦」：

1. 列表**现场**读盘——把输出文件拖走，下一次请求立刻 `output_available=false`，
   记录本身还在（历史不会因为文件没了而消失）；
2. 操作按存在性启停——输出没了禁用下载（接口 404 `output_missing`），
   原件没了禁用重新处理（接口 404 `source_missing`），互不牵连；
3. `files_available` 是两者的合取，缺任何一个都为 false；
4. reprocess 产出**全新 ID**：配置相同、原件字节相同、目录互不重叠，
   源任务的 job.json 一个字节都没变，删掉新任务也不影响老任务；
5. 搜索/筛选是服务端行为，不是前端过滤。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.integration.test_upload import (
    assert_error,
    job_dir,
    sha256_bytes,
    upload_path,
)

ENDPOINT = "/api/jobs"
LIST_KEYS = {"generation_id", "total", "count", "query", "status", "jobs"}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def listing(client, **params) -> dict:
    response = client.get(ENDPOINT, params=params)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == LIST_KEYS, payload.keys()
    assert payload["count"] == len(payload["jobs"])
    return payload


def upload(client, fixture_pdfs: Path, name: str, source: str = "text_blocks.pdf") -> dict:
    response = upload_path(client, fixture_pdfs / source, name=name)
    assert response.status_code == 201, response.text
    return response.json()


def run_to_end(client, job: dict, patch: dict | None = None) -> dict:
    if patch:
        assert client.patch(f"{ENDPOINT}/{job['id']}/config", json=patch).status_code == 200
    assert client.post(f"{ENDPOINT}/{job['id']}/start").status_code == 200
    client.app.state.ctx.worker.drain()
    return client.get(f"{ENDPOINT}/{job['id']}").json()


def read_job(client, job_id: str) -> dict:
    return json.loads((job_dir(client, job_id) / "job.json").read_text("utf-8"))


def entry_of(payload: dict, job_id: str) -> dict:
    for entry in payload["jobs"]:
        if entry["id"] == job_id:
            return entry
    raise AssertionError(f"历史里没有任务 {job_id}")


def ids(payload: dict) -> list[str]:
    return [entry["id"] for entry in payload["jobs"]]


# --------------------------------------------------------------------------- #
# 列表本身
# --------------------------------------------------------------------------- #


def test_listing_is_empty_on_a_fresh_workspace(client):
    payload = listing(client)
    assert payload["jobs"] == []
    assert payload["total"] == 0
    assert payload["generation_id"]


def test_listing_is_newest_first_and_counts_match(client, fixture_pdfs: Path):
    first = upload(client, fixture_pdfs, "最早的一份.pdf")
    second = upload(client, fixture_pdfs, "中间的一份.pdf")
    third = upload(client, fixture_pdfs, "最新的一份.pdf")

    payload = listing(client)
    assert payload["total"] == 3
    assert ids(payload) == [third["id"], second["id"], first["id"]]

    # created_at 相同（同一毫秒）时也不允许乱序：排序键是倒序的字符串时间
    stamps = [entry["created_at"] for entry in payload["jobs"]]
    assert stamps == sorted(stamps, reverse=True)


def test_listing_entry_carries_everything_the_ui_needs(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs, "报表 2024.pdf")
    entry = entry_of(listing(client), job["id"])

    assert entry["original_name"] == "报表 2024.pdf"
    assert entry["status"] == job["status"]
    assert entry["output_name"] is None
    assert entry["output_available"] is False
    assert entry["source_available"] is True
    assert entry["files_available"] is False
    assert entry["review_required"] is False
    assert entry["created_at"] == job["created_at"]


def test_listing_reflects_a_new_upload_without_any_cache_delay(client, fixture_pdfs: Path):
    """列表不做缓存：上传后下一次请求就要看见。"""
    assert listing(client)["count"] == 0
    upload(client, fixture_pdfs, "刚刚上传.pdf")
    assert listing(client)["count"] == 1


# --------------------------------------------------------------------------- #
# 搜索
# --------------------------------------------------------------------------- #


def test_search_matches_filename_case_insensitively(client, fixture_pdfs: Path):
    upper = upload(client, fixture_pdfs, "Annual Report.PDF")
    lower = upload(client, fixture_pdfs, "quarterly notes.pdf")

    payload = listing(client, q="annual")
    assert ids(payload) == [upper["id"]]
    assert payload["count"] == 1 and payload["total"] == 2
    assert payload["query"] == "annual"

    assert ids(listing(client, q="QUARTERLY")) == [lower["id"]]
    # 中文文件名也要能搜
    chinese = upload(client, fixture_pdfs, "年度报告.pdf")
    assert ids(listing(client, q="年度")) == [chinese["id"]]


def test_search_matches_the_job_id(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs, "按任务号搜.pdf")
    assert ids(listing(client, q=job["id"])) == [job["id"]]
    # 任务号的中段也能命中（模糊匹配，不是前缀匹配）
    assert ids(listing(client, q=job["id"][4:12])) == [job["id"]]


def test_search_matches_the_output_name(client, fixture_pdfs: Path):
    job = run_to_end(client, upload(client, fixture_pdfs, "输出去向.pdf"))
    output_name = job["output"]["file_name"]
    assert ids(listing(client, q=output_name[:-4])) == [job["id"]]


def test_blank_search_returns_everything(client, fixture_pdfs: Path):
    upload(client, fixture_pdfs, "甲.pdf")
    upload(client, fixture_pdfs, "乙.pdf")
    assert listing(client)["count"] == 2
    assert listing(client, q="")["count"] == 2
    assert listing(client, q="   ")["count"] == 2


def test_search_without_match_returns_empty_list_not_error(client, fixture_pdfs: Path):
    upload(client, fixture_pdfs, "有的文件.pdf")
    payload = listing(client, q="根本没有这个文件")
    assert payload["count"] == 0
    assert payload["jobs"] == []
    assert payload["total"] == 1


# --------------------------------------------------------------------------- #
# 状态筛选
# --------------------------------------------------------------------------- #


def test_status_filter_is_exact_not_prefix(client, fixture_pdfs: Path):
    finished = run_to_end(client, upload(client, fixture_pdfs, "已完成的.pdf"))
    waiting = upload(client, fixture_pdfs, "待配置的.pdf")

    assert finished["status"] == "completed_with_warnings"
    assert waiting["status"] == "configuring"

    only_finished = listing(client, status="completed_with_warnings")
    assert ids(only_finished) == [finished["id"]]
    assert only_finished["status"] == "completed_with_warnings"

    only_waiting = listing(client, status="configuring")
    assert ids(only_waiting) == [waiting["id"]]

    # `completed` 不能把 `completed_with_warnings` 一起捞进来
    assert listing(client, status="completed")["count"] == 0


def test_search_and_status_filter_combine(client, fixture_pdfs: Path):
    a = run_to_end(client, upload(client, fixture_pdfs, "alpha 完成.pdf"))
    run_to_end(client, upload(client, fixture_pdfs, "beta 完成.pdf"))
    waiting = upload(client, fixture_pdfs, "alpha 待配置.pdf")

    payload = listing(client, q="alpha", status="completed_with_warnings")
    assert ids(payload) == [a["id"]]
    assert payload["total"] == 3

    assert ids(listing(client, q="alpha", status="configuring")) == [waiting["id"]]
    assert listing(client, q="alpha", status="failed")["count"] == 0


def test_unknown_status_filter_is_rejected(client):
    response = client.get(ENDPOINT, params={"status": "complet"})
    payload = assert_error(response, "invalid_config", status=422)
    assert payload["details"]["status"] == "complet"
    assert "completed" in payload["details"]["allowed"]


# --------------------------------------------------------------------------- #
# 文件缺失
# --------------------------------------------------------------------------- #


def test_moved_output_keeps_the_record_and_disables_download(client, fixture_pdfs: Path):
    job = run_to_end(client, upload(client, fixture_pdfs, "产物会被移走.pdf"))
    output_path = job_dir(client, job["id"]) / job["output"]["file_name"]
    assert output_path.is_file()

    output_path.unlink()

    payload = listing(client)
    entry = entry_of(payload, job["id"])
    assert payload["count"] == 1, "文件没了，记录不能跟着消失"
    assert entry["output_available"] is False
    assert entry["source_available"] is True
    assert entry["files_available"] is False
    # 状态与报告都还在：这是「文件缺失」，不是「任务失败」
    assert entry["status"] == job["status"]
    assert client.get(f"{ENDPOINT}/{job['id']}/report").status_code == 200

    assert_error(
        client.get(f"{ENDPOINT}/{job['id']}/file", params={"kind": "output"}),
        "output_missing",
        status=404,
    )


def test_moved_source_disables_reprocess(client, fixture_pdfs: Path):
    job = run_to_end(client, upload(client, fixture_pdfs, "原件会被移走.pdf"))
    (job_dir(client, job["id"]) / "original.pdf").unlink()

    entry = entry_of(listing(client), job["id"])
    assert entry["source_available"] is False
    assert entry["output_available"] is True, "只缺原件，产物下载不受影响"
    assert entry["files_available"] is False

    assert_error(
        client.post(f"{ENDPOINT}/{job['id']}/reprocess"), "source_missing", status=404
    )
    # 产物仍然可下载
    assert client.get(f"{ENDPOINT}/{job['id']}/file", params={"kind": "output"}).status_code == 200


def test_files_available_requires_both_files(client, fixture_pdfs: Path):
    job = run_to_end(client, upload(client, fixture_pdfs, "两个文件都要在.pdf"))
    assert entry_of(listing(client), job["id"])["files_available"] is True

    (job_dir(client, job["id"]) / "original.pdf").unlink()
    assert entry_of(listing(client), job["id"])["files_available"] is False


def test_record_survives_when_both_files_are_gone(client, fixture_pdfs: Path):
    job = run_to_end(client, upload(client, fixture_pdfs, "全都没了.pdf"))
    directory = job_dir(client, job["id"])
    (directory / job["output"]["file_name"]).unlink()
    (directory / "original.pdf").unlink()

    entry = entry_of(listing(client), job["id"])
    assert entry["output_available"] is False
    assert entry["source_available"] is False
    assert entry["files_available"] is False
    assert (directory / "job.json").is_file()
    assert (directory / "report.json").is_file()


def test_corrupt_job_json_only_hides_that_entry(client, fixture_pdfs: Path):
    good = upload(client, fixture_pdfs, "正常的.pdf")
    broken = upload(client, fixture_pdfs, "坏掉的.pdf")
    (job_dir(client, broken["id"]) / "job.json").write_text("{ not json", "utf-8")

    payload = listing(client)
    assert ids(payload) == [good["id"]]
    assert payload["count"] == 1


# --------------------------------------------------------------------------- #
# 重新处理
# --------------------------------------------------------------------------- #


def test_reprocess_creates_a_fully_independent_job(client, fixture_pdfs: Path):
    source_job = run_to_end(
        client,
        upload(client, fixture_pdfs, "要被复制的.pdf"),
        patch={"tag_choice": "skip", "exempt_pages_raw": "1"},
    )
    source_dir = job_dir(client, source_job["id"])
    source_sha = sha256_bytes((source_dir / "original.pdf").read_bytes())
    before = read_job(client, source_job["id"])

    response = client.post(f"{ENDPOINT}/{source_job['id']}/reprocess")
    assert response.status_code == 201, response.text
    fresh = response.json()

    assert fresh["id"] != source_job["id"]
    assert fresh["reprocessed_from"] == source_job["id"]
    assert fresh["config"] == source_job["config"]
    assert fresh["output"] is None and fresh["results"] == []

    fresh_dir = job_dir(client, fresh["id"])
    assert fresh_dir != source_dir
    assert sha256_bytes((fresh_dir / "original.pdf").read_bytes()) == source_sha
    assert hashlib.sha256((fresh_dir / "job.json").read_bytes()).hexdigest() != hashlib.sha256(
        (source_dir / "job.json").read_bytes()
    ).hexdigest()

    # 源任务一个字节都没变
    assert read_job(client, source_job["id"]) == before
    assert (source_dir / source_job["output"]["file_name"]).is_file()


def test_reprocessed_job_runs_independently(client, fixture_pdfs: Path):
    source_job = run_to_end(client, upload(client, fixture_pdfs, "原件保留.pdf"))
    fresh = client.post(f"{ENDPOINT}/{source_job['id']}/reprocess").json()

    done = run_to_end(client, fresh)
    assert done["status"] == "completed_with_warnings"
    assert done["output"]["file_name"]
    # 两个任务的产物各自独立存在
    assert (job_dir(client, source_job["id"]) / source_job["output"]["file_name"]).is_file()
    assert (job_dir(client, done["id"]) / done["output"]["file_name"]).is_file()
    # 历史里两条都在，状态各自正确
    payload = listing(client)
    assert payload["count"] == 2
    assert entry_of(payload, source_job["id"])["status"] == source_job["status"]
    assert entry_of(payload, done["id"])["status"] == "completed_with_warnings"


def test_reprocess_still_works_when_only_the_output_was_moved(client, fixture_pdfs: Path):
    job = run_to_end(client, upload(client, fixture_pdfs, "产物没了但原件在.pdf"))
    (job_dir(client, job["id"]) / job["output"]["file_name"]).unlink()

    response = client.post(f"{ENDPOINT}/{job['id']}/reprocess")
    assert response.status_code == 201, response.text
    assert response.json()["reprocessed_from"] == job["id"]


def test_deleting_the_copy_does_not_touch_the_original(client, fixture_pdfs: Path):
    source_job = run_to_end(client, upload(client, fixture_pdfs, "被复制者.pdf"))
    fresh = client.post(f"{ENDPOINT}/{source_job['id']}/reprocess").json()

    directory = job_dir(client, fresh["id"])
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()

    payload = listing(client)
    assert ids(payload) == [source_job["id"]]
    assert entry_of(payload, source_job["id"])["output_available"] is True
    assert (
        client.get(
            f"{ENDPOINT}/{source_job['id']}/file", params={"kind": "output"}
        ).status_code
        == 200
    )


def test_reprocess_of_unknown_job_is_404(client):
    assert_error(
        client.post(f"{ENDPOINT}/job-0000000000000000/reprocess"),
        "job_not_found",
        status=404,
    )


# --------------------------------------------------------------------------- #
# 服务入口一致
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("via", ["service", "endpoint"])
def test_service_and_endpoint_agree(client, fixture_pdfs: Path, via: str):
    """服务层与 HTTP 层必须返回同一份数据，避免两套口径。"""
    upload(client, fixture_pdfs, "一致性检查.pdf")

    if via == "service":
        payload = client.app.state.ctx.history.list_jobs()
    else:
        payload = listing(client)

    assert set(payload) == LIST_KEYS
    assert payload["count"] == 1
    assert payload["jobs"][0]["files_available"] is False


def test_history_service_is_the_single_source_of_truth(client, fixture_pdfs: Path):
    """`ctx.jobs.list_jobs` 只是转发，实现只有 history 一份。"""
    upload(client, fixture_pdfs, "转发检查.pdf")
    ctx = client.app.state.ctx
    assert ctx.jobs.list_jobs() == ctx.history.list_jobs()
