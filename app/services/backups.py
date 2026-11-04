"""ZIP 备份与原子恢复。

备份：把当前 generation 的 `settings.json`、每个任务的 `job.json` / `original.pdf`
/ 输出 PDF 打成一个 ZIP，附一份带 sha256 的清单。写完立刻自校验一遍——
「刚写出来的包自己都读不回来」这种事必须在这一刻发现，而不是等用户来恢复时。

恢复是三步，每一步都留退路：

1. **先给当前数据做一次自动备份并校验**。这一步失败就到此为止，
   当前数据一个字节没动。
2. **解压到相邻的一个新 generation 目录**，全量校验（每个 job.json 过 schema、
   文件数与清单一致）。校验失败就把新目录删掉，`active.json` 仍然指向老数据。
3. **原子切换 `active.json`**（临时文件 + os.replace）。切换失败同样保留老数据，
   新目录留着也不影响——它没被任何指针引用。

旧 generation 永不删除：恢复错了还能再切回来。
"""

from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

from app.config import APP_VERSION, SCHEMA_VERSION
from app.core import archive, atomic
from app.core.locking import LOCK_BACKUP, LOCK_RESTORE, LockError
from app.core.storage import (
    new_backup_id,
    new_generation_id,
    utcnow_iso,
    validate_id,
    PREFIX_BACKUP,
)
from app.errors import AppError, ErrorCode
from app.schemas import JOB_STATUSES, validate_job
from app.services.jobs import SOURCE_FILE_NAME

TOKEN_TTL_SECONDS = 600
MAX_PENDING_TOKENS = 5
ACTIVE_STATUSES = frozenset({"queued", "running"})

NEXT_ACTION_WAIT_JOB = "请等待当前任务处理完成后再恢复"
NEXT_ACTION_REINSPECT = "请重新选择备份文件并再次校验"
NEXT_ACTION_KEEP = "当前数据未被改动，可以继续使用或重试"


