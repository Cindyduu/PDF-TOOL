"""数据契约：Job / Config / TagDetection / OperationResult。

这里的常量与校验函数是 API、持久化与前端三方的唯一约定来源。
"""

from __future__ import annotations

from typing import Any

from app.config import SCHEMA_VERSION

# --------------------------------------------------------------------------- #
# 九项处理
# --------------------------------------------------------------------------- #

OPERATION_KEYS: tuple[str, ...] = (
    "flatten_annotations",
    "bookmarks_inherit_zoom",
    "links_inherit_zoom",
    "initial_layout",
    "initial_zoom",
    "bookmark_panel",
    "metadata",
    "tagged_pdf",
    "fast_web_view",
)

OPERATION_TITLES: dict[str, str] = {
    "flatten_annotations": "注释展平",
    "bookmarks_inherit_zoom": "书签承前缩放",
    "links_inherit_zoom": "链接承前缩放",
    "initial_layout": "初始布局恢复默认",
    "initial_zoom": "初始缩放恢复默认",
    "bookmark_panel": "打开时显示书签面板",
    "metadata": "清除文档描述",
    "tagged_pdf": "生成标签 PDF",
    "fast_web_view": "Fast Web View",
}

OPERATION_REQUIREMENT: dict[str, str] = {
    "flatten_annotations": "R2",
    "bookmarks_inherit_zoom": "R1",
    "links_inherit_zoom": "R9",
    "initial_layout": "R3",
    "initial_zoom": "R4",
    "bookmark_panel": "R5",
    "metadata": "R6",
    "tagged_pdf": "R7",
    "fast_web_view": "R8",
}

# 受豁免页影响的处理项
EXEMPT_SENSITIVE_OPERATIONS = frozenset(
    {"flatten_annotations", "bookmarks_inherit_zoom", "links_inherit_zoom"}
)

# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #

RESULT_STATUSES: tuple[str, ...] = (
    "adjusted",
    "unchanged",
    "not_applicable",
    "review_required",
    "failed",
)

RESULT_STATUS_LABELS: dict[str, str] = {
    "adjusted": "已调整",
    "unchanged": "无需修改",
    "not_applicable": "不需要修改",
    "review_required": "待人工复核",
    "failed": "失败",
}

RESULT_STATUS_ICONS: dict[str, str] = {
    "adjusted": "✔",
    "unchanged": "＝",
    "not_applicable": "—",
    "review_required": "!",
    "failed": "✕",
}

JOB_STATUSES: tuple[str, ...] = (
    "configuring",
    "awaiting_tag_decision",
    "queued",
    "running",
    "completed",
    "completed_with_warnings",
    "failed",
    "interrupted",
)

JOB_STATUS_LABELS: dict[str, str] = {
    "configuring": "待配置",
    "awaiting_tag_decision": "等待标签选择",
    "queued": "排队中",
    "running": "处理中",
    "completed": "已完成",
    "completed_with_warnings": "完成但有警告",
    "failed": "失败",
    "interrupted": "已中断",
}

# 任务状态也要有图标：状态不能只靠颜色区分（色盲用户、黑白打印、
# 以及"红色到底是失败还是警告"这种歧义，都靠图标 + 文字一起解决）。
JOB_STATUS_ICONS: dict[str, str] = {
    "configuring": "✎",
    "awaiting_tag_decision": "?",
    "queued": "⋯",
    "running": "▶",
    "completed": "✔",
    "completed_with_warnings": "!",
    "failed": "✕",
    "interrupted": "⏸",
}

TERMINAL_JOB_STATUSES = frozenset(
    {"completed", "completed_with_warnings", "failed", "interrupted"}
)

TAG_STATUSES: tuple[str, ...] = (
    "tagged",
    "untagged",
    "markinfo_only",
    "detection_failed",
)

TAG_STATUS_LABELS: dict[str, str] = {
    "tagged": "已有真实标签",
    "untagged": "没有标签",
    "markinfo_only": "仅有标记位（结构缺失）",
    "detection_failed": "标签检测失败",
}

TAG_CHOICES: tuple[str, ...] = ("generate", "skip", "keep", "regenerate")

TAG_CHOICE_LABELS: dict[str, str] = {
    "generate": "生成标签",
    "skip": "跳过标签",
    "keep": "保持原有标签",
    "regenerate": "重新生成标签",
}

# 阶段名（只显示真实阶段，不显示百分比）
STAGE_NAMES: tuple[str, ...] = (
    "preparing",
    "flatten_annotations",
    "bookmarks_inherit_zoom",
    "links_inherit_zoom",
    "initial_layout",
    "initial_zoom",
    "bookmark_panel",
    "metadata",
    "tagged_pdf",
    "fast_web_view",
    "verifying",
    "publishing",
)

