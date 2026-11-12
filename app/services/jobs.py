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
from app.core.migrate import is_legacy, migrate_job
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
from app.pdf.content_fingerprint import describe_integrity
from app.pdf.inspector import inspect_pdf
from app.schemas import (
    COUNT_KEYS,
    DEFAULT_PROCESS_GROUPS,
    PROCESS_GROUPS,
    PROCESS_JOB_STATUSES,
    RESULT_STATUS_ICONS,
    RESULT_STATUS_LABELS,
    STAGE_LABELS,
    STAGE_NAMES,
    TERMINAL_JOB_STATUSES,
    VERIFY_JOB_STATUSES,
    VERIFY_KEYS,
    default_config,
    default_tag_detection,
    validate_job,
)
from app.services.verify import PHASE_POST, PHASE_PRECHECK

SOURCE_FILE_NAME = "original.pdf"
EVENTS_FILE_NAME = "events.jsonl"
REPORT_FILE_NAME = "report.json"
PROCESS_LOG_FILE_NAME = "process-log.html"

# 流水线先写到这里；通过最终验证后才改名为正式输出名。
# 好处：`file?kind=output` 只要看不到 `job["output"]` 就一定还没验证通过。
PENDING_OUTPUT_NAME = ".pending-output.pdf"

DISCLAIMER = "本报告由程序自动生成，结果供人工复核，不构成合规结论。"

NEXT_ACTION_CONFIG = "请检查页码格式后重新保存"
NEXT_ACTION_INTERRUPTED = "在历史中按原配置重新处理该文件"


