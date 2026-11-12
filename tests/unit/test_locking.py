"""阶段 C：文件锁语义测试。

覆盖：
- 同一把锁二次获取必须失败，并带可读的下一步提示（不静默等待）；
- 释放后可重新获取；
- 陈旧锁（进程已死 / 超龄）可被回收，不会把应用永久卡死；
- 不同锁名互不影响；
- 锁文件记录持有者信息，便于排查。
"""

from __future__ import annotations

import json
import os
import time

import pytest

from app.core.locking import LOCK_BACKUP, LOCK_TASK, FileLock, LockManager
from app.core.storage import Storage
from app.errors import ErrorCode, LockError


@pytest.fixture()
def manager(settings) -> LockManager:
    storage = Storage(settings)
    storage.init()
    return LockManager(storage)


def test_second_acquire_on_same_lock_fails_with_next_action(manager: LockManager) -> None:
    first = manager.acquire(LOCK_TASK)
    try:
        with pytest.raises(LockError) as excinfo:
            manager.acquire(LOCK_TASK)
        error = excinfo.value
        assert error.code == ErrorCode.LOCKED
        assert error.http_status == 409
        assert error.retryable is True
        # 用户必须知道下一步做什么，不能只看到一个错误码
        assert error.next_action
        assert "task" in error.message
        assert error.details["lock"] == LOCK_TASK
    finally:
        first.release()


def test_lock_released_can_be_acquired_again(manager: LockManager) -> None:
    lock = manager.acquire(LOCK_TASK)
    assert lock.held is True
    lock.release()
    assert lock.held is False
    assert manager.is_held(LOCK_TASK) is False

    again = manager.acquire(LOCK_TASK)
    assert again.held is True
    again.release()


def test_hold_context_manager_releases_even_on_exception(manager: LockManager) -> None:
    with pytest.raises(RuntimeError):
        with manager.hold(LOCK_TASK) as lock:
            assert lock.held is True
            raise RuntimeError("boom")
    assert manager.is_held(LOCK_TASK) is False

    # 异常路径退出后仍应可获取
    with manager.hold(LOCK_TASK) as lock:
        assert lock.held is True


def test_different_lock_names_do_not_conflict(manager: LockManager) -> None:
    task = manager.acquire(LOCK_TASK)
    backup = manager.acquire(LOCK_BACKUP)
    try:
        assert task.held and backup.held
    finally:
        backup.release()
        task.release()


def test_lock_file_records_pid_and_name(manager: LockManager, settings) -> None:
    lock = manager.acquire(LOCK_TASK)
    try:
        path = settings.locks_dir / f"{LOCK_TASK}.lock"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert payload["name"] == LOCK_TASK
        assert payload["acquired_at"].endswith("Z")
    finally:
        lock.release()
    assert not (settings.locks_dir / f"{LOCK_TASK}.lock").exists()


def test_lock_with_dead_pid_is_reclaimed(manager: LockManager, settings, monkeypatch) -> None:
    path = settings.locks_dir / f"{LOCK_TASK}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 424242, "name": LOCK_TASK}), encoding="utf-8")

    # 用桩函数稳定模拟"持有者进程已不存在"，避免依赖真实 PID 是否被复用
    monkeypatch.setattr("app.core.locking._pid_alive", lambda pid: False)

    lock = manager.acquire(LOCK_TASK)
    try:
        assert lock.held is True
        holder = json.loads(path.read_text(encoding="utf-8"))
        assert holder["pid"] == os.getpid()
    finally:
        lock.release()


def test_lock_with_live_pid_is_not_reclaimed(manager: LockManager, settings, monkeypatch) -> None:
    path = settings.locks_dir / f"{LOCK_TASK}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 424242, "name": LOCK_TASK}), encoding="utf-8")

    monkeypatch.setattr("app.core.locking._pid_alive", lambda pid: True)

    with pytest.raises(LockError) as excinfo:
        manager.acquire(LOCK_TASK)
    assert excinfo.value.details["holder"]["pid"] == 424242
    # 未被回收：原锁文件仍在
    assert path.is_file()


def test_stale_lock_by_age_is_reclaimed(settings) -> None:
    storage = Storage(settings)
    storage.init()
    path = storage.lock_path(LOCK_TASK)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "name": LOCK_TASK}), encoding="utf-8")

    # 进程还活着，但锁已存在两小时：超过 stale_after 即视为残留
    old = time.time() - 7200
    os.utime(path, (old, old))

    lock = FileLock(path, name=LOCK_TASK, stale_after=3600.0)
    lock.acquire()
    try:
        assert lock.held is True
    finally:
        lock.release()


def test_corrupt_lock_file_is_treated_as_stale(manager: LockManager, settings) -> None:
    path = settings.locks_dir / f"{LOCK_TASK}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")

    lock = manager.acquire(LOCK_TASK)
    try:
        assert lock.held is True
    finally:
        lock.release()


def test_release_without_holding_is_noop(manager: LockManager, settings) -> None:
    lock = manager.lock(LOCK_TASK)
    lock.release()  # 不抛异常
    assert lock.held is False
    assert not (settings.locks_dir / f"{LOCK_TASK}.lock").exists()


def test_is_held_reports_false_when_no_lock_file(manager: LockManager) -> None:
    assert manager.is_held(LOCK_TASK) is False
