"""备份包（ZIP）的写入、校验与解压。

包内布局（`manifest.json` 是**包自己的元数据**，不属于数据，恢复时不落地）：

    manifest.json                      清单：版本 + 每条 rel_path/size/sha256
    settings.json
    jobs/<job>/job.json
    jobs/<job>/original.pdf
    jobs/<job>/<输出名>.pdf

校验顺序是刻意的，每一步都在排除一类真实的坏包：

1. 文件大小与 ZIP 魔数——连 ZIP 都不是，后面的检查都是浪费；
2. `testzip()`——让 zlib 逐条验 CRC，抓位翻转/截断；
3. 条目数与解压总量、单文件上限、压缩比——抓 zip bomb；
4. 条目名合法性——抓路径穿越（`../`、绝对路径、盘符、反斜杠）；
5. manifest 的 schema 与版本——抓「别的版本/别的软件」的包；
6. 逐条比 size 与 sha256——抓「CRC 对但内容被换掉」的包。

任何一步失败都抛 `AppError`，绝不"尽量恢复一点"：半套数据比没有数据更危险。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import zipfile
import zlib
from pathlib import Path
from typing import Any, Iterable

from app.errors import AppError, ErrorCode
from app.core.storage import safe_relpath

MANIFEST_NAME = "manifest.json"
SETTINGS_NAME = "settings.json"
JOBS_DIR = "jobs"

ZIP_MAGIC = b"PK\x03\x04"
EMPTY_ZIP_MAGIC = b"PK\x05\x06"

CHUNK = 1024 * 1024

NEXT_ACTION_BACKUP = "请选择本工具导出的 .zip 备份文件"
NEXT_ACTION_RETRY = "请重新选择备份文件，或先在当前数据上创建一次新备份"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def sha256_file(path: Path, chunk: int = CHUNK) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _invalid(message: str, *, details: dict[str, Any] | None = None, next_action: str = NEXT_ACTION_BACKUP) -> AppError:
    return AppError(
        ErrorCode.INVALID_BACKUP,
        message,
        details=details or {},
        retryable=False,
        next_action=next_action,
        http_status=400,
    )


def check_rel_path(rel_path: str) -> str:
    """校验包内相对路径，返回规范化形式。

    与 `storage.safe_relpath` 同一套规则，但先在这里把坏包挡在解压之前：
    解压函数里还会再过一次 `safe_relpath`，两层都不省。
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise _invalid("备份包里有空路径")
    normalised = rel_path.replace("\\", "/")
    if normalised.startswith("/"):
        raise _invalid("备份包里有绝对路径", details={"rel_path": rel_path})
    parts = normalised.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise _invalid(
                "备份包里有越界路径（路径穿越）", details={"rel_path": rel_path}
            )
        if ":" in part or part.startswith("~"):
            raise _invalid("备份包里有非法路径片段", details={"rel_path": rel_path})
    return "/".join(parts)


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #


