"""v2.1（`schema_version == 1`）-> v4.0（`schema_version == 2`）惰性迁移。

为什么单独一个模块：读 `job.json` 的地方不止一处——任务服务、历史索引、
启动回收都要读。迁移逻辑只能有一份，否则某条路径漏了补字段，界面上就会
出现「这个任务没有 process_items」的诡异状态，而且只在老任务上复现。

放在 `app/core/` 而不是 `app/services/`：`Storage` 也要用它，而
`services.jobs` 依赖 `core.storage`，写在 services 里会形成循环导入。

三条设计约束：

1. **只在读取时迁移，不回写磁盘**。迁移逻辑万一有 bug，坏的是这一次的
   内存视图，不是用户的历史记录。回写只发生在用户真的操作了这个任务时。
2. **只补不删**。`config.tag_choice`、`job.tag_detection` 原样留着——
   R30 要求「前端不展示、后端不读取」，不是「从磁盘上抹掉」。
3. **缺字段或类型异常一律按默认值兜底**（AC-R31-E1），不抛异常。
   历史数据里出现 `"config": null` 这种值并不稀奇，读历史列表时
   不能因为一条坏记录让整页打不开。
"""

from __future__ import annotations

from typing import Any

from app.config import SCHEMA_VERSION
from app.schemas import (
    DEFAULT_PROCESS_GROUPS,
    JOB_PHASES,
    PROCESS_GROUPS,
    VERIFY_KEYS,
    default_tag_detection,
    groups_for_operations,
)

__all__ = ["migrate_job", "is_legacy"]


def is_legacy(job: dict[str, Any]) -> bool:
    """是否为 v4.0 之前的 job.json。读不出数字时按老版本处理（更保守）。"""
    try:
        return int(job.get("schema_version") or 1) < SCHEMA_VERSION
    except (TypeError, ValueError):
        return True


def migrate_job(job: dict[str, Any]) -> dict[str, Any]:
    """把老 job.json 就地补齐成 v4.0 结构，返回同一个对象。"""
    if not isinstance(job, dict):
        return job

    legacy = is_legacy(job)
    status = job.get("status")
    # `awaiting_tag_decision` 的用户当时已经选好了标签策略，只差「开始处理」；
    # 按 R31 退回配置时应落在**处理配置**那一段，而不是让他从头再走一遍前置校验。
    phase_hint: str | None = None

    if status == "awaiting_tag_decision":
        # 标签流程已移除（R30），退回「等待处理配置」。
        # 这一条不分版本：该状态在新流程里根本不可能产生。
        job["status"] = "configuring"
        status = "configuring"
        phase_hint = "process"
    elif legacy and status in ("queued", "running"):
        # v1 的 queued/running 一定是上次异常退出留下的（老进程没有别的回收
        # 路径），直接判中断。v2 的忙碌状态交给 reconcile_interrupted 处理——
        # 那里会写 last_error 和事件，信息更全，不能在这里抢先改掉。
        job["status"] = "interrupted"
        job["current_stage"] = None
        status = "interrupted"

    raw_config = job.get("config")
    config = dict(raw_config) if isinstance(raw_config, dict) else {}

    if not isinstance(config.get("exempt_pages_raw"), str):
        config["exempt_pages_raw"] = ""
    if not isinstance(config.get("exempt_pages"), list):
        config["exempt_pages"] = []

    if not isinstance(config.get("verify_items"), list):
        # 老任务没做过校验，默认「十二项全查」：用户下次点校验时按全量跑。
        config["verify_items"] = list(VERIFY_KEYS)
    else:
        config["verify_items"] = [
            key for key in config["verify_items"] if key in VERIFY_KEYS
        ]

    if not isinstance(config.get("skip_precheck"), bool):
        config["skip_precheck"] = False

    if not isinstance(config.get("process_items"), list):
        # 按历史任务实际执行过的处理项反推分组：v4.0 及更早跑的是当时的全部
        # 九项（那时还没有 `remove_annotations`），反推出来就是「七项里除
        # g6、g7 之外的全部」，与 `DEFAULT_PROCESS_GROUPS` 一致。
        # 反推而不是直接给默认值，是为了尊重用户当时真实勾过的子集。
        executed = [
            item.get("key")
            for item in (job.get("results") or [])
            if isinstance(item, dict) and item.get("key")
        ]
        derived = (
            groups_for_operations(executed)
            if executed
            else list(DEFAULT_PROCESS_GROUPS)
        )
        config["process_items"] = [key for key in derived if key in PROCESS_GROUPS]
    else:
        config["process_items"] = [
            key for key in config["process_items"] if key in PROCESS_GROUPS
        ]

    if not isinstance(config.get("write_process_log"), bool):
        config["write_process_log"] = False

    job["config"] = config

    if job.get("phase") not in JOB_PHASES:
        job["phase"] = phase_hint or ("precheck" if status == "configuring" else "process")

    for key in ("verify", "post_verify", "process_log_path"):
        job.setdefault(key, None)

    if not isinstance(job.get("results"), list):
        job["results"] = []
    if not isinstance(job.get("tag_detection"), dict):
        # 只读历史值：不读取、不展示，但结构上必须存在（前端渲染时不会炸）
        job["tag_detection"] = default_tag_detection()

    job["schema_version"] = SCHEMA_VERSION
    return job
