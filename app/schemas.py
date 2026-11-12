"""数据契约：Job / Config / OperationResult / VerifyResult。

这里的常量与校验函数是 API、持久化与前端三方的唯一约定来源。

v4.0 的两条硬约定：

1. **校验键与处理键彻底分离**（§5）：`VERIFY_KEYS` 是只读检查，`OPERATION_KEYS`
   是写操作，两者不共用编号，也不互相引用。
2. **`tagged_pdf` 已移除**（R30），位置由 `link_color` 接替；`OPERATION_KEYS`
   末项必须仍是 `fast_web_view`（线性化必须最后执行）。

v4.1 的两处扩项：`VERIFY_KEYS` 9→10（拆出 `toc`），`OPERATION_KEYS` 9→10
（新增 `remove_annotations`）。两张表**各自**变长，仍然互不引用。
"""

from __future__ import annotations

from typing import Any, Iterable

from app.config import SCHEMA_VERSION

# --------------------------------------------------------------------------- #
# 十二项校验（只读，顺序即展示顺序，见 PRD §5）
#
# v4.1：原第 8 项 `page_labels`「目录页码」按用户要求拆成两项——
# `toc`（目录：存在性 + 文字颜色 + 内链可跳转）与 `page_labels`（页码：从 1 开始）。
# 拆开是因为两者的判定依据完全不同：目录看首页链接与文字颜色，页码看
# `/PageLabels` 或页脚数字。混在一项里，报告只能给出一个含糊的结论。
#
# v4.2：`bookmarks` 从「只看有没有」扩为「还看缩放对不对」（R37），
# 并新增 `initial_view`（R38）与 `metadata_security`（R39）两项。
# 三项都是只读——对应的写操作 v2.1 就有（`initial_layout` / `initial_zoom` /
# `bookmark_panel` / `metadata` / `bookmarks`），本轮不再造第二套写入口。
#
# 第 12 项为什么叫 `metadata_security` 而不是 `metadata`：`OPERATION_KEYS`
# 里已经有一个 `metadata`（处理项 2「清除元数据」）。§5 的硬约定是校验键与
# 处理键**不得有交集**，同名会让「这一项是查还是改」在日志、报告、前端
# 状态映射里彻底分不清。加 `_security` 后缀既保留语义（查描述 + 查加密），
# 又与处理键区分开。
# --------------------------------------------------------------------------- #

VERIFY_KEYS: tuple[str, ...] = (
    "filename_naming",
    "file_size",
    "external_links",
    "page_orientation",
    "text_layer",
    "annotations",
    "font_embedding",
    "toc",
    "page_labels",
    "bookmarks",
    "initial_view",
    "metadata_security",
)

VERIFY_TITLES: dict[str, str] = {
    "filename_naming": "文件命名规范",
    "file_size": "文件大小",
    "external_links": "外部链接",
    "page_orientation": "页面方向",
    "text_layer": "文本可提取性",
    "annotations": "批注与高亮",
    "font_embedding": "字体嵌入",
    "toc": "目录",
    "page_labels": "页码",
    # 标题跟着校验范围一起改。只写「书签存在性」而实际还查了缩放，用户读报告时
    # 无从知道这一项到底查了什么——文案必须与判据同宽。
    "bookmarks": "书签与承前缩放",
    "initial_view": "初始视图",
    "metadata_security": "元数据与安全",
}

# 校验项 -> 需求编号（顺序与 VERIFY_KEYS 一一对应）
VERIFY_REQUIREMENT: dict[str, str] = {
    "filename_naming": "R14",
    "file_size": "R15",
    "external_links": "R16",
    "page_orientation": "R17",
    "text_layer": "R18",
    "annotations": "R19",
    "font_embedding": "R20",
    "toc": "R32",
    "page_labels": "R33",
    # R22 的存在性判据原样保留，R37 在其上追加「承前缩放」判定。
    # 这里记 R37 而不是 R22：这一项现在**对应**的是 R37，R22 是它的历史基线。
    "bookmarks": "R37",
    "initial_view": "R38",
    "metadata_security": "R39",
}

VERIFY_STATUSES: tuple[str, ...] = ("pass", "warning", "fail", "skipped")

# 界面显示：`skipped` 不显示成 PASS，也不显示成 FAIL，只显示破折号 + 原因。
VERIFY_STATUS_LABELS: dict[str, str] = {
    "pass": "PASS",
    "warning": "WARNING",
    "fail": "FAIL",
    "skipped": "—",
}

# 严重度排序：用于「按最严重项取」的汇总口径（§7.3）
VERIFY_STATUS_SEVERITY: dict[str, int] = {
    "pass": 0,
    "skipped": 0,
    "warning": 1,
    "fail": 2,
}

