"""任务创建、读取、配置与执行编排。

约定：
- 输入永远落在任务目录内的 `original.pdf`，用户原名只存在 JSON 里；
- 写盘走"临时文件 -> fsync -> os.replace"，中途失败不留下半个 PDF；
- 任何一步被拒绝都要把半成品任务目录清干净，历史里不出现幽灵任务；
- 状态只经 `app.core.state_machine` 迁移，本模块不直接给 `job["status"]` 赋值。

执行产物：
    job.json      事实来源（状态、配置、结果、输出文件元信息）
    events.jsonl  只追加的阶段/状态事件，seq 单调递增，**只有阶段名没有百分比**
    report.json   终态时落盘的报告快照，刷新与重启后仍可读取
    original.pdf  上传原件
    <输出名>.pdf   通过最终验证之后才改名发布
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any, BinaryIO

from app.config import APP_NAME, APP_VERSION, SCHEMA_VERSION
from app.core import atomic, state_machine
from app.core.naming import build_output_name
from app.core.pagespec import parse_page_spec
from app.core.storage import (
    PREFIX_BATCH,
    PREFIX_JOB,
    new_batch_id,
    new_job_id,
    utcnow_iso,
    validate_id,
)
from app.errors import AppError, ErrorCode
from app.pdf.inspector import inspect_pdf
from app.pdf.tags.detect import detect_tags
from app.schemas import (
    COUNT_KEYS,
    RESULT_STATUS_ICONS,
    RESULT_STATUS_LABELS,
    STAGE_LABELS,
    STAGE_NAMES,
    TAG_CHOICES,
    TERMINAL_JOB_STATUSES,
    default_config,
    validate_job,
)

SOURCE_FILE_NAME = "original.pdf"
EVENTS_FILE_NAME = "events.jsonl"
REPORT_FILE_NAME = "report.json"

# 流水线先写到这里；通过最终验证后才改名为正式输出名。
# 好处：`file?kind=output` 只要看不到 `job["output"]` 就一定还没验证通过。
PENDING_OUTPUT_NAME = ".pending-output.pdf"

DISCLAIMER = "本报告由程序自动生成，结果供人工复核，不构成合规结论。"

# 检测结果 -> 默认标签选择（用户仍可在界面上改）
DEFAULT_TAG_CHOICE: dict[str, str] = {
    "tagged": "keep",
    "untagged": "generate",
    "markinfo_only": "regenerate",
}

NEXT_ACTION_CONFIG = "请检查页码格式后重新保存"
NEXT_ACTION_INTERRUPTED = "在历史中按原配置重新处理该文件"


class JobService:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.storage = ctx.storage
        self.limits = ctx.settings.limits
        self.log = ctx.log
        # 入队与状态写入必须互斥，否则两个并发的 POST /start 可能都通过检查
        self._guard = threading.RLock()

    # ---- 内部 -----------------------------------------------------------
    def _job_path(self, job_id: str) -> Path:
        return self.storage.job_file(self.ctx.generation_id, job_id, "job.json")

    def _job_dir(self, job_id: str, create: bool = False) -> Path:
        return self.storage.job_dir(self.ctx.generation_id, job_id, create=create)

    def _write_job(self, job: dict[str, Any]) -> None:
        validate_job(job)
        atomic.atomic_write_json(self._job_path(job["id"]), job)
        self.storage.rebuild_index(self.ctx.generation_id)

    def write_job(self, job: dict[str, Any]) -> None:
        """公开的写盘入口：历史服务复制任务时也要落盘，但只走这一条路。"""
        self._write_job(job)

    def _store_original(self, directory: Path, stream: BinaryIO) -> tuple[int, str]:
        """流式落盘并校验大小 / 空文件 / magic。返回 (size, sha256)。"""
        target = directory / SOURCE_FILE_NAME
        tmp = directory / f".{SOURCE_FILE_NAME}.{secrets.token_hex(4)}.tmp"
        hasher = hashlib.sha256()
        header = b""
        total = 0
        try:
            with open(tmp, "wb") as handle:
                while True:
                    chunk = stream.read(self.limits.upload_chunk_bytes)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.limits.max_upload_bytes:
                        raise AppError(
                            ErrorCode.FILE_TOO_LARGE,
                            "文件超过大小上限",
                            details={
                                "size": total,
                                "limit": self.limits.max_upload_bytes,
                            },
                            retryable=False,
                            next_action=(
                                f"请上传不超过 "
                                f"{self.limits.max_upload_bytes // (1024 * 1024)} MB 的文件"
                            ),
                            http_status=413,
                        )
                    if len(header) < 5:
                        header += chunk[: 5 - len(header)]
                    hasher.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            if total == 0:
                raise AppError(
                    ErrorCode.EMPTY_FILE,
                    "文件是空的",
                    retryable=False,
                    next_action="请重新选择一个 PDF 文件",
                    http_status=400,
                )
            if not header.startswith(b"%PDF-"):
                raise AppError(
                    ErrorCode.NOT_A_PDF,
                    "这不是 PDF 文件",
                    details={"magic": header.decode("latin-1", "replace")},
                    retryable=False,
                    next_action="请选择 PDF 文件后重试",
                    http_status=400,
                )

            os.replace(tmp, target)
            return total, hasher.hexdigest()
        finally:
            tmp.unlink(missing_ok=True)

    # ---- 创建 -----------------------------------------------------------
    def create_job(
        self, filename: str, stream: BinaryIO, batch_id: str | None = None
    ) -> dict[str, Any]:
        if batch_id is not None:
            validate_id(batch_id, PREFIX_BATCH)
        generation_id = self.ctx.generation_id
        job_id = new_job_id()
        directory = self.storage.job_dir(generation_id, job_id, create=True)
        try:
            size, digest = self._store_original(directory, stream)
            facts = inspect_pdf(directory / SOURCE_FILE_NAME, self.limits)
            detection = detect_tags(directory / SOURCE_FILE_NAME)

            status = (
                "awaiting_tag_decision"
                if detection["status"] == "detection_failed"
                else "configuring"
            )
            config = default_config()
            config["tag_choice"] = DEFAULT_TAG_CHOICE.get(detection["status"])

            job: dict[str, Any] = {
                "id": job_id,
                "schema_version": SCHEMA_VERSION,
                "status": status,
                "current_stage": None,
                "original_name": self._safe_name(filename),
                "created_at": utcnow_iso(),
                "started_at": None,
                "finished_at": None,
                "source": {
                    "file_name": SOURCE_FILE_NAME,
                    "original_name": self._safe_name(filename),
                    "sha256": facts.sha256 or digest,
                    "size": facts.size,
                    "page_count": facts.page_count,
                },
                "output": None,
                "config": config,
                "tag_detection": detection,
                "results": [],
                "last_error": None,
                "generation_id": generation_id,
                "batch_id": batch_id,
            }
            self._write_job(job)
            return job
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    @staticmethod
    def _safe_name(filename: str) -> str:
        from app.core.naming import sanitize_filename

        return sanitize_filename(filename or "document.pdf")

    # ---- 批次 -----------------------------------------------------------
    def create_batch(self, items: list[tuple[str, BinaryIO]]) -> dict[str, Any]:
        """把一批上传文件逐个建成任务，共享一个 `batch_id`。

        设计要点（决定了这里的错误处理方式）：

        - **单个文件失败不拖垮整批**：坏文件进 `rejected`，好文件照常建任务。
          批量场景里一个文件坏了就整批退回，等于让用户重传 19 个没问题的文件。
        - **全部失败才抛错**：整批都没建成时不能返回 201，否则前端会跳到一个
          空批次页。此时把第一条失败原因抬成整批的错误码，细节里带全量清单。
        - 上限检查在**建任何任务之前**做，磁盘上不留半批目录。
        """
        limit = self.limits.max_batch_files
        if len(items) > limit:
            raise AppError(
                ErrorCode.TOO_MANY_FILES,
                f"一次最多处理 {limit} 个文件",
                details={"count": len(items), "limit": limit},
                retryable=False,
                next_action=f"请分批上传，每批不超过 {limit} 个文件",
                http_status=400,
            )

        batch_id = new_batch_id()
        jobs: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for filename, stream in items:
            try:
                jobs.append(self.create_job(filename, stream, batch_id=batch_id))
            except AppError as exc:
                rejected.append(
                    {
                        "name": self._safe_name(filename),
                        "code": exc.code,
                        "message": exc.message,
                        "next_action": exc.next_action,
                    }
                )

        if not jobs:
            first = rejected[0]
            raise AppError(
                first["code"],
                "这一批文件都没能加入处理队列",
                details={"rejected": rejected, "rejected_count": len(rejected)},
                retryable=False,
                next_action=first["next_action"],
                http_status=400,
            )

        return {"batch_id": batch_id, "jobs": jobs, "rejected": rejected}

    def batch_jobs(self, batch_id: str) -> list[dict[str, Any]]:
        """按 `batch_id` 收集任务，按创建时间正序（与上传顺序一致）。

        直接扫 job.json 而不是查 index：索引是可重建的缓存，
        批次页拿到的是"现在到底是什么状态"，不能吃缓存。
        """
        validate_id(batch_id, PREFIX_BATCH)
        jobs: list[dict[str, Any]] = []
        for job_id in self.storage.list_job_ids(self.ctx.generation_id):
            job = atomic.read_json(self._job_path(job_id), default=None)
            if isinstance(job, dict) and job.get("batch_id") == batch_id:
                jobs.append(job)
        if not jobs:
            raise AppError(
                ErrorCode.JOB_NOT_FOUND,
                "批次不存在或已被删除",
                details={"batch_id": batch_id},
                next_action="请回到首页重新上传文件",
                http_status=404,
            )
        jobs.sort(key=lambda item: (item.get("created_at") or "", item["id"]))
        return jobs

    def apply_batch_config(
        self,
        batch_id: str,
        *,
        exempt_pages_raw: str | None = None,
        tag_choice: str | None = None,
        tag_choice_provided: bool = False,
    ) -> dict[str, Any]:
        """把同一份配置套到整批任务上。

        各文件页数不同，同一个豁免页字符串在 A 文件合法、在 B 文件可能越界，
        所以必须逐个走 `update_config` 的校验：越界的进 `invalid`，
        绝不把非法页码写进配置。
        """
        jobs = self.batch_jobs(batch_id)
        updated: list[str] = []
        invalid: list[dict[str, Any]] = []
        for job in jobs:
            try:
                self.update_config(
                    job["id"],
                    exempt_pages_raw=exempt_pages_raw,
                    tag_choice=tag_choice,
                    tag_choice_provided=tag_choice_provided,
                )
                updated.append(job["id"])
            except AppError as exc:
                invalid.append(
                    {
                        "id": job["id"],
                        "name": job.get("original_name"),
                        "code": exc.code,
                        "message": exc.message,
                        "next_action": exc.next_action,
                    }
                )
        return {"batch_id": batch_id, "updated": updated, "invalid": invalid}

    def start_batch(self, batch_id: str) -> dict[str, Any]:
        """逐个校验并入队，能开始的开始，不能开始的列进 `skipped`。

        这里刻意不做"有一个不合法就整批不动"：批量处理的目的是省事，
        为了一个页数少的文件把另外 19 个也卡住，用户只能手动一个个点。
        """
        jobs = self.batch_jobs(batch_id)
        started: list[str] = []
        skipped: list[dict[str, Any]] = []
        for job in jobs:
            try:
                fresh = self.assert_startable(job["id"])
                self.enqueue(fresh)
                started.append(job["id"])
            except AppError as exc:
                skipped.append(
                    {
                        "id": job["id"],
                        "name": job.get("original_name"),
                        "code": exc.code,
                        "message": exc.message,
                        "next_action": exc.next_action,
                    }
                )
        return {"batch_id": batch_id, "started": started, "skipped": skipped}

    # ---- 读取 -----------------------------------------------------------
    def get_job(self, job_id: str) -> dict[str, Any]:
        validate_id(job_id, PREFIX_JOB)
        job = atomic.read_json(self._job_path(job_id), default=None)
        if not isinstance(job, dict):
            raise AppError(
                ErrorCode.JOB_NOT_FOUND,
                "任务不存在或已被删除",
                details={"job_id": job_id},
                next_action="请回到首页重新上传文件",
                http_status=404,
            )
        return job

    def list_jobs(self, *, query: str | None = None, status: str | None = None) -> dict[str, Any]:
        """历史列表统一由 `HistoryService` 负责（搜索/筛选/文件存在性）。

        这里保留一个薄转发，是为了让「任务服务」对外仍然是完整的一站式入口；
        实现只有一份，改规则不会两边跑偏。
        """
        return self.ctx.history.list_jobs(query=query, status=status)

    # ---- 配置 -----------------------------------------------------------
    def update_config(
        self,
        job_id: str,
        *,
        exempt_pages_raw: str | None = None,
        tag_choice: str | None = None,
        tag_choice_provided: bool = False,
    ) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job["status"] not in ("configuring", "awaiting_tag_decision"):
            raise AppError(
                ErrorCode.JOB_ALREADY_STARTED,
                "任务已开始处理，配置不可再修改",
                details={"status": job["status"]},
                next_action="如需修改配置，请在历史中重新处理该文件",
                http_status=409,
            )

        page_count = int(job["source"]["page_count"])
        config = dict(job["config"])

        if exempt_pages_raw is not None:
            spec = parse_page_spec(exempt_pages_raw, page_count)
            # 存规范化后的文本：输入框回显与页码集合永远一致，
            # 不会出现"显示 4 , 5 但实际豁免 4、5"的错位
            config["exempt_pages_raw"] = spec.normalized
            config["exempt_pages"] = spec.as_list()

        if tag_choice_provided:
            if tag_choice is not None and tag_choice not in TAG_CHOICES:
                raise AppError(
                    ErrorCode.INVALID_CONFIG,
                    "标签选择不在允许范围内",
                    details={"tag_choice": tag_choice, "allowed": list(TAG_CHOICES)},
                    next_action="请从界面给出的选项中选择",
                    http_status=400,
                )
            config["tag_choice"] = tag_choice

        if config.get("tag_choice") is None:
            raise AppError(
                ErrorCode.INVALID_CONFIG,
                "还没有选择如何处理标签",
                details={"tag_status": job["tag_detection"]["status"]},
                next_action="请选择“生成标签”或“跳过标签”后再开始",
                http_status=400,
            )

        job["config"] = config
        if job["status"] == "awaiting_tag_decision":
            job["status"] = "configuring"
        self._write_job(job)
        return job

    # ---- 标签检测重试 ---------------------------------------------------
    def retry_tag_detection(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job["status"] not in ("configuring", "awaiting_tag_decision"):
            raise AppError(
                ErrorCode.JOB_ALREADY_STARTED,
                "任务已开始处理，无法重新检测标签",
                details={"status": job["status"]},
                next_action="如需重新检测，请在历史中重新处理该文件",
                http_status=409,
            )

        source = self.storage.job_file(
            self.ctx.generation_id, job_id, SOURCE_FILE_NAME
        )
        if not source.is_file():
            raise AppError(
                ErrorCode.SOURCE_MISSING,
                "原始文件已不存在，无法重新检测",
                details={"job_id": job_id},
                next_action="请重新上传该文件",
                http_status=404,
            )

        detection = detect_tags(source)
        job["tag_detection"] = detection
        if detection["status"] == "detection_failed":
            job["status"] = "awaiting_tag_decision"
        else:
            if job["status"] == "awaiting_tag_decision":
                job["status"] = "configuring"
            if job["config"].get("tag_choice") is None:
                job["config"]["tag_choice"] = DEFAULT_TAG_CHOICE.get(detection["status"])
        self._write_job(job)
        return job

    # ---- 开始处理前的最后一道校验 ---------------------------------------
    def assert_startable(self, job_id: str) -> dict[str, Any]:
        """开工前的统一闸门。

        这里重新解析一遍 `exempt_pages_raw`，而不是信任 `exempt_pages`：
        配置文件被手工改过、或者页数与豁免页对不上时，必须在这里拦住，
        不能让非法页码带着进入流水线。
        """
        job = self.get_job(job_id)
        status = job["status"]

        if status in ("queued", "running"):
            raise AppError(
                ErrorCode.JOB_ALREADY_STARTED,
                "任务已经在处理中",
                details={"status": status},
                next_action="请等待当前处理结束后查看结果",
                http_status=409,
            )
        if status in TERMINAL_JOB_STATUSES:
            raise AppError(
                ErrorCode.JOB_ALREADY_STARTED,
                "任务已结束，不能再次开始",
                details={"status": status},
                next_action="如需重新处理，请在历史中重新发起一次处理",
                http_status=409,
            )
        if status == "awaiting_tag_decision":
            raise AppError(
                ErrorCode.INVALID_CONFIG,
                "还没有选择如何处理标签",
                details={"tag_status": job["tag_detection"]["status"]},
                next_action="请选择“生成标签”或“跳过标签”后再开始",
                http_status=400,
            )

        page_count = int(job["source"]["page_count"])
        config = dict(job["config"])
        spec = parse_page_spec(config.get("exempt_pages_raw"), page_count)
        if spec.as_list() != list(config.get("exempt_pages") or []):
            config["exempt_pages"] = spec.as_list()
            config["exempt_pages_raw"] = spec.normalized
            job["config"] = config
            self._write_job(job)

        if config.get("tag_choice") is None:
            raise AppError(
                ErrorCode.INVALID_CONFIG,
                "还没有选择如何处理标签",
                details={"tag_status": job["tag_detection"]["status"]},
                next_action="请选择“生成标签”或“跳过标签”后再开始",
                http_status=400,
            )

        source = self.storage.job_file(
            self.ctx.generation_id, job_id, SOURCE_FILE_NAME
        )
        if not source.is_file():
            raise AppError(
                ErrorCode.SOURCE_MISSING,
                "原始文件已不存在，无法开始处理",
                details={"job_id": job_id},
                next_action="请重新上传该文件",
                http_status=404,
            )
        return job

    # ---- 入队 -----------------------------------------------------------
    def enqueue(self, job: dict[str, Any]) -> dict[str, Any]:
        """把任务从 `configuring` 迁到 `queued` 并交给后台队列。

        校验与迁移在同一把锁里完成：两个并发的 `POST /start` 只有一个能改到
        `queued`，另一个会在 `assert_transition` 上收到 409，不会产生第二个任务。
        """
        with self._guard:
            fresh = self.get_job(job["id"])
            fresh = state_machine.transition(fresh, "queued")
            fresh["current_stage"] = None
            fresh["last_error"] = None
            self._write_job(fresh)
            self._append_event(fresh["id"], kind="status", status="queued")
            self.ctx.worker.submit(fresh["id"])
            return fresh

    # ---- 执行期的状态写入（只给 JobWorker 用） ---------------------------
    def work_paths(self, job_id: str) -> tuple[Path, Path]:
        """返回 (源文件, 待发布输出)。待发布文件通过最终验证后才会改名。"""
        directory = self._job_dir(job_id)
        return directory / SOURCE_FILE_NAME, directory / PENDING_OUTPUT_NAME

    def mark_running(self, job_id: str) -> dict[str, Any]:
        with self._guard:
            job = self.get_job(job_id)
            job = state_machine.transition(job, "running")
            job["current_stage"] = "preparing"
            self._write_job(job)
            self._append_event(job_id, kind="status", status="running")
            self._append_event(job_id, kind="stage", stage="preparing")
            return job

    def record_stage(self, job_id: str, stage: str) -> dict[str, Any]:
        """记录一个真实阶段名。这里**不接受**任何百分比或进度数值。"""
        if stage not in STAGE_NAMES:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "未知阶段名",
                details={"stage": stage, "known": list(STAGE_NAMES)},
                next_action="请查看 var/logs/app.log",
                http_status=500,
            )
        with self._guard:
            job = self.get_job(job_id)
            if job["status"] != "running":
                # 任务已被中断（例如重启回收），不再往里面写阶段
                return job
            if job.get("current_stage") == stage:
                # 流水线第一句就是 "preparing"，`mark_running` 已经记过一次。
                # 重复的阶段名不该在事件流里出现两遍，否则时间线会像卡了一下。
                return job
            job["current_stage"] = stage
            self._write_job(job)
            self._append_event(job_id, kind="stage", stage=stage)
            return job

    def finish_success(self, job_id: str, result, pending: Path) -> dict[str, Any]:
        """流水线成功：发布输出文件、写结果、落报告。"""
        with self._guard:
            job = self.get_job(job_id)
            if job["status"] != "running":
                # 跑的过程中任务已被回收（例如另一个进程重启后标成中断）：
                # 半成品一律丢掉，绝不发布，也绝不覆盖已经写好的终态。
                pending.unlink(missing_ok=True)
                self.log.warning(
                    "任务 %s 已不在 running（当前 %s），放弃发布输出",
                    job_id,
                    job["status"],
                )
                return job
            directory = self._job_dir(job_id)
            final_name = build_output_name(job["original_name"], job_id)
            target = atomic.unique_path(directory, final_name)
            os.replace(pending, target)
            atomic.fsync_dir(directory)

            job["results"] = result.results
            job["output"] = {
                "file_name": target.name,
                "sha256": result.output_sha256,
                "size": result.size,
                "page_count": result.page_count,
                "verified": True,
            }
            job["last_error"] = None
            job = state_machine.transition(job, result.overall_status)
            self._write_job(job)
            self._append_event(job_id, kind="status", status=job["status"])
            self.write_report(job)
            self.log.info(
                "任务 %s 完成：%s（%d 项结果）", job_id, job["status"], len(result.results)
            )
            return job

    def finish_failed(self, job_id: str, error: BaseException, *, stage: str | None = None) -> dict[str, Any]:
        """任何失败都要落到 job.json，绝不让任务停在 `running`。"""
        with self._guard:
            job = self.get_job(job_id)
            if job["status"] in TERMINAL_JOB_STATUSES:
                # 已经收尾过（例如被 reconcile 标记中断），不覆盖真实终态
                return job
            if job["status"] != "running":
                # 连 running 都没进入：退回 queued 不合理，直接按失败收尾
                job = state_machine.transition(job, "running")

            code, message, details, retryable, next_action = _describe_failure(error)
            job["last_error"] = {
                "code": code,
                "message": message,
                "details": details,
                "retryable": retryable,
                "next_action": next_action,
                "stage": stage or job.get("current_stage"),
            }
            job = state_machine.transition(job, "failed")
            self._write_job(job)
            self._append_event(job_id, kind="status", status="failed")
            self.write_report(job)
            return job

    # ---- 事件 -----------------------------------------------------------
    def _events_path(self, job_id: str) -> Path:
        return self.storage.job_file(self.ctx.generation_id, job_id, EVENTS_FILE_NAME)

    def _next_seq(self, job_id: str) -> int:
        last = 0
        for record in atomic.read_jsonl(self._events_path(job_id)):
            seq = record.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool) and seq > last:
                last = seq
        return last + 1

    def _append_event(
        self,
        job_id: str,
        *,
        kind: str,
        stage: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """追加一条事件。seq 单调递增，内容只有阶段名/状态名。"""
        record: dict[str, Any] = {
            "seq": self._next_seq(job_id),
            "type": kind,
            "at": utcnow_iso(),
        }
        if stage is not None:
            record["stage"] = stage
            record["label"] = STAGE_LABELS.get(stage, stage)
        if status is not None:
            record["status"] = status
        atomic.append_jsonl(self._events_path(job_id), record)
        return record

    def list_events(self, job_id: str, after: int = 0) -> dict[str, Any]:
        """`events?after=seq`：只返回 seq 更大的事件。"""
        self.get_job(job_id)
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise AppError(
                ErrorCode.INVALID_CONFIG,
                "after 必须是非负整数",
                details={"after": repr(after)},
                next_action="请刷新页面后重试",
                http_status=400,
            )
        events = [
            record
            for record in atomic.read_jsonl(self._events_path(job_id))
            if isinstance(record.get("seq"), int) and record["seq"] > after
        ]
        return {
            "job_id": job_id,
            "events": events,
            "last_seq": events[-1]["seq"] if events else after,
        }

    # ---- 报告 -----------------------------------------------------------
    def _report_path(self, job_id: str) -> Path:
        return self.storage.job_file(self.ctx.generation_id, job_id, REPORT_FILE_NAME)

    def build_report(self, job: dict[str, Any]) -> dict[str, Any]:
        """由 job.json 现场推导报告。不额外发明事实，只做汇总与标注。"""
        results = []
        tally = {key: 0 for key in ("adjusted", "unchanged", "not_applicable", "review_required", "failed")}
        warnings: list[str] = []
        for item in job.get("results") or []:
            status = item.get("status")
            if status in tally:
                tally[status] += 1
            for warning in item.get("warnings") or []:
                warnings.append(f"{item.get('title', item.get('key'))}：{warning}")
            results.append(
                {
                    "key": item.get("key"),
                    "title": item.get("title"),
                    "status": status,
                    "status_label": RESULT_STATUS_LABELS.get(status, status),
                    "status_icon": RESULT_STATUS_ICONS.get(status, ""),
                    "before": item.get("before"),
                    "after": item.get("after"),
                    "counts": {key: item.get("counts", {}).get(key, 0) for key in COUNT_KEYS},
                    "reason_code": item.get("reason_code"),
                    "message": item.get("message", ""),
                    "warnings": list(item.get("warnings") or []),
                    "next_action": item.get("next_action"),
                }
            )

        return {
            "schema_version": SCHEMA_VERSION,
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "generation_id": job.get("generation_id"),
            "job_id": job.get("id"),
            "generated_at": utcnow_iso(),
            "original_name": job.get("original_name"),
            "status": job.get("status"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "source": job.get("source"),
            "output": job.get("output"),
            "config": job.get("config"),
            "tag_detection": job.get("tag_detection"),
            "last_error": job.get("last_error"),
            "summary": tally,
            "results": results,
            "warnings": warnings,
            "disclaimer": DISCLAIMER,
        }

    def write_report(self, job: dict[str, Any]) -> dict[str, Any]:
        report = self.build_report(job)
        atomic.atomic_write_json(self._report_path(job["id"]), report)
        return report

    def get_report(self, job_id: str) -> dict[str, Any]:
        """读报告。文件不在（旧任务/被删）就按 job.json 重建并落盘。"""
        job = self.get_job(job_id)
        stored = atomic.read_json(self._report_path(job_id), default=None)
        if isinstance(stored, dict) and stored.get("job_id") == job_id:
            return stored
        return self.write_report(job)

    # ---- 中断回收 -------------------------------------------------------
    def reconcile_interrupted(self) -> list[str]:
        """启动时把残留的 `queued` / `running` 标记为 `interrupted`。

        进程被杀之后 job.json 里会留下一个永远不动的 `running`——界面会一直
        显示"处理中"。这里如实把它改成中断，并丢掉半成品输出文件。
        """
        reconciled: list[str] = []
        for job_id in self.storage.list_job_ids(self.ctx.generation_id):
            raw = atomic.read_json(self._job_path(job_id), default=None)
            if not isinstance(raw, dict):
                continue
            previous = raw.get("status")
            if previous not in ("queued", "running"):
                continue
            job = raw
            try:
                job = state_machine.transition(job, "interrupted")
            except AppError:  # pragma: no cover - 白名单已覆盖这两种状态
                self.log.warning("任务 %s 无法标记为中断，状态为 %s", job_id, previous)
                continue
            job["last_error"] = {
                "code": "interrupted",
                "message": "上次运行在处理过程中被中断，结果不可用",
                "details": {"previous_status": previous},
                "retryable": True,
                "next_action": NEXT_ACTION_INTERRUPTED,
                "stage": raw.get("current_stage"),
            }
            try:
                self._write_job(job)
            except Exception:  # noqa: BLE001 - 单个坏任务不能拖垮启动
                self.log.exception("任务 %s 的中断状态写入失败", job_id)
                continue
            self._append_event(job_id, kind="status", status="interrupted")
            (self._job_dir(job_id) / PENDING_OUTPUT_NAME).unlink(missing_ok=True)
            reconciled.append(job_id)
        if reconciled:
            self.log.info("已把 %d 个残留任务标记为中断", len(reconciled))
        return reconciled

    # ---- 重新处理 -------------------------------------------------------
    def reprocess(self, job_id: str) -> dict[str, Any]:
        """按原配置复制出一个**全新 ID** 的任务，原件与产物路径完全独立。

        实现放在 `HistoryService`：这是「历史里那一条还能不能再来一次」的问题，
        判断依据（原件在不在）与历史列表用的是同一份，不该有两处口径。
        """
        return self.ctx.history.reprocess(job_id)

    # ---- 输出文件 -------------------------------------------------------
    def output_path(self, job_id: str) -> Path:
        """返回已通过最终验证的输出文件路径。

        - 还没跑完 / 还没发布：409 `output_not_ready`；
        - 发布过但文件被移走：404 `output_missing`。
        两种情况都不返回半成品，也不会去猜文件名。
        """
        job = self.get_job(job_id)
        output = job.get("output")
        if not isinstance(output, dict) or not output.get("file_name"):
            raise AppError(
                ErrorCode.OUTPUT_NOT_READY,
                "输出文件还没有生成",
                details={"job_id": job_id, "status": job.get("status")},
                retryable=True,
                next_action="请等待处理完成后再下载",
                http_status=409,
            )
        if job.get("status") not in TERMINAL_JOB_STATUSES:
            raise AppError(
                ErrorCode.OUTPUT_NOT_READY,
                "任务还没有结束，输出文件暂不可下载",
                details={"job_id": job_id, "status": job.get("status")},
                retryable=True,
                next_action="请等待处理完成后再下载",
                http_status=409,
            )
        path = self.storage.job_file(
            self.ctx.generation_id, job_id, str(output["file_name"])
        )
        if not path.is_file():
            raise AppError(
                ErrorCode.OUTPUT_MISSING,
                "输出文件已不存在",
                details={"job_id": job_id, "file_name": output["file_name"]},
                next_action="查看结果详情，或按原配置重新处理",
                http_status=404,
            )
        return path

    def source_path(self, job_id: str) -> Path:
        self.get_job(job_id)
        path = self.storage.job_file(self.ctx.generation_id, job_id, SOURCE_FILE_NAME)
        if not path.is_file():
            raise AppError(
                ErrorCode.SOURCE_MISSING,
                "原始文件已不存在",
                details={"job_id": job_id},
                next_action="请重新上传该文件",
                http_status=404,
            )
        return path


def _describe_failure(error: BaseException) -> tuple[str, str, dict, bool, str]:
    """把任意异常翻成统一错误模型需要的五元组。"""
    if isinstance(error, AppError):
        return (
            error.code,
            error.message,
            dict(error.details),
            error.retryable,
            error.next_action or "请重试一次；若仍失败，请查看 var/logs/app.log",
        )

    reason_code = getattr(error, "reason_code", None)
    problems = list(getattr(error, "problems", ()) or ())
    if reason_code:
        return (
            str(reason_code),
            str(error) or "处理未能完成",
            {"problems": problems},
            False,
            "请确认源文件完整后重试；其他处理项不受影响",
        )

    return (
        "internal_error",
        "处理过程中发生未预期的错误",
        {"type": type(error).__name__, "message": str(error)},
        True,
        "重试一次；若仍失败，请查看 var/logs/app.log",
    )
