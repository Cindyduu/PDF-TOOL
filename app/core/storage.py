"""generation 数据目录与路径白名单。

数据布局：

    var/active.json                         -> {"generation_id": "gen-..."}
    var/generations/<gen>/manifest.json
    var/generations/<gen>/settings.json
    var/generations/<gen>/index.json        （可重建索引，非事实来源）
    var/generations/<gen>/jobs/<job>/job.json|report.json|events.jsonl|original.pdf|输出.pdf
    var/backups/  var/tmp/  var/logs/  var/locks/

API 只接收内部 ID，任何路径都必须经过这里的校验。
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import BACKUP_FORMAT_VERSION, SCHEMA_VERSION, Settings
from app.core import atomic
from app.errors import AppError, ErrorCode, PathTraversalError

_ID_RE = re.compile(r"^(gen|job|bak)-[0-9A-Za-z][0-9A-Za-z-]{2,63}$")

PREFIX_GENERATION = "gen"
PREFIX_JOB = "job"
PREFIX_BACKUP = "bak"


def utcnow_iso() -> str:
    """带毫秒的 UTC 时间戳。

    毫秒不是装饰：历史列表按 `created_at` 倒序，如果只精确到秒，
    同一秒内连着上传的几个文件谁前谁后就是目录遍历顺序说了算，
    用户会看到「刚传的排在中间」。毫秒让排序真的等于时间顺序。
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _id_stamp(moment: datetime | None = None) -> str:
    """ID 里的时间戳：秒 + 毫秒。

    毫秒不是装饰。备份列表按 ID 倒序（ID 本身就是时间），只精确到秒的话，
    同一秒内连着创建的两次备份谁前谁后由随机后缀决定，用户会看到
    「刚做的备份排在中间」。定宽 17 位数字保证字典序等于时间序。
    """
    moment = moment or datetime.now(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"


def new_generation_id(moment: datetime | None = None) -> str:
    return f"{PREFIX_GENERATION}-{_id_stamp(moment)}-{secrets.token_hex(3)}"


def new_job_id() -> str:
    return f"{PREFIX_JOB}-{secrets.token_hex(8)}"


def new_backup_id(moment: datetime | None = None) -> str:
    return f"{PREFIX_BACKUP}-{_id_stamp(moment)}-{secrets.token_hex(3)}"


def validate_id(value: str, prefix: str | None = None) -> str:
    """校验内部 ID 形态，拒绝任何带路径语义的输入。"""
    if not isinstance(value, str) or not value:
        raise AppError(
            ErrorCode.INVALID_ID,
            "无效的标识符",
            details={"value": repr(value)},
            next_action="请从界面重新进入该页面",
            http_status=400,
        )
    if not _ID_RE.match(value):
        raise AppError(
            ErrorCode.INVALID_ID,
            "标识符格式不正确",
            details={"value": value},
            next_action="请从界面重新进入该页面",
            http_status=400,
        )
    if prefix is not None and not value.startswith(prefix + "-"):
        raise AppError(
            ErrorCode.INVALID_ID,
            f"标识符类型不正确，应为 {prefix}",
            details={"value": value},
            next_action="请从界面重新进入该页面",
            http_status=400,
        )
    return value


def _reject_segment(segment: str) -> None:
    if not isinstance(segment, str):
        raise PathTraversalError("路径片段必须是字符串")
    if segment in ("", ".", ".."):
        raise PathTraversalError(f"不允许的路径片段：{segment!r}")
    if "/" in segment or "\\" in segment:
        raise PathTraversalError(f"路径片段不能包含分隔符：{segment!r}")
    if ":" in segment:
        raise PathTraversalError(f"路径片段不能包含冒号：{segment!r}")
    if segment.startswith("~"):
        raise PathTraversalError(f"路径片段不能以 ~ 开头：{segment!r}")


def resolve_under(root: Path | str, *segments: str) -> Path:
    """把若干**单段**名称安全拼到 root 下，拒绝穿越、绝对路径、盘符、UNC。"""
    base = Path(root).resolve()
    for segment in segments:
        _reject_segment(segment)
    candidate = base.joinpath(*segments)
    resolved = candidate.resolve(strict=False)
    if resolved != base and base not in resolved.parents:
        raise PathTraversalError(f"路径逃逸：{candidate}")
    return resolved


def safe_relpath(root: Path | str, relative: str) -> Path:
    """校验相对路径（可多段）并返回 root 下的绝对路径。用于备份包解压。"""
    base = Path(root).resolve()
    if not isinstance(relative, str) or not relative.strip():
        raise PathTraversalError("空路径")
    normalised = relative.replace("\\", "/")
    if normalised.startswith("/"):
        raise PathTraversalError(f"不允许绝对路径：{relative}")
    parts = normalised.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise PathTraversalError(f"不允许的路径片段：{relative}")
        if ":" in part:
            raise PathTraversalError(f"不允许的路径片段：{relative}")
    candidate = base.joinpath(*parts).resolve(strict=False)
    if base not in candidate.parents:
        raise PathTraversalError(f"路径逃逸：{relative}")
    return candidate


class Storage:
    """generation 级存储门面。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ---- 初始化 ---------------------------------------------------------
    def init(self) -> None:
        self.settings.ensure_dirs()

    @property
    def var_dir(self) -> Path:
        return self.settings.var_dir

    @property
    def generations_dir(self) -> Path:
        return self.settings.generations_dir

    @property
    def backups_dir(self) -> Path:
        return self.settings.backups_dir

    # ---- generation -----------------------------------------------------
    def generation_dir(self, generation_id: str) -> Path:
        validate_id(generation_id, PREFIX_GENERATION)
        return resolve_under(self.generations_dir, generation_id)

    def create_generation(
        self, generation_id: str | None = None, source: str = "new"
    ) -> str:
        gid = generation_id or new_generation_id()
        validate_id(gid, PREFIX_GENERATION)
        directory = self.generation_dir(gid)
        (directory / "jobs").mkdir(parents=True, exist_ok=True)
        manifest = {
            "format_version": self.settings.backup_format_version,
            "schema_version": SCHEMA_VERSION,
            "app_version": self.settings.app_version,
            "generation_id": gid,
            "created_at": utcnow_iso(),
            "source": source,
        }
        atomic.atomic_write_json(directory / "manifest.json", manifest)
        atomic.atomic_write_json(
            directory / "settings.json",
            {"schema_version": SCHEMA_VERSION, "created_at": utcnow_iso()},
        )
        return gid

    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        path = self.generation_dir(generation_id) / "manifest.json"
        manifest = atomic.read_json(path, default=None)
        if manifest is None:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "generation manifest 缺失",
                details={"generation_id": generation_id},
                next_action="请重新启动应用",
                http_status=500,
            )
        return manifest

    def active_generation_id(self) -> str | None:
        payload = atomic.read_json(self.settings.active_path, default=None)
        if not isinstance(payload, dict):
            return None
        gid = payload.get("generation_id")
        if not isinstance(gid, str) or not _ID_RE.match(gid):
            return None
        if not self.generation_dir(gid).is_dir():
            return None
        return gid

    def set_active_generation(self, generation_id: str) -> None:
        validate_id(generation_id, PREFIX_GENERATION)
        if not self.generation_dir(generation_id).is_dir():
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "目标 generation 不存在，无法切换",
                details={"generation_id": generation_id},
                next_action="请重新启动应用",
                http_status=500,
            )
        atomic.atomic_write_json(
            self.settings.active_path,
            {
                "generation_id": generation_id,
                "updated_at": utcnow_iso(),
                "schema_version": SCHEMA_VERSION,
            },
        )

    def ensure_active(self, source: str = "new") -> str:
        existing = self.active_generation_id()
        if existing:
            return existing
        gid = self.create_generation(source=source)
        self.set_active_generation(gid)
        return gid

    def list_generation_ids(self) -> list[str]:
        if not self.generations_dir.is_dir():
            return []
        result = []
        for child in sorted(self.generations_dir.iterdir()):
            if child.is_dir() and _ID_RE.match(child.name):
                result.append(child.name)
        return result

    # ---- job ------------------------------------------------------------
    def jobs_root(self, generation_id: str) -> Path:
        return self.generation_dir(generation_id) / "jobs"

    def job_dir(self, generation_id: str, job_id: str, create: bool = False) -> Path:
        validate_id(job_id, PREFIX_JOB)
        root = self.jobs_root(generation_id)
        path = resolve_under(root, job_id)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def job_file(
        self, generation_id: str, job_id: str, filename: str, create_dir: bool = False
    ) -> Path:
        directory = self.job_dir(generation_id, job_id, create=create_dir)
        return resolve_under(directory, filename)

    def list_job_ids(self, generation_id: str) -> list[str]:
        root = self.jobs_root(generation_id)
        if not root.is_dir():
            return []
        return sorted(
            child.name
            for child in root.iterdir()
            if child.is_dir() and _ID_RE.match(child.name)
        )

    # ---- 索引（可重建，不是事实来源） -----------------------------------
    def index_path(self, generation_id: str) -> Path:
        return self.generation_dir(generation_id) / "index.json"

    def build_index_entries(self, generation_id: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for job_id in self.list_job_ids(generation_id):
            # 单个 job.json 损坏只影响这一条，不能拖垮整张历史列表
            job = atomic.read_json(
                self.job_file(generation_id, job_id, "job.json"), default=None
            )
            if not isinstance(job, dict):
                continue
            output = job.get("output") or {}
            output_name = output.get("file_name") if isinstance(output, dict) else None
            entries.append(
                {
                    "id": job.get("id", job_id),
                    "original_name": job.get("original_name", ""),
                    "status": job.get("status", "failed"),
                    "created_at": job.get("created_at"),
                    "finished_at": job.get("finished_at"),
                    "output_name": output_name,
                    "output_available": bool(output_name)
                    and (self.job_dir(generation_id, job_id) / output_name).is_file(),
                    "source_available": (
                        self.job_file(generation_id, job_id, "original.pdf").is_file()
                    ),
                    "review_required": any(
                        r.get("status") == "review_required"
                        for r in (job.get("results") or [])
                        if isinstance(r, dict)
                    ),
                }
            )
        entries.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return entries

    def rebuild_index(self, generation_id: str) -> dict[str, Any]:
        """由每个任务的 job.json 重建 index.json。索引损坏或删除都不影响历史。"""
        payload = {
            "generation_id": generation_id,
            "rebuilt_at": utcnow_iso(),
            "jobs": self.build_index_entries(generation_id),
        }
        atomic.atomic_write_json(self.index_path(generation_id), payload)
        return payload

    def read_index(self, generation_id: str, rebuild_if_missing: bool = True) -> dict[str, Any]:
        path = self.index_path(generation_id)
        payload = atomic.read_json(path, default=None)
        if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
            return payload
        if rebuild_if_missing:
            return self.rebuild_index(generation_id)
        return {"generation_id": generation_id, "rebuilt_at": None, "jobs": []}

    # ---- backup ---------------------------------------------------------
    def backup_path(self, backup_id: str) -> Path:
        validate_id(backup_id, PREFIX_BACKUP)
        return resolve_under(self.backups_dir, f"{backup_id}.zip")

    def list_backup_ids(self) -> list[str]:
        if not self.backups_dir.is_dir():
            return []
        return sorted(
            child.stem
            for child in self.backups_dir.iterdir()
            if child.is_file() and child.suffix == ".zip" and _ID_RE.match(child.stem)
        )

    # ---- 锁 -------------------------------------------------------------
    def lock_path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", name):
            raise ValueError(f"非法锁名：{name}")
        return self.settings.locks_dir / f"{name}.lock"
