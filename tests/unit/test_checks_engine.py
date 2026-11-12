"""校验引擎骨架（M1-1）的验收测试。

这一层要证明的不是"某项查得准不准"，而是三条**结构性保证**：

1. 注册表与 `VERIFY_KEYS` 严格同构——少一个键就有需求永远不会被执行；
2. 单项抛异常只把这一项变成 `fail`，其余项照常跑完（AC-R13-E2）；
3. 单项卡死能在超时后返回，不会让整个 worker 停摆（§10.3 第 3 条）。

另外守住「只读」这条底线：跑完全部十二项，原文件的 sha256 必须一个字节都没变
（AC-R13-N4）。
"""

from __future__ import annotations

import hashlib
import time

import pytest

from app.pdf import checks
from app.pdf.checks import (
    CHECK_TIMEOUT_SECONDS,
    CHECKS,
    CheckContext,
    CheckResult,
    Finding,
    make_result,
    run_check,
    run_checks,
)
from app.schemas import VERIFY_KEYS, VERIFY_STATUSES


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #


def test_registry_has_exactly_the_contract_keys():
    """注册表与契约同构。具体项数由 `test_contract_v4` 钉死在 10 项，
    这里只保证两边不各走各的。"""
    assert set(CHECKS) == set(VERIFY_KEYS)
    assert len(CHECKS) == len(VERIFY_KEYS)


def test_registry_order_matches_verify_keys_order():
    # 顺序决定界面上十二项的排列，也决定报告里的段落顺序。
    assert tuple(CHECKS) == tuple(VERIFY_KEYS)


def test_every_registered_runner_is_callable():
    for key, runner in CHECKS.items():
        assert callable(runner), key


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #


def test_make_result_rejects_unknown_status():
    with pytest.raises(ValueError):
        make_result("file_size", "ok", "不该存在的第五种状态")


def test_make_result_accepts_every_contract_status():
    for status in VERIFY_STATUSES:
        assert make_result("file_size", status, "x").status == status


def test_finding_omits_absent_page_and_detail():
    payload = Finding(level="pass", message="没问题").as_dict()
    # 省略 None 而不是写 null：前端用 `"page" in item` 判断要不要渲染页码。
    assert payload == {"level": "pass", "message": "没问题"}


def test_finding_keeps_zero_page_number():
    # 第 0 页不存在，但这里守的是"别用真值判断"，否则 page=0 会被吞掉。
    assert Finding(level="fail", message="x", page=0).as_dict()["page"] == 0


def test_check_result_as_dict_is_complete():
    result = make_result(
        "file_size",
        "warning",
        "偏大",
        [Finding(level="warning", message="接近上限", page=2, detail="说明")],
    )
    result.duration_ms = 7
    payload = result.as_dict()
    assert payload == {
        "key": "file_size",
        "status": "warning",
        "summary": "偏大",
        "findings": [{"level": "warning", "message": "接近上限", "page": 2, "detail": "说明"}],
        "duration_ms": 7,
    }


def test_findings_satisfy_the_contract_validator():
    from app.schemas import validate_finding

    # 字段名必须与契约校验器对得上，否则报告落盘时才炸。
    validate_finding(Finding(level="pass", message="3 个书签").as_dict())
    validate_finding(
        Finding(level="warning", message="接近上限", page=2, detail="说明").as_dict()
    )


def test_results_compose_into_a_valid_verify_result(check_ctx):
    from app.schemas import validate_verify_result

    items = [item.as_dict() for item in run_checks(VERIFY_KEYS, check_ctx("check_clean.pdf"))]
    validate_verify_result(
        {
            "job_id": "j1",
            "phase": "precheck",
            "generated_at": "2025-01-01T00:00:00Z",
            "file": {"name": "check_clean.pdf", "sha256": "0" * 64, "size": 1, "page_count": 8},
            "items": items,
            "summary": {status: 0 for status in VERIFY_STATUSES},
        }
    )


# --------------------------------------------------------------------------- #
# 单项隔离
# --------------------------------------------------------------------------- #


def test_exception_in_one_check_becomes_fail_without_stopping_others(check_ctx, monkeypatch):
    def boom(_ctx):
        raise RuntimeError("字体表炸了")

    monkeypatch.setitem(CHECKS, "font_embedding", boom)
    results = run_checks(VERIFY_KEYS, check_ctx("check_clean.pdf"))

    by_key = {item.key: item for item in results}
    assert len(results) == len(VERIFY_KEYS)
    assert by_key["font_embedding"].status == "fail"
    assert "RuntimeError" in by_key["font_embedding"].summary
    assert by_key["font_embedding"].findings[0].detail == "字体表炸了"
    # 其余各项不受影响
    assert by_key["file_size"].status == "pass"
    assert by_key["filename_naming"].status == "pass"


