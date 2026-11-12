"""任务状态迁移的**唯一入口**。

为什么要单独一个模块：状态一旦散落在各处被随手赋值，就会出现
「配置页还没提交就开始处理」「任务失败了又能重新排队」这类说不清的状态。
这里把「哪些迁移合法」写成一张白名单表，任何写入 `job["status"]` 的地方
都必须经过 `transition()`，非法迁移当场抛错而不是悄悄写进去。

白名单只包含**代码里真的会发生**的迁移：

    configuring            -> verify_queued | process_queued
    verify_queued          -> verifying | failed | interrupted
    verifying              -> verified  | failed | interrupted
    verified               -> configuring | process_queued | interrupted
    process_queued         -> processing | failed | interrupted
    processing             -> post_verifying | failed | interrupted
    post_verifying         -> completed | completed_with_warnings | failed | interrupted
    queued (历史)          -> running | interrupted
    running (历史)         -> completed | completed_with_warnings | failed | interrupted
    completed / completed_with_warnings / failed / interrupted  -> （终态，无出边）

三条刻意不存在的边：

- 没有 `configuring -> failed`：还没开始处理就谈不上失败。
- 没有 `verify_queued -> verified`：跳过 `verifying` 等于伪造进度。
- 没有 `processing -> completed`：后置校验是强制的（R27），不能绕过 `post_verifying`。
"""

from __future__ import annotations

from typing import Any

from app.core.storage import utcnow_iso
from app.errors import AppError, ErrorCode
from app.schemas import JOB_STATUSES, TERMINAL_JOB_STATUSES

__all__ = [
    "TRANSITIONS",
    "TERMINAL_STATUSES",
    "allowed_from",
    "can_transition",
    "assert_transition",
    "is_terminal",
    "transition",
]


TRANSITIONS: dict[str, tuple[str, ...]] = {
    "configuring": ("verify_queued", "process_queued"),
    "verify_queued": ("verifying", "failed", "interrupted"),
    "verifying": ("verified", "failed", "interrupted"),
    "verified": ("configuring", "process_queued", "interrupted"),
    "queued": ("running", "interrupted"),
    "running": ("completed", "completed_with_warnings", "failed", "interrupted"),
    "process_queued": ("processing", "failed", "interrupted"),
    "processing": ("post_verifying", "failed", "interrupted"),
    "post_verifying": ("completed", "completed_with_warnings", "failed", "interrupted"),
    "completed": (),
    "completed_with_warnings": (),
    "failed": (),
    "interrupted": (),
}

TERMINAL_STATUSES = frozenset(TERMINAL_JOB_STATUSES)

# 白名单表与 schemas 里的状态集合必须完全一致，否则说明有状态漏配了出边
assert set(TRANSITIONS) == set(JOB_STATUSES), "状态机与 JOB_STATUSES 不一致"

# 这些状态表示「正在干活」，期间不允许改配置、不允许重复入队
BUSY_STATUSES = frozenset(
    {"verify_queued", "verifying", "queued", "running", "process_queued",
     "processing", "post_verifying"}
)


def allowed_from(status: str) -> tuple[str, ...]:
    return TRANSITIONS.get(status, ())


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def is_busy(status: str) -> bool:
    return status in BUSY_STATUSES


def can_transition(source: str, target: str) -> bool:
    return target in TRANSITIONS.get(source, ())


def assert_transition(source: str, target: str) -> None:
    """非法迁移当场抛错。

    `source == target` 也算非法：重复写同一个状态会让「有没有真的迁移过」
    变得无法判断，宁可报错也不要静默放过。
    """
    if source not in TRANSITIONS:
        raise AppError(
            ErrorCode.INVALID_STATE_TRANSITION,
            "任务处于未知状态，无法迁移",
            details={"from": source, "to": target},
            next_action="请重启应用后重试；若仍失败，请查看 var/logs/app.log",
            http_status=409,
        )
    if target not in TRANSITIONS:
        raise AppError(
            ErrorCode.INVALID_STATE_TRANSITION,
            "目标状态不存在",
            details={"from": source, "to": target, "known": list(JOB_STATUSES)},
            next_action="请重启应用后重试",
            http_status=409,
        )
    if not can_transition(source, target):
        allowed = list(allowed_from(source))
        raise AppError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"不允许从「{source}」迁移到「{target}」",
            details={"from": source, "to": target, "allowed": allowed},
            next_action=(
                "请刷新页面查看当前状态"
                if allowed
                else "任务已结束，如需再处理请在历史中重新发起一次"
            ),
            http_status=409,
        )


def transition(
    job: dict[str, Any],
    target: str,
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """把 `job` 迁移到 `target`，就地更新并返回同一个字典。

    只负责状态与时间戳；业务字段（results / output / last_error）由调用方填写。
    时间戳口径：
    - 进入 `running` / `processing`：`started_at` 若为空则补上（重跑不覆盖首次开始时间）；
    - 进入终态：`finished_at` 补上，`current_stage` 清空。
    """
    source = job.get("status")
    assert_transition(str(source), target)
    moment = at or utcnow_iso()

    job["status"] = target
    if target in ("running", "processing") and not job.get("started_at"):
        job["started_at"] = moment
    if is_terminal(target):
        job["finished_at"] = moment
        job["current_stage"] = None
    return job