# --------------------------------------------------------------------------- #
# v2.1 -> v4.0 惰性迁移（R31）
# --------------------------------------------------------------------------- #
# 实现放在 `app/core/migrate.py`：历史索引（core.storage）也要用同一份逻辑，
# 写在这里会与它形成循环导入。这里只做再导出，调用方不必关心位置。


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

    def _read_job(self, job_id: str) -> dict[str, Any] | None:
        """读 job.json 并**在内存里**完成 v1→v2 迁移。

        所有读取都必须经过这里，否则老任务会带着缺字段的字典流进各条业务线，
        每一处都得自己 `if key not in job`——那是迟早会漏的写法。
        迁移不回写磁盘（R31），落盘只发生在用户真的动了这个任务之后。
        """
        raw = atomic.read_json(self._job_path(job_id), default=None)
        if not isinstance(raw, dict):
            return None
        return migrate_job(raw)

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

            atomic.replace_with_retry(tmp, target)
            return total, hasher.hexdigest()
        finally:
            tmp.unlink(missing_ok=True)

    # ---- 创建 -----------------------------------------------------------
    def create_job(
        self, filename: str, stream: BinaryIO, batch_id: str | None = None
    ) -> dict[str, Any]:
        if batch_id is not None:
            validate_id(batch_id, PREFIX_BATCH)
        else:
            # v4.0 里没有「无批次」的任务：准备页、校验结果页、处理页的路由
            # 全是 `:batchId`。单文件上传就是 1 个文件的批次，否则那个任务
            # 从建出来那一刻起就进不了任何一页。
            batch_id = new_batch_id()
        generation_id = self.ctx.generation_id
        job_id = new_job_id()
        directory = self.storage.job_dir(generation_id, job_id, create=True)
        try:
            size, digest = self._store_original(directory, stream)
            facts = inspect_pdf(directory / SOURCE_FILE_NAME, self.limits)

            # v4.0：上传只做「落盘 + 读出事实」，不再做标签检测（R30）。
            # 新任务一律停在 `configuring`，由准备页决定先校验还是直接处理。
            job: dict[str, Any] = {
                "id": job_id,
                "schema_version": SCHEMA_VERSION,
                "status": "configuring",
                "phase": "precheck",
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
                "config": default_config(),
                # 只读历史字段：v4.0 不再检测、不再展示，但保留结构以便
                # 前端与备份格式保持一致（R30「保留不做删除」）。
                "tag_detection": default_tag_detection(),
                "verify": None,
                "post_verify": None,
                "process_log_path": None,
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
            job = self._read_job(job_id)
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
        verify_items: list[str] | None = None,
        skip_precheck: bool | None = None,
        process_items: list[str] | None = None,
        write_process_log: bool | None = None,
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
                    verify_items=verify_items,
                    skip_precheck=skip_precheck,
                    process_items=process_items,
                    write_process_log=write_process_log,
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
        job = self._read_job(job_id)
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
    #: 允许修改配置的状态。`verified` 也在内：用户校验完看完结果，可能想
    #: 改处理项再处理，强制要求"重来一遍校验"没有意义。
    CONFIGURABLE_STATUSES = ("configuring", "verified")

    def update_config(
        self,
        job_id: str,
        *,
        exempt_pages_raw: str | None = None,
        verify_items: list[str] | None = None,
        skip_precheck: bool | None = None,
        process_items: list[str] | None = None,
        write_process_log: bool | None = None,
    ) -> dict[str, Any]:
        """更新准备页配置。传 `None` 表示"这一项不动"。

        v4.0 的配置分两块：校验项（`verify_items` + `skip_precheck`）与
        处理项（`process_items` + `write_process_log`）。空列表**在这里放行**，
        由各自入口再拦（`no_verify_items` / `no_process_items`）——
        用户完全可以先清空校验项、只做处理，或者反过来。
        """
        job = self.get_job(job_id)
        if job["status"] not in self.CONFIGURABLE_STATUSES:
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

        if verify_items is not None:
            config["verify_items"] = self._validate_items(
                verify_items, VERIFY_KEYS, "校验项", job_id
            )

        if skip_precheck is not None:
            config["skip_precheck"] = bool(skip_precheck)

        if process_items is not None:
            config["process_items"] = self._validate_items(
                process_items, PROCESS_GROUPS, "处理项", job_id
            )

        if write_process_log is not None:
            config["write_process_log"] = bool(write_process_log)

        job["config"] = config
        self._write_job(job)
        return job

    @staticmethod
    def _validate_items(
        items: list[str], allowed: tuple[str, ...], label: str, job_id: str
    ) -> list[str]:
        """按 `allowed` 的声明顺序去重排序。

        顺序不能跟着用户勾选顺序走：`process_items` 展开后 `fast_web_view`
        必须在最后，否则线性化会被后续改写打乱（R25 / 流水线不变量）。
        """
        if not isinstance(items, list):
            raise AppError(
                ErrorCode.INVALID_CONFIG,
                f"{label}必须是列表",
                details={"job_id": job_id, "value": items},
                next_action="请从界面给出的选项中选择",
                http_status=400,
            )
        unknown = [item for item in items if item not in allowed]
        if unknown:
            raise AppError(
                ErrorCode.INVALID_CONFIG,
                f"{label}不在允许范围内",
                details={"job_id": job_id, "unknown": unknown, "allowed": list(allowed)},
                next_action="请从界面给出的选项中选择",
                http_status=400,
            )
        picked = set(items)
        return [key for key in allowed if key in picked]

    # ---- 开始处理前的最后一道校验 ---------------------------------------
    def assert_startable(self, job_id: str) -> dict[str, Any]:
        """开工前的统一闸门。

        这里重新解析一遍 `exempt_pages_raw`，而不是信任 `exempt_pages`：
        配置文件被手工改过、或者页数与豁免页对不上时，必须在这里拦住，
        不能让非法页码带着进入流水线。
        """
        job = self.get_job(job_id)
        status = job["status"]

        if status in ("verify_queued", "verifying"):
            raise AppError(
                ErrorCode.JOB_ALREADY_STARTED,
                "任务正在校验中",
                details={"status": status},
                next_action="请等待校验结束后再开始处理",
                http_status=409,
            )
        if status in ("queued", "running", "process_queued", "processing", "post_verifying"):
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

        page_count = int(job["source"]["page_count"])
        config = dict(job["config"])

        # 分支 A 要求先校验再处理（R13）。判定依据是「有没有校验结果」而不是
        # 状态值：`verified` 只是结果存在时的一个快照，拿它当门槛会让
        # 「校验完了又改配置」这种合法动线卡住。
        if not config.get("skip_precheck") and job.get("verify") is None:
            raise AppError(
                ErrorCode.VERIFY_NOT_READY,
                "还没有执行前置校验",
                details={"job_id": job_id, "skip_precheck": False},
                next_action="请先执行校验，或勾选「不执行前置校验」后再开始处理",
                http_status=409,
            )

        spec = parse_page_spec(config.get("exempt_pages_raw"), page_count)
        if spec.as_list() != list(config.get("exempt_pages") or []):
            config["exempt_pages"] = spec.as_list()
            config["exempt_pages_raw"] = spec.normalized
            job["config"] = config
            self._write_job(job)

        if not config.get("process_items"):
            raise AppError(
                ErrorCode.NO_PROCESS_ITEMS,
                "没有勾选任何处理项",
                details={"job_id": job_id},
                next_action="请至少勾选一项处理内容后再开始",
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
        """把任务迁到 `process_queued` 并交给后台队列。

        v4.0 里「开始处理」只负责排队，不负责校验：校验走
        `POST /verify`（`verify_queued`）。两段各有一个入队口，队列仍是同一个，
        严格串行执行。

        校验与迁移在同一把锁里完成：两个并发的 `POST /start` 只有一个能改到
        `process_queued`，另一个会在 `assert_transition` 上收到 409，
        不会产生第二个任务。
        """
        with self._guard:
            fresh = self.get_job(job["id"])
            fresh = state_machine.transition(fresh, "process_queued")
            fresh["current_stage"] = None
            fresh["last_error"] = None
            self._write_job(fresh)
            self._append_event(fresh["id"], kind="status", status="process_queued")
            self.ctx.worker.submit(fresh["id"])
            return fresh

    # ---- 前置校验（R13） -------------------------------------------------
    def assert_verifiable(self, job_id: str) -> dict[str, Any]:
        """提交校验前的闸门。

        与 `assert_startable` 是**两个**闸门而不是一个：它们挡的东西不同，
        一个挡"还没校验就想处理"，一个挡"正在处理还想校验"。
        """
        job = self.get_job(job_id)
        status = job["status"]

        if status in VERIFY_JOB_STATUSES:
            raise AppError(
                ErrorCode.JOB_ALREADY_STARTED,
                "任务正在校验中",
                details={"status": status},
                next_action="请等待本次校验结束后查看结果",
                http_status=409,
            )
        if status in PROCESS_JOB_STATUSES:
            raise AppError(
                ErrorCode.JOB_ALREADY_STARTED,
                "任务正在处理中，不能同时校验",
                details={"status": status},
                next_action="请等待处理结束后查看结果",
                http_status=409,
            )
        if status in TERMINAL_JOB_STATUSES:
            raise AppError(
                ErrorCode.JOB_ALREADY_STARTED,
                "任务已结束，不能再次校验",
                details={"status": status},
                next_action="如需重新校验，请重新上传该文件",
                http_status=409,
            )

        if not job["config"].get("verify_items"):
            raise AppError(
                ErrorCode.NO_VERIFY_ITEMS,
                "没有勾选任何校验项",
                details={"job_id": job_id},
                next_action="请至少勾选一项校验，或勾选「不执行前置校验」直接进入处理",
                http_status=400,
            )

        source = self.storage.job_file(
            self.ctx.generation_id, job_id, SOURCE_FILE_NAME
        )
        if not source.is_file():
            raise AppError(
                ErrorCode.SOURCE_MISSING,
                "原始文件已不存在，无法校验",
                details={"job_id": job_id},
                next_action="请重新上传该文件",
                http_status=404,
            )
        return job

    def enqueue_verify(self, job: dict[str, Any]) -> dict[str, Any]:
        """迁到 `verify_queued` 并入队。与 `enqueue` 对称，同一把锁、同一个队列。"""
        with self._guard:
            fresh = self.get_job(job["id"])
            fresh = state_machine.transition(fresh, "verify_queued")
            fresh["phase"] = "precheck"
            fresh["current_stage"] = None
            fresh["last_error"] = None
            self._write_job(fresh)
            self._append_event(fresh["id"], kind="status", status="verify_queued")
            self.ctx.worker.submit(fresh["id"])
            return fresh

    def submit_batch_verify(self, batch_id: str) -> dict[str, Any]:
        """整批提交校验。能提交的入队，不能提交的列进 `skipped`。

        与 `start_batch` 同样的取舍：一个文件没勾校验项，不该把同批另外 19 个
        也拦下来——那样用户只能一个个点，批量入口就失去意义。
        """
        jobs = self.batch_jobs(batch_id)
        started: list[str] = []
        skipped: list[dict[str, Any]] = []
        for job in jobs:
            try:
                fresh = self.assert_verifiable(job["id"])
                self.enqueue_verify(fresh)
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

    def mark_verifying(self, job_id: str) -> dict[str, Any]:
        with self._guard:
            job = self.get_job(job_id)
            job = state_machine.transition(job, "verifying")
            job["phase"] = "precheck"
            job["current_stage"] = "precheck"
            job["started_at"] = job.get("started_at") or utcnow_iso()
            self._write_job(job)
            self._append_event(job_id, kind="status", status="verifying")
            self._append_event(job_id, kind="stage", stage="precheck")
            return job

    def record_verify_progress(
        self,
        job_id: str,
        *,
        key: str,
        status: str,
        index: int,
        total: int,
        phase: str = PHASE_PRECHECK,
    ) -> None:
        """记一项校验完成。

        事件里只有"第几项/共几项"和这一项的状态，**没有百分比**——
        与处理阶段的进度口径保持一致：界面显示的是真实发生过的事。

        后置校验用另一个事件名（`post_verify_progress`，§9.1）：两段校验
        可能先后出现在同一条时间线上，共用一个名字会让前端没法区分
        "这是处理前那一轮"还是"这是处理完那一轮"。
        """
        expected = "verifying" if phase == PHASE_PRECHECK else "post_verifying"
        with self._guard:
            job = self.get_job(job_id)
            if job["status"] != expected:
                # 已被回收/中断，不再往里写
                return
            self._append_event(
                job_id,
                kind=(
                    "verify_progress" if phase == PHASE_PRECHECK else "post_verify_progress"
                ),
                item={"key": key, "status": status, "index": index, "total": total},
            )

    def finish_verified(self, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """校验完成：结果落到 job.json，状态进 `verified`。

        落盘而不是留在内存里，是为了 AC-R13-E3：用户关掉页面再打开，
        结果必须还能从后端读出来，且**不重新跑一遍**校验。
        """
        with self._guard:
            job = self.get_job(job_id)
            job["verify"] = result
            job["current_stage"] = None
            job = state_machine.transition(job, "verified")
            self._write_job(job)
            self._append_event(job_id, kind="status", status="verified")
            self.log.info(
                "任务 %s 校验完成：%s", job_id, _verify_brief(result)
            )
            return job

    def batch_verify_report(self, batch_id: str, *, phase: str = "precheck") -> dict[str, Any]:
        """批次级校验汇总（§8.4）。数据全部来自 job.json，不重新跑校验。"""
        jobs = self.batch_jobs(batch_id)
        field = "verify" if phase == "precheck" else "post_verify"
        files = [
            job[field]
            for job in jobs
            if isinstance(job.get(field), dict)
        ]
        if not files:
            raise AppError(
                ErrorCode.VERIFY_REPORT_MISSING,
                "还没有校验结果",
                details={"batch_id": batch_id, "phase": phase},
                next_action="请先执行校验后再查看报告",
                http_status=404,
            )
        summary: dict[str, int] = {"files": len(files)}
        for item in files:
            for status, count in (item.get("summary") or {}).items():
                summary[status] = summary.get(status, 0) + int(count)
        return {
            "batch_id": batch_id,
            "phase": phase,
            "generated_at": utcnow_iso(),
            "files": files,
            "summary": summary,
        }

    def file_verify_report(self, job_id: str, *, phase: str = "precheck") -> dict[str, Any]:
        """单文件校验结果（§8.3）。供页面 3 / 页面 4 的「查看报告」使用。

        与批次版不同，这里**不做字段挑选**：用户点的是某一个文件的报告，
        拿到的就该是这个文件那一份原始结果，不多不少。
        """
        job = self.get_job(job_id)
        field = "verify" if phase == "precheck" else "post_verify"
        result = job.get(field)
        if not isinstance(result, dict):
            raise AppError(
                ErrorCode.VERIFY_REPORT_MISSING,
                "这个文件还没有校验结果",
                details={"job_id": job_id, "phase": phase},
                next_action=(
                    "请先点「开始校验」"
                    if phase == "precheck"
                    else "后置校验在处理完成后自动执行，请先完成处理"
                ),
                http_status=404,
            )
        return result

    def record_process_log(self, job_id: str, name: str | None) -> dict[str, Any]:
        """记下处理日志文件名（相对任务目录，R28）。

        只记名字不记绝对路径：var 目录可以被整体搬走或复制到另一台机器，
        绝对路径一搬就失效，而任务目录内的相对名永远跟着任务走。
        传 `None` 表示「本次没有日志」——日志生成失败（AC-R28-E1）时用得上。
        """
        with self._guard:
            job = self.get_job(job_id)
            job["process_log_path"] = name
            self._write_job(job)
            return job

    def batch_process_log(self, batch_id: str) -> Path:
        """返回批次**最新一份**处理日志文件的路径（R28）。

        为什么取最新而不是唯一：日志是「批次完成时」的产物，而 worker 是一个
        任务一个任务收尾的。每个任务收尾时都会把**整批**的现状重渲染一遍写进
        自己的目录，所以越晚写的那份越完整 —— 取 mtime 最大的那个，用户拿到的
        就是最接近「批次完成」的那一版。

        路径从 `job["process_log_path"]`（相对任务目录）解析，并强制落在本次
        生成的 jobs 根目录之内：job.json 是磁盘上的普通 JSON，被手工改过也不能
        让它把任意文件当日志吐出去。
        """
        jobs = self.batch_jobs(batch_id)
        root = self.storage.jobs_root(self.ctx.generation_id).resolve()
        best: tuple[float, Path] | None = None
        for job in jobs:
            name = job.get("process_log_path")
            if not name:
                continue
            candidate = Path(name)
            if not candidate.is_absolute():
                candidate = self._job_dir(job["id"]) / candidate
            try:
                resolved = candidate.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            mtime = resolved.stat().st_mtime
            if best is None or mtime > best[0]:
                best = (mtime, resolved)

        if best is None:
            raise AppError(
                ErrorCode.PROCESS_LOG_DISABLED,
                "本次处理没有生成日志",
                details={"batch_id": batch_id},
                next_action="如需日志，请在处理页勾选「生成处理日志」后重新处理",
                http_status=404,
            )
        return best[1]

    def batch_results(self, batch_id: str) -> dict[str, Any]:
        """批次处理 + 后置校验结果（页面 4 数据源）。

        与 `batch_verify_report` 的关键差别：那边**只挑有校验结果的**文件，
        因为报告里出现一份空表会让人以为校验跑过了；这边**每个文件都要出现**，
        哪怕它失败了、还没开始、或者根本没有后置校验——页面 4 的头一行是
        「成功 X / 失败 Y」，少算一个文件，两个数字就对不上总数。

        每文件内容直接复用 `build_report`，与结果页、report.json 三处口径完全
        一致：页面之间对同一个文件给出不同的计数是绝对不能接受的事。
        """
        jobs = self.batch_jobs(batch_id)
        files = [self.build_report(job) for job in jobs]

        summary = {"files": len(files), "succeeded": 0, "failed": 0, "pending": 0}
        for item in files:
            status = item.get("status")
            # 只认三种落点：做完了 / 没做成 / 还没到终点。
            # 用白名单而不是"非终态即 pending"，是因为将来加状态时
            # 猜错的代价是「已完成的文件被算成待处理」，那会误导用户重跑。
            if status in ("completed", "completed_with_warnings"):
                summary["succeeded"] += 1
            elif status in ("failed", "interrupted"):
                summary["failed"] += 1
            else:
                summary["pending"] += 1

        return {
            "batch_id": batch_id,
            "generated_at": utcnow_iso(),
            "summary": summary,
            "files": files,
        }

    # ---- 执行期的状态写入（只给 JobWorker 用） ---------------------------
    def work_paths(self, job_id: str) -> tuple[Path, Path]:
        """返回 (源文件, 待发布输出)。待发布文件通过最终验证后才会改名。"""
        directory = self._job_dir(job_id)
        return directory / SOURCE_FILE_NAME, directory / PENDING_OUTPUT_NAME

    def mark_running(self, job_id: str) -> dict[str, Any]:
        with self._guard:
            job = self.get_job(job_id)
            job = state_machine.transition(job, "processing")
            job["phase"] = "process"
            job["current_stage"] = "preparing"
            self._write_job(job)
            self._append_event(job_id, kind="status", status="processing")
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
            if job["status"] != "processing":
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

    def begin_post_verify(self, job_id: str) -> dict[str, Any]:
        """处理阶段收尾：`processing` → `post_verifying`。

        后置校验（R27）是强制步骤，`processing` 没有直达终态的边。
        """
        with self._guard:
            job = self.get_job(job_id)
            job = state_machine.transition(job, "post_verifying")
            job["current_stage"] = "post_verifying"
            self._write_job(job)
            self._append_event(job_id, kind="status", status="post_verifying")
            self._append_event(job_id, kind="stage", stage="post_verifying")
            return job

    def finish_post_verify(self, job_id: str, result: dict[str, Any] | None) -> dict[str, Any]:
        """后置校验结果落盘（R27）。只写结果，不改状态。

        状态由随后的 `finish_success` 决定：校验通过就按处理项的结论收尾，
        整块失败就降级成 `completed_with_warnings`（AC-R27-E1）。把这两件事
        分开，是为了让"结果长什么样"和"任务算不算成功"各自只有一处判断。
        """
        with self._guard:
            job = self.get_job(job_id)
            if job["status"] != "post_verifying":
                # 已被回收/中断：半成品结果不写进终态任务
                return job
            job["post_verify"] = result
            self._write_job(job)
            return job

    def finish_success(self, job_id: str, result, pending: Path, *, status: str | None = None) -> dict[str, Any]:
        """后置校验通过：发布输出文件、写结果、落报告。

        `status` 用来覆盖终态。正常情况取 `result.overall_status`（由处理项的
        结果决定）；后置校验整块失败时（AC-R27-E1）产物照样要发布——处理本身
        是成功的——但结论必须降级成 `completed_with_warnings`，不能让一个
        "没通过最终校验的文件"顶着 `completed` 出去。
        """
        with self._guard:
            job = self.get_job(job_id)
            if job["status"] != "post_verifying":
                # 跑的过程中任务已被回收（例如另一个进程重启后标成中断）：
                # 半成品一律丢掉，绝不发布，也绝不覆盖已经写好的终态。
                pending.unlink(missing_ok=True)
                self.log.warning(
                    "任务 %s 已不在 post_verifying（当前 %s），放弃发布输出",
                    job_id,
                    job["status"],
                )
                return job
            directory = self._job_dir(job_id)
            final_name = build_output_name(job["original_name"], job_id)
            target = atomic.unique_path(directory, final_name)

            # 发布是有阶段事件的：改名之后文件才对用户可见。
            job["current_stage"] = "publishing"
            self._write_job(job)
            self._append_event(job_id, kind="stage", stage="publishing")

            atomic.replace_with_retry(pending, target)
            atomic.fsync_dir(directory)

            job["results"] = result.results
            # R36 内容一致性结论。放在 job.json 里而不是只挂在内存对象上，
            # 是因为 `build_report` 是从 job.json 现场推导的（§R28「一份数据源」）：
            # 不落盘，报告和结果页就永远看不到这一行，AC-R36-E2 要求显示的
            # 「内容一致性未能核对」也就无处可来。
            integrity = getattr(result, "content_integrity", None)
            job["content_integrity"] = (
                integrity.as_dict() if hasattr(integrity, "as_dict") else None
            )
            job["output"] = {
                "file_name": target.name,
                "sha256": result.output_sha256,
                "size": result.size,
                "page_count": result.page_count,
                "verified": True,
            }
            job["last_error"] = None
            job = state_machine.transition(job, status or result.overall_status)
            self._write_job(job)
            self._append_event(job_id, kind="status", status=job["status"])
            self.write_report(job)
            self.log.info(
                "任务 %s 完成：%s（%d 项结果）", job_id, job["status"], len(result.results)
            )
            return job

    def finish_failed(self, job_id: str, error: BaseException, *, stage: str | None = None) -> dict[str, Any]:
        """任何失败都要落到 job.json，绝不让任务停在忙碌态。"""
        with self._guard:
            job = self.get_job(job_id)
            if job["status"] in TERMINAL_JOB_STATUSES:
                # 已经收尾过（例如被 reconcile 标记中断），不覆盖真实终态
                return job

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
        item: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加一条事件。seq 单调递增，内容只有阶段名/状态名/校验项。"""
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
        if item is not None:
            record["item"] = item
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
            "verify": job.get("verify"),
            "post_verify": job.get("post_verify"),
            "process_log_path": job.get("process_log_path"),
            "last_error": job.get("last_error"),
            "summary": tally,
            "results": results,
            # R36：内容一致性结论。`summary` 由 `describe_integrity` 统一生成，
            # 保证结果页、日志、report.json 三处说的是同一句话。
            "content_integrity": job.get("content_integrity"),
            "content_integrity_summary": describe_integrity(
                job.get("content_integrity")
            ),
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
            raw = self._read_job(job_id)
            if not isinstance(raw, dict):
                continue
            previous = raw.get("status")
            if previous not in state_machine.BUSY_STATUSES:
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


def _verify_brief(result: dict[str, Any] | None) -> str:
    """把校验结果压成一行日志。

    日志是排障时最先看的东西，但十项结果全量打印会把一行撑成十行。只留
    「几项通过 / 几项警告 / 几项不通过」和整体结论，细节去 `verify` 字段里看。
    """
    if not isinstance(result, dict):
        return "无结果"
    summary = result.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    passed = int(summary.get("pass", 0) or 0)
    warning = int(summary.get("warning", 0) or 0)
    failed = int(summary.get("fail", 0) or 0)
    skipped = int(summary.get("skipped", 0) or 0)
    return f"通过 {passed} 项，警告 {warning} 项，不通过 {failed} 项，跳过 {skipped} 项"


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
