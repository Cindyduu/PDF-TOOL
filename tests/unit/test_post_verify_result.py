"""AC-R27-E1：后置校验「整块拿不到结论」时构造的那份结果。

这个构造器存在的唯一理由是：**不能只标一项 fail**。只标一项等于默许另外
八项"通过"，而那些项根本没跑过——那是凭空造出来的结论，比不报还危险。
所以这里的断言集中在两件事上：

1. 十二项全都记 `fail`，每一项都带上同一句原因（用户看到的是"这份产物没通过
   后置校验"，不是"第 3 项碰巧出错"）；
2. 返回的仍是一份**结构完整**的结果，而不是 `None`。`None` 在页面上的含义是
   「没执行」，两种情形必须能区分。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas import VERIFY_KEYS, validate_verify_result
from app.services.verify import PHASE_POST, PHASE_PRECHECK, build_failed_verify_result


def _job(verify_items: list[str] | None = None) -> dict:
    """最小可用的 job 快照——构造器只读 `id` 与 `config.verify_items`。"""
    return {
        "id": "job-0123456789abcdef",
        "config": {
            "verify_items": list(VERIFY_KEYS) if verify_items is None else verify_items
        },
    }


REASON = "后置校验未能执行：无法打开 PDF：FileDataError"


def test_every_check_is_marked_fail_not_just_one():
    result = build_failed_verify_result(_job(), filename="doc.pdf", reason=REASON)
    assert [item["key"] for item in result["items"]] == list(VERIFY_KEYS)
    assert {item["status"] for item in result["items"]} == {"fail"}


def test_each_item_carries_the_same_reason_and_a_fail_finding():
    result = build_failed_verify_result(_job(), filename="doc.pdf", reason=REASON)
    for item in result["items"]:
        assert item["summary"] == REASON
        assert len(item["findings"]) == 1
        finding = item["findings"][0]
        assert finding["level"] == "fail"
        assert finding["message"] == REASON
        assert finding["detail"], "明细里必须有下一步动作，否则用户不知道怎么办"


def test_summary_counts_all_nine_as_fail():
    result = build_failed_verify_result(_job(), filename="doc.pdf", reason=REASON)
    assert result["summary"] == {
        "pass": 0,
        "warning": 0,
        "fail": len(VERIFY_KEYS),
        "skipped": 0,
    }


def test_result_is_structurally_complete_and_passes_the_schema():
    """必须是完整结构而不是 `None`——`None` 的语义是「没执行」。"""
    result = build_failed_verify_result(_job(), filename="doc.pdf", reason=REASON)
    assert isinstance(result, dict)
    validate_verify_result(result)
    assert result["phase"] == PHASE_POST
    assert result["job_id"] == "job-0123456789abcdef"
    assert result["file"]["name"] == "doc.pdf"
    assert result["generated_at"]


def test_durations_are_zero_because_nothing_ran():
    result = build_failed_verify_result(_job(), filename="doc.pdf", reason=REASON)
    assert {item["duration_ms"] for item in result["items"]} == {0}


def test_missing_path_records_zero_size_instead_of_raising(tmp_path: Path):
    """产物读不出来正是这个构造器的使用场景，它自己不能再因为读不到而抛。"""
    gone = tmp_path / "not-here.pdf"
    result = build_failed_verify_result(
        _job(), filename="doc.pdf", reason=REASON, path=gone
    )
    assert result["file"]["size_bytes"] == 0
    assert result["file"]["page_count"] == 0


def test_existing_path_records_its_real_size(tmp_path: Path):
    target = tmp_path / "pending.pdf"
    target.write_bytes(b"%PDF-1.7 broken")
    result = build_failed_verify_result(
        _job(), filename="doc.pdf", reason=REASON, path=target
    )
    assert result["file"]["size_bytes"] == len(b"%PDF-1.7 broken")


def test_next_action_replaces_the_default_detail():
    result = build_failed_verify_result(
        _job(), filename="doc.pdf", reason=REASON, next_action="请关闭其他程序后重试"
    )
    assert result["items"][0]["findings"][0]["detail"] == "请关闭其他程序后重试"


def test_only_selected_items_are_listed():
    """构造器尊重 `config.verify_items`；"补足十二项"是 worker 的责任，不是它的。"""
    picked = ["filename_naming", "file_size"]
    result = build_failed_verify_result(
        _job(picked), filename="doc.pdf", reason=REASON
    )
    assert [item["key"] for item in result["items"]] == picked


def test_unknown_phase_is_rejected():
    with pytest.raises(ValueError):
        build_failed_verify_result(
            _job(), filename="doc.pdf", reason=REASON, phase="afterwards"
        )


def test_phase_constants_are_shared_with_the_checks_package():
    """两处各写一份常量，值必须一致，否则报告里的 phase 会和校验项的判断对不上。"""
    from app.pdf import checks as checks_pkg

    assert (checks_pkg.PHASE_PRECHECK, checks_pkg.PHASE_POST) == (
        PHASE_PRECHECK,
        PHASE_POST,
    )
