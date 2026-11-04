"""基于锁文件的互斥锁。

三把命名锁：task / backup / restore。
用 `O_CREAT|O_EXCL` 原子创建，写入 pid 与时间；陈旧锁（进程已不存在，
或超过 stale_after 秒）可被回收。
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.errors import LockError

LOCK_TASK = "task"
LOCK_BACKUP = "backup"
LOCK_RESTORE = "restore"

DEFAULT_STALE_AFTER = 3600.0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - 平台差异
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FileLock:
    """单文件互斥锁。同一进程内重复获取也会失败（不提供重入）。"""

    def __init__(
        self,
        path: Path | str,
        name: str = "lock",
        stale_after: float = DEFAULT_STALE_AFTER,
    ) -> None:
        self.path = Path(path)
        self.name = name
        self.stale_after = stale_after
        self._held = False

    # ---- 内部 -----------------------------------------------------------
    def _read_holder(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8")
            return json.loads(raw) if raw.strip() else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _is_stale(self, holder: dict) -> bool:
        pid = holder.get("pid")
        if not isinstance(pid, int):
            return True
        if not _pid_alive(pid):
            return True
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True
        return age > self.stale_after

    def _try_create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        payload = json.dumps(
            {"pid": os.getpid(), "name": self.name, "acquired_at": _now_iso()},
            ensure_ascii=False,
        )
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return True

    # ---- 对外 -----------------------------------------------------------
    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            if self._try_create():
                self._held = True
                return
            holder = self._read_holder()
            if self._is_stale(holder):
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise LockError(self.name, holder)
        raise LockError(self.name, self._read_holder())

    def release(self) -> None:
        if not self._held:
            return
        holder = self._read_holder()
        if holder.get("pid") in (os.getpid(), None):
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class LockManager:
    """按名字获取锁，路径统一放在 var/locks/。"""

    def __init__(self, storage) -> None:
        self.storage = storage

    def lock(self, name: str) -> FileLock:
        return FileLock(self.storage.lock_path(name), name=name)

    def acquire(self, name: str) -> FileLock:
        lock = self.lock(name)
        lock.acquire()
        return lock

    @contextmanager
    def hold(self, name: str) -> Iterator[FileLock]:
        lock = self.lock(name)
        lock.acquire()
        try:
            yield lock
        finally:
            lock.release()

    def is_held(self, name: str) -> bool:
        path = self.storage.lock_path(name)
        if not path.is_file():
            return False
        lock = FileLock(path, name=name)
        return not lock._is_stale(lock._read_holder())
