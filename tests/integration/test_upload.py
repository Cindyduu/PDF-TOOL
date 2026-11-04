"""上传与物理检查。

验收点：
- 单个合法 PDF 建出完整 Job，原文件字节与哈希与上传前完全一致；
- 七类拒绝（多文件/非 PDF/空文件/加密/签名/损坏/无文件）都返回统一错误模型；
- 任何被拒绝的上传都不留下任务目录，历史里不出现幽灵任务；
- 同名不同内容的两次上传互不覆盖。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.errors import ErrorCode

ENDPOINT = "/api/jobs"
ERROR_FIELDS = {"code", "message", "details", "retryable", "next_action"}


def upload(client, name: str, data: bytes, field: str = "files"):
    return client.post(
        ENDPOINT, files=[(field, (name, data, "application/pdf"))]
    )


def upload_path(client, path: Path, name: str | None = None):
    return upload(client, name or path.name, path.read_bytes())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jobs_root(client) -> Path:
    ctx = client.app.state.ctx
    return ctx.storage.jobs_root(ctx.generation_id)


def job_dir(client, job_id: str) -> Path:
    ctx = client.app.state.ctx
    return ctx.storage.job_dir(ctx.generation_id, job_id)


def assert_error(response, code: str, status: int = 400) -> dict:
    assert response.status_code == status, response.text
    payload = response.json()
    assert set(payload) == ERROR_FIELDS
    assert payload["code"] == code
    assert payload["message"]
    assert payload["next_action"]
    return payload


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #


def test_upload_single_pdf_creates_job(client, fixture_pdfs: Path):
    source = fixture_pdfs / "untagged.pdf"
    response = upload_path(client, source)
    assert response.status_code == 201, response.text
    job = response.json()

    assert job["id"].startswith("job-")
    assert job["status"] == "configuring"
    assert job["schema_version"] == 1
    assert job["current_stage"] is None
    assert job["original_name"] == "untagged.pdf"
    assert job["created_at"]
    assert job["started_at"] is None
    assert job["finished_at"] is None
    assert job["output"] is None
    assert job["results"] == []
    assert job["last_error"] is None
    assert job["generation_id"] == client.app.state.ctx.generation_id

    assert job["source"]["file_name"] == "original.pdf"
    assert job["source"]["original_name"] == "untagged.pdf"
    assert job["source"]["page_count"] == 1
    assert job["source"]["size"] == source.stat().st_size

    assert job["tag_detection"]["status"] == "untagged"
    assert job["config"]["exempt_pages_raw"] == ""
    assert job["config"]["exempt_pages"] == []
    assert job["config"]["tag_choice"] == "generate"


def test_upload_stores_original_with_identical_bytes(client, fixture_pdfs: Path):
    source = fixture_pdfs / "exempt_mix.pdf"
    data = source.read_bytes()
    job = upload_path(client, source).json()

    stored = job_dir(client, job["id"]) / "original.pdf"
    assert stored.is_file()
    assert stored.read_bytes() == data
    assert sha256_bytes(stored.read_bytes()) == sha256_bytes(data)
    assert job["source"]["sha256"] == sha256_bytes(data)
    assert job["source"]["size"] == len(data)


def test_upload_is_indexed_for_history(client, fixture_pdfs: Path):
    job = upload_path(client, fixture_pdfs / "untagged.pdf").json()
    index = client.app.state.ctx.storage.read_index(client.app.state.ctx.generation_id)
    ids = [entry["id"] for entry in index["jobs"]]
    assert job["id"] in ids
    entry = next(item for item in index["jobs"] if item["id"] == job["id"])
    assert entry["source_available"] is True
    assert entry["output_available"] is False
    assert entry["original_name"] == "untagged.pdf"


def test_get_job_roundtrip(client, fixture_pdfs: Path):
    created = upload_path(client, fixture_pdfs / "tagged_real.pdf").json()
    fetched = client.get(f"{ENDPOINT}/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_upload_accepts_chunked_large_file(client, tmp_path: Path):
    """跨过 1MB 分块边界也要完整落盘。"""
    body = b"%PDF-1.7\n" + b"0" * (1024 * 1024 * 2) + b"\n%%EOF\n"
    response = upload(client, "big.pdf", body)
    # 这不是结构合法的 PDF，但大小与 magic 都通过，应该被"损坏"拦下，
    # 关键是走完了分块写入路径而没有中途崩溃。
    payload = assert_error(response, ErrorCode.CORRUPT_PDF)
    assert payload["details"]["reason"]
    assert list(jobs_root(client).iterdir()) == []


# --------------------------------------------------------------------------- #
# 拒绝路径
# --------------------------------------------------------------------------- #


def test_multiple_files_rejected(client, fixture_pdfs: Path):
    first = (fixture_pdfs / "untagged.pdf").read_bytes()
    second = (fixture_pdfs / "meta_clean.pdf").read_bytes()
    response = client.post(
        ENDPOINT,
        files=[
            ("files", ("a.pdf", first, "application/pdf")),
            ("files", ("b.pdf", second, "application/pdf")),
        ],
    )
    payload = assert_error(response, ErrorCode.MULTIPLE_FILES)
    assert payload["details"]["count"] == 2
    assert payload["details"]["names"] == ["a.pdf", "b.pdf"]
    assert list(jobs_root(client).iterdir()) == []


def test_no_file_rejected(client):
    response = client.post(ENDPOINT)
    assert_error(response, ErrorCode.EMPTY_FILE)


def test_non_pdf_rejected(client):
    response = upload(client, "notes.pdf", b"hello, this is not a pdf at all")
    assert_error(response, ErrorCode.NOT_A_PDF)


def test_empty_file_rejected(client):
    response = upload(client, "empty.pdf", b"")
    assert_error(response, ErrorCode.EMPTY_FILE)


@pytest.mark.parametrize(
    "fixture,code",
    [
        ("encrypted.pdf", ErrorCode.ENCRYPTED_PDF),
        ("signed.pdf", ErrorCode.SIGNED_PDF),
        ("corrupt.pdf", ErrorCode.CORRUPT_PDF),
    ],
)
def test_physically_unusable_pdfs_rejected(client, fixture_pdfs: Path, fixture, code):
    response = upload_path(client, fixture_pdfs / fixture)
    assert_error(response, code)


@pytest.mark.parametrize(
    "fixture",
    ["encrypted.pdf", "signed.pdf", "corrupt.pdf"],
)
def test_rejected_upload_leaves_no_job_directory(client, fixture_pdfs: Path, fixture):
    upload_path(client, fixture_pdfs / fixture)
    assert list(jobs_root(client).iterdir()) == []
    index = client.app.state.ctx.storage.read_index(client.app.state.ctx.generation_id)
    assert index["jobs"] == []


def test_too_many_pages_rejected(client, fixture_pdfs: Path):
    """页数上限是冻结点上的配置，用 replace 造一个更严的限额。"""
    import dataclasses

    service = client.app.state.ctx.jobs
    original = service.limits
    service.limits = dataclasses.replace(original, max_pages=0)
    try:
        response = upload_path(client, fixture_pdfs / "untagged.pdf")
    finally:
        service.limits = original
    payload = assert_error(response, ErrorCode.TOO_MANY_PAGES)
    assert payload["details"]["page_count"] == 1
    assert payload["details"]["limit"] == 0
    assert list(jobs_root(client).iterdir()) == []


def test_oversized_file_rejected(client, fixture_pdfs: Path):
    import dataclasses

    service = client.app.state.ctx.jobs
    original = service.limits
    service.limits = dataclasses.replace(original, max_upload_bytes=16)
    try:
        response = upload_path(client, fixture_pdfs / "untagged.pdf")
    finally:
        service.limits = original
    payload = assert_error(response, ErrorCode.FILE_TOO_LARGE, status=413)
    assert payload["details"]["limit"] == 16
    assert list(jobs_root(client).iterdir()) == []


# --------------------------------------------------------------------------- #
# 文件名
# --------------------------------------------------------------------------- #


def test_unicode_same_name_uploads_do_not_collide(client, fixture_pdfs: Path):
    first = (fixture_pdfs / "untagged.pdf").read_bytes()
    second = (fixture_pdfs / "meta_clean.pdf").read_bytes()
    assert first != second

    job_a = upload(client, "报告 第1版.pdf", first).json()
    job_b = upload(client, "报告 第1版.pdf", second).json()

    assert job_a["id"] != job_b["id"]
    assert job_a["original_name"] == "报告 第1版.pdf"
    assert job_b["original_name"] == "报告 第1版.pdf"
    assert job_a["source"]["sha256"] != job_b["source"]["sha256"]

    stored_a = job_dir(client, job_a["id"]) / "original.pdf"
    stored_b = job_dir(client, job_b["id"]) / "original.pdf"
    assert stored_a.read_bytes() == first
    assert stored_b.read_bytes() == second


def test_path_like_name_is_stripped(client, fixture_pdfs: Path):
    data = (fixture_pdfs / "untagged.pdf").read_bytes()
    job = upload(client, "../../etc/passwd.pdf", data).json()
    assert "/" not in job["original_name"]
    assert ".." not in job["original_name"]
    assert job["original_name"].endswith(".pdf")
    assert job_dir(client, job["id"]).resolve().parent == jobs_root(client).resolve()


def test_reserved_windows_name_is_defused(client, fixture_pdfs: Path):
    data = (fixture_pdfs / "untagged.pdf").read_bytes()
    job = upload(client, "CON.pdf", data).json()
    assert job["original_name"].upper() != "CON.PDF"
    assert job["original_name"].endswith(".pdf")


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


def test_unknown_job_returns_404(client):
    response = client.get(f"{ENDPOINT}/job-0000000000000000")
    payload = assert_error(response, ErrorCode.JOB_NOT_FOUND, status=404)
    assert payload["details"]["job_id"] == "job-0000000000000000"


@pytest.mark.parametrize("bad_id", ["notajob", "job-1", "job_00000000", "gen-abcdef"])
def test_malformed_job_id_returns_400(client, bad_id):
    response = client.get(f"{ENDPOINT}/{bad_id}")
    payload = assert_error(response, ErrorCode.INVALID_ID)
    assert payload["details"]["value"]


def test_job_json_on_disk_matches_response(client, fixture_pdfs: Path):
    import json

    job = upload_path(client, fixture_pdfs / "untagged.pdf").json()
    raw = (job_dir(client, job["id"]) / "job.json").read_text("utf-8")
    assert json.loads(raw) == job
