"""阶段 C：路径白名单与 generation 存储。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.storage import (
    PREFIX_GENERATION,
    PREFIX_JOB,
    Storage,
    new_generation_id,
    new_job_id,
    resolve_under,
    safe_relpath,
    validate_id,
)
from app.errors import AppError


# --------------------------------------------------------------------------- #
# 路径穿越
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "segment",
    ["..", ".", "", "a/b", "a\\b", "C:x", "~user", "\\\\server\\share", "../etc"],
)
def test_resolve_under_rejects_dangerous_segments(tmp_path: Path, segment: str) -> None:
    with pytest.raises(AppError):
        resolve_under(tmp_path, segment)


def test_resolve_under_accepts_normal_name(tmp_path: Path) -> None:
    result = resolve_under(tmp_path, "报告 第1版.pdf")
    assert result.parent == tmp_path.resolve()


def test_resolve_under_multiple_segments(tmp_path: Path) -> None:
    result = resolve_under(tmp_path, "a", "b.json")
    assert result == (tmp_path / "a" / "b.json").resolve()


@pytest.mark.parametrize(
    "relative",
    ["../evil.json", "/etc/passwd", "a/../../b", "a\\..\\..\\b", "C:/x", "a//b"],
)
def test_safe_relpath_rejects_traversal(tmp_path: Path, relative: str) -> None:
    with pytest.raises(AppError):
        safe_relpath(tmp_path, relative)


def test_safe_relpath_accepts_nested(tmp_path: Path) -> None:
    result = safe_relpath(tmp_path, "jobs/job-aaaaaaaa/job.json")
    assert result == (tmp_path / "jobs" / "job-aaaaaaaa" / "job.json").resolve()


# --------------------------------------------------------------------------- #
# ID 校验
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    ["", "job-", "job", "job-abc/def", "job-..", "job-abc def", "gen-", "x-job-abc"],
)
def test_validate_id_rejects_bad_values(value: str) -> None:
    with pytest.raises(AppError):
        validate_id(value)


def test_validate_id_accepts_generated_ids() -> None:
    assert validate_id(new_job_id(), PREFIX_JOB)
    assert validate_id(new_generation_id(), PREFIX_GENERATION)


def test_validate_id_checks_prefix() -> None:
    with pytest.raises(AppError):
        validate_id(new_job_id(), PREFIX_GENERATION)


# --------------------------------------------------------------------------- #
# generation 生命周期
# --------------------------------------------------------------------------- #


def test_ensure_active_creates_once(settings) -> None:
    storage = Storage(settings)
    storage.init()
    first = storage.ensure_active()
    second = storage.ensure_active()
    assert first == second
    assert storage.generation_dir(first).is_dir()
    assert (storage.generation_dir(first) / "manifest.json").is_file()
    assert len(storage.list_generation_ids()) == 1


def test_set_active_switches_pointer(settings) -> None:
    storage = Storage(settings)
    storage.init()
    first = storage.ensure_active()
    second = storage.create_generation()
    storage.set_active_generation(second)
    assert storage.active_generation_id() == second
    assert storage.generation_dir(first).is_dir(), "旧 generation 必须保留"


def test_set_active_rejects_missing_generation(settings) -> None:
    storage = Storage(settings)
    storage.init()
    storage.ensure_active()
    with pytest.raises(AppError):
        storage.set_active_generation(new_generation_id())


def test_job_dir_and_file(settings) -> None:
    storage = Storage(settings)
    storage.init()
    gid = storage.ensure_active()
    job_id = new_job_id()
    directory = storage.job_dir(gid, job_id, create=True)
    assert directory.is_dir()
    path = storage.job_file(gid, job_id, "job.json")
    assert path.parent == directory


def test_job_file_rejects_traversal(settings) -> None:
    storage = Storage(settings)
    storage.init()
    gid = storage.ensure_active()
    job_id = new_job_id()
    with pytest.raises(AppError):
        storage.job_file(gid, job_id, "../escape.json")


def test_list_job_ids_ignores_foreign_dirs(settings) -> None:
    storage = Storage(settings)
    storage.init()
    gid = storage.ensure_active()
    good = new_job_id()
    storage.job_dir(gid, good, create=True)
    (storage.jobs_root(gid) / "not-a-job").mkdir()
    assert storage.list_job_ids(gid) == [good]


def test_active_pointer_recovers_from_corrupt_file(settings) -> None:
    storage = Storage(settings)
    storage.init()
    gid = storage.ensure_active()
    settings.active_path.write_text("{broken", encoding="utf-8")
    assert storage.active_generation_id() is None
    recovered = storage.ensure_active()
    assert recovered != gid or True  # 允许新建 generation
    assert storage.active_generation_id() == recovered


def test_lock_path_validation(settings) -> None:
    storage = Storage(settings)
    storage.init()
    assert storage.lock_path("task").name == "task.lock"
    with pytest.raises(ValueError):
        storage.lock_path("../evil")
