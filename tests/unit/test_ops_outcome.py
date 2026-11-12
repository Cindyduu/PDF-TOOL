"""`OpOutcome` 与状态判定。

状态只允许在一个地方判定，所以这里的用例本质上是在钉住那条判定顺序：
**失败优先**，其次「改过」，再次「没东西可查」，最后「已符合/已豁免」。
"""

from __future__ import annotations

import pytest

from app.pdf.outcome import OpOutcome, decide_status, new_counts
from app.schemas import COUNT_KEYS, validate_operation_result


def test_new_counts_rejects_unknown_key():
    with pytest.raises(KeyError):
        new_counts(checked=1, checkedd=2)


def test_new_counts_starts_from_full_six_tuple():
    counts = new_counts(modified=2)
    assert tuple(counts) == COUNT_KEYS
    assert counts["modified"] == 2
    assert counts["checked"] == 0


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        # 失败优先：哪怕改成功了一部分，也不能算完成
        ({"checked": 5, "modified": 4, "failed": 1}, "failed"),
        # 有修改即「已调整」
        ({"checked": 3, "modified": 1}, "adjusted"),
        # 什么都没查到
        ({}, "not_applicable"),
        # 查过，且全部已符合或豁免
        ({"checked": 4, "compliant": 3, "exempt": 1}, "unchanged"),
        # 查过但全部不适用
        ({"checked": 2, "not_applicable": 2}, "not_applicable"),
        # 部分符合、部分不适用，也算「无需修改」
        ({"checked": 2, "compliant": 1, "not_applicable": 1}, "not_applicable"),
    ],
)
def test_decide_status_order(counts, expected):
    merged = new_counts(**counts)
    assert decide_status(merged) == expected


def test_outcome_fills_missing_count_keys_and_infers_changed():
    outcome = OpOutcome(counts={"checked": 2, "modified": 2})
    assert tuple(outcome.counts) == COUNT_KEYS
    assert outcome.status == "adjusted"
    assert outcome.changed is True


def test_outcome_does_not_infer_changed_when_nothing_modified():
    outcome = OpOutcome(counts=new_counts(checked=1, compliant=1))
    assert outcome.status == "unchanged"
    assert outcome.changed is False


def test_outcome_explicit_changed_overrides_counts():
    """线性化是「保存时才生效」，处理器阶段必须能自己声明改过。"""
    outcome = OpOutcome(counts=new_counts(checked=1, modified=1), changed=True)
    assert outcome.changed is True


def test_warn_deduplicates_and_skips_empty():
    outcome = OpOutcome()
    outcome.warn("同一句")
    outcome.warn("同一句")
    outcome.warn("")
    assert outcome.warnings == ["同一句"]


def test_to_result_matches_schema_contract():
    outcome = OpOutcome(
        before={"a": 1},
        after={"a": 2},
        counts=new_counts(checked=1, modified=1),
        reason_code="demo",
        message="改了",
        next_action="无需操作",
    )
    outcome.warn("注意")
    result = outcome.to_result("metadata", started_at="t0", finished_at="t1")

    validate_operation_result(result)
    assert result["key"] == "metadata"
    assert result["status"] == "adjusted"
    assert result["before"] == {"a": 1}
    assert result["after"] == {"a": 2}
    assert result["counts"]["modified"] == 1
    assert result["warnings"] == ["注意"]
    assert result["started_at"] == "t0"
    assert result["finished_at"] == "t1"


def test_to_result_keeps_counts_copy_independent():
    outcome = OpOutcome(counts=new_counts(checked=1, modified=1))
    result = outcome.to_result("metadata")
    result["counts"]["modified"] = 99
    assert outcome.counts["modified"] == 1
