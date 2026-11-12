"""历史列表与「按原配置重新处理」。

三条约定：

1. **列表现场读盘**。每次调用都走 `Storage.build_index_entries`，逐条读
   `job.json` 并现算文件是否存在；`index.json` 只是可重建的缓存，绝不作为
   「文件还在不在」的依据。用户把输出 PDF 拖走之后，`output_available`
   必须下一次刷新就变 false。
2. **`files_available` 是派生标志**，表示「输出与原件都还在」。界面用它决定
   是否提示「文件已不在本机」；两个具体按钮则各看 `output_available` /
   `source_available`，避免一个缺失把另一个操作也一起禁掉。
3. **reprocess 永远产出新 ID**。复制原件字节 + 复制配置，源任务的目录、产物、
   状态一个字节都不动；两个任务之间没有任何共享路径，删掉任何一个都不影响另一个。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.errors import AppError, ErrorCode
from app.schemas import JOB_STATUSES, default_config, validate_config
from app.services.jobs import SOURCE_FILE_NAME

# 搜索命中的字段：文件名、任务号、输出文件名。
SEARCH_FIELDS: tuple[str, ...] = ("original_name", "id", "output_name")


class HistoryService:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.storage = ctx.storage
        self.log = ctx.log

    # ---- 内部 -----------------------------------------------------------
    def _generation(self) -> str:
        return self.ctx.generation_id

    def _job_dir(self, job_id: str) -> Path:
        return self.storage.job_dir(self._generation(), job_id)

    def _source_file(self, job_id: str) -> Path:
        return self.storage.job_file(self._generation(), job_id, SOURCE_FILE_NAME)

    @staticmethod
    def _matches(entry: dict[str, Any], needle: str) -> bool:
        return any(needle in str(entry.get(field) or "").lower() for field in SEARCH_FIELDS)

    # ---- 查询 -----------------------------------------------------------
    def entries(self) -> list[dict[str, Any]]:
        """当前 generation 的全部历史条目（按创建时间倒序，已带派生标志）。"""
        entries = self.storage.build_index_entries(self._generation())
        for entry in entries:
            output_ok = bool(entry.get("output_available"))
            source_ok = bool(entry.get("source_available"))
            entry["files_available"] = output_ok and source_ok
        return entries

    def list_jobs(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """搜索 + 状态筛选 + 批次筛选。

        `total` 是筛选前的总数，`count` 是筛选后的条数——界面用它显示
        「3 / 12 条」，也方便测试确认筛选真的起了作用。
        """
        if status and status not in JOB_STATUSES:
            raise AppError(
                ErrorCode.INVALID_CONFIG,
                "未知的任务状态筛选值",
                details={"status": status, "allowed": list(JOB_STATUSES)},
                next_action="请从下拉框里选择一个有效状态",
                http_status=422,
            )

        entries = self.entries()
        total = len(entries)

        if status:
            entries = [entry for entry in entries if entry.get("status") == status]

        if batch_id:
            entries = [entry for entry in entries if entry.get("batch_id") == batch_id]

        needle = (query or "").strip().lower()
        if needle:
            entries = [entry for entry in entries if self._matches(entry, needle)]

        return {
            "generation_id": self._generation(),
            "total": total,
            "count": len(entries),
            "query": query or None,
            "status": status or None,
            "jobs": entries,
        }

    def get_entry(self, job_id: str) -> dict[str, Any]:
        """单条历史摘要。任务不存在时抛 404，与 `GET /api/jobs/{id}` 一致。"""
        for entry in self.entries():
            if entry.get("id") == job_id:
                return entry
        raise AppError(
            ErrorCode.JOB_NOT_FOUND,
            "任务不存在或已被删除",
            details={"job_id": job_id},
            next_action="请回到首页重新上传文件",
            http_status=404,
        )

    # ---- 重新处理 -------------------------------------------------------
    def reprocess(self, job_id: str) -> dict[str, Any]:
        """按原配置复制出一个**全新 ID** 的任务。

        原件缺失时明确 404 `source_missing`，而不是拿旧产物凑一个任务出来：
        没有原件就没有可验证的输入，产出只会是误导。
        """
        job = self.ctx.jobs.get_job(job_id)
        source = self._source_file(job_id)
        if not source.is_file():
            raise AppError(
                ErrorCode.SOURCE_MISSING,
                "原始文件已不存在，无法重新处理",
                details={"job_id": job_id},
                next_action="请重新上传该文件",
                http_status=404,
            )

        with open(source, "rb") as handle:
            # `create_job` 在缺省批次号时自动生成一个（v4.0 没有「无批次」的
            # 任务）。重新处理出来的任务因此也能直接进准备页 / 校验结果页。
            fresh = self.ctx.jobs.create_job(
                str(job.get("original_name") or SOURCE_FILE_NAME), handle
            )

        config = dict(job.get("config") or default_config())
        validate_config(config)
        fresh["config"] = config
        fresh["reprocessed_from"] = job_id
        self.ctx.jobs.write_job(fresh)
        self.log.info(
            "任务 %s 按原配置复制为 %s", job_id, fresh["id"]
        )
        return fresh
