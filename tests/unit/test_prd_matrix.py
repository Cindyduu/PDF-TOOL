"""阶段 A：PRD 与验收基线的一致性检查。

这些测试不检查实现，只检查 PRD 本身是否构成一个无歧义的验收基线：
- 十一项需求 R1–R11 都有编号、都有正常验收和异常验收；
- 九项处理操作键齐全且顺序固定；
- 五种结果状态齐全；
- 不出现「完全合规」这类结论性表述（只允许出现在禁止性语句中）；
- 豁免语义表把九项都覆盖到，且只有 R1/R2/R9 受影响。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PRD_PATH = Path(__file__).resolve().parents[2] / "PRD.md"

OPERATION_KEYS = [
    "flatten_annotations",
    "bookmarks_inherit_zoom",
    "links_inherit_zoom",
    "initial_layout",
    "initial_zoom",
    "bookmark_panel",
    "metadata",
    "tagged_pdf",
    "fast_web_view",
]

STATUSES = ["adjusted", "unchanged", "not_applicable", "review_required", "failed"]

TASK_STATUSES = [
    "configuring",
    "awaiting_tag_decision",
    "queued",
    "running",
    "completed",
    "completed_with_warnings",
    "failed",
    "interrupted",
]

REQUIREMENT_IDS = [f"R{i}" for i in range(1, 12)]


@pytest.fixture(scope="module")
def prd_text() -> str:
    assert PRD_PATH.is_file(), f"PRD.md 不存在：{PRD_PATH}"
    return PRD_PATH.read_text(encoding="utf-8")


def test_prd_exists_and_not_empty(prd_text: str) -> None:
    assert len(prd_text) > 2000


@pytest.mark.parametrize("req_id", REQUIREMENT_IDS)
def test_each_requirement_has_heading(prd_text: str, req_id: str) -> None:
    assert re.search(rf"^### {req_id} ", prd_text, flags=re.MULTILINE), (
        f"PRD 缺少 {req_id} 的章节标题"
    )


@pytest.mark.parametrize("req_id", REQUIREMENT_IDS)
def test_each_requirement_has_normal_and_exception_acceptance(
    prd_text: str, req_id: str
) -> None:
    normal = re.findall(rf"AC-{req_id}-N\d+", prd_text)
    exception = re.findall(rf"AC-{req_id}-E\d+", prd_text)
    assert normal, f"{req_id} 缺少正常验收条目（AC-{req_id}-N*）"
    assert exception, f"{req_id} 缺少异常验收条目（AC-{req_id}-E*）"
    # 编号不得重复
    assert len(normal) == len(set(normal)), f"{req_id} 正常验收编号重复"
    assert len(exception) == len(set(exception)), f"{req_id} 异常验收编号重复"


@pytest.mark.parametrize("key", OPERATION_KEYS)
def test_operation_keys_present(prd_text: str, key: str) -> None:
    assert f"`{key}`" in prd_text, f"PRD 缺少操作键 {key}"


def test_operation_key_order_is_fixed(prd_text: str) -> None:
    """九项处理的操作键必须按流水线顺序出现。"""
    positions = [prd_text.index(f"`{key}`") for key in OPERATION_KEYS]
    assert positions == sorted(positions), "操作键在 PRD 中的顺序与流水线顺序不一致"


@pytest.mark.parametrize("status", STATUSES)
def test_result_statuses_present(prd_text: str, status: str) -> None:
    assert f"`{status}`" in prd_text, f"PRD 缺少结果状态 {status}"


@pytest.mark.parametrize("status", TASK_STATUSES)
def test_task_statuses_present(prd_text: str, status: str) -> None:
    assert status in prd_text, f"PRD 缺少任务状态 {status}"


def test_no_compliance_claim_allowed(prd_text: str) -> None:
    """「完全合规」只允许出现在禁止性语句里，不得作为产品结论。"""
    offenders = []
    for lineno, line in enumerate(prd_text.splitlines(), start=1):
        if "完全合规" not in line:
            continue
        if any(word in line for word in ("禁止", "不得", "不出现", "不能", "不允许")):
            continue
        offenders.append(f"{lineno}: {line.strip()}")
    assert not offenders, "PRD 出现结论性合规表述：" + "; ".join(offenders)


def test_markinfo_is_not_real_tagging(prd_text: str) -> None:
    """必须明确「只设 MarkInfo 不算加标签」，消除歧义。"""
    assert "`/MarkInfo`" in prd_text
    assert "冒充" in prd_text or "不算加标签" in prd_text or "不允许" in prd_text
    assert "markinfo_only" in prd_text


def test_exemption_semantics_cover_all_nine_operations(prd_text: str) -> None:
    """豁免语义表必须逐项覆盖九项处理，且只有 R1/R2/R9 受豁免影响。"""
    table_lines = [
        line
        for line in prd_text.splitlines()
        if line.startswith("|") and re.search(r"\| (R[1-9]|R1[0-9])? ?", line)
    ]
    exemption_rows = {}
    for line in prd_text.splitlines():
        m = re.match(r"^\| (R\d) [^|]+\| [^|]+\| (是|否) \|$", line.strip())
        if m:
            exemption_rows[m.group(1)] = m.group(2)
    expected = {
        "R1": "是",
        "R2": "是",
        "R9": "是",
        "R3": "否",
        "R4": "否",
        "R5": "否",
        "R6": "否",
        "R7": "否",
        "R8": "否",
    }
    assert exemption_rows == expected, (
        f"豁免语义表不完整或语义错误，实际={exemption_rows}"
    )
    assert table_lines  # 表存在


def test_error_contract_has_all_fields(prd_text: str) -> None:
    for field in ("code", "message", "details", "retryable", "next_action"):
        assert f"`{field}`" in prd_text, f"错误契约缺少字段 {field}"


def test_signature_pdf_is_rejected_by_default(prd_text: str) -> None:
    assert "signed_pdf" in prd_text
    assert "无签名" in prd_text


def test_license_note_present(prd_text: str) -> None:
    assert "AGPL" in prd_text, "必须记录 PyMuPDF 的 AGPL 许可提醒"
