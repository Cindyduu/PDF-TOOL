"""M6-2 / M7-3 / M8-6：PRD 的验收矩阵 —— 每条需求都有实现、也有测试。

这个文件不测业务逻辑，它测的是**矩阵本身没有洞**：

1. **文档完整**：R13–R39 每节有标题、有 `AC-N*`、有 `AC-E*`，编号不重复。
   v2.1 的 R1–R12 仍完整留在 `PRD.md` 里，v4.0 只做增量，不覆盖历史基线。
2. **契约对齐**：PRD §5 / §6.1 / §6.2 / §7 的表格逐行与 `app/schemas.py`
   的常量一致。表格给人看，常量给代码看，两边必须同源——只改常量不改文档，
   下一个人就会照着文档写出错的东西。
3. **矩阵无缺口**：`REQUIREMENT_MATRIX` 为 R13–R31 每一条登记实现文件与测试文件；
   `V41_REQUIREMENT_MATRIX` 为 R32–R36、`V42_REQUIREMENT_MATRIX` 为 R37–R39 同理。
   文件必须真的存在、测试文件必须真的有 `def test_`，且至少有一个该需求的 AC
   编号被具名引用。登记表少一条、写错一个路径，都会失败。

**为什么每轮增量单独一张表**：R32–R36 不在 v4.0 的 `### Rn 标题` 体系里，而是写在
§16「第二轮开发计划（v4.1 增量）」下，标题形如 `### 16.1 R32 目录校验`；
R37–R39 写在 §17「第三轮开发计划（v4.2 增量）」下，形如 `### 17.1 R37 书签承前缩放校验`。
章节形状不同，解析规则就得分开——把 37–39 塞进 `V4_REQUIREMENT_IDS` 会让
`^### R37 ` 这类标题检查全部落空。

为什么第 3 条要查「具名引用」而不是只查文件存在：只查存在的话，把任何测试
文件名填进去都能过——那只是抄了一遍目录树。要求测试里出现 `AC-R{n}-`，
才说明这条需求真的被人对着验收条目写过断言。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.schemas import (
    DEFAULT_PROCESS_GROUPS,
    EXEMPT_SENSITIVE_OPERATIONS,
    JOB_STATUSES,
    OPERATION_KEYS,
    PROCESS_GROUP_OPERATIONS,
    PROCESS_GROUPS,
    RESULT_STATUSES,
    VERIFY_KEYS,
    VERIFY_REQUIREMENT,
    VERIFY_STATUSES,
)

ROOT = Path(__file__).resolve().parents[2]
PRD_V4_PATH = ROOT / "PRDv4.0.md"
PRD_V21_PATH = ROOT / "PRD.md"

V4_REQUIREMENT_IDS = tuple(f"R{i}" for i in range(13, 32))
V41_REQUIREMENT_IDS = tuple(f"R{i}" for i in range(32, 37))
V42_REQUIREMENT_IDS = ("R37", "R38", "R39")
V21_REQUIREMENT_IDS = tuple(f"R{i}" for i in range(1, 13))

#: v4.1 增量的章节标题形状：`### 16.1 R32 目录校验（新，只读）`。
#:
#: 不能沿用 `^### R{n} `：§16 的小节都挂在 16.x 下面，一级标题里没有 `### R32`。
_V41_HEADING = re.compile(r"^### 16\.\d+ (R\d+) (.+)$", flags=re.MULTILINE)

#: v4.2 增量的章节标题形状：`### 17.1 R37 书签承前缩放校验（增强 R22）`。
_V42_HEADING = re.compile(r"^### 17\.\d+ (R\d+) (.+)$", flags=re.MULTILINE)


# --------------------------------------------------------------------------- #
# 需求 -> 实现 / 测试 登记表
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Requirement:
    """一条 v4.0 需求的可追溯信息。

    `title` 必须出现在 PRD 该节标题行里（用来抓「改了标题忘了改登记表」）。
    """

    title: str
    impl: tuple[str, ...]
    tests: tuple[str, ...]


REQUIREMENT_MATRIX: dict[str, Requirement] = {
    "R13": Requirement(
        title="两阶段流程与统一入口",
        impl=(
            "app/api/jobs.py",
            "app/services/jobs.py",
            "app/core/worker.py",
            "app/static/js/views/prepare.js",
        ),
        tests=(
            "tests/integration/test_verify_flow.py",
            "tests/integration/test_static_v4_ui.py",
        ),
    ),
    "R14": Requirement(
        title="文件命名规范校验",
        impl=("app/pdf/checks/naming.py",),
        tests=("tests/unit/test_checks_naming.py",),
    ),
    "R15": Requirement(
        title="文件大小校验",
        impl=("app/pdf/checks/size.py",),
        tests=("tests/unit/test_checks_size.py",),
    ),
    "R16": Requirement(
        title="外部链接校验",
        # 遍历复用 links.py，不另写一套 /Annots 扫描（PRD 明写）。
        impl=("app/pdf/checks/external_links.py", "app/pdf/links.py"),
        tests=("tests/unit/test_checks_links.py",),
    ),
    "R17": Requirement(
        title="页面方向校验",
        impl=("app/pdf/checks/orientation.py",),
        tests=("tests/unit/test_checks_orientation.py",),
    ),
    "R18": Requirement(
        title="文本可提取性校验",
        impl=("app/pdf/checks/text_layer.py",),
        tests=("tests/unit/test_checks_text_layer.py",),
    ),
    "R19": Requirement(
        title="批注与高亮校验",
        impl=("app/pdf/checks/annotations.py",),
        tests=("tests/unit/test_checks_annotations.py",),
    ),
    "R20": Requirement(
        title="字体嵌入校验",
        impl=("app/pdf/checks/fonts.py",),
        tests=("tests/unit/test_checks_fonts.py",),
    ),
    "R21": Requirement(
        title="页码校验",
        # v4.1 起这一条按用户要求拆成两项：目录（R32，`checks/toc.py`）与页码
        # （R33，`checks/page_labels.py`）。R21 这一节作为历史基线仍留在 PRD 里，
        # 它的 `/PageLabels` 判据被 R33 原样继承，所以这里仍登记 page_labels。
        #
        # 标题也跟着从「目录页码校验」收窄成「页码校验」——两件事混在一个标题里，
        # 读的人会以为这一项还在管目录。PRD 里改的是标题，AC-R21-* 的编号不动
        # （`test_checks_page_labels.py` 的 docstring 还引用着它们）。
        impl=("app/pdf/checks/page_labels.py", "app/pdf/view_labels.py"),
        tests=("tests/unit/test_checks_page_labels.py",),
    ),
    "R22": Requirement(
        title="书签存在性校验",
        impl=("app/pdf/checks/outlines.py",),
        tests=("tests/unit/test_checks_outlines.py",),
    ),
    "R23": Requirement(
        title="校验结果页与 HTML 校验报告",
        impl=(
            "app/report/verify_html.py",
            "app/api/jobs.py",
            "app/static/js/views/verify.js",
        ),
        tests=(
            "tests/unit/test_report_verify_html.py",
            "tests/integration/test_report_endpoints.py",
        ),
    ),
    "R24": Requirement(
        title="自动处理页与处理项",
        impl=("app/static/js/views/process.js", "app/schemas.py"),
        tests=(
            "tests/unit/test_contract_v4.py",
            "tests/integration/test_static_v4_ui.py",
        ),
    ),
    "R25": Requirement(
        title="内链颜色修正",
        impl=("app/pdf/link_color.py",),
        tests=("tests/unit/test_link_color.py",),
    ),
    "R26": Requirement(
        title="注释扁平化",
        impl=("app/pdf/annotations.py",),
        tests=("tests/unit/test_ops_annotations.py",),
    ),
    "R27": Requirement(
        title="后置校验",
        impl=("app/core/worker.py", "app/services/verify.py"),
        tests=(
            "tests/integration/test_post_verify.py",
            "tests/unit/test_post_verify_result.py",
        ),
    ),
    "R28": Requirement(
        title="处理日志（HTML）",
        impl=("app/report/process_log_html.py", "app/core/worker.py"),
        tests=(
            "tests/unit/test_report_process_log.py",
            "tests/integration/test_post_verify.py",
        ),
    ),
    "R29": Requirement(
        title="上传上限与加密 PDF 策略",
        impl=("app/config.py", "app/services/jobs.py"),
        tests=(
            "tests/unit/test_contract_v4.py",
            "tests/integration/test_upload.py",
        ),
    ),
    "R30": Requirement(
        title="移除 tagged_pdf",
        impl=("app/schemas.py", "app/pdf/pipeline.py", "app/core/migrate.py"),
        tests=(
            "tests/unit/test_contract_v4.py",
            "tests/integration/test_pipeline_nontag.py",
        ),
    ),
    "R31": Requirement(
        title="兼容与迁移",
        impl=("app/core/migrate.py", "app/services/jobs.py"),
        tests=("tests/unit/test_contract_v4.py",),
    ),
}


#: v4.1 增量（§16）的登记表。`title` 只需是该节标题的**子串**——标题里那些
#: 括号补充（「（新，只读）」「（改文字填充色）」）属于当轮说明，会随迭代改写，
#: 把它们钉进登记表只会制造无意义的失败。
V41_REQUIREMENT_MATRIX: dict[str, Requirement] = {
    "R32": Requirement(
        title="目录校验",
        # 只读校验，落在 checks/ 下，走和其它校验项同一套四态出口。
        impl=("app/pdf/checks/toc.py",),
        tests=("tests/unit/test_checks_toc.py",),
    ),
    "R33": Requirement(
        title="页码校验",
        # R21 收窄而来：判据仍是 `/PageLabels`，但「目录」不再混在这一项里。
        # `view_labels.py` 是 `/PageLabels` 的读取与格式化，两个需求共用。
        impl=("app/pdf/checks/page_labels.py", "app/pdf/view_labels.py"),
        tests=("tests/unit/test_checks_page_labels.py",),
    ),
    "R34": Requirement(
        title="清除高亮和批注",
        # 写操作，且不可逆——所以它在 `EXEMPT_SENSITIVE_OPERATIONS` 里，
        # 也默认不勾选（见 `DEFAULT_PROCESS_GROUPS` 只有 g1–g5）。
        impl=("app/pdf/remove_annotations.py",),
        tests=("tests/unit/test_ops_remove_annotations.py",),
    ),
    "R35": Requirement(
        title="内链颜色重做",
        # 两行都登记：真正的实现在 content_color.py，link_color.py 是流水线
        # 注册表指向的薄转发层。只登记前者会让 `pipeline.py` 的入口无人看管，
        # 只登记后者则会把「已删掉的旧实现」当成实现。
        impl=("app/pdf/content_color.py", "app/pdf/link_color.py"),
        tests=("tests/unit/test_link_color.py",),
    ),
    "R36": Requirement(
        title="内容一致性护栏",
        # 护栏本身是只读比对：算指纹、比差异、把差异归因到已勾选的处理项。
        impl=("app/pdf/content_fingerprint.py",),
        tests=(
            "tests/unit/test_content_fingerprint.py",
            "tests/integration/test_pipeline_nontag.py",
        ),
    ),
}


#: v4.2 增量（§17）的登记表。三条需求**全部落在校验侧**，所以 impl 里既有
#: `checks/` 的新模块，也有被复用的处理侧模块——复用的那几个正是「同口径」的
#: 实现证据：校验不新写遍历，直接用处理侧那套函数，否则两边迟早分叉。
V42_REQUIREMENT_MATRIX: dict[str, Requirement] = {
    "R37": Requirement(
        title="书签承前缩放校验",
        # `checks/outlines.py` 是判定入口；另外两个是它复用的处理侧模块。
        # 把复用的模块也登记进来，是为了让「口径同源」这件事有文件可查——
        # 将来谁想在校验里另写一套遍历，会先看到这三行。
        impl=(
            "app/pdf/checks/outlines.py",
            "app/pdf/bookmarks.py",
            "app/pdf/destinations.py",
        ),
        tests=("tests/unit/test_checks_outlines.py",),
    ),
    "R38": Requirement(
        title="初始视图校验",
        # `initial_view.py` 的 `open_action_kind()` / `zoom_view()` 同时供处理项 ③
        # 与校验使用；`view_labels.py` 是唯一翻译层（中文取值只有一份）。
        impl=(
            "app/pdf/checks/initial_view.py",
            "app/pdf/initial_view.py",
            "app/pdf/view_labels.py",
        ),
        tests=("tests/unit/test_checks_initial_view.py",),
    ),
    "R39": Requirement(
        title="元数据与安全校验",
        # 校验按「整个 /Info 存不存在」算，与 `metadata.process()` 删整个 /Info 对齐。
        impl=("app/pdf/checks/metadata.py", "app/pdf/metadata.py"),
        tests=("tests/unit/test_checks_metadata.py",),
    ),
}


# --------------------------------------------------------------------------- #
# 夹具与解析工具
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def prd_v4() -> str:
    assert PRD_V4_PATH.is_file(), f"PRDv4.0.md 不存在：{PRD_V4_PATH}"
    return PRD_V4_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def prd_v21() -> str:
    assert PRD_V21_PATH.is_file(), f"PRD.md 不存在：{PRD_V21_PATH}"
    return PRD_V21_PATH.read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    """截取 `start` 与 `end` 之间的正文；两个标记都必须存在。"""
    begin = text.index(start)
    stop = text.index(end, begin + len(start))
    return text[begin:stop]


_VERIFY_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*`([a-z_]+)`\s*\|\s*([^|]+?)\s*\|\s*(R\d+)\s*\|"
    r"\s*([^|]+?)\s*\|\s*$"
)
_OPERATION_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*`([a-z_]+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|"
    r"\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$"
)
_GROUP_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*`(g\d_[a-z_]+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|"
    r"\s*([^|]+?)\s*\|\s*(\*{0,2}不?勾\*{0,2})\s*\|\s*$"
)
_STATUS_ROW = re.compile(r"^\|\s*`([a-z]+)`\s*\|")


def _verify_table(prd: str) -> list[tuple[str, str, str]]:
    """§5 校验项表 → [(key, 名称, 需求编号)]，按表格顺序。"""
    rows = []
    for line in _section(prd, "## 5. 校验项键表", "## 6. 处理项与流水线键表").splitlines():
        m = _VERIFY_ROW.match(line.strip())
        if m:
            rows.append((m.group(2), m.group(3), m.group(4)))
    return rows


def _operation_table(prd: str) -> list[tuple[str, str, str]]:
    """§6.2 流水线项表 → [(key, 名称, 需求)]，按表格顺序。"""
    rows = []
    body = _section(prd, "### 6.2 流水线项", "## 7. 状态常量")
    for line in body.splitlines():
        m = _OPERATION_ROW.match(line.strip())
        if m:
            rows.append((m.group(2), m.group(3), m.group(5)))
    return rows


def _group_table(prd: str) -> list[tuple[str, tuple[str, ...], bool]]:
    """§6.1 UI 处理项表 → [(group, 映射到的流水线项, 是否默认勾选)]。"""
    rows = []
    body = _section(prd, "### 6.1 UI 处理项", "### 6.2 流水线项")
    for line in body.splitlines():
        m = _GROUP_ROW.match(line.strip())
        if m:
            mapped = tuple(re.findall(r"`([a-z_]+)`", m.group(5)))
            ticked = m.group(6).replace("*", "") == "勾"
            rows.append((m.group(2), mapped, ticked))
    return rows


# --------------------------------------------------------------------------- #
# 1. 文档完整
# --------------------------------------------------------------------------- #


def test_prd_v4_exists_and_is_not_empty(prd_v4: str) -> None:
    assert len(prd_v4) > 20000


@pytest.mark.parametrize("req_id", V21_REQUIREMENT_IDS)
def test_v21_baseline_is_still_intact(prd_v21: str, req_id: str) -> None:
    """v4.0 是增量改动：R1–R12 仍必须完整留在 `PRD.md` 里。

    v4.0 的 `OPERATION_REQUIREMENT` 还在引用 R1/R2/R6/R8/R9，删掉旧文档
    会让这些引用指向不存在的章节。
    """
    assert re.search(rf"^### {req_id} ", prd_v21, flags=re.MULTILINE), (
        f"v2.1 PRD 缺少 {req_id} 的章节标题"
    )
    assert re.findall(rf"AC-{req_id}-N\d+", prd_v21), f"{req_id} 缺少正常验收"
    assert re.findall(rf"AC-{req_id}-E\d+", prd_v21), f"{req_id} 缺少异常验收"


@pytest.mark.parametrize("req_id", V21_REQUIREMENT_IDS)
def test_v4_does_not_redefine_v21_requirement_ids(prd_v4: str, req_id: str) -> None:
    """同一个编号不能在两份 PRD 里指两件事——否则 `R2` 到底指什么就说不清了。"""
    assert not re.search(rf"^### {req_id} ", prd_v4, flags=re.MULTILINE), (
        f"PRDv4.0.md 重新定义了 {req_id}，与 v2.1 冲突"
    )


@pytest.mark.parametrize("req_id", V4_REQUIREMENT_IDS)
def test_each_v4_requirement_has_heading(prd_v4: str, req_id: str) -> None:
    assert re.search(rf"^### {req_id} ", prd_v4, flags=re.MULTILINE), (
        f"PRDv4.0.md 缺少 {req_id} 的章节标题"
    )


@pytest.mark.parametrize("req_id", V4_REQUIREMENT_IDS)
def test_each_v4_requirement_has_normal_and_exception_acceptance(
    prd_v4: str, req_id: str
) -> None:
    """每条需求都要以「- AC-R{n}-N* 」的形式**定义**至少一条正常与一条异常验收。

    只统计行首的定义，不统计正文里的交叉引用：v4.0 大量写「（AC-R25-N2）」
    这种回指，把引用也算进去，重复检查就永远失败。
    """
    normal = re.findall(rf"^- AC-{req_id}-N\d+", prd_v4, flags=re.MULTILINE)
    exception = re.findall(rf"^- AC-{req_id}-E\d+", prd_v4, flags=re.MULTILINE)
    assert normal, f"{req_id} 缺少正常验收条目（AC-{req_id}-N*）"
    assert exception, f"{req_id} 缺少异常验收条目（AC-{req_id}-E*）"
    # 定义不得重复：重复说明复制粘贴时没改号，验收会漏
    assert len(normal) == len(set(normal)), f"{req_id} 正常验收编号重复定义"
    assert len(exception) == len(set(exception)), f"{req_id} 异常验收编号重复定义"


# --------------------------------------------------------------------------- #
# 2. 契约对齐：PRD 表格 ↔ app/schemas.py
# --------------------------------------------------------------------------- #


def test_verify_key_table_matches_schemas(prd_v4: str) -> None:
    """§5 表格 12 行（v4.2 起），key 与顺序都要和 `VERIFY_KEYS` 一致。"""
    rows = _verify_table(prd_v4)
    assert [key for key, _title, _req in rows] == list(VERIFY_KEYS)
    # 表里的需求编号也要对得上，否则报告里的「依据 R14」会指错章节
    assert {key: req for key, _title, req in rows} == VERIFY_REQUIREMENT


def test_operation_key_table_matches_schemas(prd_v4: str) -> None:
    """§6.2 表格 10 行，key 与顺序都要和 `OPERATION_KEYS` 一致。"""
    rows = _operation_table(prd_v4)
    assert [key for key, _title, _req in rows] == list(OPERATION_KEYS)


def test_operation_table_marks_tagged_pdf_as_removed(prd_v4: str) -> None:
    """被删的那一项要以划掉的形式留在表里，读者才知道它去哪了。"""
    body = _section(prd_v4, "### 6.2 流水线项", "## 7. 状态常量")
    assert "~~`tagged_pdf`~~" in body
    assert "tagged_pdf" not in OPERATION_KEYS


def test_process_group_table_matches_schemas(prd_v4: str) -> None:
    """§6.1 表格 7 行，group key 与展开映射都要和代码一致。

    映射按**集合**比对：表里 `g5_security` 那格写成「`fast_web_view`（与 g2
    共享 `metadata`，去重）」，顺序是叙述顺序；代码里是 `("metadata",
    "fast_web_view")`，顺序是流水线顺序。组内顺序不影响执行——`expand_process_groups`
    会按 `OPERATION_KEYS` 重排，所以这里只钉「哪些项被映射到」。
    """
    rows = _group_table(prd_v4)
    assert [group for group, _mapped, _ticked in rows] == list(PROCESS_GROUPS)
    assert {group: set(mapped) for group, mapped, _ticked in rows} == {
        group: set(mapped) for group, mapped in PROCESS_GROUP_OPERATIONS.items()
    }
    # 七个组展开后必须正好覆盖十项，不多不少
    assert set().union(*[set(m) for _g, m, _t in rows]) == set(OPERATION_KEYS)


def test_default_ticked_column_matches_default_groups(prd_v4: str) -> None:
    """§6.1「默认」列勾上的，就是 `DEFAULT_PROCESS_GROUPS`——第 6、7 项都不勾。"""
    ticked = tuple(
        group for group, _mapped, is_ticked in _group_table(prd_v4) if is_ticked
    )
    assert ticked == DEFAULT_PROCESS_GROUPS
    # 两项都不可逆：扁平化把注释画进内容流，删除则连痕迹都不留，都不替用户决定
    assert "g6_flatten" not in ticked
    assert "g7_remove_annotations" not in ticked


def test_verify_keys_and_operation_keys_do_not_overlap() -> None:
    """§5 末句「校验不占处理键」：两张表不能有交集。"""
    assert not set(VERIFY_KEYS) & set(OPERATION_KEYS)


def test_verify_status_table_matches_schemas(prd_v4: str) -> None:
    """§7.1 四态表 == `VERIFY_STATUSES`，顺序也一致。"""
    body = _section(prd_v4, "### 7.1 校验状态", "### 7.2 处理结果状态")
    listed = [m.group(1) for m in map(_STATUS_ROW.match, body.splitlines()) if m]
    assert tuple(listed) == VERIFY_STATUSES


def test_result_status_sentence_matches_schemas(prd_v4: str) -> None:
    """§7.2 用一句话列了五态，`RESULT_STATUSES` 必须与它同序同集。"""
    body = _section(prd_v4, "### 7.2 处理结果状态", "### 7.3 后置校验汇总口径")
    listed = re.findall(r"`([a-z_]+)`", body)
    assert tuple(listed) == RESULT_STATUSES


def test_job_statuses_match_the_prd_list_minus_the_removed_one(prd_v4: str) -> None:
    """§8.1 的 `JOB_STATUSES` 代码块必须与常量逐项一致。

    唯一的差集是 `awaiting_tag_decision`：R30 明写要从 `JOB_STATUSES` 里移除，
    所以它出现在 PRD 的旧草图里、不在代码里——这个差集本身也要被钉住，
    否则「少了一个状态」和「少了一个本该删的状态」就分不清了。
    """
    body = _section(prd_v4, "JOB_STATUSES = (", "\ndef default_config")
    declared = tuple(re.findall(r'"([a-z_]+)"', body))
    assert set(declared) - set(JOB_STATUSES) == {"awaiting_tag_decision"}
    assert set(JOB_STATUSES) - set(declared) == set()
    # 顺序也要一致：状态列表的顺序就是文档里给人读的顺序
    assert tuple(s for s in declared if s != "awaiting_tag_decision") == JOB_STATUSES


# --------------------------------------------------------------------------- #
# 3. 矩阵无缺口：每条需求都有实现文件、有测试文件、有具名验收引用
# --------------------------------------------------------------------------- #


def test_matrix_covers_exactly_r13_to_r31() -> None:
    """登记表的键必须**恰好**是 R13–R31：少一条是缺口，多一条是幻觉。"""
    assert set(REQUIREMENT_MATRIX) == set(V4_REQUIREMENT_IDS)


def test_v41_matrix_covers_exactly_r32_to_r36() -> None:
    """v4.1 增量表同理：R32–R36 一条不多一条不少。"""
    assert set(V41_REQUIREMENT_MATRIX) == set(V41_REQUIREMENT_IDS)


def test_v42_matrix_covers_exactly_r37_to_r39() -> None:
    """v4.2 增量表：R37–R39 一条不多一条不少。"""
    assert set(V42_REQUIREMENT_MATRIX) == set(V42_REQUIREMENT_IDS)


def test_the_three_matrices_do_not_share_keys() -> None:
    """三张表按编号切开，同一个 R 号不能同时出现在两张里。

    这条挡的是「R21 拆成 R32/R33 之后，又把 R33 顺手抄进 v4.0 那张表」这类
    半迁移状态：两边都登记，改一处漏一处，最后谁在管这条需求就说不清了。
    """
    v4 = set(REQUIREMENT_MATRIX)
    v41 = set(V41_REQUIREMENT_MATRIX)
    v42 = set(V42_REQUIREMENT_MATRIX)
    assert not v4 & v41
    assert not v4 & v42
    assert not v41 & v42


def test_no_v41_requirement_redefines_a_v4_heading(prd_v4: str) -> None:
    """R32–R36 只能以 §16.x 小节的形式出现，不能另起 `### R32 ` 一级标题。

    编号体系一旦分叉，`test_registered_title_matches_the_prd_heading` 里那条
    `^### R{n} ` 的匹配就会开始互相干扰。
    """
    for req_id in V41_REQUIREMENT_IDS:
        assert not re.search(rf"^### {req_id} ", prd_v4, flags=re.MULTILINE), (
            f"PRDv4.0.md 把 {req_id} 写成了独立一级标题，应留在 §16.x 下"
        )


def test_no_v42_requirement_redefines_a_v4_heading(prd_v4: str) -> None:
    """R37–R39 同理，只能以 §17.x 小节的形式出现。"""
    for req_id in V42_REQUIREMENT_IDS:
        assert not re.search(rf"^### {req_id} ", prd_v4, flags=re.MULTILINE), (
            f"PRDv4.0.md 把 {req_id} 写成了独立一级标题，应留在 §17.x 下"
        )


@pytest.mark.parametrize("req_id", V4_REQUIREMENT_IDS)
def test_registered_title_matches_the_prd_heading(prd_v4: str, req_id: str) -> None:
    """登记表里的标题必须出现在 PRD 该节标题行——改标题忘了改登记表会失败。"""
    heading = re.search(rf"^### {req_id} (.+)$", prd_v4, flags=re.MULTILINE)
    assert heading, f"PRDv4.0.md 缺少 {req_id} 的章节标题"
    assert REQUIREMENT_MATRIX[req_id].title in heading.group(1), (
        f"{req_id} 的登记标题与 PRD 标题不一致：{heading.group(1)}"
    )


@pytest.mark.parametrize("req_id", V41_REQUIREMENT_IDS)
def test_v41_registered_title_matches_the_prd_heading(
    prd_v4: str, req_id: str
) -> None:
    """v4.1 的标题在 `### 16.n R{n} 标题` 里，用另一套正则取。"""
    headings = dict(
        (found_id, title) for found_id, title in _V41_HEADING.findall(prd_v4)
    )
    assert req_id in headings, f"PRDv4.0.md §16 缺少 {req_id} 的小节标题"
    assert V41_REQUIREMENT_MATRIX[req_id].title in headings[req_id], (
        f"{req_id} 的登记标题与 PRD 标题不一致：{headings[req_id]}"
    )


@pytest.mark.parametrize("req_id", V42_REQUIREMENT_IDS)
def test_v42_registered_title_matches_the_prd_heading(
    prd_v4: str, req_id: str
) -> None:
    """v4.2 的标题在 `### 17.n R{n} 标题` 里，用第三套正则取。"""
    headings = dict((found_id, title) for found_id, title in _V42_HEADING.findall(prd_v4))
    assert req_id in headings, f"PRDv4.0.md §17 缺少 {req_id} 的小节标题"
    assert V42_REQUIREMENT_MATRIX[req_id].title in headings[req_id], (
        f"{req_id} 的登记标题与 PRD 标题不一致：{headings[req_id]}"
    )


def _assert_impl_files_exist(matrix: dict[str, Requirement], req_id: str) -> None:
    requirement = matrix[req_id]
    assert requirement.impl, f"{req_id} 没有登记任何实现文件"
    missing = [path for path in requirement.impl if not (ROOT / path).is_file()]
    assert not missing, f"{req_id} 登记的实现文件不存在：{missing}"


def _assert_test_files_exist(matrix: dict[str, Requirement], req_id: str) -> None:
    requirement = matrix[req_id]
    assert requirement.tests, f"{req_id} 没有登记任何测试文件"
    missing = [path for path in requirement.tests if not (ROOT / path).is_file()]
    assert not missing, f"{req_id} 登记的测试文件不存在：{missing}"
    # 文件在 ≠ 里面有测试：空壳文件不能拿来充数
    empty = [
        path
        for path in requirement.tests
        if "def test_" not in (ROOT / path).read_text(encoding="utf-8")
    ]
    assert not empty, f"{req_id} 登记的测试文件里没有测试函数：{empty}"


def _assert_cites_acceptance(matrix: dict[str, Requirement], req_id: str) -> None:
    number = req_id[1:]
    pattern = re.compile(rf"AC-R{number}-[NE]\d+")
    cited = [
        path
        for path in matrix[req_id].tests
        if pattern.search((ROOT / path).read_text(encoding="utf-8"))
    ]
    assert cited, f"{req_id} 的测试文件里没有引用任何 AC-{req_id}-* 验收条目"


@pytest.mark.parametrize("req_id", V4_REQUIREMENT_IDS)
def test_each_requirement_registers_existing_impl_files(req_id: str) -> None:
    _assert_impl_files_exist(REQUIREMENT_MATRIX, req_id)


@pytest.mark.parametrize("req_id", V4_REQUIREMENT_IDS)
def test_each_requirement_registers_existing_test_files(req_id: str) -> None:
    _assert_test_files_exist(REQUIREMENT_MATRIX, req_id)


@pytest.mark.parametrize("req_id", V4_REQUIREMENT_IDS)
def test_each_requirement_cites_at_least_one_acceptance_id(req_id: str) -> None:
    """登记的测试文件里必须出现 `AC-R{n}-`。

    只查文件存在等于抄目录树；要求出现验收编号，才说明有人对着
    PRD 的那一条写过断言。
    """
    _assert_cites_acceptance(REQUIREMENT_MATRIX, req_id)


@pytest.mark.parametrize("req_id", V41_REQUIREMENT_IDS)
def test_v41_requirement_registers_existing_impl_files(req_id: str) -> None:
    _assert_impl_files_exist(V41_REQUIREMENT_MATRIX, req_id)


@pytest.mark.parametrize("req_id", V41_REQUIREMENT_IDS)
def test_v41_requirement_registers_existing_test_files(req_id: str) -> None:
    _assert_test_files_exist(V41_REQUIREMENT_MATRIX, req_id)


@pytest.mark.parametrize("req_id", V41_REQUIREMENT_IDS)
def test_v41_requirement_cites_at_least_one_acceptance_id(req_id: str) -> None:
    _assert_cites_acceptance(V41_REQUIREMENT_MATRIX, req_id)


@pytest.mark.parametrize("req_id", V41_REQUIREMENT_IDS)
def test_v41_requirement_has_normal_and_exception_acceptance(
    prd_v4: str, req_id: str
) -> None:
    """§16 里每条需求同样要有 `- AC-R{n}-N*` 与 `- AC-R{n}-E*` 各至少一条。

    口径与 v4.0 那批一致：只统计行首的定义，不统计正文里的回指。
    """
    normal = re.findall(rf"^- AC-{req_id}-N\d+", prd_v4, flags=re.MULTILINE)
    exception = re.findall(rf"^- AC-{req_id}-E\d+", prd_v4, flags=re.MULTILINE)
    assert normal, f"{req_id} 缺少正常验收条目（AC-{req_id}-N*）"
    assert exception, f"{req_id} 缺少异常验收条目（AC-{req_id}-E*）"
    assert len(normal) == len(set(normal)), f"{req_id} 正常验收编号重复定义"
    assert len(exception) == len(set(exception)), f"{req_id} 异常验收编号重复定义"


@pytest.mark.parametrize("req_id", V42_REQUIREMENT_IDS)
def test_v42_requirement_registers_existing_impl_files(req_id: str) -> None:
    _assert_impl_files_exist(V42_REQUIREMENT_MATRIX, req_id)


@pytest.mark.parametrize("req_id", V42_REQUIREMENT_IDS)
def test_v42_requirement_registers_existing_test_files(req_id: str) -> None:
    _assert_test_files_exist(V42_REQUIREMENT_MATRIX, req_id)


@pytest.mark.parametrize("req_id", V42_REQUIREMENT_IDS)
def test_v42_requirement_cites_at_least_one_acceptance_id(req_id: str) -> None:
    _assert_cites_acceptance(V42_REQUIREMENT_MATRIX, req_id)


@pytest.mark.parametrize("req_id", V42_REQUIREMENT_IDS)
def test_v42_requirement_has_normal_and_exception_acceptance(
    prd_v4: str, req_id: str
) -> None:
    """§17 里每条需求同样要有 `- AC-R{n}-N*` 与 `- AC-R{n}-E*` 各至少一条。"""
    normal = re.findall(rf"^- AC-{req_id}-N\d+", prd_v4, flags=re.MULTILINE)
    exception = re.findall(rf"^- AC-{req_id}-E\d+", prd_v4, flags=re.MULTILINE)
    assert normal, f"{req_id} 缺少正常验收条目（AC-{req_id}-N*）"
    assert exception, f"{req_id} 缺少异常验收条目（AC-{req_id}-E*）"
    assert len(normal) == len(set(normal)), f"{req_id} 正常验收编号重复定义"
    assert len(exception) == len(set(exception)), f"{req_id} 异常验收编号重复定义"


def test_every_acceptance_id_in_the_matrix_exists_in_the_prd(prd_v4: str) -> None:
    """反向检查：测试里引用的 AC 编号必须真的写在 PRD 里。

    这条抓的是「测试引了个不存在的验收条目」——常见于需求改写后忘了同步，
    那种引用读起来像有覆盖，实际对不上任何一行验收。
    """
    declared = set(re.findall(r"AC-R\d+-[NE]\d+", prd_v4))
    cited: set[str] = set()
    for requirement in REQUIREMENT_MATRIX.values():
        for path in requirement.tests:
            text = (ROOT / path).read_text(encoding="utf-8")
            cited.update(re.findall(r"AC-R\d+-[NE]\d+", text))
    v4_cited = {
        ac for ac in cited if 13 <= int(ac.split("-")[1][1:]) <= 31
    }
    assert v4_cited <= declared, f"测试引用了 PRD 里不存在的验收条目：{sorted(v4_cited - declared)}"


def test_every_v41_acceptance_id_in_the_matrix_exists_in_the_prd(prd_v4: str) -> None:
    """v4.1 的同一个反向检查，按 R32–R36 切段。

    单独一条而不是把上一条的区间改成 13..36：R32–R36 是另一张登记表，
    混在一个集合里算，「R35 引了个不存在的 AC」会报成「v4.0 引用错」。
    """
    declared = set(re.findall(r"AC-R\d+-[NE]\d+", prd_v4))
    cited: set[str] = set()
    for requirement in V41_REQUIREMENT_MATRIX.values():
        for path in requirement.tests:
            text = (ROOT / path).read_text(encoding="utf-8")
            cited.update(re.findall(r"AC-R\d+-[NE]\d+", text))
    v41_cited = {ac for ac in cited if 32 <= int(ac.split("-")[1][1:]) <= 36}
    assert v41_cited, "v4.1 的测试文件里一条 AC-R32..R36 都没引用"
    assert v41_cited <= declared, (
        f"v4.1 测试引用了 PRD 里不存在的验收条目：{sorted(v41_cited - declared)}"
    )


def test_every_v42_acceptance_id_in_the_matrix_exists_in_the_prd(prd_v4: str) -> None:
    """v4.2 的同一个反向检查，按 R37–R39 切段。"""
    declared = set(re.findall(r"AC-R\d+-[NE]\d+", prd_v4))
    cited: set[str] = set()
    for requirement in V42_REQUIREMENT_MATRIX.values():
        for path in requirement.tests:
            text = (ROOT / path).read_text(encoding="utf-8")
            cited.update(re.findall(r"AC-R\d+-[NE]\d+", text))
    v42_cited = {ac for ac in cited if 37 <= int(ac.split("-")[1][1:]) <= 39}
    assert v42_cited, "v4.2 的测试文件里一条 AC-R37..R39 都没引用"
    assert v42_cited <= declared, (
        f"v4.2 测试引用了 PRD 里不存在的验收条目：{sorted(v42_cited - declared)}"
    )


# --------------------------------------------------------------------------- #
# 4. 措辞与契约的硬约束（沿用 v2.1，口径不变）
# --------------------------------------------------------------------------- #


def test_no_compliance_claim_allowed(prd_v4: str) -> None:
    """「完全合规」只允许出现在禁止性/否定性语句里，不得作为产品结论。"""
    negations = ("禁止", "不得", "不出现", "不能", "不允许", "不代表")
    offenders = []
    for lineno, line in enumerate(prd_v4.splitlines(), start=1):
        if "完全合规" not in line:
            continue
        if any(word in line for word in negations):
            continue
        offenders.append(f"{lineno}: {line.strip()}")
    assert not offenders, "PRD 出现结论性合规表述：" + "; ".join(offenders)


def test_report_disclaimer_is_the_only_place_that_mentions_full_compliance(
    prd_v4: str,
) -> None:
    """免责声明原文被 AC-R23-E2 与渲染器同时钉住，改一个字就两边不一致。"""
    assert "本报告仅反映工具可自动检测的项目，不代表文件完全合规。" in prd_v4


def test_markinfo_is_not_real_tagging(prd_v21: str) -> None:
    """v2.1 的歧义消除条款仍然有效：只设 MarkInfo 不算加标签。"""
    assert "`/MarkInfo`" in prd_v21
    assert "冒充" in prd_v21 or "不算加标签" in prd_v21 or "不允许" in prd_v21
    assert "markinfo_only" in prd_v21


def test_exemption_only_touches_four_operations(prd_v4: str) -> None:
    """R24：豁免页只影响四项，且必须是代码里那四项。

    v4.1 把 `remove_annotations` 加了进去（§16.3「受豁免页约束…加入
    `EXEMPT_SENSITIVE_OPERATIONS`」）：豁免页往往是被刻意排除在批量处理之外的
    页面，删注释同样是不可逆的写操作，不能因为「顺手」就把豁免页上的批注删了。
    """
    line = next(
        (ln for ln in prd_v4.splitlines() if "豁免页输入沿用" in ln), None
    )
    assert line, "PRD 缺少豁免页作用范围的说明"
    named = tuple(re.findall(r"`([a-z_]+)`", line))
    operations = tuple(key for key in named if key in OPERATION_KEYS)
    assert set(operations) == EXEMPT_SENSITIVE_OPERATIONS
    assert len(operations) == 4


def test_error_contract_has_all_fields(prd_v21: str) -> None:
    """错误契约（五字段）是 v2.1 定的，v4.0 只往里加错误码，不改结构。"""
    for field in ("code", "message", "details", "retryable", "next_action"):
        assert f"`{field}`" in prd_v21, f"错误契约缺少字段 {field}"


def test_signature_pdf_is_rejected_by_default(prd_v21: str) -> None:
    """v2.1 就有的拒收策略，v4.0 没改（R29 只说加密 PDF）。"""
    assert "signed_pdf" in prd_v21
    assert "无签名" in prd_v21


def test_license_note_present(prd_v21: str) -> None:
    assert "AGPL" in prd_v21, "必须记录 PyMuPDF 的 AGPL 许可提醒"
