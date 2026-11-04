"""阶段 C：原子写。

要求：
- 写入中断不损坏旧文件；
- 并发写入不混排；
- 写后校验失败要抛错，且旧数据保持完整；
- 独占创建绝不覆盖。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app.core import atomic
from app.core.atomic import (
    AtomicWriteError,
    append_jsonl,
    atomic_create,
    atomic_write_bytes,
    atomic_write_json,
    publish_file,
    read_json,
    read_jsonl,
    unique_path,
)


def test_atomic_write_json_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "a.json"
    atomic_write_json(target, {"名称": "报告", "n": 1})
    assert read_json(target) == {"名称": "报告", "n": 1}
    # UTF-8 不转义中文
    assert "报告" in target.read_text(encoding="utf-8")


def test_atomic_write_json_overwrites_cleanly(tmp_path: Path) -> None:
    target = tmp_path / "a.json"
    atomic_write_json(target, {"v": 1})
    atomic_write_json(target, {"v": 2})
    assert read_json(target) == {"v": 2}


def test_interrupted_replace_keeps_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.json"
    atomic_write_json(target, {"v": "old"})

    def boom(*args, **kwargs):
        raise OSError("模拟磁盘中断")

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(target, {"v": "new"})
    monkeypatch.undo()

    assert read_json(target) == {"v": "old"}
    # 不留下临时文件
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".a.json.")]
    assert leftovers == []


def test_interrupted_fsync_keeps_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.json"
    atomic_write_json(target, {"v": "old"})

    real_fsync = atomic.os.fsync

    def failing_fsync(fd):
        raise OSError("模拟 fsync 失败")

    monkeypatch.setattr(atomic.os, "fsync", failing_fsync)
    with pytest.raises(OSError):
        atomic_write_json(target, {"v": "new"})
    monkeypatch.setattr(atomic.os, "fsync", real_fsync)

    assert read_json(target) == {"v": "old"}
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".a.json.")]
    assert leftovers == []


def test_schema_failure_leaves_old_file_intact(tmp_path: Path) -> None:
    target = tmp_path / "a.json"
    atomic_write_json(target, {"v": "old"})

    def schema(payload):
        if payload.get("v") != "ok":
            raise ValueError("不允许的值")

    with pytest.raises(AtomicWriteError):
        atomic_write_json(target, {"v": "bad"}, schema=schema)

    assert read_json(target) == {"v": "old"}


def test_replace_retries_when_target_is_being_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标文件被别的句柄打开时，替换要重试而不是直接失败。

    Windows 上 os.replace 走 MoveFileEx(REPLACE_EXISTING)，目标被任何不带
    FILE_SHARE_DELETE 的句柄打开就返回 WinError 5。真实触发场景：前端轮询
    job.json（HTTP 线程正在读）时，worker 线程 mark_running 要写同一个文件。
    e2e 里就是这样失败在 mark_running 上的。
    """
    target = tmp_path / "job.json"
    atomic_write_json(target, {"v": "old"})

    real_replace = atomic.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 3:  # 前三次撞上「有人在读」
            raise PermissionError(13, "拒绝访问。")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic.os, "replace", flaky_replace)
    atomic_write_json(target, {"v": "new"})
    monkeypatch.undo()

    assert calls["n"] == 4, "没有重试：撞到一次读句柄就放弃了"
    assert read_json(target) == {"v": "new"}
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".job.json.")]
    assert leftovers == []


