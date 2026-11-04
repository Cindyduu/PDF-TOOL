"""配置校验：非法页码必须在写入前就被拦住，并且不能带着进入处理。

两条防线：
1. `PATCH /config` 当场拒绝，磁盘上的配置保持原样；
2. `POST /start` 再按 `exempt_pages_raw` 重新解析一遍，
   手工改坏 job.json 也过不去。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_upload import assert_error, job_dir, upload_path

ENDPOINT = "/api/jobs"

BAD_PAGES = ["0", "5-3", "-1", "1,,2", "999", "abc", "1-", "1.5"]


def patch_config(client, job_id: str, body: dict):
    return client.patch(f"{ENDPOINT}/{job_id}/config", json=body)


def read_job(client, job_id: str) -> dict:
    return json.loads((job_dir(client, job_id) / "job.json").read_text("utf-8"))


def write_job(client, job: dict) -> None:
    (job_dir(client, job["id"]) / "job.json").write_text(
        json.dumps(job, ensure_ascii=False), "utf-8"
    )


def five_page_job(client, fixture_pdfs: Path) -> dict:
    return upload_path(client, fixture_pdfs / "exempt_mix.pdf").json()


# --------------------------------------------------------------------------- #
# 合法配置
# --------------------------------------------------------------------------- #


def test_patch_exempt_pages_normalizes(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    response = patch_config(client, job["id"], {"exempt_pages_raw": " 4 , 5 "})
    assert response.status_code == 200, response.text
    config = response.json()["config"]
    assert config["exempt_pages"] == [4, 5]
    # 相邻页码合并成区间，输入框回显与页码集合永远一致
    assert config["exempt_pages_raw"] == "4-5"
    assert read_job(client, job["id"])["config"] == config


def test_patch_keeps_gaps_between_ranges(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    config = patch_config(client, job["id"], {"exempt_pages_raw": "1,3,5"}).json()["config"]
    assert config["exempt_pages"] == [1, 3, 5]
    assert config["exempt_pages_raw"] == "1,3,5"


def test_patch_range_is_expanded(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    config = patch_config(client, job["id"], {"exempt_pages_raw": "2-4"}).json()["config"]
    assert config["exempt_pages"] == [2, 3, 4]
    assert config["exempt_pages_raw"] == "2-4"


def test_patch_clears_exempt_pages(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    patch_config(client, job["id"], {"exempt_pages_raw": "1,2"})
    config = patch_config(client, job["id"], {"exempt_pages_raw": None}).json()["config"]
    assert config["exempt_pages"] == []
    assert config["exempt_pages_raw"] == ""


def test_patch_without_fields_keeps_config(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    before = job["config"]
    config = patch_config(client, job["id"], {}).json()["config"]
    assert config == before


def test_patch_tag_choice_is_saved(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    config = patch_config(client, job["id"], {"tag_choice": "skip"}).json()["config"]
    assert config["tag_choice"] == "skip"
    assert read_job(client, job["id"])["config"]["tag_choice"] == "skip"


def test_config_survives_reload(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    patch_config(client, job["id"], {"exempt_pages_raw": "3", "tag_choice": "generate"})
    fetched = client.get(f"{ENDPOINT}/{job['id']}").json()
    assert fetched["config"]["exempt_pages"] == [3]
    assert fetched["config"]["tag_choice"] == "generate"


# --------------------------------------------------------------------------- #
# 非法配置
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", BAD_PAGES)
def test_invalid_pages_are_rejected_before_write(client, fixture_pdfs: Path, raw):
    job = five_page_job(client, fixture_pdfs)
    response = patch_config(client, job["id"], {"exempt_pages_raw": raw})
    payload = assert_error(response, "invalid_pages")
    assert payload["next_action"]
    assert payload["details"]["raw"] == raw
    assert payload["retryable"] is False

    on_disk = read_job(client, job["id"])["config"]
    assert on_disk["exempt_pages_raw"] == ""
    assert on_disk["exempt_pages"] == []


def test_out_of_range_reports_page_count(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    payload = assert_error(
        patch_config(client, job["id"], {"exempt_pages_raw": "999"}), "invalid_pages"
    )
    assert payload["details"]["page_count"] == 5
    assert "5" in payload["message"]


def test_invalid_tag_choice_is_rejected(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    payload = assert_error(
        patch_config(client, job["id"], {"tag_choice": "bogus"}), "invalid_config"
    )
    assert payload["details"]["allowed"] == ["generate", "skip", "keep", "regenerate"]
    assert read_job(client, job["id"])["config"]["tag_choice"] == "generate"


def test_unknown_field_is_rejected(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    response = patch_config(client, job["id"], {"exempt": "1"})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_config"


def test_null_tag_choice_is_rejected(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    payload = assert_error(
        patch_config(client, job["id"], {"tag_choice": None}), "invalid_config"
    )
    assert payload["next_action"]


def test_patch_on_started_job_returns_409(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    started = read_job(client, job["id"])
    started["status"] = "queued"
    write_job(client, started)
    response = patch_config(client, job["id"], {"exempt_pages_raw": "1"})
    assert_error(response, "job_already_started", status=409)


def test_patch_unknown_job_returns_404(client):
    response = client.patch(f"{ENDPOINT}/job-0000000000000000/config", json={})
    assert_error(response, "job_not_found", status=404)


# --------------------------------------------------------------------------- #
# 开始处理的闸门
# --------------------------------------------------------------------------- #


def test_start_rejects_tampered_pages(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    tampered = read_job(client, job["id"])
    tampered["config"]["exempt_pages_raw"] = "5-3"
    tampered["config"]["exempt_pages"] = [3, 4, 5]
    write_job(client, tampered)

    response = client.post(f"{ENDPOINT}/{job['id']}/start")
    payload = assert_error(response, "invalid_pages")
    assert payload["details"]["raw"] == "5-3"


def test_start_rejects_out_of_range_pages(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    tampered = read_job(client, job["id"])
    tampered["config"]["exempt_pages_raw"] = "999"
    tampered["config"]["exempt_pages"] = [999]
    write_job(client, tampered)

    payload = assert_error(
        client.post(f"{ENDPOINT}/{job['id']}/start"), "invalid_pages"
    )
    assert payload["details"]["page_count"] == 5


def test_start_repairs_inconsistent_page_list(client, fixture_pdfs: Path):
    """raw 与解析结果不一致时，以 raw 为准并写回磁盘。"""
    job = five_page_job(client, fixture_pdfs)
    tampered = read_job(client, job["id"])
    tampered["config"]["exempt_pages_raw"] = "2,4"
    tampered["config"]["exempt_pages"] = [9]
    write_job(client, tampered)

    client.post(f"{ENDPOINT}/{job['id']}/start")
    repaired = read_job(client, job["id"])["config"]
    assert repaired["exempt_pages"] == [2, 4]
    assert repaired["exempt_pages_raw"] == "2,4"


def test_start_rejects_missing_source(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    (job_dir(client, job["id"]) / "original.pdf").unlink()
    assert_error(
        client.post(f"{ENDPOINT}/{job['id']}/start"), "source_missing", status=404
    )


def test_start_rejects_finished_job(client, fixture_pdfs: Path):
    job = five_page_job(client, fixture_pdfs)
    finished = read_job(client, job["id"])
    finished["status"] = "completed"
    write_job(client, finished)
    assert_error(
        client.post(f"{ENDPOINT}/{job['id']}/start"), "job_already_started", status=409
    )


def test_start_rejects_unknown_job(client):
    assert_error(
        client.post(f"{ENDPOINT}/job-0000000000000000/start"),
        "job_not_found",
        status=404,
    )


def test_start_with_valid_config_enqueues_job(client, fixture_pdfs: Path):
    """配置合法时 `POST /start` 真的把任务放进队列，并如实变成 `queued`。

    阶段 D 这里曾回 503（队列未接入）；阶段 G 接上之后语义变成"已排队"。
    测试应用用 `start_services=False` 建，后台线程不启动，所以任务停在
    `queued`——状态完全可预测，不会出现测试与后台线程抢着改 job.json。
    """
    job = five_page_job(client, fixture_pdfs)
    patch_config(client, job["id"], {"exempt_pages_raw": "1,3"})

    response = client.post(f"{ENDPOINT}/{job['id']}/start")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert read_job(client, job["id"])["status"] == "queued"
    assert read_job(client, job["id"])["config"]["exempt_pages"] == [1, 3]

    # 第二次开始必须被拒，且队列里不会多出第二个任务
    second = client.post(f"{ENDPOINT}/{job['id']}/start")
    assert_error(second, "job_already_started", status=409)
    assert client.app.state.ctx.worker.pending() == 1
