"""阶段 J 集成验收：ZIP 备份与原子恢复。

要证明四件事：

1. **备份是自足的**：包里只有事实来源（`settings.json`、每个 `job.json`、
   `original.pdf`、输出 PDF），清单逐条记 `size` 与 `sha256`；
   可重建的 `index.json` 不进包——它不是事实，带进去只会让恢复后的索引说谎。
2. **校验不落地**：inspect 只读，改不动任何数据，通过后只发一个短期一次性令牌。
   「手滑点了一下恢复」不会直接覆盖现有数据——必须拿着令牌再来一次。
3. **恢复三步留退路**：先自动备份当前数据并校验 → 解压到相邻的新 generation
   并全量校验 → 原子切换 `active.json`。旧 generation 一个都不删。
4. **冲突要说人话**：有活动任务 / 锁被占用 → 409 + `next_action`，且数据零变化。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.config import SCHEMA_VERSION
from app.core import archive
from app.services import backups as backups_module
from tests.integration.test_history import run_to_end, upload
from tests.integration.test_upload import assert_error, job_dir, sha256_bytes, upload_path

ENDPOINT = "/api/backups"
JOBS = "/api/jobs"
SUMMARY_KEYS = {
    "size",
    "created_at",
    "format_version",
    "schema_version",
    "app_version",
    "generation_id",
    "job_count",
    "entry_count",
    "total_bytes",
    "note",
}
RESTORE_KEYS = {
    "generation_id",
    "previous_generation_id",
    "auto_backup_id",
    "job_count",
    "restored_at",
}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def backup_list(client) -> dict:
    response = client.get(ENDPOINT)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"generation_id", "count", "backups"}
    assert payload["count"] == len(payload["backups"])
    return payload


def create_backup(client) -> dict:
    response = client.post(ENDPOINT)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert set(payload) == {"backup"}
    return payload["backup"]


def inspect_bytes(client, data: bytes, name: str = "backup.zip"):
    return client.post(
        f"{ENDPOINT}/inspect", files=[("files", (name, data, "application/zip"))]
    )


def inspect_file(client, path: Path):
    return inspect_bytes(client, path.read_bytes(), path.name)


def token_for(client, path: Path) -> str:
    response = inspect_file(client, path)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def restore(client, token: str):
    return client.post(f"{ENDPOINT}/restore", json={"token": token})


def restore_ok(client, token: str) -> dict:
    response = restore(client, token)
    assert response.status_code == 200, response.text
    return response.json()


def backup_path(client, backup_id: str) -> Path:
    return client.app.state.ctx.storage.backup_path(backup_id)


def archive_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as handle:
        return handle.namelist()


def archive_manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as handle:
        return json.loads(handle.read(archive.MANIFEST_NAME).decode("utf-8"))


def read_job(client, job_id: str) -> dict:
    return json.loads((job_dir(client, job_id) / "job.json").read_text("utf-8"))


def snapshot_generation(client, generation_id: str) -> dict[str, str]:
    """一个 generation 里所有事实文件的哈希。

    `index.json` 刻意排除：它是可重建的缓存，`rebuilt_at` 每次重建都会变，
    拿它当「数据变没变」的证据只会误报。
    """
    root = client.app.state.ctx.storage.generation_dir(generation_id)
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "index.json":
            continue
        result[str(path.relative_to(root)).replace("\\", "/")] = sha256_bytes(
            path.read_bytes()
        )
    return result


def state(client) -> dict:
    """「当前数据」的完整快照：指针字节、内存里的 generation、文件哈希、目录集合。

    失败路径的验收标准是「当前数据零变化」，就得有一个能真的比出来的东西。
    """
    ctx = client.app.state.ctx
    active = ctx.storage.active_generation_id()
    return {
        "pointer": ctx.settings.active_path.read_bytes(),
        "memory": ctx.generation_id,
        "active": active,
        "files": snapshot_generation(client, active),
        "generations": sorted(ctx.storage.list_generation_ids()),
    }


def history_ids(client) -> list[str]:
    response = client.get(JOBS)
    assert response.status_code == 200, response.text
    return [entry["id"] for entry in response.json()["jobs"]]


def history_entry(client, job_id: str) -> dict:
    response = client.get(JOBS)
    assert response.status_code == 200, response.text
    for entry in response.json()["jobs"]:
        if entry["id"] == job_id:
            return entry
    raise AssertionError(f"历史里没有任务 {job_id}")


def complete_job(client, fixture_pdfs: Path, name: str) -> dict:
    """上传并跑完一个任务，返回终态 job（含 output）。"""
    job = upload(client, fixture_pdfs, name)
    done = run_to_end(client, job)
    assert done["output"] is not None, done
    return done


# --------------------------------------------------------------------------- #
# 备份内容
# --------------------------------------------------------------------------- #


def test_create_backup_returns_describable_backup(client, fixture_pdfs):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)

    assert backup["id"].startswith("bak-")
    assert backup["readable"] is True
    assert backup["format_version"] == 1
    assert backup["job_count"] == 1
    assert backup["size"] > 0
    assert backup["created_at"]
    assert backup["download_url"] == f"{ENDPOINT}/{backup['id']}/file"
    assert backup_path(client, backup["id"]).is_file()


def test_manifest_carries_versions_and_checksums(client, fixture_pdfs):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    path = backup_path(client, backup["id"])

    manifest = archive_manifest(path)
    assert manifest["format_version"] == 1
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["app_version"]
    assert manifest["generation_id"] == client.app.state.ctx.generation_id
    assert manifest["job_count"] == 1
    assert manifest["entry_count"] == len(manifest["entries"])
    assert manifest["total_bytes"] == sum(item["size"] for item in manifest["entries"])

    with zipfile.ZipFile(path) as handle:
        for item in manifest["entries"]:
            assert set(item) == {"rel_path", "size", "sha256"}
            assert len(item["sha256"]) == 64
            data = handle.read(item["rel_path"])
            assert item["size"] == len(data)
            assert item["sha256"] == sha256_bytes(data)


def test_archive_holds_every_fact_file_and_no_cache(client, fixture_pdfs):
    done = complete_job(client, fixture_pdfs, "a.pdf")
    output_name = done["output"]["file_name"]
    upload(client, fixture_pdfs, "b.pdf")  # 只有原件的任务也要进包
    backup = create_backup(client)
    names = set(archive_names(backup_path(client, backup["id"])))

    assert archive.MANIFEST_NAME in names
    assert archive.SETTINGS_NAME in names
    for job_id in (done["id"], history_ids(client)[0]):
        assert f"jobs/{job_id}/job.json" in names
        assert f"jobs/{job_id}/original.pdf" in names
    assert f"jobs/{done['id']}/{output_name}" in names

    # 可重建的索引不进包：恢复后由 rebuild_index 重新生成
    assert not any(name.endswith("index.json") for name in names)
    assert not any(name.endswith("events.jsonl") for name in names)


def test_list_backups_is_newest_first(client, fixture_pdfs):
    complete_job(client, fixture_pdfs, "a.pdf")
    first = create_backup(client)
    second = create_backup(client)

    payload = backup_list(client)
    assert payload["count"] == 2
    assert [item["id"] for item in payload["backups"]] == [second["id"], first["id"]]
    assert all(item["readable"] for item in payload["backups"])


def test_download_returns_the_same_bytes(client, fixture_pdfs):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)

    response = client.get(f"{ENDPOINT}/{backup['id']}/file")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert backup["id"] in response.headers["content-disposition"]
    assert response.content == backup_path(client, backup["id"]).read_bytes()


def test_download_unknown_or_invalid_id_is_rejected(client):
    missing = client.get(f"{ENDPOINT}/bak-20200101T000000Z-abcdef/file")
    assert_error(missing, "backup_not_found", status=404)

    invalid = client.get(f"{ENDPOINT}/not-a-backup/file")
    assert_error(invalid, "invalid_id", status=400)


# --------------------------------------------------------------------------- #
# 校验（inspect）与令牌
# --------------------------------------------------------------------------- #


def test_inspect_returns_summary_and_token(client, fixture_pdfs):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)

    response = inspect_file(client, backup_path(client, backup["id"]))
    assert response.status_code == 200, response.text
    payload = response.json()

    assert set(payload) == {"token", "summary", "expires_in", "file_name"}
    assert isinstance(payload["token"], str) and len(payload["token"]) >= 16
    assert payload["expires_in"] == 600
    assert payload["file_name"].endswith(".zip")
    assert set(payload["summary"]) == SUMMARY_KEYS
    assert payload["summary"]["job_count"] == 1
    assert payload["summary"]["format_version"] == 1
    assert payload["summary"]["size"] == backup["size"]


def test_inspect_is_read_only(client, fixture_pdfs):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    before = state(client)

    assert inspect_file(client, backup_path(client, backup["id"])).status_code == 200

    assert state(client) == before


def test_inspect_needs_exactly_one_file(client):
    none = client.post(f"{ENDPOINT}/inspect")
    assert_error(none, "invalid_backup", status=400)

    two = client.post(
        f"{ENDPOINT}/inspect",
        files=[
            ("files", ("a.zip", b"PK\x03\x04", "application/zip")),
            ("files", ("b.zip", b"PK\x03\x04", "application/zip")),
        ],
    )
    assert_error(two, "invalid_backup", status=400)
    assert two.json()["details"]["count"] == 2


def test_inspect_rejects_non_zip_and_empty(client):
    not_zip = inspect_bytes(client, b"this is not a zip file at all")
    assert_error(not_zip, "invalid_backup", status=400)
    assert "ZIP" in not_zip.json()["message"]

    empty = inspect_bytes(client, b"")
    assert_error(empty, "invalid_backup", status=400)


def test_consumed_token_drops_its_temp_package(client, fixture_pdfs):
    """令牌用过就作废，它对应的临时包也必须立刻删掉。

    没用过的令牌**不该**删：用户可能校验完过几分钟才点「确认恢复」。
    """
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    ctx = client.app.state.ctx

    token = token_for(client, backup_path(client, backup["id"]))
    temp = ctx.settings.tmp_dir / f"restore-{token}.zip"
    assert temp.is_file()

    restore_ok(client, token)
    assert not temp.exists()

    # 另一个没消费的令牌仍保留自己的临时包
    pending = token_for(client, backup_path(client, backup["id"]))
    assert (ctx.settings.tmp_dir / f"restore-{pending}.zip").is_file()


def test_token_is_single_use(client, fixture_pdfs):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    token = token_for(client, backup_path(client, backup["id"]))

    assert restore(client, token).status_code == 200

    again = restore(client, token)
    assert_error(again, "restore_token_invalid", status=400)
    assert again.json()["retryable"] is True


def test_unknown_token_is_rejected(client):
    response = restore(client, "definitely-not-a-token")
    assert_error(response, "restore_token_invalid", status=400)


def test_expired_token_is_rejected_and_temp_removed(client, fixture_pdfs, monkeypatch):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    monkeypatch.setattr(backups_module, "TOKEN_TTL_SECONDS", 0.0)

    token = token_for(client, backup_path(client, backup["id"]))
    before = state(client)

    response = restore(client, token)
    assert_error(response, "restore_token_invalid", status=400)
    assert state(client) == before
    assert list(client.app.state.ctx.settings.tmp_dir.glob("restore-*.zip")) == []


# --------------------------------------------------------------------------- #
# 恢复成功路径
# --------------------------------------------------------------------------- #


def test_restore_brings_history_and_files_back(client, fixture_pdfs):
    done = complete_job(client, fixture_pdfs, "a.pdf")
    other = upload(client, fixture_pdfs, "b.pdf")
    output_name = done["output"]["file_name"]
    output_bytes = (job_dir(client, done["id"]) / output_name).read_bytes()
    source_bytes = (job_dir(client, done["id"]) / "original.pdf").read_bytes()

    backup = create_backup(client)
    previous = client.app.state.ctx.generation_id

    # 用户把数据弄丢了：一个任务目录整个没了，另一个任务的产物也没了
    import shutil

    shutil.rmtree(job_dir(client, other["id"]))
    (job_dir(client, done["id"]) / output_name).unlink()
    assert history_ids(client) == [done["id"]]

    token = token_for(client, backup_path(client, backup["id"]))
    payload = restore_ok(client, token)

    assert set(payload) == RESTORE_KEYS
    assert payload["previous_generation_id"] == previous
    assert payload["generation_id"] != previous
    assert payload["job_count"] == 2
    assert payload["auto_backup_id"].startswith("bak-")
    assert payload["restored_at"]

    ctx = client.app.state.ctx
    assert ctx.generation_id == payload["generation_id"]
    assert ctx.storage.active_generation_id() == payload["generation_id"]

    # 历史回来了，文件也回来了，字节完全一致
    assert sorted(history_ids(client)) == sorted([done["id"], other["id"]])
    entry = history_entry(client, done["id"])
    assert entry["output_available"] is True
    assert entry["source_available"] is True
    assert entry["files_available"] is True

    downloaded = client.get(f"{JOBS}/{done['id']}/file?kind=output")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == output_bytes

    source = client.get(f"{JOBS}/{done['id']}/file?kind=source")
    assert source.status_code == 200, source.text
    assert source.content == source_bytes

    # 报告在恢复后可以按 job.json 重建，不需要包里带 report.json
    report = client.get(f"{JOBS}/{done['id']}/report")
    assert report.status_code == 200, report.text
    assert report.json()["job_id"] == done["id"]


def test_restore_keeps_old_generation_untouched(client, fixture_pdfs):
    done = complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    ctx = client.app.state.ctx
    previous = ctx.generation_id
    previous_files = snapshot_generation(client, previous)

    token = token_for(client, backup_path(client, backup["id"]))
    restore_ok(client, token)

    # 旧 generation 目录还在，内容一个字节没变（恢复错了还能切回去）
    assert previous in ctx.storage.list_generation_ids()
    assert snapshot_generation(client, previous) == previous_files
    assert (job_dir(client, done["id"]) / "job.json").is_file()


def test_restore_makes_an_auto_backup_of_previous_data(client, fixture_pdfs):
    done = complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    previous = client.app.state.ctx.generation_id

    token = token_for(client, backup_path(client, backup["id"]))
    payload = restore_ok(client, token)

    auto = backup_path(client, payload["auto_backup_id"])
    assert auto.is_file()
    manifest = archive_manifest(auto)
    assert manifest["reason"] == "pre-restore"
    assert manifest["generation_id"] == previous
    assert manifest["job_count"] == 1

    listed = {item["id"] for item in backup_list(client)["backups"]}
    assert {backup["id"], payload["auto_backup_id"]} <= listed


def test_restored_backup_is_still_downloadable_and_identical(client, fixture_pdfs):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    original_bytes = backup_path(client, backup["id"]).read_bytes()

    token = token_for(client, backup_path(client, backup["id"]))
    restore_ok(client, token)

    response = client.get(f"{ENDPOINT}/{backup['id']}/file")
    assert response.status_code == 200, response.text
    assert response.content == original_bytes


def test_restore_twice_lands_on_a_third_generation(client, fixture_pdfs):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    path = backup_path(client, backup["id"])

    first = restore_ok(client, token_for(client, path))
    second = restore_ok(client, token_for(client, path))

    assert first["generation_id"] != second["generation_id"]
    assert second["previous_generation_id"] == first["generation_id"]
    ctx = client.app.state.ctx
    assert len(ctx.storage.list_generation_ids()) == 3
    assert history_ids(client) == [entry["id"] for entry in client.get(JOBS).json()["jobs"]]


# --------------------------------------------------------------------------- #
# 冲突
# --------------------------------------------------------------------------- #


def test_restore_is_blocked_while_a_job_is_active(client, fixture_pdfs):
    done = complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    token = token_for(client, backup_path(client, backup["id"]))

    # 让一个任务停在 queued：恢复期间不允许有活动任务
    stuck = read_job(client, done["id"])
    stuck["status"] = "queued"
    stuck["finished_at"] = None
    (job_dir(client, done["id"]) / "job.json").write_text(
        json.dumps(stuck, ensure_ascii=False), "utf-8"
    )
    before = state(client)

    response = restore(client, token)
    assert_error(response, "restore_conflict", status=409)
    body = response.json()
    assert body["retryable"] is True
    assert body["next_action"]
    assert body["details"]["active"][0]["job_id"] == done["id"]
    assert state(client) == before

    # 任务结束后同一个令牌还能用（令牌只在成功时消费）
    stuck["status"] = "completed"
    (job_dir(client, done["id"]) / "job.json").write_text(
        json.dumps(stuck, ensure_ascii=False), "utf-8"
    )
    restore_ok(client, token)


def test_restore_is_blocked_by_the_restore_lock(client, fixture_pdfs):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    token = token_for(client, backup_path(client, backup["id"]))
    ctx = client.app.state.ctx
    before = state(client)

    lock = ctx.locks.acquire("restore")
    try:
        response = restore(client, token)
    finally:
        lock.release()

    assert_error(response, "restore_conflict", status=409)
    assert response.json()["details"]["lock"] == "restore"
    assert state(client) == before

    restore_ok(client, token)


def test_backup_is_blocked_by_the_backup_lock(client):
    ctx = client.app.state.ctx
    lock = ctx.locks.acquire("backup")
    try:
        response = client.post(ENDPOINT)
    finally:
        lock.release()

    assert_error(response, "locked", status=409)
    assert response.json()["details"]["lock"] == "backup"