# 图标与处理结果保持同一套字形：✔ ＝ — ! ✕，用户不用学两套符号。
VERIFY_STATUS_ICONS: dict[str, str] = {
    "pass": "✔",
    "warning": "!",
    "fail": "✕",
    "skipped": "—",
}

# 校验结果所属阶段：前置校验 / 后置校验（§8.3 VerifyResult.phase）
VERIFY_PHASES: tuple[str, ...] = ("precheck", "post")

# `job["phase"]` 是另一个概念：它回答「这份任务现在在配置哪一段」，
# 所以取值是 precheck / process，和后置校验无关（§2.4）。
# 两者共用 `precheck` 这个词纯属巧合，不能合成一张表——合并后
# 要么 `post` 被当成合法 job 阶段，要么 `process` 被判成非法。
JOB_PHASES: tuple[str, ...] = ("precheck", "process")

# --------------------------------------------------------------------------- #
# 十项处理（写，顺序即执行顺序，见 PRD §6.2）
#
# v4.1：新增 `remove_annotations`「清除高亮和批注」，排在**第 1 位**。三条理由
# （PRD §6.2 末尾）：必须在扁平化之前（否则将被删的对象先被画进内容流，删不掉）、
# 必须在改色之前（否则被删注释的外观会干扰链接矩形判定）、删注释不写内容流
# 所以对后续项没有副作用。
# --------------------------------------------------------------------------- #

OPERATION_KEYS: tuple[str, ...] = (
    "remove_annotations",
    "flatten_annotations",
    "bookmarks_inherit_zoom",
    "links_inherit_zoom",
    "initial_layout",
    "initial_zoom",
    "bookmark_panel",
    "metadata",
    "link_color",
    "fast_web_view",
)

OPERATION_TITLES: dict[str, str] = {
    "remove_annotations": "清除高亮和批注",
    "flatten_annotations": "注释展平",
    "bookmarks_inherit_zoom": "书签承前缩放",
    "links_inherit_zoom": "链接承前缩放",
    "initial_layout": "初始布局恢复默认",
    "initial_zoom": "初始缩放恢复默认",
    "bookmark_panel": "打开时显示书签面板",
    "metadata": "清除文档描述",
    "link_color": "内链颜色修正",
    "fast_web_view": "Fast Web View",
}

OPERATION_REQUIREMENT: dict[str, str] = {
    "remove_annotations": "R34",
    "flatten_annotations": "R2",
    "bookmarks_inherit_zoom": "R1",
    "links_inherit_zoom": "R9",
    "initial_layout": "R3",
    "initial_zoom": "R4",
    "bookmark_panel": "R5",
    "metadata": "R6",
    "link_color": "R35",
    "fast_web_view": "R8",
}

# 受豁免页影响的处理项
EXEMPT_SENSITIVE_OPERATIONS = frozenset(
    {
        "remove_annotations",
        "flatten_annotations",
        "bookmarks_inherit_zoom",
        "links_inherit_zoom",
    }
)

# --------------------------------------------------------------------------- #
# 七个 UI 处理项 -> 十项流水线（§6.1）
# --------------------------------------------------------------------------- #

PROCESS_GROUPS: tuple[str, ...] = (
    "g1_bookmarks",
    "g2_metadata",
    "g3_initial_view",
    "g4_link_color",
    "g5_security",
    "g6_flatten",
    "g7_remove_annotations",
)

PROCESS_GROUP_TITLES: dict[str, str] = {
    "g1_bookmarks": "书签属性（承前缩放）",
    "g2_metadata": "清除文档描述（标题/作者/主题/关键字）",
    "g3_initial_view": "初始视图与目标位置",
    "g4_link_color": "内链颜色修正（改为蓝色 RGB 0,0,255）",
    "g5_security": "安全属性与元数据（快速 Web 查看）",
    "g6_flatten": "注释扁平化",
    "g7_remove_annotations": "清除高亮和批注",
}

PROCESS_GROUP_REQUIREMENT: dict[str, str] = {
    "g1_bookmarks": "4.2.1",
    "g2_metadata": "4.2.2",
    "g3_initial_view": "4.2.3",
    "g4_link_color": "4.2.4",
    "g5_security": "4.2.5",
    "g6_flatten": "4.2.6",
    "g7_remove_annotations": "R34",
}

# 一个 group 可以展开成多个流水线项；`metadata` 被 g2 与 g5 共享，
# 展开时必须去重，否则同一个处理项会跑两遍（AC-R24-N3）。
PROCESS_GROUP_OPERATIONS: dict[str, tuple[str, ...]] = {
    "g1_bookmarks": ("bookmarks_inherit_zoom", "links_inherit_zoom"),
    "g2_metadata": ("metadata",),
    "g3_initial_view": ("initial_layout", "initial_zoom", "bookmark_panel"),
    "g4_link_color": ("link_color",),
    "g5_security": ("metadata", "fast_web_view"),
    "g6_flatten": ("flatten_annotations",),
    "g7_remove_annotations": ("remove_annotations",),
}

