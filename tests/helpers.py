"""测试辅助函数：构造合法 Job / OperationResult 与落盘。"""

from __future__ import annotations

from typing import Any

from app.config import SCHEMA_VERSION
from app.core import atomic
from app.core.storage import new_job_id, utcnow_iso
from app.schemas import default_config, default_tag_detection, new_operation_result


def make_job(
    generation_id: str,
    job_id: str | None = None,
    original_name: str = "report.pdf",
    status: str = "configuring",
    created_at: str | None = None,
    output_name: str | None = None,
    results: list | None = None,
    config: dict | None = None,
    tag_detection: dict | None = None,
    phase: str | None = None,
    verify: dict | None = None,
    post_verify: dict | None = None,
) -> dict[str, Any]:
    job_id = job_id or new_job_id()
    return {
        "id": job_id,
        # v4.0 结构。测试里构造的任务必须是"当前版本"，否则每次读取都会
        # 触发惰性迁移，测到的就不是写进去的那份数据了。
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "phase": phase or ("precheck" if status == "configuring" else "process"),
        "current_stage": None,
        "original_name": original_name,
        "created_at": created_at or utcnow_iso(),
        "started_at": None,
        "finished_at": None,
        "source": {
            "file_name": "original.pdf",
            "original_name": original_name,
            "sha256": "0" * 64,
            "size": 1024,
            "page_count": 3,
        },
        "output": (
            {
                "file_name": output_name,
                "sha256": "1" * 64,
                "size": 2048,
                "page_count": 3,
            }
            if output_name
            else None
        ),
        "config": config or default_config(),
        "tag_detection": tag_detection or default_tag_detection(),
        "verify": verify,
        "post_verify": post_verify,
        "process_log_path": None,
        "results": results or [],
        "last_error": None,
        "generation_id": generation_id,
    }


def make_result(key: str, status: str = "adjusted") -> dict[str, Any]:
    return new_operation_result(key, status)


def write_job(
    ctx,
    job: dict,
    with_original: bool = False,
    output: bytes | None = None,
):
    gid = job["generation_id"]
    directory = ctx.storage.job_dir(gid, job["id"], create=True)
    atomic.atomic_write_json(directory / "job.json", job)
    if with_original:
        (directory / "original.pdf").write_bytes(b"%PDF-1.7\n%fake original\n")
    if output is not None and job.get("output"):
        (directory / job["output"]["file_name"]).write_bytes(output)
    return directory
