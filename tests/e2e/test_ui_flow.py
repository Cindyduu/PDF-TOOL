"""浏览器端到端：上传 → 配置 → 开始 → 处理中 → 结果 → 下载。

这些用例只通过界面操作完成，不看接口返回、不改任何 JSON——
如果某条分支必须手改文件才能走通，这里就会失败。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

PDF_NAME = "text_blocks.pdf"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def wait_hash(page, prefix: str, timeout: int = 20000) -> None:
    page.wait_for_function(
        "prefix => location.hash.startsWith(prefix)", arg=prefix, timeout=timeout
    )


def upload(page, live_server, pdf_path: Path) -> None:
    page.goto(live_server.url_for("/"))
    page.wait_for_selector("#dropzone")
    page.set_input_files("#file-input", str(pdf_path))


def run_to_result(page, live_server, pdf_path: Path) -> str:
    """走完 上传 → 配置 → 开始 → 结果，返回 job_id。"""
    upload(page, live_server, pdf_path)
    wait_hash(page, "#/config/")

    page.wait_for_selector("#start-job")
    page.click("#start-job")

    wait_hash(page, "#/processing/")
    wait_hash(page, "#/result/", timeout=60000)
    page.wait_for_selector("#result-table")

    return page.evaluate("location.hash.split('/')[2]")


def job_id_from_hash(page) -> str:
    return page.evaluate("location.hash.split('/')[2]")


def press_enter_and_take_file_chooser(page, action, timeout_ms: int = 10000):
    """执行 action 并等 filechooser 事件，返回捕获到的 FileChooser。

    这里不用 page.expect_file_chooser()。它的监听器要等进入 with 上下文才注册，
    紧随其后的按键偶尔会抢在注册生效之前，事件就整个被漏掉了——症状是
    "等了 30 秒没有文件选择框"，但焦点在拖放区、浏览器控制台零报错，看着像是
    前端坏了。实测连跑 30 轮失败 3 次，每次都这样。
    改成先注册监听器、再按键：同一环境下 30 轮 0 失败。
    """
    caught = []

    def handler(chooser):  # noqa: ANN001 - Playwright 传进来的是 FileChooser
        caught.append(chooser)

    # 不配对调用 remove_listener：它内部会重新包装处理器，拿不到同一个函数对象，
    # 删不掉。page 是用例级 fixture，随用例结束销毁，监听器不会跨用例累积。
    page.on("filechooser", handler)
    action()
    waited = 0
    while not caught:
        if waited >= timeout_ms:
            raise AssertionError(
                "按键没有触发文件选择框：activeElement={!r}, hash={!r}".format(
                    page.evaluate(
                        "() => document.activeElement && "
                        "(document.activeElement.id || document.activeElement.tagName)"
                    ),
                    page.evaluate("() => location.hash"),
                )
            )
        page.wait_for_timeout(50)
        waited += 50
    return caught[0]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def test_full_flow_from_upload_to_download(page, live_server, fixture_pdfs):
    upload(page, live_server, fixture_pdfs / PDF_NAME)

    # 配置页：文件信息与标签检测都来自后端，不是前端编的
    wait_hash(page, "#/config/")
    page.wait_for_selector("#start-job")
    assert "text_blocks.pdf" in page.inner_text("#main")
    assert page.query_selector("input[name='tag-choice']:checked") is not None

    page.fill("#exempt-pages", "1")
    page.click("#start-job")

    # 处理中：只有阶段名，没有百分比
    wait_hash(page, "#/processing/")
    page.wait_for_selector("#stage-list li")
    stage_text = page.inner_text("#stage-list")
    assert "%" not in stage_text
    assert "准备处理" in stage_text

    # 结果页
    wait_hash(page, "#/result/", timeout=60000)
    page.wait_for_selector("#result-table tbody tr")
    rows = page.query_selector_all("#result-table tbody tr")
    assert len(rows) == 9

    first_row_text = rows[0].inner_text()
    assert "处理前" not in first_row_text  # 表头不在 tbody 里
    assert page.query_selector("#disclaimer").inner_text().strip() != ""

    # 下载：必须真的是 PDF
    download_link = page.query_selector("#download-output")
    assert download_link is not None
    assert download_link.get_attribute("aria-disabled") is None

    with page.expect_download() as download_info:
        page.click("#download-output")
    download = download_info.value
    assert download.suggested_filename.endswith(".pdf")
    saved = Path(download.path())
    assert saved.read_bytes()[:5] == b"%PDF-"


def test_result_rows_carry_before_after_counts_reason_and_next_action(
    page, live_server, fixture_pdfs
):
    run_to_result(page, live_server, fixture_pdfs / PDF_NAME)

    headers = [th.inner_text().strip() for th in page.query_selector_all("#result-table thead th")]
    assert headers == ["处理项", "结果", "处理前", "处理后", "计数", "原因", "下一步"]

    for row in page.query_selector_all("#result-table tbody tr"):
        cells = row.query_selector_all("td")
        assert len(cells) == 6  # 第一列是 th（处理项名）
        status_text = cells[0].inner_text().strip()
        assert any(
            label in status_text
            for label in ("已调整", "无需修改", "不需要修改", "待人工复核", "失败")
        )
        # 图标 + 文字：状态不能只靠颜色
        assert cells[0].query_selector(".badge .icon") is not None


def test_result_shows_initial_view_audit_with_acrobat_wording(page, live_server, fixture_pdfs):
    """用户是拿 Acrobat 三个下拉框验收的，页面上必须有能逐字对号的对照。"""
    run_to_result(page, live_server, fixture_pdfs / PDF_NAME)

    card = page.query_selector("#view-audit")
    assert card is not None, "结果页缺少「初始视图核对」卡片"

    rows = page.query_selector_all("#view-audit tbody tr[data-view]")
    assert [row.get_attribute("data-view") for row in rows] == [
        "bookmark_panel",
        "initial_layout",
        "initial_zoom",
    ]

    text = card.inner_text()
    # Acrobat 口径的目标值必须出现，否则用户还是对不上号
    assert "书签面板和页面" in text
    assert "默认" in text

    for row in rows:
        cells = row.query_selector_all("td")
        assert len(cells) == 3  # 第一列是 th（项目名）
        assert cells[0].inner_text().strip() != "—", "处理后必须给出 Acrobat 文字"
        raw = cells[1].inner_text().strip()
        assert "→" in raw and raw != "→", f"原始键值列缺内容：{raw!r}"
        assert cells[2].inner_text().strip() in ("已调整", "已符合")


# --------------------------------------------------------------------------- #
# 刷新与历史导航
# --------------------------------------------------------------------------- #


def test_refresh_keeps_result_state(page, live_server, fixture_pdfs):
    job_id = run_to_result(page, live_server, fixture_pdfs / PDF_NAME)
    before = page.inner_text("#result-table")

    page.reload()
    page.wait_for_selector("#result-table tbody tr")

    assert job_id_from_hash(page) == job_id
    assert page.inner_text("#result-table") == before
    assert len(page.query_selector_all("#result-table tbody tr")) == 9


def test_refresh_during_processing_still_reaches_the_result(page, live_server, fixture_pdfs):
    upload(page, live_server, fixture_pdfs / PDF_NAME)
    wait_hash(page, "#/config/")
    page.wait_for_selector("#start-job")
    page.click("#start-job")
    wait_hash(page, "#/processing/")

    page.reload()
    # 刷新之后要么还在处理中，要么已经跳到结果页——两种都不算丢状态
    wait_hash(page, "#/result/", timeout=60000)
    page.wait_for_selector("#result-table tbody tr")
    assert len(page.query_selector_all("#result-table tbody tr")) == 9


def test_back_and_forward_restore_views(page, live_server, fixture_pdfs):
    run_to_result(page, live_server, fixture_pdfs / PDF_NAME)

    page.click("#nav a[data-nav='history']")
    wait_hash(page, "#/history")
    page.wait_for_selector("#history-table tbody tr")

    page.go_back()
    wait_hash(page, "#/result/")
    page.wait_for_selector("#result-table tbody tr")
    assert len(page.query_selector_all("#result-table tbody tr")) == 9

    page.go_forward()
    wait_hash(page, "#/history")
    page.wait_for_selector("#history-table tbody tr")


def test_history_lists_the_job_and_offers_download(page, live_server, fixture_pdfs):
    job_id = run_to_result(page, live_server, fixture_pdfs / PDF_NAME)

    page.click("#nav a[data-nav='history']")
    wait_hash(page, "#/history")
    page.wait_for_selector(f"tr[data-job='{job_id}']")

    row = page.query_selector(f"tr[data-job='{job_id}']")
    assert "text_blocks.pdf" in row.inner_text()
    assert "输出可用" in row.inner_text()

    download = row.query_selector("[data-action='download']")
    assert download is not None
    assert download.get_attribute("aria-disabled") is None
    assert download.get_attribute("href").startswith("/api/jobs/")

    # 搜索框能过滤掉不存在的名字
    page.fill("#history-search", "不存在的文件名")
    page.wait_for_selector(".empty")
    page.fill("#history-search", "text_blocks")
    page.wait_for_selector(f"tr[data-job='{job_id}']")


# --------------------------------------------------------------------------- #
# 键盘可达与导航结构
# --------------------------------------------------------------------------- #


def test_top_nav_has_exactly_three_items(page, live_server):
    page.goto(live_server.url_for("/"))
    page.wait_for_selector("#nav a")

    links = page.query_selector_all("#nav a")
    assert len(links) == 3
    assert [link.inner_text().strip() for link in links] == ["首页", "历史", "备份"]


def test_keyboard_only_path_can_start_a_job(page, live_server, fixture_pdfs):
    page.goto(live_server.url_for("/"))
    page.wait_for_selector("#dropzone")

    # 首屏渲染完会主动把焦点放到拖放区（键盘用户不用先 Tab 找目标）。
    # 这里等的是这条产品行为本身，不是定时器：焦点没落到拖放区就直接报错，
    # 否则回车会落到 body 上，症状是"等了 30 秒没有文件选择框"，看不出原因。
    try:
        page.wait_for_function(
            "() => document.activeElement && document.activeElement.id === 'dropzone'",
            timeout=10000,
        )
    except Exception:  # noqa: BLE001 - 要把现场信息带出来
        raise AssertionError(
            "首屏没有把焦点放到拖放区：activeElement={!r}".format(
                page.evaluate(
                    "() => document.activeElement && "
                    "(document.activeElement.id || document.activeElement.tagName)"
                )
            )
        ) from None

    # 键盘：回车打开选择框 → 选文件
    chooser = press_enter_and_take_file_chooser(page, lambda: page.keyboard.press("Enter"))
    chooser.set_files(str(fixture_pdfs / PDF_NAME))

    wait_hash(page, "#/config/")
    page.wait_for_selector("#start-job")

    # 只用 Tab + Enter 走到"开始处理"
    reached = False
    for _ in range(40):
        page.keyboard.press("Tab")
        focused = page.evaluate(
            "() => { const a = document.activeElement; return a ? (a.id || a.tagName) : null; }"
        )
        if focused == "start-job":
            reached = True
            break
    assert reached, "Tab 键走不到开始处理按钮"

    page.keyboard.press("Enter")
    wait_hash(page, "#/processing/")
    wait_hash(page, "#/result/", timeout=60000)


# --------------------------------------------------------------------------- #
# 预览失败不阻塞下载
# --------------------------------------------------------------------------- #


def test_preview_failure_shows_notice_but_keeps_download_enabled(
    page, live_server, fixture_pdfs
):
    job_id = run_to_result(page, live_server, fixture_pdfs / PDF_NAME)

    # 把输出文件挪走：模拟"记录还在、文件没了"
    job_json = live_server.job_dir(job_id) / "job.json"
    output_name = json.loads(job_json.read_text(encoding="utf-8"))["output"]["file_name"]
    (live_server.job_dir(job_id) / output_name).unlink()

    page.click("#preview-output")
    page.wait_for_function(
        "() => document.querySelector('#preview-notice').textContent.trim().length > 0",
        timeout=10000,
    )
    notice = page.inner_text("#preview-notice")
    assert "下载不受影响" in notice

    download_link = page.query_selector("#download-output")
    assert download_link is not None
    assert download_link.get_attribute("aria-disabled") is None
    assert f"/api/jobs/{job_id}/file?kind=output" in download_link.get_attribute("href")