# 默认勾选：第 1–5 项勾；第 6 项（扁平化）与第 7 项（删除批注）都不勾——
# 两者都不可逆，替用户做这个决定不合适。
DEFAULT_PROCESS_GROUPS: tuple[str, ...] = (
    "g1_bookmarks",
    "g2_metadata",
    "g3_initial_view",
    "g4_link_color",
    "g5_security",
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
    # 前置校验
    "verify_queued",
    "verifying",
    "verified",
    # 历史兼容：新流程不再产生
    "queued",
    "running",
    # 处理
    "process_queued",
    "processing",
    "post_verifying",
    # 终态
    "completed",
    "completed_with_warnings",
    "failed",
    "interrupted",
)

JOB_STATUS_LABELS: dict[str, str] = {
    "configuring": "待配置",
    "verify_queued": "校验排队中",
    "verifying": "校验中",
    "verified": "校验完成",
    "queued": "排队中",
    "running": "处理中",
    "process_queued": "处理排队中",
    "processing": "处理中",
    "post_verifying": "后置校验中",
    "completed": "已完成",
    "completed_with_warnings": "完成但有警告",
    "failed": "失败",
    "interrupted": "已中断",
}

# 任务状态也要有图标：状态不能只靠颜色区分（色盲用户、黑白打印、
# 以及"红色到底是失败还是警告"这种歧义，都靠图标 + 文字一起解决）。
JOB_STATUS_ICONS: dict[str, str] = {
    "configuring": "✎",
    "verify_queued": "⋯",
    "verifying": "◔",
    "verified": "☑",
    "queued": "⋯",
    "running": "▶",
    "process_queued": "⋯",
    "processing": "▶",
    "post_verifying": "◔",
    "completed": "✔",
    "completed_with_warnings": "!",
    "failed": "✕",
    "interrupted": "⏸",
}

TERMINAL_JOB_STATUSES = frozenset(
    {"completed", "completed_with_warnings", "failed", "interrupted"}
)

# 校验相关状态（供状态机与前端判断「是否在忙」）
VERIFY_JOB_STATUSES = frozenset({"verify_queued", "verifying"})
PROCESS_JOB_STATUSES = frozenset(
    {"queued", "running", "process_queued", "processing", "post_verifying"}
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
    # v4.0：前置校验是独立阶段（R13）。它和处理阶段的 `verifying` 不是一回事，
    # 复用一个名字会让「正在验证输出文件」出现在什么都没处理的时候。
    "precheck",
    "preparing",
    "remove_annotations",
    "flatten_annotations",
    "bookmarks_inherit_zoom",
    "links_inherit_zoom",
    "initial_layout",
    "initial_zoom",
    "bookmark_panel",
    "metadata",
    "link_color",
    "fast_web_view",
    "verifying",
    "post_verifying",
    "publishing",
)

