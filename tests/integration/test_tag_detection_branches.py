"""标签检测四分支 + 检测失败可重试。

判定原则是"不轻信 /MarkInfo /Marked"：
- 完全没有结构、也没有标记位 -> untagged
- 结构树 + ParentTree + 每页 StructParents + 内容流 MCID 齐全 -> tagged
- 其余（含只有 MarkInfo、结构树残缺） -> markinfo_only
- 读不出来 -> detection_failed，任务进入"等待标签选择"并允许重试
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.pdf.tags.detect import detect_tags
from app.schemas import default_tag_detection
from tests.integration.test_upload import assert_error, job_dir, upload_path

ENDPOINT = "/api/jobs"

DETECTION_FIELDS = set(default_tag_detection())

BRANCHES = [
    # (样本, 期望状态, 期望默认标签选择)
    ("untagged.pdf", "untagged", "generate"),
    ("exempt_mix.pdf", "untagged", "generate"),
    ("tagged_real.pdf", "tagged", "keep"),
    ("markinfo_only.pdf", "markinfo_only", "regenerate"),
    ("broken_struct.pdf", "markinfo_only", "regenerate"),
]


@pytest.mark.parametrize("fixture,status,choice", BRANCHES)
def test_upload_lands_in_expected_branch(client, fixture_pdfs: Path, fixture, status, choice):
    job = upload_path(client, fixture_pdfs / fixture).json()
    detection = job["tag_detection"]
    assert set(detection) == DETECTION_FIELDS
    assert detection["status"] == status
    assert detection["message"]
    assert detection["checked_at"]
    assert detection["error"] is None
    assert job["config"]["tag_choice"] == choice
    assert job["status"] == "configuring"


@pytest.mark.parametrize("fixture,status,choice", BRANCHES)
def test_detect_tags_agrees_with_api(client, fixture_pdfs: Path, fixture, status, choice):
    path = fixture_pdfs / fixture
    job = upload_path(client, path).json()
    direct = detect_tags(path)
    assert direct["status"] == status
    for field in ("has_struct_tree", "has_markinfo", "marked",
                  "parent_tree_ok", "struct_parents_ok", "mcid_count"):
        assert direct[field] == job["tag_detection"][field], field


def test_untagged_has_no_structure_and_no_markinfo(client, fixture_pdfs: Path):
    detection = upload_path(client, fixture_pdfs / "untagged.pdf").json()["tag_detection"]
    assert detection["has_struct_tree"] is False
    assert detection["has_markinfo"] is False
    assert detection["marked"] is False
    assert detection["mcid_count"] == 0


def test_tagged_real_has_complete_structure(client, fixture_pdfs: Path):
    detection = upload_path(client, fixture_pdfs / "tagged_real.pdf").json()["tag_detection"]
    assert detection["has_struct_tree"] is True
    assert detection["parent_tree_ok"] is True
    assert detection["struct_parents_ok"] is True
    assert detection["mcid_count"] > 0


def test_markinfo_only_is_not_trusted(client, fixture_pdfs: Path):
    detection = upload_path(client, fixture_pdfs / "markinfo_only.pdf").json()["tag_detection"]
    assert detection["has_markinfo"] is True
    assert detection["marked"] is True
    assert detection["has_struct_tree"] is False
    assert detection["status"] == "markinfo_only"


def test_broken_struct_tree_is_treated_as_markinfo_only(client, fixture_pdfs: Path):
    detection = upload_path(client, fixture_pdfs / "broken_struct.pdf").json()["tag_detection"]
    assert detection["has_struct_tree"] is True
    assert detection["status"] == "markinfo_only"
    assert detection["parent_tree_ok"] is False or detection["struct_parents_ok"] is False


# --------------------------------------------------------------------------- #
# 检测失败与重试
# --------------------------------------------------------------------------- #


def _failed_detection() -> dict:
    payload = default_tag_detection()
    payload.update(
        status="detection_failed",
        checked_at="2026-01-01T00:00:00Z",
        error="RuntimeError: simulated",
        message="无法读取标签信息，请重试检测",
    )
    return payload


def test_detection_failure_parks_job_in_awaiting_decision(
    client, fixture_pdfs: Path, monkeypatch
):
    monkeypatch.setattr(
        "app.services.jobs.detect_tags", lambda path: _failed_detection()
    )
    job = upload_path(client, fixture_pdfs / "untagged.pdf").json()
    assert job["status"] == "awaiting_tag_decision"
    assert job["tag_detection"]["status"] == "detection_failed"
    assert job["tag_detection"]["error"]
    assert job["config"]["tag_choice"] is None


def test_detection_failure_blocks_start(client, fixture_pdfs: Path, monkeypatch):
    monkeypatch.setattr(
        "app.services.jobs.detect_tags", lambda path: _failed_detection()
    )
    job = upload_path(client, fixture_pdfs / "untagged.pdf").json()
    response = client.post(f"{ENDPOINT}/{job['id']}/start")
    assert_error(response, "invalid_config")


def test_retry_after_failure_restores_branch(client, fixture_pdfs: Path, monkeypatch):
    original = "app.services.jobs.detect_tags"
    monkeypatch.setattr(original, lambda path: _failed_detection())
    job = upload_path(client, fixture_pdfs / "tagged_real.pdf").json()
    assert job["status"] == "awaiting_tag_decision"

    monkeypatch.undo()
    response = client.post(f"{ENDPOINT}/{job['id']}/tag-detection/retry")
    assert response.status_code == 200, response.text
    retried = response.json()
    assert retried["tag_detection"]["status"] == "tagged"
    assert retried["status"] == "configuring"
    assert retried["config"]["tag_choice"] == "keep"


def test_retry_keeps_user_choice(client, fixture_pdfs: Path, monkeypatch):
    original = "app.services.jobs.detect_tags"
    monkeypatch.setattr(original, lambda path: _failed_detection())
    job = upload_path(client, fixture_pdfs / "untagged.pdf").json()

    patched = client.patch(
        f"{ENDPOINT}/{job['id']}/config", json={"tag_choice": "skip"}
    )
    assert patched.status_code == 200
    assert patched.json()["config"]["tag_choice"] == "skip"

    monkeypatch.undo()
    retried = client.post(f"{ENDPOINT}/{job['id']}/tag-detection/retry").json()
    assert retried["tag_detection"]["status"] == "untagged"
    assert retried["config"]["tag_choice"] == "skip"


def test_retry_persists_to_disk(client, fixture_pdfs: Path, monkeypatch):
    original = "app.services.jobs.detect_tags"
    monkeypatch.setattr(original, lambda path: _failed_detection())
    job = upload_path(client, fixture_pdfs / "untagged.pdf").json()
    monkeypatch.undo()
    client.post(f"{ENDPOINT}/{job['id']}/tag-detection/retry")

    import json

    on_disk = json.loads((job_dir(client, job["id"]) / "job.json").read_text("utf-8"))
    assert on_disk["tag_detection"]["status"] == "untagged"
    assert on_disk["status"] == "configuring"


def test_retry_without_source_returns_404(client, fixture_pdfs: Path):
    job = upload_path(client, fixture_pdfs / "untagged.pdf").json()
    (job_dir(client, job["id"]) / "original.pdf").unlink()
    response = client.post(f"{ENDPOINT}/{job['id']}/tag-detection/retry")
    assert_error(response, "source_missing", status=404)


def test_retry_on_started_job_returns_409(client, fixture_pdfs: Path):
    job = upload_path(client, fixture_pdfs / "untagged.pdf").json()
    directory = job_dir(client, job["id"])
    job["status"] = "running"
    (directory / "job.json").write_text(
        __import__("json").dumps(job, ensure_ascii=False), "utf-8"
    )
    response = client.post(f"{ENDPOINT}/{job['id']}/tag-detection/retry")
    assert_error(response, "job_already_started", status=409)


# --------------------------------------------------------------------------- #
# detect_tags 自身绝不抛异常
# --------------------------------------------------------------------------- #


def test_detect_tags_on_missing_file_does_not_raise(tmp_path: Path):
    payload = detect_tags(tmp_path / "nope.pdf")
    assert payload["status"] == "detection_failed"
    assert payload["error"]
    assert set(payload) == DETECTION_FIELDS


def test_detect_tags_on_garbage_does_not_raise(tmp_path: Path):
    path = tmp_path / "garbage.pdf"
    path.write_bytes(b"%PDF-1.7\nnot really a pdf")
    payload = detect_tags(path)
    assert payload["status"] == "detection_failed"
    assert payload["error"]