STAGE_LABELS: dict[str, str] = {
    "preparing": "准备处理",
    "flatten_annotations": "正在展平注释",
    "bookmarks_inherit_zoom": "正在调整书签缩放",
    "links_inherit_zoom": "正在调整链接缩放",
    "initial_layout": "正在恢复初始布局",
    "initial_zoom": "正在恢复初始缩放",
    "bookmark_panel": "正在设置书签面板",
    "metadata": "正在清除文档描述",
    "tagged_pdf": "正在生成标签结构",
    "fast_web_view": "正在执行 Fast Web View",
    "verifying": "正在验证输出文件",
    "publishing": "正在发布结果",
}

# --------------------------------------------------------------------------- #
# 必含字段
# --------------------------------------------------------------------------- #

JOB_REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "schema_version",
    "status",
    "current_stage",
    "original_name",
    "created_at",
    "started_at",
    "finished_at",
    "source",
    "output",
    "config",
    "tag_detection",
    "results",
    "last_error",
    "generation_id",
)

RESULT_REQUIRED_FIELDS: tuple[str, ...] = (
    "key",
    "title",
    "status",
    "before",
    "after",
    "counts",
    "reason_code",
    "message",
    "warnings",
    "next_action",
    "started_at",
    "finished_at",
)

COUNT_KEYS: tuple[str, ...] = (
    "checked",
    "modified",
    "compliant",
    "exempt",
    "not_applicable",
    "failed",
)

COUNT_LABELS: dict[str, str] = {
    "checked": "检查数",
    "modified": "修改数",
    "compliant": "已符合数",
    "exempt": "豁免数",
    "not_applicable": "不适用数",
    "failed": "失败数",
}


# --------------------------------------------------------------------------- #
# 构造函数
# --------------------------------------------------------------------------- #


def empty_counts() -> dict[str, int]:
    return {key: 0 for key in COUNT_KEYS}


def new_operation_result(key: str, status: str = "unchanged") -> dict[str, Any]:
    return {
        "key": key,
        "title": OPERATION_TITLES[key],
        "status": status,
        "before": None,
        "after": None,
        "counts": empty_counts(),
        "reason_code": None,
        "message": "",
        "warnings": [],
        "next_action": None,
        "started_at": None,
        "finished_at": None,
    }


def default_config() -> dict[str, Any]:
    return {
        "exempt_pages_raw": "",
        "exempt_pages": [],
        "tag_choice": None,
    }


def default_tag_detection() -> dict[str, Any]:
    return {
        "status": "untagged",
        "checked_at": None,
        "has_struct_tree": False,
        "has_markinfo": False,
        "marked": False,
        "parent_tree_ok": False,
        "struct_parents_ok": False,
        "mcid_count": 0,
        "error": None,
        "message": "",
    }


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


class SchemaError(ValueError):
    pass


def validate_counts(counts: Any) -> None:
    if not isinstance(counts, dict):
        raise SchemaError("counts 必须是对象")
    missing = [k for k in COUNT_KEYS if k not in counts]
    if missing:
        raise SchemaError(f"counts 缺少字段：{missing}")
    for key in COUNT_KEYS:
        value = counts[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SchemaError(f"counts.{key} 必须是非负整数")


def validate_operation_result(result: Any) -> None:
    if not isinstance(result, dict):
        raise SchemaError("OperationResult 必须是对象")
    missing = [f for f in RESULT_REQUIRED_FIELDS if f not in result]
    if missing:
        raise SchemaError(f"OperationResult 缺少字段：{missing}")
    if result["status"] not in RESULT_STATUSES:
        raise SchemaError(f"未知结果状态：{result['status']}")
    if not isinstance(result["warnings"], list):
        raise SchemaError("warnings 必须是数组")
    validate_counts(result["counts"])


def validate_job(job: Any) -> None:
    if not isinstance(job, dict):
        raise SchemaError("Job 必须是对象")
    missing = [f for f in JOB_REQUIRED_FIELDS if f not in job]
    if missing:
        raise SchemaError(f"Job 缺少字段：{missing}")
    if job["status"] not in JOB_STATUSES:
        raise SchemaError(f"未知任务状态：{job['status']}")
    if not isinstance(job["results"], list):
        raise SchemaError("results 必须是数组")
    for result in job["results"]:
        validate_operation_result(result)
    if job["schema_version"] != SCHEMA_VERSION:
        raise SchemaError(
            f"schema_version 不匹配：{job['schema_version']} != {SCHEMA_VERSION}"
        )


def validate_config(config: Any) -> None:
    if not isinstance(config, dict):
        raise SchemaError("config 必须是对象")
    for key in ("exempt_pages_raw", "exempt_pages", "tag_choice"):
        if key not in config:
            raise SchemaError(f"config 缺少字段：{key}")
    if not isinstance(config["exempt_pages"], list):
        raise SchemaError("config.exempt_pages 必须是数组")
    choice = config["tag_choice"]
    if choice is not None and choice not in TAG_CHOICES:
        raise SchemaError(f"未知标签选择：{choice}")
