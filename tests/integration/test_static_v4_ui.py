"""M4 验收：v4.0 四页面动线的前端契约（R13、R24、R26、R30）。

M4 只动前端。这里不去点按钮（Playwright 留给 M6-3），而是把「页面必须长成
什么样」钉在源码与接口上——前端没有类型系统，字符串就是契约：

1. 四步动线必须一级一级往下走：首页 → 准备 → 校验 → 处理 → 结果（§3.1），
   任何一步都不许跳过中间页直接落地（R13 的两阶段分离）；
2. 准备页有「不执行前置校验」开关（AC-R13-N3），跳过时不产生假的校验结果；
3. 处理页的六项勾选全部来自 `/api/meta`，不是前端另抄一份中文表（R24）；
4. 注释扁平化必须二次确认（AC-R26-E1）——它是唯一不可逆的一项；
5. 处理页与结果页不再出现 tagged_pdf 相关字样（AC-R30-N2），
   且没有任何 `tag_choice` 的读写（AC-R30-E1）；
6. 页面 4 的后置校验固定九行，缺项写「本次未执行」而不是让行消失；
7. 免责声明在结果页上，且界面不出现「完全合规」这类结论性表述（AC-R23-E2）；
8. 「内容一致性」提示按六态给结论，且「没核对」「没算成」两态必须劝阻（R43）。
   这一组不查字符串：把 `result.js` 里的函数抠出来交给 node 真跑，才能证明
   `ok=true` + `warning` 不会渲染成绿色的「一致」——那是本轮修掉的假绿灯。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.schemas import OPERATION_KEYS, PROCESS_GROUPS, VERIFY_KEYS

STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "static"
VIEWS_DIR = STATIC_DIR / "js" / "views"

JOBS = "/api/jobs"
BATCH = "/api/jobs/batch"

SUCCESS_STATUSES = ("completed", "completed_with_warnings")


def read(rel: str) -> str:
    return (STATIC_DIR / rel).read_text("utf-8")


def strip_comments(source: str) -> str:
    """去掉 /* */ 与 // 注释。

    断言「页面里不出现某字样」时，注释里提到「v4.0 删掉了标签处理方式」
    反而应该被允许——那是给人看的说明，不会渲染给用户。把注释剔掉之后，
    剩下的才是真正会出现在屏幕上的字符串。
    """
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_block, flags=re.M)


# --------------------------------------------------------------------------- #
# 1. 四步动线：一级一级往下走
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("view", "target"),
    [
        ("home.js", "#/prepare/${payload.batch_id}"),
        ("prepare.js", "#/verify/${batchId}"),
        ("verify.js", "#/process/${batchId}"),
        ("process.js", "#/done/${batchId}"),
    ],
)
def test_wizard_advances_one_stage_at_a_time(view: str, target: str) -> None:
    """每一页只把用户送去下一页，不越级。

    越级的代价是真实的：从准备页直接跳 `#/process` 就等于绕过了前置校验，
    而 `#/process` 上的「开始处理」会被后端以 `verify_not_ready` 拦下，
    用户看到的是一个空转的页面。
    """
    assert target in read(f"js/views/{view}"), f"{view} 没有把用户送到 {target}"


def test_single_file_upload_also_creates_a_batch() -> None:
    """AC-R13-N1：单文件不再走 v2.1 的配置页，一律建批次进准备页。

    v2.1 的分支是「1 个文件 → `#/config/:jobId`」，那条路绕开了校验项勾选，
    正是 v4.0 要补的第一件事。
    """
    source = read("js/views/home.js")
    assert "#/prepare/" in source
    assert "#/config/" not in source, "首页不该再有单文件直达配置页的分支"
    # 两个入口共用同一次 createBatch：单文件也是批次
    assert source.count("createBatch(") == 1


# --------------------------------------------------------------------------- #
# 2. 准备页的跳过开关（AC-R13-N3）
# --------------------------------------------------------------------------- #


def test_prepare_page_offers_the_skip_switch() -> None:
    source = read("js/views/prepare.js")
    assert 'id="skip-precheck"' in source
    assert "不执行前置校验" in source
    # 跳过时十二项要置灰，否则用户会以为勾了但没生效
    assert "box.disabled = skipping" in source
    # 提交时把 skip_precheck 一起带上：勾了跳过就不该再发校验请求
    assert "skip_precheck: true" in source


def test_prepare_page_reads_the_nine_items_from_meta() -> None:
    source = read("js/views/prepare.js")
    assert "meta.verify_keys" in source
    assert "meta.verify_titles" in source
    for key in VERIFY_KEYS:
        assert f"'{key}'" not in source, f"准备页硬编码了校验项 {key}"


# --------------------------------------------------------------------------- #
# 3. 处理页：六项来自 meta（R24）
# --------------------------------------------------------------------------- #


def test_process_page_takes_the_six_groups_from_meta() -> None:
    source = read("js/views/process.js")
    for field in (
        "meta.process_groups",
        "meta.process_group_titles",
        "meta.process_group_requirements",
        "meta.default_process_groups",
    ):
        assert field in source, f"处理页没有从 /api/meta 取 {field}"

    hints = re.findall(r"^\s{2}(g\d_[a-z_]+):", source, flags=re.M)
    assert hints == list(PROCESS_GROUPS), (
        f"处理页的六项说明与后端分组不一致：{hints}"
    )


def test_process_page_does_not_hardcode_pipeline_operations() -> None:
    """页面只认七个分组；十个流水线操作由后端展开（AC-R24-N3 的去重在前端之外）。"""
    for view in ("process.js", "done.js"):
        source = read(f"js/views/{view}")
        for key in OPERATION_KEYS:
            assert f"'{key}'" not in source, f"{view} 硬编码了流水线操作 {key}"
            assert f'"{key}"' not in source, f"{view} 硬编码了流水线操作 {key}"


def test_flatten_needs_a_second_confirmation() -> None:
    """AC-R26-E1：唯一不可逆的一项，勾上时要拦一道。"""
    source = read("js/views/process.js")
    assert "const FLATTEN = 'g6_flatten'" in source
    assert "window.confirm(" in source
    assert "不可逆" in source
    # 取消后要把勾去掉，否则「点了取消却还是勾着」比不弹窗更糟
    assert "box.checked = false" in source


def test_process_page_reports_exempt_page_overflow_without_blocking() -> None:
    """豁免页越界只对相关文件跳过，不该让整批都提交不了。"""
    source = read("js/views/process.js")
    assert "applied.invalid" in source
    assert "startBatch(" in source


# --------------------------------------------------------------------------- #
# 4. R30：标签链路已从界面移除
# --------------------------------------------------------------------------- #


TAGGED_WORDS = ("标签 PDF", "标签PDF", "生成标签", "标签检测", "标签处理方式", "标签状态")


@pytest.mark.parametrize("view", ["process.js", "done.js", "processing.js"])
def test_processing_and_result_pages_have_no_tag_wording(view: str) -> None:
    """AC-R30-N2：处理页与结果页均不出现「标签」相关字样。

    只看渲染出来的字符串（注释已剔除）。`result.js` 不在名单里：它的
    「导览标签」是 Acrobat 对 `/PageMode` 的官方叫法，与 tagged_pdf 无关。
    """
    source = strip_comments(read(f"js/views/{view}"))
    for word in TAGGED_WORDS:
        assert word not in source, f"{view} 仍在展示「{word}」"


@pytest.mark.parametrize(
    "view", ["process.js", "done.js", "processing.js", "result.js", "config.js", "batch.js"]
)
def test_no_view_touches_tag_fields(view: str) -> None:
    """AC-R30-E1：代码里不再有对 `tag_choice` 的读写。

    老 job.json 里这些字段还在（只读历史值），但界面既不展示也不回写。
    """
    source = strip_comments(read(f"js/views/{view}"))
    for token in ("tag_choice", "tag_status", "tag_detection", "tagged_pdf"):
        assert token not in source, f"{view} 仍在读写 {token}"


def test_api_js_exposes_no_tag_endpoint() -> None:
    """AC-R30-N3 的前端侧：客户端连重新检测的入口都不该有。"""
    source = read("js/api.js")
    assert "tag-detection" not in source
    assert "retry" not in source.lower().replace("retry_", "")


# --------------------------------------------------------------------------- #
# 5. 页面 4：后置校验固定九行
# --------------------------------------------------------------------------- #


def test_done_page_renders_nine_post_verify_rows_from_meta() -> None:
    source = read("js/views/done.js")
    assert "meta.verify_keys" in source
    assert "本次未执行" in source, "缺项要显式写「本次未执行」，不能悄悄少一行"
    for key in VERIFY_KEYS:
        assert f"'{key}'" not in source, f"页面 4 硬编码了校验项 {key}"


def test_done_page_only_lists_the_operations_the_user_picked() -> None:
    """处理明细只列勾选过的项：没勾的项出现在「处理结果」里是噪音。"""
    source = read("js/views/done.js")
    assert "meta.process_group_operations" in source
    assert "selectedOperations(" in source
    # 去重靠 Set：metadata 同时属于 g2 与 g5，不去重会出现两行
    assert "new Set()" in source


def test_done_page_lists_failed_files_too() -> None:
    """「成功 X / 失败 Y」要对得上总数，失败的文件必须留在列表里。"""
    source = read("js/views/done.js")
    assert "getBatchResults(" in source
    for status in ("completed_with_warnings", "interrupted", "failed"):
        assert status in source, f"页面 4 没有处理 {status} 状态"


def test_result_page_has_a_post_verify_card() -> None:
    source = read("js/views/result.js")
    assert 'id="post-verify"' in source
    assert "meta.verify_keys" in source
    assert "open-verify-report" in source


def test_result_page_shows_the_content_integrity_verdict() -> None:
    """R36 / AC-R36-E2：结果页必须有「内容一致性」这一行，且没核对时不许显示成一致。

    查的是「页面上有没有这件事」，不是文案好不好：

    - 结论用后端的 `content_integrity_summary`，前端不重算——处理日志用的是同一个
      函数生成的那句话，重算就会出现「日志说 3 页变化、结果页说 2 页」；
    - `content_integrity` 缺失（老任务，或护栏在建指纹前就抛了）时必须落到
      「未核对」。若写成 `if (!data) return ''`，这一行会静默消失，用户会把
      「没有这一行」读成「验过了、没问题」。
    """
    source = read("js/views/result.js")
    assert 'id="content-integrity"' in source
    assert "contentIntegrityCard(report, meta)" in source
    assert "report.content_integrity_summary" in source
    assert "内容一致性未核对" in source


# --------------------------------------------------------------------------- #
# 6. R43：内容一致性提示的六态（AC-R43-N1…N4）
#
# 这一组不查字符串，而是把 result.js 里的函数抠出来交给 node 真跑一遍：
# 「文案里有没有『不要』」用 grep 能查，「`ok=true` + `warning` 会不会渲染成
# 绿色的一致」只有真执行才查得出来——那正是本轮补上的假绿灯。
# --------------------------------------------------------------------------- #

NODE = shutil.which("node")

needs_node = pytest.mark.skipif(
    NODE is None, reason="需要 node 执行前端函数（本机 v22；缺 node 时跳过而不是假装通过）"
)

META = {
    "operation_titles": {
        "remove_annotations": "清除高亮和批注",
        "link_color": "内链颜色修正",
    }
}

# node 侧的最小夹具：读入函数源码与用例，逐条求值后把结果吐成 JSON。
# 用 `new Function` 而不是 `import`，因为 result.js 顶部 import 了 `../dom.js`
# 与 `../save-as.js`，那两个模块会碰 `document`，在 node 里跑不起来。
HARNESS = """
import fs from 'node:fs';

