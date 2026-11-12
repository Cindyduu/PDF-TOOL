"""M0 契约验收：v4.0 的数据契约本身。

这些测试**不看实现、只看常量表**，因为契约一旦歪了，后面的校验引擎、
处理流水线、前端渲染会一起歪，而且症状会散落在十几个不相关的地方。
所以这里把 PRD §12 M0 的八条完成标准逐条钉成断言：

- M0-1 十二项校验键：数量、顺序、标题、需求编号、四态；
- M0-2 六个处理组：组数、映射覆盖十项流水线且无遗漏无多余、共享项去重；
- M0-3 十项处理键：长度 10、无 `tagged_pdf`、有 `link_color`、末项 `fast_web_view`；
- M0-4 任务状态：新增四个、历史两个仍在、`awaiting_tag_decision` 已移出；
- M0-5 `default_config()`：五个字段、无 `tag_choice`、默认值正确；
- M0-6 `SCHEMA_VERSION == 2` 且 v1 老 job 读入后字段齐全；
- M0-7 上传上限 1GB；
- M0-8 五个新错误码。
"""

from __future__ import annotations

import copy

import pytest

from app.config import SCHEMA_VERSION, Limits
from app.core.migrate import is_legacy, migrate_job
from app.core.state_machine import TRANSITIONS
from app.errors import ErrorCode
from app.schemas import (
    COUNT_KEYS,
    DEFAULT_PROCESS_GROUPS,
    JOB_STATUSES,
    JOB_V4_FIELDS,
    OPERATION_KEYS,
    OPERATION_REQUIREMENT,
    OPERATION_TITLES,
    PROCESS_GROUPS,
    PROCESS_GROUP_OPERATIONS,
    PROCESS_GROUP_TITLES,
    RESULT_STATUSES,
    VERIFY_KEYS,
    VERIFY_REQUIREMENT,
    VERIFY_STATUS_ICONS,
    VERIFY_STATUS_LABELS,
    VERIFY_STATUS_SEVERITY,
    VERIFY_STATUSES,
    VERIFY_TITLES,
    default_config,
    expand_process_groups,
    groups_for_operations,
    new_operation_result,
    validate_job,
)

# --------------------------------------------------------------------------- #
# M0-1 十二项校验
# --------------------------------------------------------------------------- #