def test_replace_gives_up_after_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一直拒绝访问时必须如实失败：不能无限重试，也不能假装写成功。"""
    target = tmp_path / "job.json"
    atomic_write_json(target, {"v": "old"})

    monkeypatch.setattr(atomic, "_REPLACE_INITIAL_DELAY", 0.001)
    monkeypatch.setattr(atomic, "_REPLACE_MAX_DELAY", 0.002)

    calls = {"n": 0}

    def always_denied(src, dst):
        calls["n"] += 1
        raise PermissionError(13, "拒绝访问。")

    monkeypatch.setattr(atomic.os, "replace", always_denied)
    with pytest.raises(PermissionError):
        atomic_write_json(target, {"v": "new"})
    monkeypatch.undo()

    assert calls["n"] == atomic._REPLACE_ATTEMPTS, "重试次数没有上限"
    assert read_json(target) == {"v": "old"}, "失败时旧内容被破坏了"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".job.json.")]
    assert leftovers == [], "失败后残留临时文件"


def test_post_write_validation_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.json"

    real_read_bytes = Path.read_bytes

    def tampered(self):
        raw = real_read_bytes(self)
        if self == target:
            return raw.replace(b"1", b"9")
        return raw

    monkeypatch.setattr(Path, "read_bytes", tampered)
    with pytest.raises(AtomicWriteError):
        atomic_write_json(target, {"v": 1})


def test_concurrent_writes_to_different_files_do_not_interleave(tmp_path: Path) -> None:
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            target = tmp_path / f"f{index}.json"
            for iteration in range(25):
                atomic_write_json(
                    target, {"index": index, "iteration": iteration, "pad": "x" * 500}
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    for index in range(8):
        payload = read_json(tmp_path / f"f{index}.json")
        assert payload["index"] == index
        assert payload["iteration"] == 24


def test_concurrent_writes_to_same_file_stay_consistent(tmp_path: Path) -> None:
    target = tmp_path / "same.json"
    payloads = {i: {"writer": i, "pad": str(i) * 800} for i in range(8)}

    def worker(index: int) -> None:
        for _ in range(20):
            atomic_write_json(target, payloads[index])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final = read_json(target)
    assert final in list(payloads.values()), "文件内容被混排"


def test_atomic_create_refuses_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "out.pdf"
    atomic_create(target, b"first")
    with pytest.raises(FileExistsError):
        atomic_create(target, b"second")
    assert target.read_bytes() == b"first"


def test_publish_file_refuses_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    dst = tmp_path / "dst.bin"
    publish_file(src, dst)
    assert dst.read_bytes() == b"payload"
    assert not src.exists()

    src2 = tmp_path / "src2.bin"
    src2.write_bytes(b"other")
    with pytest.raises(FileExistsError):
        publish_file(src2, dst)
    assert dst.read_bytes() == b"payload"


def test_atomic_write_bytes_validates_before_replace(tmp_path: Path) -> None:
    target = tmp_path / "b.bin"
    target.write_bytes(b"old")

    def validate(raw: bytes) -> None:
        if not raw.startswith(b"%PDF-"):
            raise ValueError("不是 PDF")

    with pytest.raises(ValueError):
        atomic_write_bytes(target, b"not pdf", validate=validate)
    assert target.read_bytes() == b"old"


def test_append_jsonl_and_tolerate_half_line(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    append_jsonl(target, {"seq": 1})
    append_jsonl(target, {"seq": 2})
    with open(target, "a", encoding="utf-8") as handle:
        handle.write('{"seq": 3')  # 模拟进程被杀留下半行

    records = list(read_jsonl(target))
    assert [r["seq"] for r in records] == [1, 2]


def test_read_json_default_when_missing(tmp_path: Path) -> None:
    assert read_json(tmp_path / "nope.json", default={"a": 1}) == {"a": 1}


def test_unique_path_appends_counter(tmp_path: Path) -> None:
    first = unique_path(tmp_path, "报告.pdf")
    assert first.name == "报告.pdf"
    first.write_bytes(b"x")
    second = unique_path(tmp_path, "报告.pdf")
    assert second.name == "报告 (2).pdf"
    second.write_bytes(b"x")
    third = unique_path(tmp_path, "报告.pdf")
    assert third.name == "报告 (3).pdf"


def test_written_json_is_utf8_decodable(tmp_path: Path) -> None:
    target = tmp_path / "u.json"
    atomic_write_json(target, {"k": "中文内容"})
    raw = target.read_bytes()
    assert json.loads(raw.decode("utf-8"))["k"] == "中文内容"