const { body, meta, cases } = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const factory = new Function(
  `${body}\\nreturn { integrityState, integrityBadge, contentIntegrityNotice };`
);
const api = factory();
const out = cases.map((report) => ({
  state: api.integrityState(report.content_integrity),
  badge: api.integrityBadge(report.content_integrity),
  text: api.contentIntegrityNotice(report, meta),
}));
process.stdout.write(JSON.stringify(out));
"""


def js_function(source: str, name: str) -> str:
    """从源码里按大括号配对抠出 `function name(...) {...}`。

    不用正则：模板字符串里的 `${...}` 和正文里的 `{}` 会让贪婪匹配跑飞。
    函数被改名或删掉时这里抛 ValueError，测试红——这正是想要的失败方式。
    """
    marker = f"function {name}("
    start = source.index(marker)
    depth = 0
    opened = False
    for index in range(start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
            opened = True
        elif char == "}":
            depth -= 1
            if opened and depth == 0:
                return source[start : index + 1]
    raise ValueError(f"{name} 的函数体没有闭合")


def run_notice(cases: list[dict], meta: dict | None = None) -> list[dict]:
    """把用例交给 node 求值，返回每条用例的 state / badge / text。

    输出**落文件**再读回来，不用 `capture_output`：Windows 上管道写端一旦被别的
    长命子进程继承，`communicate()` 就等不到 EOF，node 早已退出 pytest 还在等。
    全量跑时前面有 live server 与 playwright 的残留子进程，这个坑真实发生过——
    这个文件单跑 15 秒全绿，塞进全量就卡在一个 0.3 秒的用例上。
    """
    source = read("js/views/result.js")
    body = "\n".join(
        js_function(source, name)
        for name in ("pageList", "integrityState", "integrityBadge", "attributedText", "contentIntegrityNotice")
    )
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.mjs"
        payload = Path(tmp) / "cases.json"
        out_path = Path(tmp) / "stdout.txt"
        err_path = Path(tmp) / "stderr.txt"
        harness.write_text(HARNESS, "utf-8")
        payload.write_text(
            json.dumps(
                {"body": body, "meta": META if meta is None else meta, "cases": cases},
                ensure_ascii=False,
            ),
            "utf-8",
        )
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            code = subprocess.run(
                [NODE, str(harness), str(payload)],
                stdout=out,
                stderr=err,
                timeout=120,
            ).returncode
        stdout = out_path.read_text("utf-8", errors="replace")
        stderr = err_path.read_text("utf-8", errors="replace")
    assert code == 0, stderr
    return json.loads(stdout)


@needs_node
def test_notice_warns_when_integrity_was_never_checked() -> None:
    """`content_integrity` 为 `null`：必须劝阻，不能说「一致」。"""
    [result] = run_notice([{"content_integrity": None}])
    assert result["state"] == "missing"
    assert result["badge"]["label"] == "未核对"
    assert "不要" in result["text"]
    assert "没有核对" in result["text"]


@needs_node
def test_notice_warns_when_the_verdict_is_a_failure() -> None:
    """`ok=false`：产物没发布，必须劝阻。"""
    [result] = run_notice(
        [{"content_integrity": {"ok": False, "problems": ["页数变化：3 → 4"]}}]
    )
    assert result["state"] == "bad"
    assert result["badge"] == {"tone": "danger", "label": "不一致", "icon": "✗"}
    assert "不要" in result["text"]
    assert "没有发布" in result["text"]


@needs_node
def test_notice_warns_when_the_fingerprint_could_not_be_computed() -> None:
    """回归：`ok=true` + `warning`（指纹没算成）不许渲染成「一致」。

    `content_fingerprint.compare()` 在指纹出错时返回 `ok=True` + `warning`，
    只看 `ok` 会走到最后一个分支说「处理前后内容一致」——一个字都没比过却给
    绿灯。这条用例就是钉住这个假绿灯。
    """
    warning = "内容一致性未能核对（指纹计算失败：broken xref）"
    [result] = run_notice([{"content_integrity": {"ok": True, "warning": warning}}])
    assert result["state"] == "unknown"
    assert result["badge"]["label"] == "未核对"
    assert result["badge"]["tone"] == "warn"
    assert "不要" in result["text"]
    assert "指纹计算失败" in result["text"]
    assert "处理前后内容一致" not in result["text"]


@needs_node
def test_notice_explains_an_attributable_text_change() -> None:
    """文本层有变化：报页号、报中文处理项名，不报英文键。"""
    [result] = run_notice(
        [
            {
                "content_integrity": {
                    "ok": True,
                    "changed_pages": {"content": [2, 5], "text": [2, 5]},
                    "attributed_to": ["remove_annotations"],
                }
            }
        ]
    )
    assert result["state"] == "good"
    assert "2、5" in result["text"]
    assert "文字层" in result["text"]
    assert "清除高亮和批注" in result["text"]
    assert "remove_annotations" not in result["text"]


@needs_node
def test_notice_explains_a_paint_only_change() -> None:
    """只有内容流变、文字没变：必须说清「文字一字未改」，否则用户会当成内容被改。"""
    [result] = run_notice(
        [
            {
                "content_integrity": {
                    "ok": True,
                    "changed_pages": {"content": [3], "text": []},
                    "attributed_to": ["link_color"],
                }
            }
        ]
    )
    assert result["state"] == "good"
    assert "一字未改" in result["text"]
    assert "3" in result["text"]
    assert "内链颜色修正" in result["text"]


@needs_node
def test_notice_says_identical_only_when_nothing_changed() -> None:
    [result] = run_notice(
        [
            {
                "content_integrity": {
                    "ok": True,
                    "page_count": {"before": 3, "after": 3},
                    "changed_pages": {"content": [], "text": []},
                    "attributed_to": [],
                }
            }
        ]
    )
    assert result["state"] == "good"
    assert "内容一致" in result["text"]
    assert "不要" not in result["text"]


@needs_node
def test_notice_truncates_long_page_lists() -> None:
    """页号多到十几个时不能把整行撑爆——沿用 `pageList` 的截断。"""
    [result] = run_notice(
        [
            {
                "content_integrity": {
                    "ok": True,
                    "changed_pages": {"content": [], "text": list(range(1, 13))},
                    "attributed_to": ["remove_annotations"],
                }
            }
        ]
    )
    assert "等 12 页" in result["text"]


@needs_node
def test_notice_falls_back_to_the_raw_key_without_titles() -> None:
    """`/api/meta` 没给 `operation_titles` 时退回键名，不许整句消失。"""
    [result] = run_notice(
        [
            {
                "content_integrity": {
                    "ok": True,
                    "changed_pages": {"content": [1], "text": []},
                    "attributed_to": ["link_color"],
                }
            }
        ],
        meta={},
    )
    assert "link_color" in result["text"]


def test_content_integrity_card_keeps_every_row() -> None:
    """AC-R43-N3：加提示不许删信息；AC-R43-N2：提示不读 `content_integrity_summary`。"""
    source = read("js/views/result.js")
    assert 'id="content-integrity-summary"' in source
    assert 'id="content-integrity-notice"' in source
    for row in ("页数", "内容流变化页", "文本层变化页", "归因到", "问题"):
        assert f"'{row}'" in source, f"卡片少了「{row}」这一行"
    notice = js_function(source, "contentIntegrityNotice")
    assert "content_integrity_summary" not in notice, "提示句不许复述后端结论（会变成两套账）"
    assert "integrityState" in notice, "提示与徽标必须共用同一套状态判定"


def test_result_page_points_at_the_independent_checker() -> None:
    """AC-R42-N6 的前端一半：结果页要给出独立复核的入口。"""
    source = read("js/views/result.js")
    assert "scripts/verify_content_same.py" in source


# --------------------------------------------------------------------------- #
# 7. 免责声明与结论性表述
# --------------------------------------------------------------------------- #


def test_disclaimer_is_rendered_on_both_result_pages() -> None:
    """两处都要有：页面 4 用 /api/meta，v2.1 结果页用报告里的字段。"""
    assert "meta.disclaimer" in read("js/views/done.js")
    assert "report.disclaimer" in read("js/views/result.js")


def test_views_never_claim_full_compliance() -> None:
    """AC-R23-E2：界面不得出现「完全合规」这种结论性表述。"""
    for path in VIEWS_DIR.glob("*.js"):
        source = strip_comments(path.read_text("utf-8"))
        assert "完全合规" not in source, f"{path.name} 给出了合规结论"


def test_result_page_says_verification_is_not_official_approval() -> None:
    source = read("js/views/result.js")
    assert "不代表已通过任何官方审评" in source


# --------------------------------------------------------------------------- #
# 8. 页面 4 的数据源：/api/jobs/batch/{id}/results
# --------------------------------------------------------------------------- #


def upload(client, fixture_pdfs: Path, name: str = "text_blocks.pdf") -> dict:
    from tests.integration.test_upload import upload_path

    response = upload_path(client, fixture_pdfs / name)
    assert response.status_code == 201, response.text
    return response.json()


def job_path(client, job_id: str) -> Path:
    from tests.integration.test_upload import job_dir

    return job_dir(client, job_id)


def run_branch_a(client, fixture_pdfs: Path) -> dict:
    job = upload(client, fixture_pdfs)
    submitted = client.post(f"{BATCH}/{job['batch_id']}/verify", json={})
    assert submitted.status_code == 202, submitted.text
    client.app.state.ctx.worker.drain()
    assert client.post(f"{JOBS}/{job['id']}/start").status_code == 200
    client.app.state.ctx.worker.drain()
    return job


def test_batch_results_reports_one_file_per_uploaded_pdf(client, fixture_pdfs: Path):
    job = run_branch_a(client, fixture_pdfs)

    response = client.get(f"{BATCH}/{job['batch_id']}/results")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["batch_id"] == job["batch_id"]
    assert payload["generated_at"]
    assert payload["summary"] == {"files": 1, "succeeded": 1, "failed": 0, "pending": 0}
    assert [item["job_id"] for item in payload["files"]] == [job["id"]]

    file = payload["files"][0]
    assert file["status"] in SUCCESS_STATUSES
    assert file["original_name"] == "text_blocks.pdf"
    assert file["output"] is not None
    assert file["disclaimer"]
    # 页面 4 要渲染的三个区块：处理明细、后置校验、处理日志
    for key in ("results", "post_verify", "process_log_path", "config"):
        assert key in file, f"批次结果缺少字段 {key}"


def test_batch_results_counts_pending_files(client, fixture_pdfs: Path):
    """还没开始的文件既不算成功也不算失败——单列一项 `pending`。"""
    job = upload(client, fixture_pdfs)

    payload = client.get(f"{BATCH}/{job['batch_id']}/results").json()
    assert payload["summary"] == {"files": 1, "succeeded": 0, "failed": 0, "pending": 1}
    assert payload["files"][0]["status"] == "configuring"


def test_batch_results_keeps_failed_files_in_the_list(client, fixture_pdfs: Path):
    """失败的文件必须出现。

    如果像 `verify-report` 那样只收有结果的文件，「成功 X / 失败 Y」就永远
    对不上文件总数——用户会以为少掉的那个文件被吞了。
    """
    job = run_branch_a(client, fixture_pdfs)

    path = job_path(client, job["id"]) / "job.json"
    record = json.loads(path.read_text("utf-8"))
    record["status"] = "failed"
    record["last_error"] = {"code": "pipeline_failed", "message": "测试构造"}
    path.write_text(json.dumps(record, ensure_ascii=False), "utf-8")

    payload = client.get(f"{BATCH}/{job['batch_id']}/results").json()
    assert payload["summary"] == {"files": 1, "succeeded": 0, "failed": 1, "pending": 0}
    assert [item["job_id"] for item in payload["files"]] == [job["id"]]
    assert payload["files"][0]["status"] == "failed"


def test_batch_results_is_404_for_an_unknown_batch(client):
    """格式合法但查不到的批次：404 + `job_not_found`（与批次详情页同一口径）。"""
    from tests.integration.test_upload import assert_error

    assert_error(
        client.get(f"{BATCH}/bat-0000000000000000/results"),
        "job_not_found",
        status=404,
    )


# --------------------------------------------------------------------------- #
# 9. AC-R30-E1 的后端侧：配置补丁不再接受 tag_choice
# --------------------------------------------------------------------------- #


def test_config_patch_rejects_tag_choice(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    response = client.patch(f"{JOBS}/{job['id']}/config", json={"tag_choice": "generate"})
    assert response.status_code == 422, response.text

    fresh = client.get(f"{JOBS}/{job['id']}").json()
    assert "tag_choice" not in fresh["config"]


def test_batch_config_patch_rejects_tag_choice(client, fixture_pdfs: Path):
    job = upload(client, fixture_pdfs)
    response = client.patch(
        f"{BATCH}/{job['batch_id']}/config", json={"tag_choice": "generate"}
    )
    assert response.status_code == 422, response.text
