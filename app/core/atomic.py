"""原子写与文件发布。

全项目**唯一**的 JSON/文件落盘入口，其他模块不得另写一套。
写流程：同目录唯一临时文件 → write → flush → fsync → 校验 → 锁内 os.replace → 重读确认。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "AtomicWriteError",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_create",
    "append_jsonl",
    "read_json",
    "read_jsonl",
    "publish_file",
    "replace_with_retry",
    "unique_path",
    "fsync_dir",
]


class AtomicWriteError(RuntimeError):
    """原子写或写后校验失败。"""


# --------------------------------------------------------------------------- #
# 每个目标路径一把进程内锁，防止同进程并发写混排
# --------------------------------------------------------------------------- #

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(Path(path).absolute()))
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def fsync_dir(directory: Path) -> None:
    """在支持目录 fsync 的平台上同步目录项。Windows 上静默跳过。"""
    if os.name == "nt":  # pragma: no cover - 平台差异
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_tmp(target: Path, data: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


# 重试参数：读方通常只持有句柄几毫秒，10ms 起退避足够跨过；
# 上限 15 次、单次最长 100ms，最坏总耗时约 1.2s，不至于让写方长期卡住。
_REPLACE_ATTEMPTS = 15
_REPLACE_INITIAL_DELAY = 0.01
_REPLACE_MAX_DELAY = 0.1


def replace_with_retry(tmp: Path, target: Path) -> None:
    """`os.replace`，遇到「目标正被别人打开」时短暂退避重试。

    Windows 上 `os.replace` 走 `MoveFileEx(REPLACE_EXISTING)`：只要此刻还有别的
    句柄打开着目标文件，系统就返回 `ERROR_ACCESS_DENIED`（`PermissionError`，
    WinError 5）。Python 的 `open()` 打开文件时不带 `FILE_SHARE_DELETE`，所以
    「有人在读」就等于「不许替换」。

    本工具天生是双线程：后台 worker 写 `job.json`，HTTP 线程同时读 `job.json`
    响应前端轮询。刷新页面时前端读、worker 写，这个窗口真实存在——e2e 里表现为
    任务直接失败在 `mark_running`，用户看到的是「处理失败」，跟并发毫无关系。

    读方只持有句柄几毫秒，退避重试即可跨过；重试用尽仍失败就如实抛出，
    不假装写成功。

    这个函数不只服务 `atomic_write_*`：PDF 候选文件的替换（`app/pdf/pipeline.py`）
    和成品发布（`app/services/jobs.py`）走的是同一条 Windows 语义——刚落盘的文件
    往往正被解析器、杀毒软件或资源管理器索引器短暂持有，直接 `os.replace`
    就会整项失败。所以它是公开的，全项目替换文件的唯一入口。
    """
    delay = _REPLACE_INITIAL_DELAY
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS:
                raise
            time.sleep(delay)
            delay = min(delay * 2, _REPLACE_MAX_DELAY)


def atomic_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    validate: Callable[[bytes], None] | None = None,
    post_validate: Callable[[bytes], None] | None = None,
) -> Path:
    """原子写入字节内容。

    validate 在替换前对**待写入内容**校验，校验失败时旧文件完全不受影响。
    post_validate 在替换后对**磁盘上重新读出的内容**校验，用于发现替换异常。
    """
    target = Path(path)
    with _path_lock(target):
        tmp = _write_tmp(target, data)
        try:
            if validate is not None:
                validate(data)
            replace_with_retry(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        fsync_dir(target.parent)
        reread = target.read_bytes()
        if reread != data:
            raise AtomicWriteError(f"写入后重读内容不一致：{target}")
        if post_validate is not None:
            try:
                post_validate(reread)
            except Exception as exc:  # noqa: BLE001
                raise AtomicWriteError(f"写入后校验失败：{target}: {exc}") from exc
    return target


def atomic_write_json(
    path: Path | str,
    payload: Any,
    *,
    schema: Callable[[Any], None] | None = None,
    indent: int = 2,
) -> Path:
    """原子写入 JSON（UTF-8，不转义中文）。"""
    data = json.dumps(payload, ensure_ascii=False, indent=indent).encode("utf-8")

    def _validate_bytes(raw: bytes) -> None:
        if schema is not None:
            try:
                schema(json.loads(raw.decode("utf-8")))
            except Exception as exc:  # noqa: BLE001
                raise AtomicWriteError(f"JSON Schema 校验失败：{path}: {exc}") from exc

    return atomic_write_bytes(path, data, validate=_validate_bytes)


def atomic_create(path: Path | str, data: bytes) -> Path:
    """独占创建：目标已存在时抛 FileExistsError，绝不覆盖。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _path_lock(target):
        with open(target, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    return target


def publish_file(source: Path | str, target: Path | str) -> Path:
    """把临时文件发布为目标文件，目标已存在时抛 FileExistsError。"""
    src = Path(source)
    dst = Path(target)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with _path_lock(dst):
        if dst.exists():
            raise FileExistsError(str(dst))
        try:
            os.link(src, dst)
        except OSError:
            with open(src, "rb") as fsrc, open(dst, "xb") as fdst:
                while True:
                    chunk = fsrc.read(1024 * 1024)
                    if not chunk:
                        break
                    fdst.write(chunk)
                fdst.flush()
                os.fsync(fdst.fileno())
        fsync_dir(dst.parent)
    src.unlink(missing_ok=True)
    return dst


def unique_path(directory: Path | str, name: str, max_tries: int = 200) -> Path:
    """在目录内找一个不冲突的路径，不实际创建文件。

    重名时追加 ` (2)`、` (3)`…，保留扩展名。
    """
    base_dir = Path(directory)
    candidate = base_dir / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    for index in range(2, max_tries + 2):
        candidate = base_dir / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise AtomicWriteError(f"无法在 {base_dir} 中为 {name} 找到可用文件名")


_UNSET = object()


def read_json(path: Path | str, default: Any = _UNSET) -> Any:
    """读取 JSON。

    - 文件不存在：给了 default 就返回 default，否则抛 FileNotFoundError；
    - 内容损坏：给了 default 就返回 default（让调用方走"重建/降级"路径），
      否则抛 JSONDecodeError。

    显式传 default 即表示"这份文件损坏可容忍"，这样单个坏文件不会拖垮整张列表。
    """
    target = Path(path)
    if not target.is_file():
        if default is _UNSET:
            raise FileNotFoundError(target)
        return default
    try:
        with open(target, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        if default is _UNSET:
            raise
        return default


def append_jsonl(path: Path | str, record: dict[str, Any]) -> None:
    """追加一行 JSONL 并 fsync（事件日志用）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _path_lock(target):
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def read_jsonl(path: Path | str) -> Iterable[dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return
    with open(target, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # 半行（进程被杀）直接忽略，不让日志破坏业务
                continue
