"""批量上传端点 `POST /api/jobs/batch` 的行为契约。

P1 的验收核心是三条：
1. 多个文件 → 多个独立任务，共享同一个 `batch_id`，彼此不覆盖；
2. 超过上限 → 整批拒绝，且**磁盘上不留任何任务目录**（不能出现幽灵任务）；
3. 批次里混入坏文件 → 只淘汰那一个，好文件照常建任务。

这些断言在实现之前必然失败（端点不存在 / 错误码不存在），
失败即是 P1 的复现证据。
"""

from __future__ import annotations

from pathlib import Path

from app.errors import ErrorCode

BATCH_ENDPOINT = "/api/jobs/batch"


def batch_upload(client, files: list[tuple[str, bytes]]):
    payload = [
        ("files", (name, data, "application/pdf")) for name, data in files
    ]
    return client.post(BATCH_ENDPOINT, files=payload)


def jobs_root(client) -> Path:
    ctx = client.app.state.ctx
    return ctx.storage.jobs_root(ctx.generation_id)


def job_dirs(client) -> list[Path]:
    root = jobs_root(client)
    return sorted(p for p in root.iterdir() if p.is_dir())


def pdf_bytes(fixture_pdfs: Path, name: str = "untagged.pdf") -> bytes:
    return (fixture_pdfs / name).read_bytes()


# --------------------------------------------------------------------------- #
# 1. 三个文件 → 三个任务，同一个 batch_id
# --------------------------------------------------------------------------- #


def test_batch_creates_one_job_per_file(client, fixture_pdfs: Path):
    data = pdf_bytes(fixture_pdfs)
    response = batch_upload(
        client,
        [
            ("a.pdf", data),
            ("b.pdf", data),
            ("c.pdf", data),
        ],
    )
    assert response.status_code == 201, response.text
    payload = response.json()

    assert payload["batch_id"].startswith("bat-")
    assert len(payload["jobs"]) == 3
    assert payload["rejected"] == []

    ids = [job["id"] for job in payload["jobs"]]
    assert len(set(ids)) == 3
    for job in payload["jobs"]:
        assert job["batch_id"] == payload["batch_id"]
        assert job["status"] == "configuring"
        assert job["generation_id"] == client.app.state.ctx.generation_id
        # 每个任务必须真的落盘，且原件字节与上传一致
        directory = jobs_root(client) / job["id"]
        assert directory.is_dir()
        assert (directory / "original.pdf").read_bytes() == data

    assert [job["original_name"] for job in payload["jobs"]] == [
        "a.pdf",
        "b.pdf",
        "c.pdf",
    ]
    assert len(job_dirs(client)) == 3


# --------------------------------------------------------------------------- #
# 2. 超过上限 → 整批拒绝，磁盘不留目录
# --------------------------------------------------------------------------- #


def test_batch_rejects_over_limit_without_leaving_dirs(client, fixture_pdfs: Path):
    limit = client.app.state.ctx.settings.limits.max_batch_files
    data = pdf_bytes(fixture_pdfs)
    files = [(f"doc-{index}.pdf", data) for index in range(limit + 1)]

    response = batch_upload(client, files)

    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["code"] == ErrorCode.TOO_MANY_FILES
    assert payload["details"]["count"] == limit + 1
    assert payload["details"]["limit"] == limit
    assert payload["next_action"]
    # 一个都不能留：批量是"整批要么全建、要么全不建"
    assert job_dirs(client) == []


# --------------------------------------------------------------------------- #
# 3. 坏文件只淘汰自己
# --------------------------------------------------------------------------- #


def test_batch_skips_bad_file_and_keeps_good_ones(client, fixture_pdfs: Path):
    good = pdf_bytes(fixture_pdfs)
    response = batch_upload(
        client,
        [
            ("good-1.pdf", good),
            ("not-really.pdf", b"this is not a pdf at all"),
            ("good-2.pdf", good),
        ],
    )

    assert response.status_code == 201, response.text
    payload = response.json()

    assert [job["original_name"] for job in payload["jobs"]] == [
        "good-1.pdf",
        "good-2.pdf",
    ]
    assert len(payload["rejected"]) == 1
    rejected = payload["rejected"][0]
    assert rejected["name"] == "not-really.pdf"
    assert rejected["code"] == ErrorCode.NOT_A_PDF
    assert rejected["message"]

    # 坏文件不留目录：磁盘上只有两个好任务
    assert len(job_dirs(client)) == 2
    names = sorted(job["id"] for job in payload["jobs"])
    assert sorted(p.name for p in job_dirs(client)) == names


def test_batch_all_bad_returns_400(client):
    response = batch_upload(
        client,
        [
            ("bad-1.pdf", b"nope"),
            ("bad-2.pdf", b"still nope"),
        ],
    )

    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["code"] == ErrorCode.NOT_A_PDF
    assert payload["details"]["rejected_count"] == 2
    assert job_dirs(client) == []


def test_batch_without_files_returns_400(client):
    response = client.post(BATCH_ENDPOINT, files=[])
    assert response.status_code == 400, response.text
    assert response.json()["code"] == ErrorCode.EMPTY_FILE
    assert job_dirs(client) == []