class BackupService:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.storage = ctx.storage
        self.settings = ctx.settings
        self.limits = ctx.settings.limits
        self.log = ctx.log
        # token -> {path, sha256, summary, expires_at}
        self._tokens: dict[str, dict[str, Any]] = {}
        self._guard = threading.Lock()
        self._cleanup_stale_temps()

    # ---- 内部 -----------------------------------------------------------
    def _generation(self) -> str:
        return self.ctx.generation_id

    def _payload_files(self) -> list[tuple[str, Path]]:
        """当前 generation 里值得进备份的文件（都是事实来源，不含可重建的索引）。"""
        gid = self._generation()
        files: list[tuple[str, Path]] = []

        settings_file = self.storage.generation_dir(gid) / archive.SETTINGS_NAME
        if settings_file.is_file():
            files.append((archive.SETTINGS_NAME, settings_file))

        for job_id in self.storage.list_job_ids(gid):
            job_json = self.storage.job_file(gid, job_id, "job.json")
            if not job_json.is_file():
                continue  # 坏任务不进包，恢复后也不会带进来一个读不出来的记录
            job = atomic.read_json(job_json, default=None)
            files.append((f"{archive.JOBS_DIR}/{job_id}/job.json", job_json))

            source = self.storage.job_file(gid, job_id, SOURCE_FILE_NAME)
            if source.is_file():
                files.append((f"{archive.JOBS_DIR}/{job_id}/{SOURCE_FILE_NAME}", source))

            output_name = self._output_name(job)
            if output_name:
                try:
                    output = self.storage.job_file(gid, job_id, output_name)
                except AppError:
                    continue  # 文件名里有路径语义：宁可漏一个产物，也不写出越界路径
                if output.is_file():
                    files.append(
                        (f"{archive.JOBS_DIR}/{job_id}/{output_name}", output)
                    )
        return files

    @staticmethod
    def _output_name(job: Any) -> str | None:
        if not isinstance(job, dict):
            return None
        output = job.get("output")
        if not isinstance(output, dict):
            return None
        name = output.get("file_name")
        return name if isinstance(name, str) and name else None

    def _active_jobs(self) -> list[dict[str, str]]:
        """还在队列里或正在跑的任务。有它们就不允许恢复。"""
        gid = self._generation()
        active: list[dict[str, str]] = []
        for job_id in self.storage.list_job_ids(gid):
            job = atomic.read_json(
                self.storage.job_file(gid, job_id, "job.json"), default=None
            )
            if isinstance(job, dict) and job.get("status") in ACTIVE_STATUSES:
                active.append({"job_id": job_id, "status": str(job.get("status"))})
        return active

    def _cleanup_stale_temps(self) -> None:
        """清掉上次进程留下的待恢复临时包（超过 TTL 的一律删）。"""
        tmp_dir = self.settings.tmp_dir
        if not tmp_dir.is_dir():
            return
        deadline = time.time() - TOKEN_TTL_SECONDS
        for child in tmp_dir.glob("restore-*.zip"):
            try:
                if child.stat().st_mtime < deadline:
                    child.unlink()
            except OSError:
                continue

    def _describe(self, backup_id: str) -> dict[str, Any]:
        path = self.storage.backup_path(backup_id)
        stat = path.stat()
        try:
            manifest = archive.read_manifest(path)
            created_at = manifest.get("created_at")
            job_count = manifest.get("job_count")
            format_version = manifest.get("format_version")
            readable = True
        except AppError:
            manifest = {}
            created_at = None
            job_count = None
            format_version = None
            readable = False

        return {
            "id": backup_id,
            "created_at": created_at,
            "size": stat.st_size,
            "job_count": job_count,
            "format_version": format_version,
            "readable": readable,
            "download_url": f"/api/backups/{backup_id}/file",
        }

    # ---- 备份 -----------------------------------------------------------
    def create(self, *, reason: str = "manual") -> dict[str, Any]:
        """打包 + 自校验。自校验不过的包立刻删掉，绝不留一个"看起来像备份"的文件。"""
        with self.ctx.locks.hold(LOCK_BACKUP):
            gid = self._generation()
            files = self._payload_files()
            entries = [
                {
                    "rel_path": rel_path,
                    "size": path.stat().st_size,
                    "sha256": archive.sha256_file(path),
                }
                for rel_path, path in files
            ]
            manifest = archive.build_manifest(
                generation_id=gid,
                entries=entries,
                app_version=APP_VERSION,
                format_version=self.settings.backup_format_version,
                schema_version=SCHEMA_VERSION,
                created_at=utcnow_iso(),
                reason=reason,
            )

            backup_id = new_backup_id()
            dest = self.storage.backup_path(backup_id)
            archive.write_archive(dest, manifest, files)

            try:
                archive.inspect_archive(
                    dest,
                    limits=self.limits,
                    expected_format_version=self.settings.backup_format_version,
                )
            except AppError:
                dest.unlink(missing_ok=True)
                raise

            self.log.info("备份 %s 完成：%d 个任务", backup_id, manifest["job_count"])
            return self._describe(backup_id)

    def list_backups(self) -> dict[str, Any]:
        backups = [
            self._describe(backup_id)
            for backup_id in reversed(self.storage.list_backup_ids())
        ]
        return {
            "generation_id": self._generation(),
            "count": len(backups),
            "backups": backups,
        }

    def path(self, backup_id: str) -> Path:
        validate_id(backup_id, PREFIX_BACKUP)
        path = self.storage.backup_path(backup_id)
        if not path.is_file():
            raise AppError(
                ErrorCode.BACKUP_NOT_FOUND,
                "备份文件不存在",
                details={"backup_id": backup_id},
                next_action="请刷新备份列表后重试",
                http_status=404,
            )
        return path

    # ---- 校验（拿令牌） --------------------------------------------------
    def inspect_upload(self, filename: str, stream: BinaryIO) -> dict[str, Any]:
        """校验用户上传的备份包，通过则发一个短期一次性令牌。"""
        safe_name = Path(filename or "backup.zip").name or "backup.zip"
        token = secrets.token_urlsafe(24)
        temp = self.settings.tmp_dir / f"restore-{token}.zip"
        temp.parent.mkdir(parents=True, exist_ok=True)

        try:
            size = 0
            with open(temp, "wb") as sink:
                while True:
                    block = stream.read(self.limits.upload_chunk_bytes)
                    if not block:
                        break
                    size += len(block)
                    if size > self.limits.max_upload_bytes:
                        raise AppError(
                            ErrorCode.INVALID_BACKUP,
                            "备份文件过大",
                            details={"limit": self.limits.max_upload_bytes},
                            next_action="请确认选择的是本工具导出的备份文件",
                            http_status=400,
                        )
                    sink.write(block)
                sink.flush()
            if size == 0:
                raise AppError(
                    ErrorCode.INVALID_BACKUP,
                    "备份文件是空的",
                    next_action="请重新选择备份文件",
                    http_status=400,
                )

            inspected = archive.inspect_archive(
                temp,
                limits=self.limits,
                expected_format_version=self.settings.backup_format_version,
                max_archive_bytes=self.limits.max_upload_bytes,
            )
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

        with self._guard:
            self._evict_expired()
            self._tokens[token] = {
                "path": temp,
                "sha256": archive.sha256_file(temp),
                "summary": inspected["summary"],
                "expires_at": time.time() + TOKEN_TTL_SECONDS,
                "file_name": safe_name,
            }
            self._enforce_token_limit()

        return {
            "token": token,
            "summary": inspected["summary"],
            "expires_in": TOKEN_TTL_SECONDS,
            "file_name": safe_name,
        }

    def _evict_expired(self) -> None:
        now = time.time()
        for key in [k for k, v in self._tokens.items() if v["expires_at"] <= now]:
            self._drop_token(key)

    def _enforce_token_limit(self) -> None:
        while len(self._tokens) > MAX_PENDING_TOKENS:
            oldest = min(self._tokens, key=lambda key: self._tokens[key]["expires_at"])
            self._drop_token(oldest)

    def _drop_token(self, token: str) -> None:
        entry = self._tokens.pop(token, None)
        if entry:
            Path(entry["path"]).unlink(missing_ok=True)

    # ---- 恢复 -----------------------------------------------------------
    def restore(self, token: str) -> dict[str, Any]:
        with self._guard:
            self._evict_expired()
            entry = self._tokens.get(token)

        if not entry:
            raise AppError(
                ErrorCode.RESTORE_TOKEN_INVALID,
                "恢复令牌无效或已过期",
                details={"token": token[:8]},
                retryable=True,
                next_action=NEXT_ACTION_REINSPECT,
                http_status=400,
            )

        active = self._active_jobs()
        if active:
            raise AppError(
                ErrorCode.RESTORE_CONFLICT,
                "还有任务正在处理，暂时不能恢复",
                details={"active": active},
                retryable=True,
                next_action=NEXT_ACTION_WAIT_JOB,
                http_status=409,
            )

        try:
            with self.ctx.locks.hold(LOCK_RESTORE):
                return self._restore_locked(token, entry)
        except LockError as error:
            raise AppError(
                ErrorCode.RESTORE_CONFLICT,
                "另一个恢复操作正在进行",
                details={"lock": LOCK_RESTORE, "holder": error.details.get("holder", {})},
                retryable=True,
                next_action="请等待当前恢复结束后重试",
                http_status=409,
            ) from error

    def _restore_locked(self, token: str, entry: dict[str, Any]) -> dict[str, Any]:
        temp = Path(entry["path"])
        if not temp.is_file():
            with self._guard:
                self._drop_token(token)
            raise AppError(
                ErrorCode.RESTORE_TOKEN_INVALID,
                "待恢复的备份文件已不存在",
                retryable=True,
                next_action=NEXT_ACTION_REINSPECT,
                http_status=400,
            )
        if archive.sha256_file(temp) != entry["sha256"]:
            with self._guard:
                self._drop_token(token)
            raise AppError(
                ErrorCode.INVALID_BACKUP,
                "备份文件在校验之后被改动过",
                next_action=NEXT_ACTION_REINSPECT,
                http_status=400,
            )

        # 再校验一次：令牌签发到真正恢复之间可能过了几分钟。
        inspected = archive.inspect_archive(
            temp,
            limits=self.limits,
            expected_format_version=self.settings.backup_format_version,
        )
        manifest = inspected["manifest"]

        previous = self._generation()

        # 第一步：自动备份当前数据（失败就停，当前数据零变化）
        try:
            auto = self.create(reason="pre-restore")
        except BaseException as error:
            if isinstance(error, AppError):
                raise AppError(
                    ErrorCode.RESTORE_FAILED,
                    "恢复前的自动备份失败，当前数据未被改动",
                    details={"error": error.code, "message": error.message},
                    next_action=NEXT_ACTION_KEEP,
                    http_status=500,
                ) from error
            raise AppError(
                ErrorCode.RESTORE_FAILED,
                "恢复前的自动备份失败，当前数据未被改动",
                details={"error": str(error)},
                next_action=NEXT_ACTION_KEEP,
                http_status=500,
            ) from error

        # 第二步：解压到相邻的新 generation，全量校验
        target_gid = new_generation_id()
        self.storage.create_generation(target_gid, source=f"restore:{auto['id']}")
        target_dir = self.storage.generation_dir(target_gid)
        try:
            archive.extract_archive(temp, target_dir, manifest)
            job_count = self._verify_restored(target_gid, manifest)
        except BaseException as error:
            self._discard_generation(target_gid)
            if isinstance(error, AppError):
                raise
            raise AppError(
                ErrorCode.RESTORE_FAILED,
                "恢复失败，当前数据未被改动",
                details={"generation_id": target_gid, "error": str(error)},
                next_action=NEXT_ACTION_KEEP,
                http_status=500,
            ) from error

        # 第三步：原子切换指针
        try:
            self.storage.set_active_generation(target_gid)
        except BaseException as error:
            self._discard_generation(target_gid)
            raise AppError(
                ErrorCode.RESTORE_FAILED,
                "切换数据版本失败，当前数据未被改动",
                details={"generation_id": target_gid, "error": str(error)},
                next_action=NEXT_ACTION_KEEP,
                http_status=500,
            ) from error

        self.ctx.generation_id = target_gid
        self.ctx.jobs.reconcile_interrupted()
        with self._guard:
            self._drop_token(token)

        self.log.info(
            "恢复完成：%s -> %s（自动备份 %s，%d 个任务）",
            previous,
            target_gid,
            auto["id"],
            job_count,
        )
        return {
            "generation_id": target_gid,
            "previous_generation_id": previous,
            "auto_backup_id": auto["id"],
            "job_count": job_count,
            "restored_at": utcnow_iso(),
        }

    def _verify_restored(self, generation_id: str, manifest: dict[str, Any]) -> int:
        """恢复后的全量校验：每个 job.json 都要过 schema，文件数要对得上。"""
        jobs_root = self.storage.jobs_root(generation_id)
        job_dirs = (
            [child for child in jobs_root.iterdir() if child.is_dir()]
            if jobs_root.is_dir()
            else []
        )

        expected = int(manifest.get("job_count") or 0)
        if len(job_dirs) != expected:
            raise AppError(
                ErrorCode.RESTORE_FAILED,
                "恢复后的任务数与清单不符",
                details={"expected": expected, "actual": len(job_dirs)},
                next_action=NEXT_ACTION_KEEP,
                http_status=500,
            )

        for directory in job_dirs:
            payload = atomic.read_json(directory / "job.json", default=None)
            if not isinstance(payload, dict):
                raise AppError(
                    ErrorCode.RESTORE_FAILED,
                    "恢复后的任务记录读不出来",
                    details={"job_id": directory.name},
                    next_action=NEXT_ACTION_KEEP,
                    http_status=500,
                )
            validate_job(payload)
            # 合法状态就是全部 JOB_STATUSES：包里完全可能有一个"待配置"的任务
            # （上传了但还没点开始），那不是坏数据。包里若带着 queued/running，
            # 切换指针后的 reconcile_interrupted() 会如实改成"已中断"。
            if payload.get("status") not in JOB_STATUSES:
                raise AppError(
                    ErrorCode.RESTORE_FAILED,
                    "恢复后的任务状态不合法",
                    details={"job_id": directory.name, "status": payload.get("status")},
                    next_action=NEXT_ACTION_KEEP,
                    http_status=500,
                )

        self.storage.rebuild_index(generation_id)
        return len(job_dirs)

    def _discard_generation(self, generation_id: str) -> None:
        """丢弃半成品 generation。只在指针没切过去时调用。"""
        import shutil

        try:
            shutil.rmtree(self.storage.generation_dir(generation_id), ignore_errors=True)
        except OSError:  # pragma: no cover - 尽力而为
            self.log.warning("清理半成品 generation 失败：%s", generation_id)
