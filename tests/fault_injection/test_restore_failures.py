"""阶段 J 故障注入：坏包与恢复各步失败。

门槛只有一句话：**任何失败路径，当前数据零变化**。

「零变化」不是形容词，是四样可比的东西：`active.json` 的字节、内存里的
`ctx.generation_id`、每个事实文件的 sha256、generation 目录集合。恢复失败后
如果多出一个半成品 generation 目录，第四项就会不等——这正是「解压到相邻新
generation」这套设计的意义：指针没切，垃圾也清干净。

坏包分七类，每类对应一个真实的坏法：不是 ZIP、CRC 错（位翻转/截断）、
哈希错（CRC 对但内容被换）、路径穿越、缺文件、错误版本、zip bomb。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.core import archive
from app.errors import AppError, ErrorCode
from tests.integration.test_backups import (
    backup_list,
    backup_path,
    complete_job,
    create_backup,
    history_ids,
    inspect_bytes,
    restore,
    restore_ok,
    state,
    token_for,
)
from tests.integration.test_upload import assert_error, sha256_bytes

MANIFEST = archive.MANIFEST_NAME
MIB = 1024 * 1024


# --------------------------------------------------------------------------- #
# 造包工具
# --------------------------------------------------------------------------- #


def items_of(path: Path) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as handle:
        return [(info.filename, handle.read(info.filename)) for info in handle.infolist()]


def manifest_of(items: list[tuple[str, bytes]]) -> dict:
    return json.loads(dict(items)[MANIFEST].decode("utf-8"))


def pack(items: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for name, data in items:
            handle.writestr(name, data)
    return buffer.getvalue()


def with_manifest(items: list[tuple[str, bytes]], manifest: dict) -> list[tuple[str, bytes]]:
    payload = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    return [(name, payload if name == MANIFEST else data) for name, data in items]


def first_pdf(items: list[tuple[str, bytes]]) -> str:
    for name, _ in items:
        if name.endswith("/original.pdf"):
            return name
    raise AssertionError("基础备份里没有 original.pdf")


def corrupt_entry_payload(data: bytes, entry: str) -> bytes:
    """把某个条目的**压缩数据**翻一个字节。

    改本地文件头比重新打包更接近真实坏法（磁盘位翻转、传输截断）。
    压缩流被破坏时连解压都做不到，报的是「已损坏」。
    """
    position = data.index(entry.encode("utf-8"))
    header = position - 30
    assert data[header : header + 4] == b"PK\x03\x04", "本地文件头定位失败"
    name_len = int.from_bytes(data[header + 26 : header + 28], "little")
    extra_len = int.from_bytes(data[header + 28 : header + 30], "little")
    start = header + 30 + name_len + extra_len
    buffer = bytearray(data)
    buffer[start] ^= 0xFF
    return bytes(buffer)


def break_entry_crc(data: bytes, entry: str) -> bytes:
    """把条目的 CRC 字段改错（本地头 + 中央目录各一处）。

    这是「内容能解出来、但和登记的 CRC 对不上」那一类坏法，
    也是传输被截断/拼接之后最常见的样子。
    """
    buffer = bytearray(data)
    marker = entry.encode("utf-8")

    local_name = buffer.find(marker)
    local = local_name - 30
    assert bytes(buffer[local : local + 4]) == b"PK\x03\x04", "本地文件头定位失败"
    name_len = int.from_bytes(buffer[local + 26 : local + 28], "little")
    extra_len = int.from_bytes(buffer[local + 28 : local + 30], "little")
    buffer[local + 14 : local + 18] = b"\xde\xad\xbe\xef"

    central_name = buffer.find(marker, local + 30 + name_len + extra_len)
    central = central_name - 46
    assert bytes(buffer[central : central + 4]) == b"PK\x01\x02", "中央目录定位失败"
    buffer[central + 16 : central + 20] = b"\xde\xad\xbe\xef"
    return bytes(buffer)


# ---- 七类坏包 -------------------------------------------------------------- #


def build_not_zip(items):
    return b"MZ\x90\x00 this is an executable, not a backup"


def build_payload_corrupt(items):
    return corrupt_entry_payload(pack(items), first_pdf(items))


def build_crc_mismatch(items):
    return break_entry_crc(pack(items), first_pdf(items))


def build_hash_mismatch(items):
    """CRC 由 zipfile 重算，所以 testzip 会通过；只有 sha256 能抓出来。"""
    target = first_pdf(items)
    broken = [
        (name, bytes([data[0] ^ 0xFF]) + data[1:]) if name == target else (name, data)
        for name, data in items
    ]
    return pack(broken)


def build_path_traversal(items):
    return pack(items + [("../evil.txt", b"boom")])


def build_absolute_path(items):
    return pack(items + [("/etc/passwd", b"boom")])


def build_drive_letter(items):
    return pack(items + [("C:/Windows/evil.dll", b"boom")])


def build_missing_file(items):
    target = first_pdf(items)
    return pack([(name, data) for name, data in items if name != target])


def build_wrong_version(items):
    manifest = manifest_of(items)
    manifest["format_version"] = 99
    return pack(with_manifest(items, manifest))


def build_zip_bomb(items):
    payload = b"\0" * (8 * MIB)
    manifest = {
        "format_version": 1,
        "schema_version": 1,
        "app_version": "0.1.0",
        "created_at": "2026-01-01T00:00:00.000Z",
        "generation_id": "gen-20260101T000000000Z-abcdef",
        "reason": "manual",
        "job_count": 0,
        "entry_count": 1,
        "total_bytes": len(payload),
        "entries": [
            {
                "rel_path": archive.SETTINGS_NAME,
                "size": len(payload),
                "sha256": sha256_bytes(payload),
            }
        ],
    }
    return pack(
        [
            (MANIFEST, json.dumps(manifest, ensure_ascii=False).encode("utf-8")),
            (archive.SETTINGS_NAME, payload),
        ]
    )


def build_count_mismatch(items):
    """清单里的统计被人改过：摘要里的「3 个任务」不能是没校验过的数字。"""
    manifest = manifest_of(items)
    manifest["job_count"] = 99
    return pack(with_manifest(items, manifest))


def build_extra_file(items):
    return pack(items + [("jobs/job-0000000000000000/job.json", b"{}")])


BAD_PACKAGES = {
    "not_zip": build_not_zip,
    "payload_corrupt": build_payload_corrupt,
    "crc_mismatch": build_crc_mismatch,
    "hash_mismatch": build_hash_mismatch,
    "path_traversal": build_path_traversal,
    "absolute_path": build_absolute_path,
    "drive_letter": build_drive_letter,
    "missing_file": build_missing_file,
    "wrong_version": build_wrong_version,
    "zip_bomb": build_zip_bomb,
    "count_mismatch": build_count_mismatch,
    "extra_file": build_extra_file,
}


# --------------------------------------------------------------------------- #
# 七类坏包：一律拒收，数据零变化
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", sorted(BAD_PACKAGES))
def test_bad_package_is_rejected_and_data_is_untouched(client, fixture_pdfs, case):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    payload = BAD_PACKAGES[case](items_of(backup_path(client, backup["id"])))
    ctx = client.app.state.ctx

    before = state(client)
    response = inspect_bytes(client, payload)

    assert_error(response, "invalid_backup", status=400)
    assert response.json()["next_action"], "拒收必须告诉用户下一步做什么"
    assert state(client) == before
    assert ctx.settings.active_path.read_bytes() == before["pointer"]
    # 被拒的包连半张令牌都不能留下
    assert ctx.backups._tokens == {}

    # 没有令牌，手搓一个也恢复不了
    forged = restore(client, "forged-token")
    assert_error(forged, "restore_token_invalid", status=400)
    assert state(client) == before


def test_bad_package_reason_matches_the_actual_defect(client, fixture_pdfs):
    """错误文案要说清是哪一类坏法——用户得知道该重做备份还是换个包。"""
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    base = items_of(backup_path(client, backup["id"]))

    messages = {
        case: inspect_bytes(client, builder(base)).json()["message"]
        for case, builder in BAD_PACKAGES.items()
    }

    assert "ZIP" in messages["not_zip"]
    assert "损坏" in messages["payload_corrupt"]
    assert "CRC" in messages["crc_mismatch"]
    assert "哈希" in messages["hash_mismatch"]
    assert "穿越" in messages["path_traversal"]
    assert "绝对路径" in messages["absolute_path"]
    assert "非法路径片段" in messages["drive_letter"]
    assert "缺少" in messages["missing_file"]
    assert "版本" in messages["wrong_version"]
    assert "zip bomb" in messages["zip_bomb"]
    assert "统计" in messages["count_mismatch"]
    assert "清单之外" in messages["extra_file"]


def test_zip_bomb_details_report_the_ratio(client, fixture_pdfs):
    complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    base = items_of(backup_path(client, backup["id"]))

    body = inspect_bytes(client, build_zip_bomb(base)).json()
    assert body["details"]["ratio"] > 200


# --------------------------------------------------------------------------- #
# 四个注入点：失败之后当前数据一个字节都没动，且还能重试成功
# --------------------------------------------------------------------------- #


def prepare(client, fixture_pdfs) -> tuple[dict, dict, str]:
    """跑一个真实任务、做一次备份、拿到令牌。"""
    done = complete_job(client, fixture_pdfs, "a.pdf")
    backup = create_backup(client)
    return done, backup, token_for(client, backup_path(client, backup["id"]))


def test_auto_backup_failure_leaves_data_untouched(client, fixture_pdfs, monkeypatch):
    """第一步失败：连自动备份都做不出来，就到此为止。"""
    done, backup, token = prepare(client, fixture_pdfs)
    ctx = client.app.state.ctx
    before = state(client)
    backups_before = backup_list(client)["count"]

    def boom(*args, **kwargs):
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "磁盘写不进去了",
            next_action="请检查磁盘空间",
            http_status=500,
        )

    monkeypatch.setattr(ctx.backups, "create", boom)
    response = restore(client, token)

    assert_error(response, "restore_failed", status=500)
    assert "自动备份" in response.json()["message"]
    assert state(client) == before
    assert backup_list(client)["count"] == backups_before

    # 注入解除后，同一个令牌还能用（令牌只在成功时消费）
    monkeypatch.undo()
    restore_ok(client, token)
    assert history_ids(client) == [done["id"]]


def test_extract_failure_discards_the_new_generation(client, fixture_pdfs, monkeypatch):
    """第二步失败：解压到一半，新 generation 删掉，指针还指着老数据。"""
    done, backup, token = prepare(client, fixture_pdfs)
    before = state(client)
    backups_before = backup_list(client)["count"]

    def boom(*args, **kwargs):
        raise OSError("磁盘坏了")

    monkeypatch.setattr(archive, "extract_archive", boom)
    response = restore(client, token)

    assert_error(response, "restore_failed", status=500)
    assert "当前数据未被改动" in response.json()["message"]
    assert state(client) == before
    # 自动备份已经做成了：失败也留了一层保险
    assert backup_list(client)["count"] == backups_before + 1

    monkeypatch.undo()
    restore_ok(client, token)
    assert history_ids(client) == [done["id"]]


def test_verify_failure_discards_the_new_generation(client, fixture_pdfs, monkeypatch):
    """第二步后半段失败：解压完了但校验不过，同样不许切指针。"""
    done, backup, token = prepare(client, fixture_pdfs)
    ctx = client.app.state.ctx
    before = state(client)
    backups_before = backup_list(client)["count"]

    def boom(generation_id, manifest):
        raise AppError(
            ErrorCode.RESTORE_FAILED,
            "恢复后的任务数与清单不符",
            details={"expected": 9, "actual": 1},
            next_action="当前数据未被改动，可以继续使用或重试",
            http_status=500,
        )

    monkeypatch.setattr(ctx.backups, "_verify_restored", boom)
    response = restore(client, token)

    assert_error(response, "restore_failed", status=500)
    assert response.json()["details"]["expected"] == 9
    assert state(client) == before
    assert backup_list(client)["count"] == backups_before + 1

    monkeypatch.undo()
    restore_ok(client, token)
    assert history_ids(client) == [done["id"]]


def test_pointer_switch_failure_keeps_the_old_pointer(client, fixture_pdfs, monkeypatch):
    """第三步失败：切换 active.json 出错，老数据还在原地，新目录也不留。"""
    done, backup, token = prepare(client, fixture_pdfs)
    ctx = client.app.state.ctx
    before = state(client)
    backups_before = backup_list(client)["count"]

    def boom(generation_id):
        raise OSError("指针写不进去")

    monkeypatch.setattr(ctx.storage, "set_active_generation", boom)
    response = restore(client, token)

    assert_error(response, "restore_failed", status=500)
    assert "切换" in response.json()["message"]
    assert state(client) == before
    assert backup_list(client)["count"] == backups_before + 1

    monkeypatch.undo()
    restore_ok(client, token)
    assert history_ids(client) == [done["id"]]


def test_failed_restore_never_leaves_a_half_generation(client, fixture_pdfs, monkeypatch):
    """所有注入点共用的一条：失败后 generation 目录集合必须和失败前一模一样。"""
    done, backup, token = prepare(client, fixture_pdfs)
    ctx = client.app.state.ctx
    generations_before = sorted(ctx.storage.list_generation_ids())

    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(archive, "extract_archive", boom)
    for _ in range(3):
        assert restore(client, token).status_code == 500
        assert sorted(ctx.storage.list_generation_ids()) == generations_before


# --------------------------------------------------------------------------- #
# 失败之后应用仍然完全可用
# --------------------------------------------------------------------------- #


def test_app_still_works_after_a_rejected_package(client, fixture_pdfs):
    done = complete_job(client, fixture_pdfs, "a.pdf")
    create_backup(client)

    assert_error(inspect_bytes(client, b"not a backup at all"), "invalid_backup", status=400)

    # 老数据照样能读、能下、能再备份，也能处理新任务
    assert history_ids(client) == [done["id"]]
    assert client.get(f"/api/jobs/{done['id']}/file?kind=output").status_code == 200
    assert backup_list(client)["count"] == 1

    fresh = complete_job(client, fixture_pdfs, "b.pdf")
    assert set(history_ids(client)) == {done["id"], fresh["id"]}
    for job in (done, fresh):
        assert client.get(f"/api/jobs/{job['id']}/file?kind=output").status_code == 200
    assert create_backup(client)["job_count"] == 2