def test_verify_keys_are_exactly_twelve_in_prd_order() -> None:
    """§5 的顺序就是界面展示顺序，改动顺序等于改动产品。

    v4.1 把原来的第 8 项「目录页码」拆成两项：`toc`（目录）在前、
    `page_labels`（页码）在后。拆开是因为两者的判定依据完全不同——
    目录看首页内链与文字颜色，页码看 `/PageLabels` 或页脚数字。

    v4.2 又加了 `initial_view`（R38）与 `metadata_security`（R39）两项，并把
    `bookmarks` 从「只看有没有」扩为「还看缩放对不对」（R37）。三项都只读，
    对应的写操作 v2.1 就有，所以 `OPERATION_KEYS` 仍然是 10 项——两边不再等长。

    第 12 项叫 `metadata_security` 而非 `metadata`，是为了不和 `OPERATION_KEYS`
    里的 `metadata` 撞名（§5 要求两张表无交集）。下面的 overlap 断言守着这条。
    """
    assert VERIFY_KEYS == (
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
    assert len(VERIFY_KEYS) == 12
    assert len(set(VERIFY_KEYS)) == 12


def test_every_verify_key_has_a_title_and_requirement() -> None:
    for key in VERIFY_KEYS:
        assert VERIFY_TITLES[key].strip(), f"{key} 缺标题"
        assert VERIFY_REQUIREMENT[key].startswith("R"), f"{key} 需求编号异常"
    # 标题表与需求表不得有多余键：多出来的键意味着有项被悄悄删了但表没清。
    assert set(VERIFY_TITLES) == set(VERIFY_KEYS)
    assert set(VERIFY_REQUIREMENT) == set(VERIFY_KEYS)


def test_verify_requirements_follow_the_prd_order() -> None:
    """前七项沿用 R14–R20；v4.1 把原来的第 8 项拆成 R32（目录）与 R33（页码）；
    v4.2 起书签记 R37（R22 的存在性判据仍在，R37 在其上追加承前缩放），
    并新增 R38（初始视图）与 R39（元数据与安全）。需求编号不必单调递增，
    但顺序必须与 PRD §5 一致。
    """
    assert [VERIFY_REQUIREMENT[key] for key in VERIFY_KEYS] == [
        "R14",
        "R15",
        "R16",
        "R17",
        "R18",
        "R19",
        "R20",
        "R32",
        "R33",
        "R37",
        "R38",
        "R39",
    ]


def test_verify_statuses_are_four_states_with_full_metadata() -> None:
    assert VERIFY_STATUSES == ("pass", "warning", "fail", "skipped")
    for table in (
        VERIFY_STATUS_LABELS,
        VERIFY_STATUS_SEVERITY,
        VERIFY_STATUS_ICONS,
    ):
        assert set(table) == set(VERIFY_STATUSES)


def test_skipped_is_never_dressed_up_as_pass_or_fail() -> None:
    """`skipped` 是「没查」，既不能显示 PASS 也不能显示 FAIL（§7.1）。"""
    assert VERIFY_STATUS_LABELS["skipped"] == "—"
    assert VERIFY_STATUS_ICONS["skipped"] == "—"
    assert VERIFY_STATUS_SEVERITY["skipped"] < VERIFY_STATUS_SEVERITY["warning"]


def test_severity_order_is_fail_over_warning_over_pass() -> None:
    assert (
        VERIFY_STATUS_SEVERITY["fail"]
        > VERIFY_STATUS_SEVERITY["warning"]
        > VERIFY_STATUS_SEVERITY["pass"]
    )


# --------------------------------------------------------------------------- #
# M0-2 七个处理组（v4.1 由六个扩为七个）
# --------------------------------------------------------------------------- #


def test_process_groups_are_seven() -> None:
    assert PROCESS_GROUPS == (
        "g1_bookmarks",
        "g2_metadata",
        "g3_initial_view",
        "g4_link_color",
        "g5_security",
        "g6_flatten",
        "g7_remove_annotations",
    )
    assert set(PROCESS_GROUP_TITLES) == set(PROCESS_GROUPS)


def test_group_mapping_covers_every_operation_exactly() -> None:
    """映射必须覆盖全部 10 项流水线，且不出现不存在的流水线项。"""
    mapped = [key for ops in PROCESS_GROUP_OPERATIONS.values() for key in ops]
    assert set(mapped) == set(OPERATION_KEYS), "分组映射与流水线项不一致"
    assert set(PROCESS_GROUP_OPERATIONS) == set(PROCESS_GROUPS)


def test_every_operation_is_reachable_from_at_least_one_group() -> None:
    for key in OPERATION_KEYS:
        owners = [g for g, ops in PROCESS_GROUP_OPERATIONS.items() if key in ops]
        assert owners, f"{key} 无法从任何处理组勾选到"


def test_metadata_is_shared_by_g2_and_g5_and_deduped() -> None:
    """`metadata` 被两组共享，展开时必须只出现一次（AC-R24-N3）。"""
    assert "metadata" in PROCESS_GROUP_OPERATIONS["g2_metadata"]
    assert "metadata" in PROCESS_GROUP_OPERATIONS["g5_security"]
    expanded = expand_process_groups(["g2_metadata", "g5_security"])
    assert expanded.count("metadata") == 1


def test_expand_keeps_pipeline_order_not_click_order() -> None:
    """用户勾选顺序不能改变流水线顺序，否则线性化会被排到中间。"""
    assert expand_process_groups(["g6_flatten", "g5_security", "g1_bookmarks"]) == (
        "flatten_annotations",
        "bookmarks_inherit_zoom",
        "links_inherit_zoom",
        "metadata",
        "fast_web_view",
    )


def test_expand_ignores_unknown_groups() -> None:
    """老前端可能传来已废弃的键，展开时忽略而不是抛异常。"""
    assert expand_process_groups(["g4_link_color", "g9_gone"]) == ("link_color",)


def test_groups_for_operations_is_the_inverse_of_expand() -> None:
    for group in PROCESS_GROUPS:
        assert group in groups_for_operations(
            expand_process_groups([group])
        ), f"{group} 反推失败"


def test_default_groups_cover_every_operation_except_the_irreversible_two() -> None:
    """默认勾选 g1–g5，展开后是「十项减去两个不可逆项」。

    v4.1 的不可逆项有两个：`flatten_annotations`（把注释画进内容流，画完就分不开）
    与 `remove_annotations`（直接删除，连痕迹都不留）。替用户默认打开其中任何一个
    都不合适，所以两者都不在 `DEFAULT_PROCESS_GROUPS` 里。
    """
    assert DEFAULT_PROCESS_GROUPS == (
        "g1_bookmarks",
        "g2_metadata",
        "g3_initial_view",
        "g4_link_color",
        "g5_security",
    )
    assert "g6_flatten" not in DEFAULT_PROCESS_GROUPS, "扁平化不可逆，默认不能勾"
    assert "g7_remove_annotations" not in DEFAULT_PROCESS_GROUPS, "删除批注不可逆，默认不能勾"
    assert expand_process_groups(DEFAULT_PROCESS_GROUPS) == tuple(
        key
        for key in OPERATION_KEYS
        if key not in ("flatten_annotations", "remove_annotations")
    )


# --------------------------------------------------------------------------- #
# M0-3 十项处理（v4.1 由九项扩为十项）
# --------------------------------------------------------------------------- #


def test_operation_keys_are_ten_and_end_with_fast_web_view() -> None:
    assert len(OPERATION_KEYS) == 10
    assert OPERATION_KEYS[-1] == "fast_web_view"
    assert len(set(OPERATION_KEYS)) == 10


def test_remove_annotations_is_the_new_first_slot() -> None:
    """R34：新项排第 1 位，必须早于扁平化与改色（PRD §6.2 的三条理由）。"""
    assert OPERATION_KEYS[0] == "remove_annotations"
    order = list(OPERATION_KEYS)
    assert order.index("remove_annotations") < order.index("flatten_annotations")
    assert order.index("remove_annotations") < order.index("link_color")


def test_tagged_pdf_is_gone_and_link_color_took_the_ninth_slot() -> None:
    """R30 移除 1 项（AC-R30-N1），R34 又在最前面插入 1 项，于是 link_color 后移一位。"""
    assert "tagged_pdf" not in OPERATION_KEYS
    assert "link_color" in OPERATION_KEYS
    assert OPERATION_KEYS[8] == "link_color"


def test_link_color_runs_after_flatten_and_before_linearize() -> None:
    """§6.2 的两条硬约束：先扁平化再改色；线性化必须最后。"""
    order = list(OPERATION_KEYS)
    assert order.index("flatten_annotations") < order.index("link_color")
    assert order.index("link_color") < order.index("fast_web_view")


def test_every_operation_has_title_and_requirement() -> None:
    assert set(OPERATION_TITLES) == set(OPERATION_KEYS)
    assert set(OPERATION_REQUIREMENT) == set(OPERATION_KEYS)
    for key in OPERATION_KEYS:
        assert OPERATION_TITLES[key].strip()
    # v4.1：link_color 依据从 R25 换成 R35（R25 只改注释 /C，用户看不到变化）
    assert OPERATION_REQUIREMENT["link_color"] == "R35"
    assert OPERATION_REQUIREMENT["remove_annotations"] == "R34"


# --------------------------------------------------------------------------- #
# M0-4 任务状态
# --------------------------------------------------------------------------- #


def test_new_statuses_are_present_and_legacy_ones_are_kept() -> None:
    for status in (
        "verify_queued",
        "verifying",
        "verified",
        "process_queued",
        "processing",
        "post_verifying",
    ):
        assert status in JOB_STATUSES, f"缺少新状态 {status}"
    # 历史任务仍会读到这两个状态，不能从枚举里删掉
    assert "queued" in JOB_STATUSES
    assert "running" in JOB_STATUSES


def test_awaiting_tag_decision_is_no_longer_a_producible_status() -> None:
    """R30：新流程不再产生该状态；老数据由迁移函数兜底。"""
    assert "awaiting_tag_decision" not in JOB_STATUSES


def test_job_statuses_match_the_state_machine() -> None:
    """状态表与迁移图必须一一对应，否则 `transition()` 会踩空。"""
    assert set(TRANSITIONS) == set(JOB_STATUSES)


def test_busy_statuses_cover_both_phases() -> None:
    from app.core.state_machine import BUSY_STATUSES

    assert {"verify_queued", "verifying"} <= BUSY_STATUSES
    assert {"process_queued", "processing", "post_verifying"} <= BUSY_STATUSES
    assert BUSY_STATUSES.isdisjoint({"configuring", "verified"})
    assert BUSY_STATUSES.isdisjoint(
        {"completed", "completed_with_warnings", "failed", "interrupted"}
    )


def test_processing_cannot_publish_without_post_verification() -> None:
    """R27：处理完不能直接进终态，必须经过 `post_verifying`。"""
    assert "completed" not in TRANSITIONS["processing"]
    assert "post_verifying" in TRANSITIONS["processing"]


# --------------------------------------------------------------------------- #
# M0-5 默认配置
# --------------------------------------------------------------------------- #


def test_default_config_has_five_fields_and_no_tag_choice() -> None:
    config = default_config()
    assert set(config) == {
        "exempt_pages_raw",
        "exempt_pages",
        "verify_items",
        "skip_precheck",
        "process_items",
        "write_process_log",
    }
    assert "tag_choice" not in config


def test_default_config_values() -> None:
    config = default_config()
    assert config["exempt_pages_raw"] == ""
    assert config["exempt_pages"] == []
    assert config["verify_items"] == list(VERIFY_KEYS)
    assert config["skip_precheck"] is False
    assert config["process_items"] == list(DEFAULT_PROCESS_GROUPS)
    assert config["write_process_log"] is False


def test_default_config_is_a_fresh_copy_each_time() -> None:
    """共享可变默认值是老 bug 温床：改一个任务的配置不该影响下一个任务。"""
    first = default_config()
    first["verify_items"].append("bogus")
    first["process_items"].clear()
    second = default_config()
    assert second["verify_items"] == list(VERIFY_KEYS)
    assert second["process_items"] == list(DEFAULT_PROCESS_GROUPS)


def test_new_operation_result_has_fixed_shape() -> None:
    result = new_operation_result("link_color")
    assert result["title"] == OPERATION_TITLES["link_color"]
    assert result["status"] in RESULT_STATUSES
    assert set(result["counts"]) == set(COUNT_KEYS)
    assert result["warnings"] == []


# --------------------------------------------------------------------------- #
# M0-6 SCHEMA_VERSION 与惰性迁移
# --------------------------------------------------------------------------- #


def test_schema_version_is_two() -> None:
    assert SCHEMA_VERSION == 2


def _legacy_job(**overrides) -> dict:
    """一份「v2.1 时期」的 job.json：没有 phase/verify/process_items。"""
    job = {
        "id": "job-legacy0001",
        "schema_version": 1,
        "status": "completed",
        "current_stage": None,
        "original_name": "old.pdf",
        "created_at": "2025-01-01T00:00:00Z",
        "started_at": "2025-01-01T00:00:01Z",
        "finished_at": "2025-01-01T00:00:09Z",
        "source": {
            "file_name": "original.pdf",
            "original_name": "old.pdf",
            "sha256": "0" * 64,
            "size": 1024,
            "page_count": 2,
        },
        "output": None,
        "config": {
            "exempt_pages_raw": "",
            "exempt_pages": [],
            "tag_choice": "skip",
        },
        "tag_detection": {
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
        },
        "results": [],
        "last_error": None,
        "generation_id": "gen-legacy",
    }
    job.update(overrides)
    return job


def test_is_legacy_detects_v1_and_tolerates_garbage() -> None:
    assert is_legacy({"schema_version": 1}) is True
    assert is_legacy({"schema_version": 2}) is False
    assert is_legacy({}) is True
    assert is_legacy({"schema_version": "not-a-number"}) is True


def test_migrated_legacy_job_passes_current_validation() -> None:
    """迁移后的老 job 必须能通过当前 `validate_job()`，否则历史页打不开。"""
    migrated = migrate_job(_legacy_job())
    validate_job(migrated)
    assert migrated["schema_version"] == SCHEMA_VERSION
    for field in JOB_V4_FIELDS:
        assert field in migrated, f"迁移后缺少字段 {field}"


def test_migration_fills_verify_and_process_defaults() -> None:
    config = migrate_job(_legacy_job())["config"]
    assert config["verify_items"] == list(VERIFY_KEYS)
    assert config["skip_precheck"] is False
    assert config["process_items"] == list(DEFAULT_PROCESS_GROUPS)
    assert config["write_process_log"] is False


def test_migration_keeps_tag_choice_as_read_only_history() -> None:
    """R30 只要求「不读不显示」，不是「从磁盘抹掉」。"""
    config = migrate_job(_legacy_job())["config"]
    assert config["tag_choice"] == "skip"


def test_migration_maps_awaiting_tag_decision_to_configuring() -> None:
    """R31：退回配置时落在「处理配置」段——标签策略他已经选过了。"""
    migrated = migrate_job(_legacy_job(status="awaiting_tag_decision"))
    assert migrated["status"] == "configuring"
    assert migrated["phase"] == "process"
    validate_job(migrated)


def test_migration_marks_legacy_busy_status_as_interrupted() -> None:
    for status in ("queued", "running"):
        migrated = migrate_job(_legacy_job(status=status, current_stage="metadata"))
        assert migrated["status"] == "interrupted", status
        assert migrated["current_stage"] is None


def test_migration_does_not_touch_a_v2_job() -> None:
    """已经是最新版本的任务，迁移函数必须是恒等变换。"""
    from tests.helpers import make_job

    job = make_job("gen-x")
    snapshot = copy.deepcopy(job)
    assert is_legacy(job) is False
    assert migrate_job(copy.deepcopy(job)) == snapshot


def test_migration_derives_process_groups_from_old_results() -> None:
    """老任务跑过哪些处理项，就反推出对应的分组，而不是一律给默认值。"""
    from tests.helpers import make_result

    job = _legacy_job(
        results=[
            make_result("initial_layout", "adjusted"),
            make_result("fast_web_view", "adjusted"),
        ]
    )
    config = migrate_job(job)["config"]
    assert "g3_initial_view" in config["process_items"]
    assert "g5_security" in config["process_items"]
    assert "g1_bookmarks" not in config["process_items"]


@pytest.mark.parametrize(
    "broken",
    [
        {"config": None},
        {"config": "not-a-dict"},
        {"results": None},
        {"tag_detection": None},
        {"config": {"verify_items": "all"}},
        {"config": {"process_items": ["tagged_pdf"]}},
    ],
)
def test_migration_never_raises_on_broken_history(broken: dict) -> None:
    """AC-R31-E1：一条坏记录不能让整个历史页打不开。"""
    migrated = migrate_job(_legacy_job(**broken))
    validate_job(migrated)


def test_migration_drops_removed_operation_keys_from_config() -> None:
    """老配置里残留 `tagged_pdf` 分组时，迁移后不应把它带进来。"""
    job = _legacy_job(config={"process_items": ["g1_bookmarks", "g6_flatten"]})
    assert migrate_job(job)["config"]["process_items"] == [
        "g1_bookmarks",
        "g6_flatten",
    ]


# --------------------------------------------------------------------------- #
# M0-7 上传上限
# --------------------------------------------------------------------------- #


def test_max_upload_bytes_is_one_gigabyte() -> None:
    """AC-R29-N2：单文件上限 1GB；1GB + 1 字节才该被拒。

    这里只钉常量。真实 800MB / 1.1GB 文件的落盘行为由
    `scripts/live_smoke.py` 在真实文件上验证——单测里造 1GB 文件不现实。
    """
    assert Limits().max_upload_bytes == 1024 * 1024 * 1024


def test_batch_file_count_limit_is_unchanged() -> None:
    """AC-R29-N3：R29 只放宽单文件大小，文件数上限保持 20。"""
    assert Limits().max_batch_files == 20


# --------------------------------------------------------------------------- #
# M0-8 新错误码
# --------------------------------------------------------------------------- #


NEW_ERROR_CODES = (
    "NO_VERIFY_ITEMS",
    "NO_PROCESS_ITEMS",
    "VERIFY_NOT_READY",
    "VERIFY_REPORT_MISSING",
    "PROCESS_LOG_DISABLED",
)


@pytest.mark.parametrize("name", NEW_ERROR_CODES)
def test_new_error_code_exists(name: str) -> None:
    value = getattr(ErrorCode, name)
    assert isinstance(value, str) and value
    assert value == name.lower()


def test_error_codes_are_unique() -> None:
    values = [value for key, value in vars(ErrorCode).items() if key.isupper()]
    assert len(values) == len(set(values)), "错误码出现重复值"
