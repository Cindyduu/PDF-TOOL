"""浏览器端到端：v4.0 四页面动线（R13）。

上传 → 准备页 `#/prepare/:batchId` → 校验结果页 `#/verify/:batchId`
→ 自动处理页 `#/process/:batchId` → 结果页 `#/done/:batchId`。

为什么这些只能在浏览器里验：hash 路由、页面自己的轮询、刷新恢复、前进后退、
二次确认框、另存为面板——TestClient 一个都碰不到。

**v2.1 的三页面动线已不存在**（`#/config/` → `#/processing/` → `#/result/`），
所以本文件整体按四页面重写，而不是修补旧的等待条件。`#/result/:jobId` 仍保留
给历史页的「查看」按钮，因此「初始视图核对」与「预览降级」两条用例改为从结果页
跳过去验——它们覆盖的行为在 v4.0 里没有替代页面，删掉就等于降覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas import DEFAULT_PROCESS_GROUPS, OPERATION_KEYS, PROCESS_GROUPS, VERIFY_KEYS

pytestmark = pytest.mark.e2e

PDF_NAME = "text_blocks.pdf"

PREPARE = "#/prepare/"
VERIFY = "#/verify/"
PROCESS = "#/process/"
DONE = "#/done/"

# 页面上「还在跑」和「跑完了」的分界文案。校验页与结果页各自有一段汇总句，
# 判"跑完"只认前缀，因为后面的计数会随样本文件变化。
VERIFY_DONE = "校验完成"
DONE_DONE = "处理完成"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def wait_hash(page, prefix: str, timeout: int = 30000) -> None:
    page.wait_for_function(
        "prefix => location.hash.startsWith(prefix)", arg=prefix, timeout=timeout
    )


def id_from_hash(page) -> str:
    """`#/xxx/bat-123` 里的 `bat-123`。"""
    return page.evaluate("location.hash.split('/')[2]")


def upload(page, live_server, fixture_pdfs, *names: str) -> str:
    """打开首页、选中样本文件，等落到准备页，返回批次号。"""
    page.goto(live_server.url_for("/"))
    page.wait_for_selector("#dropzone")
    page.set_input_files("#file-input", [str(fixture_pdfs / name) for name in names])
    wait_hash(page, PREPARE)
    page.wait_for_selector("#prepare-table tbody tr")
    return id_from_hash(page)


def submit_precheck(page, timeout: int = 30000) -> None:
    """点「开始校验」并等落到校验结果页。"""
    page.wait_for_selector("#prepare-submit")
    page.click("#prepare-submit")
    wait_hash(page, VERIFY, timeout=timeout)


def wait_precheck_done(page, timeout: int = 120000) -> None:
    """等校验跑完。

    校验页自己在轮询（1.2 秒一次），所以这里不刷新、不调接口，只等它把
    汇总句从「正在校验…」换成「校验完成 · …」。
    """
    page.wait_for_selector("#verify-headline")
    page.wait_for_function(
        "prefix => (document.querySelector('#verify-headline')?.textContent || '').startsWith(prefix)",
        arg=VERIFY_DONE,
        timeout=timeout,
    )


def expand_all(page) -> None:
    """展开每个文件的十二项明细。只有一个文件时页面默认就是展开的。

    用页面内一次性 JS 点击，而不是 `query_selector_all` + 逐个 `click()`：
    校验页在轮询，句柄拿到的瞬间可能已经被重新渲染掉了，逐个点会撞上
    「Element is not attached to the DOM」。展开后等明细行真的出现再返回，
    否则调用方拿到的会是空列表（而不是一个明确的失败）。
    """
    page.evaluate(
        """() => {
            document.querySelectorAll('[data-toggle]').forEach((button) => {
                if (button.getAttribute('aria-expanded') === 'false') button.click();
            });
        }"""
    )
    page.wait_for_selector("#verify-files table tbody tr[data-key]")


def detail_keys(page, timeout: int = 15000) -> list[str]:
    """校验结果页里每个文件的十二项行键。

    走 `eval_on_selector_all` 在页面里一次取完：轮询重渲染会让句柄失效，
    先拿句柄再逐个读属性迟早会撞上「detached」。

    先等行出现再取：页面是先把汇总句换成「校验完成」、再渲染明细表的，
    只等汇总句会读到一张还没画出来的空表。
    """
    page.wait_for_selector("#verify-files table tbody tr[data-key]", timeout=timeout)
    return page.eval_on_selector_all(
        "#verify-files table tbody tr[data-key]", "rows => rows.map((row) => row.dataset.key)"
    )


def to_process(page) -> None:
    page.click("#to-process")
    wait_hash(page, PROCESS)
    page.wait_for_selector("#start-process")


def start_process(page, timeout: int = 30000) -> None:
    page.click("#start-process")
    wait_hash(page, DONE, timeout=timeout)


def wait_done(page, timeout: int = 180000) -> None:
    """等整批处理 + 后置校验跑完。"""
    page.wait_for_selector("#done-headline")
    page.wait_for_function(
        "prefix => (document.querySelector('#done-headline')?.textContent || '').startsWith(prefix)",
        arg=DONE_DONE,
        timeout=timeout,
    )


def run_to_done(page, live_server, fixture_pdfs, name: str = PDF_NAME) -> tuple[str, str]:
    """走完分支 A 全链路，返回 (批次号, 任务号)。"""
    batch_id = upload(page, live_server, fixture_pdfs, name)
    submit_precheck(page)
    wait_precheck_done(page)
    to_process(page)
    start_process(page)
    wait_done(page)
    job_id = page.get_attribute("#done-files section[data-job]", "data-job")
    return batch_id, job_id


def run_to_result(page, live_server, source) -> str:
    """走完分支 A 全链路后，再跳到单文件结果页 `#/result/:jobId`，返回任务号。

    `source` 是**完整的文件路径**，不是目录：`test_ui_save_as.py` 一直按
    `run_to_result(page, server, fixture_pdfs / PDF_NAME)` 这样调，把目录和
    文件名分开传会让它拼出 `…/text_blocks.pdf/text_blocks.pdf`，
    `set_input_files` 当场报「文件不存在」——整组用例连带红掉。

    v4.0 的主动线到 `#/done/:batchId` 就结束了，但 `#/result/:jobId` 并没有下线：
    历史页的「查看」按钮（`history.js`）仍然指向它，初始视图核对与预览降级这两块
    内容也只在这一页上。所以「另存为」那组用例继续从这里进——它们验的是下载面板
    的行为，和批次/单文件无关，换个入口就等于改断言对象。
    """
    source = Path(source)
    _, job_id = run_to_done(page, live_server, source.parent, source.name)
    page.goto(live_server.url_for(f"/#/result/{job_id}"))
    page.wait_for_selector("#download-output")
    return job_id


def row_keys(page, job_id: str) -> list[str]:
    """结果页某个文件下的全部 `tr[data-key]`，按文档顺序。"""
    return page.eval_on_selector_all(
        f"section[data-job='{job_id}'] tbody tr[data-key]",
        "rows => rows.map((row) => row.dataset.key)",
    )


def process_keys(page, job_id: str) -> list[str]:
    """处理明细的行键：只保留流水线项（排除后置校验那九行）。"""
    return [key for key in row_keys(page, job_id) if key in OPERATION_KEYS]


def post_verify_keys(page, job_id: str) -> list[str]:
    """后置校验的行键：只保留校验项（排除处理明细那几行）。"""
    return [key for key in row_keys(page, job_id) if key in VERIFY_KEYS]


def table_headers(page, job_id: str, keys: tuple[str, ...]) -> list[str]:
    """按「行键属于哪一套」定位到具体的表，再取表头。

    一个文件下有两张表（处理明细 / 后置校验），它们都带 `tr[data-key]`，
    所以不能按位置取第几张，只能看行键落在哪一套里。
    """
    headers = page.evaluate(
        """({ jobId, keys }) => {
            const section = document.querySelector(`section[data-job='${jobId}']`);
            if (!section) return null;
            const table = Array.from(section.querySelectorAll('table')).find((node) => {
                const first = node.querySelector('tbody tr[data-key]');
                return first && keys.includes(first.dataset.key);
            });
            if (!table) return null;
            return Array.from(table.querySelectorAll('thead th')).map((th) =>
                th.textContent.trim(),
            );
        }""",
        {"jobId": job_id, "keys": list(keys)},
    )
    assert headers is not None, f"{job_id} 下找不到表头属于 {keys[:2]} 的表"
    return headers


# --------------------------------------------------------------------------- #
# 分支 A：全链路（AC-R13-N2）
# --------------------------------------------------------------------------- #


def test_full_flow_from_upload_to_download(page, live_server, fixture_pdfs):
    """分支 A：准备 → 校验 → 处理 → 结果，每一页的硬约束都在。

    AC-R13-N2 全链路可走通；AC-R13-N5 十二个校验项默认全勾；
    AC-R24-N1 处理页六个复选框、默认勾第 1–5 项；AC-R27-N1 结果页十二行后置校验。
    """
    batch_id = upload(page, live_server, fixture_pdfs, PDF_NAME)
    assert batch_id.startswith("bat-")

    # ---- 页面 1：准备页 ------------------------------------------------
    boxes = page.query_selector_all('input[name="verify-item"]')
    assert len(boxes) == len(VERIFY_KEYS) == 12, "准备页应正好十二项校验"
    assert [box.get_attribute("value") for box in boxes] == list(VERIFY_KEYS), (
        "十二项的顺序必须与 VERIFY_KEYS 一致"
    )
    assert all(box.is_checked() for box in boxes), "十二个校验项默认应全部勾选（AC-R13-N5）"
    assert page.is_checked("#verify-all"), "全选框应随默认全勾而勾上"
    assert page.inner_text("#prepare-count").strip().startswith("已上传 1 个文件")

    # ---- 页面 2：校验结果页 --------------------------------------------
    submit_precheck(page)
    wait_precheck_done(page)

    assert page.inner_text("#verify-headline").startswith(VERIFY_DONE)
    # 单文件时明细默认就是摊开的：只有一份结果，摊开比折叠有用。
    # 先断言这个开关状态再读行——读不到行时，失败信息应该指出「没展开」，
    # 而不是甩一个 15 秒超时。
    assert page.get_attribute("#verify-files [data-toggle]", "aria-expanded") == "true", (
        "单文件批次的明细应默认展开"
    )
    assert detail_keys(page) == list(VERIFY_KEYS), "明细必须固定十二行，顺序与 VERIFY_KEYS 一致"

    to_process(page)

    # ---- 页面 3：自动处理页 --------------------------------------------
    groups = page.query_selector_all('input[name="process-group"]')
    assert len(groups) == len(PROCESS_GROUPS) == 7, "处理页应正好七个复选框"
    assert [box.get_attribute("value") for box in groups] == list(PROCESS_GROUPS)
    ticked = [box.get_attribute("value") for box in groups if box.is_checked()]
    assert ticked == list(DEFAULT_PROCESS_GROUPS), (
        "默认勾第 1–5 项，第 6 项（注释扁平化）不勾（AC-R24-N1）"
    )

    start_process(page)
    wait_done(page)

    # ---- 页面 4：结果页 ------------------------------------------------
    job_id = page.get_attribute("#done-files section[data-job]", "data-job")
    assert job_id and job_id.startswith("job-")

    link = page.query_selector(f"[data-action='download'][data-job='{job_id}']")
    assert link is not None, "结果页每个文件都要有下载入口"
    href = link.get_attribute("href")
    assert "/api/jobs/" in href and "kind=output" in href
    assert link.get_attribute("aria-disabled") is None

    # 后置校验固定九行（AC-R27-N1），且顺序与 VERIFY_KEYS 一致
    assert post_verify_keys(page, job_id) == list(VERIFY_KEYS)

    disclaimer = page.inner_text("#done-disclaimer")
    assert "不构成合规结论" in disclaimer, "结果页必须带免责声明"


def test_result_rows_carry_counts_and_reason(page, live_server, fixture_pdfs):
    """处理明细四栏齐全，且共享项只出现一次。

    AC-R24-N3：`metadata` 同时属于第 2 项与第 5 项，展开时必须去重，
    否则同一个处理项会跑两遍、在结果里出现两行。
    """
    _batch_id, job_id = run_to_done(page, live_server, fixture_pdfs)

    assert table_headers(page, job_id, OPERATION_KEYS) == [
        "处理项",
        "结果",
        "计数",
        "说明",
    ]

    keys = process_keys(page, job_id)
    # 默认五项展开成八项流水线（第 6 项扁平化没勾，所以不含 flatten_annotations）
    assert "flatten_annotations" not in keys, "未勾第 6 项时不该出现注释扁平化行"
    assert len(keys) == len(set(keys)), f"处理明细出现重复行：{keys}"
    assert keys.count("metadata") == 1, "metadata 被 g2/g5 共享，去重后只应出现一次"

    table_text = page.inner_text(f"section[data-job='{job_id}']")
    # 计数列不是空的：至少有一项写出了「已调整 / 已符合」这类计数
    assert "已调整" in table_text or "已符合" in table_text or "无需修改" in table_text


def test_single_file_details_open_themselves_when_the_result_arrives(
    page, live_server, fixture_pdfs
):
    """单文件批次的十二项明细应当自己摊开，哪怕打开页面时校验还在跑。

    回归：默认展开原本只在页面挂载那一刻判断，而挂载时 `verify` 还是空的
    （校验刚提交），结果要等轮询才回来。于是同一个单文件批次会「先打开页面就
    折叠、等跑完再打开就展开」，全看手速。现在改成「第一次看到结果时」摊开。

    这里刻意**不点**任何展开按钮：点了就验不出默认行为了。
    """
    upload(page, live_server, fixture_pdfs, PDF_NAME)
    submit_precheck(page)
    wait_precheck_done(page)

    assert page.get_attribute("#verify-files [data-toggle]", "aria-expanded") == "true", (
        "单文件批次的结果出来后，明细应当默认展开"
    )
    assert detail_keys(page) == list(VERIFY_KEYS)


def test_refresh_keeps_the_precheck_result(page, live_server, fixture_pdfs):
    """AC-R13-E3：刷新校验结果页要能看到同一份结论，不重新跑一遍。

    两边都先 `expand_all` 再比：`#verify-files` 的文本里含着「展开/收起明细」
    这个开关文案，而展开状态本来就不需要跨刷新保持（明细在服务端，刷完点一下
    就在）。单文件时两边其实都已经默认展开，这里点一下只是不依赖那个默认值——
    让这条用例只验「结论有没有变」，不被展开状态牵着走。
    展开之后比的是十二行明细本身——比只比汇总句更严，不是更松。
    """
    upload(page, live_server, fixture_pdfs, PDF_NAME)
    submit_precheck(page)
    wait_precheck_done(page)
    expand_all(page)

    before = page.inner_text("#verify-files")
    assert len(detail_keys(page)) == len(VERIFY_KEYS), "展开后应能看到十二行明细"

    page.reload()
    wait_precheck_done(page)
    expand_all(page)

    assert page.inner_text("#verify-files") == before, (
        "刷新后结论变了，说明页面自己又跑了一遍校验，或者读的不是后端结果"
    )


def test_refresh_during_processing_still_reaches_the_result(page, live_server, fixture_pdfs):
    """处理中途刷新不该丢进度——结果页的轮询会接着跑到终态。"""
    upload(page, live_server, fixture_pdfs, PDF_NAME)
    submit_precheck(page)
    wait_precheck_done(page)
    to_process(page)
    start_process(page)

    page.reload()
    wait_done(page)

    job_id = page.get_attribute("#done-files section[data-job]", "data-job")
    assert post_verify_keys(page, job_id) == list(VERIFY_KEYS)


def test_back_and_forward_restore_views(page, live_server, fixture_pdfs):
    """浏览器的前进/后退必须能回到对应页面，不能白屏。"""
    upload(page, live_server, fixture_pdfs, PDF_NAME)
    submit_precheck(page)
    wait_precheck_done(page)
    to_process(page)
    start_process(page)
    wait_done(page)

    page.go_back()
    wait_hash(page, PROCESS)
    page.wait_for_selector("#start-process")

    page.go_back()
    wait_hash(page, VERIFY)
    page.wait_for_selector("#verify-headline")

    page.go_forward()
    wait_hash(page, PROCESS)
    page.wait_for_selector("#start-process")


def test_history_lists_the_job_and_offers_download(page, live_server, fixture_pdfs):
    """产物同时留在历史里，可以从历史页再下载。"""
    _batch_id, job_id = run_to_done(page, live_server, fixture_pdfs)

    page.click('#nav a[data-nav="history"]')
    page.wait_for_selector("#history-table tbody tr")

    row = page.query_selector(f"#history-table tbody tr[data-job='{job_id}']")
    assert row is not None, "历史里找不到刚跑完的任务"
    assert PDF_NAME in row.inner_text()

    link = row.query_selector("[data-action='download']")
    assert link is not None
    assert link.get_attribute("href").startswith("/api/jobs/")
    assert link.get_attribute("aria-disabled") is None


# --------------------------------------------------------------------------- #
# 分支 B：跳过前置校验（AC-R13-N3）
# --------------------------------------------------------------------------- #


def test_branch_b_skips_precheck_but_still_post_verifies(page, live_server, fixture_pdfs):
    """AC-R13-N3 / AC-R27-N3：跳过前置校验，但后置校验仍然强制跑满十二项。"""
    upload(page, live_server, fixture_pdfs, PDF_NAME)

    # 勾「不执行前置校验」：十二个校验项必须置灰，而不是"看着还勾着但不生效"
    page.check("#skip-precheck")
    boxes = page.query_selector_all('input[name="verify-item"]')
    assert all(box.is_disabled() for box in boxes), "跳过校验时十二项应置灰"
    assert page.inner_text("#prepare-submit") == "进入自动处理"

    page.click("#prepare-submit")
    wait_hash(page, PROCESS, timeout=30000)
    page.wait_for_selector("#start-process")

    # 分支 B 不进校验结果页，直接从准备页跳到处理页
    assert not page.evaluate("() => location.hash").startswith(VERIFY)

    start_process(page)
    wait_done(page)

    job_id = page.get_attribute("#done-files section[data-job]", "data-job")
    assert post_verify_keys(page, job_id) == list(VERIFY_KEYS), (
        "跳过前置校验的文件，处理完仍必须有完整十二行后置校验"
    )
    assert "后置校验" in page.inner_text(f"section[data-job='{job_id}']")


# --------------------------------------------------------------------------- #
# 两个拦截点
# --------------------------------------------------------------------------- #


def test_prepare_blocks_submit_when_nothing_is_ticked(page, live_server, fixture_pdfs):
    """AC-R13-E1：十二项全不勾且没勾跳过时，点按钮被拦住并给出提示。"""
    upload(page, live_server, fixture_pdfs, PDF_NAME)

    page.uncheck("#verify-all")
    assert not any(
        box.is_checked() for box in page.query_selector_all('input[name="verify-item"]')
    )

    page.click("#prepare-submit")
    page.wait_for_function(
        "() => document.querySelector('#prepare-error').textContent.trim().length > 0",
        timeout=10000,
    )

    message = page.inner_text("#prepare-error")
    assert "至少勾选一项校验" in message
    assert "不执行前置校验" in message
    # 被拦住时不能已经跳走，否则等于"校验没提交但页面骗人"
    assert page.evaluate("() => location.hash").startswith(PREPARE)


def test_process_blocks_start_when_no_group_is_ticked(page, live_server, fixture_pdfs):
    """AC-R24-E1：六项全不勾时点「开始处理」被拦住。"""
    upload(page, live_server, fixture_pdfs, PDF_NAME)
    submit_precheck(page)
    wait_precheck_done(page)
    to_process(page)

    for box in page.query_selector_all('input[name="process-group"]'):
        box.uncheck()

    page.click("#start-process")
    page.wait_for_function(
        "() => document.querySelector('#process-error').textContent.trim().length > 0",
        timeout=10000,
    )

    assert "至少勾选一项处理" in page.inner_text("#process-error")
    assert page.evaluate("() => location.hash").startswith(PROCESS)


def test_flatten_asks_for_confirmation_and_reverts_on_cancel(
    page, live_server, fixture_pdfs
):
    """AC-R26-E1：勾第 6 项弹不可逆确认框，点「取消」后恢复未勾选。

    这是 v4.0 唯一一个不可逆动作，确认框走的是 `window.confirm`——
    Playwright 用 dialog 事件应答，点「取消」后复选框必须自己弹回去。
    """
    upload(page, live_server, fixture_pdfs, PDF_NAME)
    submit_precheck(page)
    wait_precheck_done(page)
    to_process(page)

    flatten = page.query_selector("input[name='process-group'][value='g6_flatten']")
    assert flatten is not None and not flatten.is_checked(), "第 6 项默认不勾"

    seen: list[str] = []

    def decline(dialog) -> None:
        seen.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", decline)
    # 用 click 而不是 check：`check()` 会断言「点完必须变成选中」，
    # 而这条路径的设计就是点完弹框、取消后自己弹回未勾选——用 check 一定红，
    # 且红的原因跟被测行为无关。
    flatten.click()
    page.wait_for_function(
        "() => document.querySelector(\"input[value='g6_flatten']\").checked === false",
        timeout=10000,
    )

    assert seen, "勾第 6 项必须弹确认框"
    assert "不可逆" in seen[0], f"确认框要说清不可逆，实际文案：{seen[0]!r}"

    # 同意时保持勾选
    page.remove_listener("dialog", decline)
    page.on("dialog", lambda dialog: dialog.accept())
    page.click("input[name='process-group'][value='g6_flatten']")
    page.wait_for_function(
        "() => document.querySelector(\"input[value='g6_flatten']\").checked === true",
        timeout=10000,
    )


# --------------------------------------------------------------------------- #
# 键盘可达性
# --------------------------------------------------------------------------- #


def test_keyboard_only_path_can_start_a_job(page, live_server, fixture_pdfs):
    """全键盘走完上传与提交：不点鼠标也能发起一次处理。"""
    page.goto(live_server.url_for("/"))
    page.wait_for_selector("#dropzone")

    # 首屏焦点就该在拖放区，键盘用户进来直接回车，不必先 Tab 找目标
    assert page.evaluate("() => document.activeElement.id") == "dropzone"

    with page.expect_file_chooser() as info:
        page.keyboard.press("Enter")
    info.value.set_files(str(fixture_pdfs / PDF_NAME))

    wait_hash(page, PREPARE)
    page.wait_for_selector("#prepare-submit")

    page.focus("#prepare-submit")
    page.keyboard.press("Enter")
    wait_hash(page, VERIFY)


# --------------------------------------------------------------------------- #
# 顶栏
# --------------------------------------------------------------------------- #


def test_top_nav_has_exactly_three_items(page, live_server):
    """顶栏只有三个入口：四步动线靠页面里的按钮推进，不占顶栏。"""
    page.goto(live_server.url_for("/"))
    page.wait_for_selector("#nav")

    labels = page.eval_on_selector_all(
        "#nav a[data-nav]", "links => links.map((link) => link.textContent.trim())"
    )
    assert labels == ["首页", "历史", "备份"]


# --------------------------------------------------------------------------- #
# 历史页「查看」→ v2.1 结果页（v4.0 没有替代页面，行为仍须保留）
# --------------------------------------------------------------------------- #


def test_result_shows_initial_view_audit_with_acrobat_wording(
    page, live_server, fixture_pdfs
):
    """初始视图三项要用 Acrobat 里的文字并排显示，不能只给 PDF 原始键值。

    v4.0 的结果页只列后置校验结论，这张对照表留在历史页「查看」打开的
    单文件结果页上——所以这里从结果页跳过去验，而不是把断言删掉。
    """
    _batch_id, job_id = run_to_done(page, live_server, fixture_pdfs)

    page.goto(live_server.url_for(f"/#/result/{job_id}"))
    page.wait_for_selector("#view-audit")

    rows = page.query_selector_all("#view-audit tbody tr[data-view]")
    assert [row.get_attribute("data-view") for row in rows] == [
        "bookmark_panel",
        "initial_layout",
        "initial_zoom",
    ]

    text = page.inner_text("#view-audit")
    assert "Acrobat" in text
    assert "文档属性" in text and "初始视图" in text


def test_preview_failure_shows_notice_keeps_download_enabled(
    page, live_server, fixture_pdfs
):
    """预览打不开只提示、不禁用下载：文件可能是好的，只是浏览器不肯内嵌。"""
    _batch_id, job_id = run_to_done(page, live_server, fixture_pdfs)

    page.goto(live_server.url_for(f"/#/result/{job_id}"))
    page.wait_for_selector("#preview-output")
    page.route(
        f"**/api/jobs/{job_id}/file*",
        lambda route: route.fulfill(status=500, body="boom"),
    )

    page.click("#preview-output")
    page.wait_for_function(
        "() => document.querySelector('#preview-notice').textContent.includes('下载不受影响')",
        timeout=15000,
    )

    link = page.query_selector("#download-output")
    assert link is not None, "预览失败不该把下载入口也拿掉"
    assert link.get_attribute("aria-disabled") is None