def build_manifest(
    *,
    generation_id: str,
    entries: Iterable[dict[str, Any]],
    app_version: str,
    format_version: int,
    schema_version: int,
    created_at: str,
    reason: str = "manual",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = sorted(entries, key=lambda item: item["rel_path"])
    job_ids = {
        item["rel_path"].split("/")[1]
        for item in items
        if item["rel_path"].startswith(f"{JOBS_DIR}/") and item["rel_path"].endswith("/job.json")
    }
    manifest: dict[str, Any] = {
        "format_version": format_version,
        "schema_version": schema_version,
        "app_version": app_version,
        "created_at": created_at,
        "generation_id": generation_id,
        "reason": reason,
        "job_count": len(job_ids),
        "entry_count": len(items),
        "total_bytes": sum(int(item["size"]) for item in items),
        "entries": items,
    }
    if extra:
        manifest.update(extra)
    return manifest


def write_archive(dest: Path, manifest: dict[str, Any], files: list[tuple[str, Path]]) -> Path:
    """原子写出 ZIP：先写临时文件，成功后 `os.replace` 顶上。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr(MANIFEST_NAME, payload)
            for rel_path, source in sorted(files, key=lambda item: item[0]):
                archive.write(source, arcname=rel_path)
        # fsync 要在可写句柄上做：Windows 对只读句柄的 _commit 会失败。
        with open(tmp, "r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def read_manifest(path: Path) -> dict[str, Any]:
    """只读清单，不做完整校验（列表页用）。坏包抛 `INVALID_BACKUP`。"""
    try:
        with zipfile.ZipFile(path) as archive:
            if MANIFEST_NAME not in archive.namelist():
                raise _invalid("备份包缺少 manifest.json")
            raw = archive.read(MANIFEST_NAME)
    except (zipfile.BadZipFile, zlib.error) as error:
        raise _invalid("备份包已损坏（无法解析）", details={"error": str(error)}) from error
    except OSError as error:
        raise _invalid("备份包读不出来", details={"error": str(error)}) from error

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _invalid("备份清单不是合法 JSON", details={"error": str(error)}) from error
    if not isinstance(manifest, dict):
        raise _invalid("备份清单结构不正确")
    return manifest


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def _check_zip_magic(path: Path) -> None:
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError as error:
        raise _invalid("备份包读不出来", details={"error": str(error)}) from error
    if head not in (ZIP_MAGIC, EMPTY_ZIP_MAGIC):
        raise _invalid(
            "这不是 ZIP 备份包",
            details={"magic": head.hex()},
            next_action=NEXT_ACTION_BACKUP,
        )


def _check_entry_limits(archive: zipfile.ZipFile, limits) -> tuple[int, int]:
    infos = archive.infolist()
    if len(infos) > limits.max_zip_entries:
        raise _invalid(
            "备份包条目过多",
            details={"entries": len(infos), "limit": limits.max_zip_entries},
        )

    total = 0
    for info in infos:
        if info.file_size > limits.max_zip_entry_bytes:
            raise _invalid(
                "备份包里单个文件过大",
                details={
                    "name": info.filename,
                    "size": info.file_size,
                    "limit": limits.max_zip_entry_bytes,
                },
            )
        total += info.file_size
        if total > limits.max_zip_total_bytes:
            raise _invalid(
                "备份包解压后总量过大",
                details={"total": total, "limit": limits.max_zip_total_bytes},
            )
        if info.file_size > 0 and info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > limits.max_zip_ratio:
                raise _invalid(
                    "备份包压缩比异常（疑似 zip bomb）",
                    details={
                        "name": info.filename,
                        "ratio": round(ratio, 1),
                        "limit": limits.max_zip_ratio,
                    },
                )
    return len(infos), total


def _check_manifest_shape(manifest: dict[str, Any], expected_format_version: int) -> list[dict[str, Any]]:
    version = manifest.get("format_version")
    if version != expected_format_version:
        raise _invalid(
            "备份包格式版本不受支持",
            details={"format_version": version, "expected": expected_format_version},
            next_action="请用同一版本的工具导出/恢复备份",
        )

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise _invalid("备份清单缺少 entries")
    if not entries:
        raise _invalid("备份包是空的（清单里没有任何文件）")

    seen: set[str] = set()
    checked: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            raise _invalid("备份清单里的条目结构不正确")
        rel_path = check_rel_path(item.get("rel_path"))
        if rel_path == MANIFEST_NAME:
            raise _invalid("清单不能把自己列为数据文件")
        if rel_path in seen:
            raise _invalid("备份清单里有重复条目", details={"rel_path": rel_path})
        seen.add(rel_path)

        size = item.get("size")
        digest = item.get("sha256")
        if not isinstance(size, int) or size < 0:
            raise _invalid("备份清单里的文件大小不合法", details={"rel_path": rel_path})
        if not isinstance(digest, str) or len(digest) != 64:
            raise _invalid("备份清单里的哈希不合法", details={"rel_path": rel_path})
        checked.append({"rel_path": rel_path, "size": size, "sha256": digest})

    # 这三个统计值全都能由 entries 推出来。对不上就说明清单被改过——
    # 而 `job_count` 是要显示给用户看的（「这个包里有 3 个任务」），
    # 不能是一个谁都没校验过的数字。
    derived_jobs = {
        item["rel_path"].split("/")[1]
        for item in checked
        if item["rel_path"].startswith(f"{JOBS_DIR}/")
        and item["rel_path"].endswith("/job.json")
    }
    derived = {
        "job_count": len(derived_jobs),
        "entry_count": len(checked),
        "total_bytes": sum(item["size"] for item in checked),
    }
    for field_name, expected in derived.items():
        declared = manifest.get(field_name)
        if declared is not None and declared != expected:
            raise _invalid(
                "备份清单里的统计与条目不一致",
                details={
                    "field": field_name,
                    "declared": declared,
                    "derived": expected,
                },
            )
    return checked


def inspect_archive(
    path: Path,
    *,
    limits,
    expected_format_version: int,
    max_archive_bytes: int | None = None,
) -> dict[str, Any]:
    """全量校验一个备份包。返回 `{"manifest", "summary"}`，失败抛 `AppError`。"""
    path = Path(path)
    if not path.is_file():
        raise _invalid("备份文件不存在")
    size = path.stat().st_size
    if size <= 0:
        raise _invalid("备份文件是空的")
    if max_archive_bytes is not None and size > max_archive_bytes:
        raise _invalid(
            "备份文件过大",
            details={"size": size, "limit": max_archive_bytes},
        )

    _check_zip_magic(path)

    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise _invalid(
                    "备份包已损坏（CRC 校验失败）",
                    details={"entry": bad},
                    next_action=NEXT_ACTION_RETRY,
                )

            entry_count, total_bytes = _check_entry_limits(archive, limits)

            names = [info.filename for info in archive.infolist()]
            for name in names:
                check_rel_path(name)

            if MANIFEST_NAME not in names:
                raise _invalid("备份包缺少 manifest.json")

            try:
                manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise _invalid("备份清单不是合法 JSON", details={"error": str(error)}) from error
            if not isinstance(manifest, dict):
                raise _invalid("备份清单结构不正确")

            entries = _check_manifest_shape(manifest, expected_format_version)

            listed = {item["rel_path"] for item in entries}
            actual = set(names) - {MANIFEST_NAME}
            missing = sorted(listed - actual)
            if missing:
                raise _invalid(
                    "备份包里缺少清单列出的文件",
                    details={"missing": missing[:10], "count": len(missing)},
                )
            extra = sorted(actual - listed)
            if extra:
                raise _invalid(
                    "备份包里有清单之外的文件",
                    details={"extra": extra[:10], "count": len(extra)},
                )

            for item in entries:
                info = archive.getinfo(item["rel_path"])
                if info.file_size != item["size"]:
                    raise _invalid(
                        "备份包里的文件大小与清单不符",
                        details={
                            "rel_path": item["rel_path"],
                            "expected": item["size"],
                            "actual": info.file_size,
                        },
                    )
                digest = hashlib.sha256()
                with archive.open(info) as handle:
                    while True:
                        block = handle.read(CHUNK)
                        if not block:
                            break
                        digest.update(block)
                if digest.hexdigest() != item["sha256"]:
                    raise _invalid(
                        "备份包里的文件内容与清单哈希不符",
                        details={"rel_path": item["rel_path"]},
                        next_action=NEXT_ACTION_RETRY,
                    )
    except (zipfile.BadZipFile, zlib.error) as error:
        raise _invalid("备份包已损坏（无法解析）", details={"error": str(error)}) from error
    except OSError as error:
        raise _invalid("备份包读不出来", details={"error": str(error)}) from error

    summary = {
        "size": size,
        "created_at": manifest.get("created_at"),
        "format_version": manifest.get("format_version"),
        "schema_version": manifest.get("schema_version"),
        "app_version": manifest.get("app_version"),
        "generation_id": manifest.get("generation_id"),
        "job_count": manifest.get("job_count"),
        "entry_count": entry_count,
        "total_bytes": total_bytes,
        "note": "校验通过。恢复会先自动备份当前数据，再整体切换。",
    }
    return {"manifest": manifest, "summary": summary}


# --------------------------------------------------------------------------- #
# 解压
# --------------------------------------------------------------------------- #


def extract_archive(path: Path, dest_root: Path, manifest: dict[str, Any]) -> int:
    """把清单里的文件解到 `dest_root`，边写边复核 size 与 sha256。

    写入用「临时文件 -> os.replace」，中途失败不会留下半个文件；
    路径过两道（`check_rel_path` + `safe_relpath`），逃不出 `dest_root`。
    """
    dest_root = Path(dest_root)
    entries = manifest.get("entries") or []
    written = 0

    try:
        with zipfile.ZipFile(path) as archive:
            for item in entries:
                rel_path = check_rel_path(item["rel_path"])
                target = safe_relpath(dest_root, rel_path)
                target.parent.mkdir(parents=True, exist_ok=True)

                tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
                digest = hashlib.sha256()
                size = 0
                try:
                    with archive.open(item["rel_path"]) as source, open(tmp, "wb") as sink:
                        while True:
                            block = source.read(CHUNK)
                            if not block:
                                break
                            size += len(block)
                            digest.update(block)
                            sink.write(block)
                        sink.flush()
                        os.fsync(sink.fileno())
                    if size != item["size"] or digest.hexdigest() != item["sha256"]:
                        raise _invalid(
                            "解压出的文件与清单不符",
                            details={"rel_path": rel_path},
                            next_action=NEXT_ACTION_RETRY,
                        )
                    os.replace(tmp, target)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
                written += 1
    except (zipfile.BadZipFile, zlib.error) as error:
        raise _invalid("备份包已损坏（无法解析）", details={"error": str(error)}) from error
    except KeyError as error:
        raise _invalid(
            "备份包里缺少清单列出的文件", details={"rel_path": str(error)}
        ) from error
    except OSError as error:
        raise _invalid("解压备份包失败", details={"error": str(error)}) from error

    return written