STAGE_LABELS: dict[str, str] = {
    "precheck": "正在执行前置校验",
    "preparing": "准备处理",
    "remove_annotations": "正在清除高亮和批注",
    "flatten_annotations": "正在展平注释",
    "bookmarks_inherit_zoom": "正在调整书签缩放",
    "links_inherit_zoom": "正在调整链接缩放",
    "initial_layout": "正在恢复初始布局",
    "initial_zoom": "正在恢复初始缩放",
    "bookmark_panel": "正在设置书签面板",
    "metadata": "正在清除文档描述",
    "link_color": "正在修正内链颜色",
    "fast_web_view": "正在执行 Fast Web View",
    "verifying": "正在验证输出文件",
    "post_verifying": "正在执行后置校验",
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

# v4.0 新增的 job 字段（迁移时补齐；老任务缺这些字段不算坏数据）
JOB_V4_FIELDS: tuple[str, ...] = (
    "phase",
    "verify",
    "post_verify",
    "process_log_path",
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
        # 校验配置
        "verify_items": list(VERIFY_KEYS),
        "skip_precheck": False,
        # 处理配置
        "process_items": list(DEFAULT_PROCESS_GROUPS),
        "write_process_log": False,
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
# 处理项展开
# --------------------------------------------------------------------------- #


def expand_process_groups(groups: Iterable[str]) -> tuple[str, ...]:
    """把 UI 处理项展开成流水线项，按 `OPERATION_KEYS` 顺序去重。

    顺序必须回到 `OPERATION_KEYS` 的顺序：用户勾选顺序不该改变流水线顺序，
    否则 `fast_web_view` 可能被排到中间，线性化会失效。
    """
    selected: set[str] = set()
    for group in groups:
        selected.update(PROCESS_GROUP_OPERATIONS.get(group, ()))
    return tuple(key for key in OPERATION_KEYS if key in selected)


def groups_for_operations(operations: Iterable[str]) -> list[str]:
    """反向映射：流水线项 -> 覆盖它的 UI 处理项（历史任务迁移用）。"""
    present = set(operations)
    result: list[str] = []
    for group in PROCESS_GROUPS:
        mapped = PROCESS_GROUP_OPERATIONS[group]
        if any(key in present for key in mapped):
            result.append(group)
    return result


# --------------------------------------------------------------------------- #
# 校验结果汇总
# --------------------------------------------------------------------------- #


def summarize_verify_items(items: Iterable[dict[str, Any]]) -> dict[str, int]:
    """按状态计数，四个键永远齐全（前端直接取用，不做缺键兜底）。"""
    summary = {status: 0 for status in VERIFY_STATUSES}
    for item in items:
        status = item.get("status")
        if status in summary:
            summary[status] += 1
    return summary


def worst_verify_status(items: Iterable[dict[str, Any]]) -> str:
    """取最严重项：fail > warning > pass/skipped。"""
    worst = "pass"
    for item in items:
        status = item.get("status")
        if VERIFY_STATUS_SEVERITY.get(status, 0) > VERIFY_STATUS_SEVERITY[worst]:
            worst = status if status in VERIFY_STATUS_SEVERITY else worst
    return worst


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


def validate_finding(finding: Any) -> None:
    if not isinstance(finding, dict):
        raise SchemaError("Finding 必须是对象")
    for key in ("level", "message"):
        if key not in finding:
            raise SchemaError(f"Finding 缺少字段：{key}")
    if finding["level"] not in VERIFY_STATUSES:
        raise SchemaError(f"未知明细级别：{finding['level']}")


def validate_verify_result(result: Any) -> None:
    """校验结果结构（PRD §8.3）。"""
    if not isinstance(result, dict):
        raise SchemaError("VerifyResult 必须是对象")
    for key in ("job_id", "phase", "generated_at", "file", "items", "summary"):
        if key not in result:
            raise SchemaError(f"VerifyResult 缺少字段：{key}")
    if result["phase"] not in VERIFY_PHASES:
        raise SchemaError(f"未知校验阶段：{result['phase']}")
    if not isinstance(result["items"], list):
        raise SchemaError("VerifyResult.items 必须是数组")
    seen: list[str] = []
    for item in result["items"]:
        if not isinstance(item, dict):
            raise SchemaError("校验项必须是对象")
        for key in ("key", "status", "summary", "findings", "duration_ms"):
            if key not in item:
                raise SchemaError(f"校验项缺少字段：{key}")
        if item["key"] not in VERIFY_KEYS:
            raise SchemaError(f"未知校验项：{item['key']}")
        if item["status"] not in VERIFY_STATUSES:
            raise SchemaError(f"未知校验状态：{item['status']}")
        if not isinstance(item["findings"], list):
            raise SchemaError("校验项 findings 必须是数组")
        for finding in item["findings"]:
            validate_finding(finding)
        seen.append(item["key"])
    if len(set(seen)) != len(seen):
        raise SchemaError("校验项重复")


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
    phase = job.get("phase")
    if phase is not None and phase not in JOB_PHASES:
        raise SchemaError(f"未知阶段：{phase}")
    for key in ("verify", "post_verify"):
        value = job.get(key)
        if value is not None:
            validate_verify_result(value)


def validate_config(config: Any) -> None:
    if not isinstance(config, dict):
        raise SchemaError("config 必须是对象")
    for key in ("exempt_pages_raw", "exempt_pages"):
        if key not in config:
            raise SchemaError(f"config 缺少字段：{key}")
    if not isinstance(config["exempt_pages"], list):
        raise SchemaError("config.exempt_pages 必须是数组")

    verify_items = config.get("verify_items", [])
    if not isinstance(verify_items, list):
        raise SchemaError("config.verify_items 必须是数组")
    unknown = [key for key in verify_items if key not in VERIFY_KEYS]
    if unknown:
        raise SchemaError(f"未知校验项：{unknown}")
    if len(set(verify_items)) != len(verify_items):
        raise SchemaError("config.verify_items 有重复项")

    if not isinstance(config.get("skip_precheck", False), bool):
        raise SchemaError("config.skip_precheck 必须是布尔值")

    process_items = config.get("process_items", [])
    if not isinstance(process_items, list):
        raise SchemaError("config.process_items 必须是数组")
    unknown_groups = [key for key in process_items if key not in PROCESS_GROUPS]
    if unknown_groups:
        raise SchemaError(f"未知处理项：{unknown_groups}")
    if len(set(process_items)) != len(process_items):
        raise SchemaError("config.process_items 有重复项")

    if not isinstance(config.get("write_process_log", False), bool):
        raise SchemaError("config.write_process_log 必须是布尔值")