def test_only_the_first_line_of_the_error_is_kept(check_ctx, monkeypatch):
    def boom(_ctx):
        raise ValueError("第一行\n第二行\n第三行")

    monkeypatch.setitem(CHECKS, "text_layer", boom)
    result = run_check("text_layer", check_ctx("check_clean.pdf"))
    assert result.findings[0].detail == "第一行"


def test_runner_returning_wrong_type_becomes_fail(check_ctx, monkeypatch):
    monkeypatch.setitem(CHECKS, "page_labels", lambda _ctx: {"status": "pass"})
    result = run_check("page_labels", check_ctx("check_clean.pdf"))
    assert result.status == "fail"
    assert result.summary == "检查未返回结果"


def test_runner_returning_none_becomes_fail(check_ctx, monkeypatch):
    monkeypatch.setitem(CHECKS, "bookmarks", lambda _ctx: None)
    result = run_check("bookmarks", check_ctx("check_clean.pdf"))
    assert result.status == "fail"
    assert result.summary == "检查未返回结果"


def test_unknown_key_raises_key_error(check_ctx):
    # 未知键是**编程错误**，不该被静默吞成 fail —— 否则注册表写错名字
    # 会表现成"这项永远失败"，很难定位。
    with pytest.raises(KeyError):
        run_check("no_such_check", check_ctx("check_clean.pdf"))


# --------------------------------------------------------------------------- #
# 超时
# --------------------------------------------------------------------------- #


def test_slow_check_is_cut_off_by_timeout(check_ctx, monkeypatch):
    def slow(_ctx):
        time.sleep(2.0)
        return make_result("annotations", "pass", "本来想返回这个")

    monkeypatch.setitem(CHECKS, "annotations", slow)
    started = time.perf_counter()
    result = run_checks(["annotations"], check_ctx("check_clean.pdf"), timeout=0.05)[0]
    elapsed = time.perf_counter() - started

    assert result.status == "fail"
    assert "超时" in result.summary
    # 真的提前返回了，而不是等满 2 秒
    assert elapsed < 1.5


def test_timeout_does_not_poison_the_following_checks(check_ctx, monkeypatch):
    def slow(_ctx):
        time.sleep(2.0)

    monkeypatch.setitem(CHECKS, "annotations", slow)
    results = run_checks(
        ["annotations", "file_size"], check_ctx("check_clean.pdf"), timeout=0.05
    )
    assert results[0].status == "fail"
    assert results[1].status == "pass"


def test_default_timeout_is_sixty_seconds():
    assert CHECK_TIMEOUT_SECONDS == 60.0


# --------------------------------------------------------------------------- #
# 只读与顺序
# --------------------------------------------------------------------------- #


def test_all_checks_leave_the_file_byte_identical(check_ctx):
    ctx = check_ctx("check_clean.pdf")
    before = _sha256(ctx.path)
    run_checks(VERIFY_KEYS, ctx)
    assert _sha256(ctx.path) == before


def test_results_keep_the_requested_order(check_ctx):
    keys = ["bookmarks", "file_size", "filename_naming"]
    assert [item.key for item in run_checks(keys, check_ctx("check_clean.pdf"))] == keys


def test_duration_is_recorded_for_real_runs(check_ctx):
    for result in run_checks(VERIFY_KEYS, check_ctx("check_clean.pdf")):
        assert result.duration_ms >= 0
        assert isinstance(result.duration_ms, int)


def test_check_context_is_frozen(check_ctx):
    # 校验项不该顺手往 context 里塞缓存——那会让前后置校验互相污染。
    import dataclasses

    ctx = check_ctx("check_clean.pdf")
    assert dataclasses.is_dataclass(ctx)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.page_count = 99  # type: ignore[misc]


def test_module_exposes_only_the_documented_api():
    for name in checks.__all__:
        assert hasattr(checks, name), name


def test_check_result_is_mutable_only_for_duration():
    result = CheckResult(key="file_size", status="pass", summary="x")
    result.duration_ms = 12
    assert result.as_dict()["duration_ms"] == 12
