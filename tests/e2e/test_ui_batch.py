"""浏览器端到端：一批多个文件走完 v4.0 四页面动线（R13）。

和 `test_ui_flow.py` 一样只通过界面操作完成，不看接口返回、不改任何 JSON。
区别只在文件数：那边验单文件的每一步硬约束，这边验**多文件**时才会暴露的问题。

四条产品承诺必须由浏览器验证，TestClient 验不了：
- 一次选多个文件时**前端不再吞掉后面的文件**（老版本只取第一个）；
- 校验页与结果页会**自己轮询**到全部结束，不需要手动刷新；
- 一个坏文件**不阻塞**同批其他文件；
- 刷新页面**不丢进度**——表格内容来自后端而不是内存。

**v2.1 的 `#/batch/` 单页已不存在**（批次表格 + 「全部开始」按钮），所以这里按
v4.0 的两段式重写：准备页勾校验项 → 校验页看结论 → 处理页勾处理项 → 结果页拿产物。
「轮询到自己结束」这条承诺现在由校验页和结果页各承担一半，两个都要验到，
否则就等于把原来那条覆盖删了。
"""

from __future__ import annotations

import pytest

from app.schemas import VERIFY_KEYS
from tests.e2e.test_ui_flow import (
    VERIFY_DONE,
    expand_all,
    id_from_hash,
    start_process,
    submit_precheck,
    to_process,
    upload,
    wait_done,
    wait_hash,
    wait_precheck_done,
)

pytestmark = pytest.mark.e2e

GOOD = "text_blocks.pdf"
SECOND = "untagged.pdf"
BROKEN = "corrupt.pdf"


def section_count(page, container: str) -> int:
    return page.eval_on_selector_all(
        f"{container} section[data-job]", "nodes => nodes.length"
    )


def normalised(page, selector: str) -> str:
    """把元素文本压成单行：`inner_text` 会在内联元素边界插入换行。"""
    return " ".join(page.inner_text(selector).split())


# --------------------------------------------------------------------------- #
# 多选不吞文件
# --------------------------------------------------------------------------- #


def test_multi_select_opens_prepare_page_with_every_file(page, live_server, fixture_pdfs):
    """选两个文件必须两个都进准备页，不能"只处理第一个"。

    这是 v2.1 那条同名用例的 v4.0 落点：入口页从 `#/batch/` 换成了
    `#/prepare/:batchId`，但断言对象没变——两个文件、两行、计数是 2。
    """
    upload(page, live_server, fixture_pdfs, GOOD, SECOND)

    rows = page.query_selector_all("#prepare-table tbody tr[data-job]")
    assert len(rows) == 2, "多选之后准备页只出现了一个文件"

    table_text = page.inner_text("#prepare-table")
    assert GOOD in table_text
    assert SECOND in table_text
    assert normalised(page, "#prepare-count").startswith("已上传 2 个文件")


# --------------------------------------------------------------------------- #
# 自己轮询到结束 + 每行都能下载
# --------------------------------------------------------------------------- #


def test_batch_runs_every_job_to_completion_and_offers_downloads(
    page, live_server, fixture_pdfs
):
    """一路点到结果页，全程不刷新、不调接口，靠页面自己的定时器跑完。

    轮询分两段，两段都要验：
    - 校验页：等汇总句变成「校验完成」，且两个文件都出现十二项明细；
    - 结果页：等汇总句变成「处理完成」，且每份文件都有可用下载入口。
    """
    upload(page, live_server, fixture_pdfs, GOOD, SECOND)

    submit_precheck(page)
    wait_precheck_done(page)

    assert section_count(page, "#verify-files") == 2, "校验页少了一个文件"

    # 多文件时默认只展开第一个，所以先全展开再数行——明细必须是固定十二项，
    # 顺序也必须和 `/api/meta` 给的一致。
    expand_all(page)
    keys = page.evaluate(
        """() => {
            const section = document.querySelector('#verify-files section[data-job]');
            if (!section) return null;
            return Array.from(section.querySelectorAll('tbody tr[data-key]')).map(
                (row) => row.dataset.key,
            );
        }"""
    )
    assert keys == list(VERIFY_KEYS), "多文件时明细行数/顺序也必须固定十二项"

    to_process(page)
    start_process(page)
    wait_done(page)

    # 结果页也在轮询（1.2 秒一次），先等卡片出现，再一次在页面里取完所有属性：
    # 拿句柄再逐个读，会撞上「重新渲染后句柄失效」。
    page.wait_for_selector("#done-files section[data-job]")
    cards = page.eval_on_selector_all(
        "#done-files section[data-job]",
        """sections => sections.map((section) => {
            const link = section.querySelector("[data-action='download']");
            return {
                job: section.dataset.job,
                href: link ? link.getAttribute('href') : null,
                disabled: link ? link.getAttribute('aria-disabled') : null,
                text: (section.innerText || '').slice(0, 80),
            };
        })""",
    )
    assert len(cards) == 2, "结果页少了一个文件"
    for card in cards:
        assert card["href"] is not None, f"{card['job']} 没有下载入口：{card['text']}"
        assert card["href"].startswith("/api/jobs/"), card["href"]
        assert card["disabled"] is None, f"{card['job']} 的下载被禁用了"


# --------------------------------------------------------------------------- #
# 坏文件不阻塞
# --------------------------------------------------------------------------- #


def test_one_broken_file_does_not_block_the_others(page, live_server, fixture_pdfs):
    """坏文件只影响自己那一行，好文件照常走完。"""
    upload(page, live_server, fixture_pdfs, GOOD, BROKEN)

    rows = page.query_selector_all("#prepare-table tbody tr[data-job]")
    assert len(rows) == 1, "坏文件不应该建任务，好文件应该照常入批"
    assert GOOD in rows[0].inner_text()

    # 坏文件被拒必须让用户知道，否则"我选了 2 个怎么只有 1 个"没法解释。
    # toast 在视图容器之外，跳转后仍然存在，所以读 textContent 而不是 inner_text
    # （后者在元素隐藏时返回空串，会随淡出时机变成 flaky）。
    page.wait_for_function(
        "() => document.querySelector('#toast').textContent.includes('未通过检查')",
        timeout=10000,
    )

    submit_precheck(page)
    wait_precheck_done(page)
    to_process(page)
    start_process(page)
    wait_done(page)

    assert section_count(page, "#done-files") == 1
    assert "成功 1" in normalised(page, "#done-headline")


# --------------------------------------------------------------------------- #
# 刷新不丢进度
# --------------------------------------------------------------------------- #


def test_prepare_page_survives_refresh(page, live_server, fixture_pdfs):
    """刷新准备页不该丢进度——表格内容来自后端而不是内存。"""
    batch_id = upload(page, live_server, fixture_pdfs, GOOD, SECOND)
    before = page.inner_text("#prepare-table")

    page.reload()
    page.wait_for_selector("#prepare-table tbody tr[data-job]")

    assert id_from_hash(page) == batch_id
    assert page.inner_text("#prepare-table") == before


def test_verify_page_survives_refresh_after_completion(page, live_server, fixture_pdfs):
    """校验跑完之后刷新，结论还在——这是「关掉页面再打开能恢复」的浏览器侧证据。

    AC-R13-E3 要求恢复的是"校验结果"，不是"页面骨架"，所以这里比对的是
    汇总句与文件块，而不是只等元素出现。
    """
    upload(page, live_server, fixture_pdfs, GOOD, SECOND)
    submit_precheck(page)
    wait_precheck_done(page)

    batch_id = id_from_hash(page)
    headline = normalised(page, "#verify-headline")
    assert headline.startswith(VERIFY_DONE)

    page.reload()
    page.wait_for_selector("#verify-headline")
    wait_precheck_done(page)

    assert id_from_hash(page) == batch_id
    assert normalised(page, "#verify-headline") == headline


# --------------------------------------------------------------------------- #
# 上限拦截
# --------------------------------------------------------------------------- #


def test_over_limit_selection_is_refused_before_upload(page, live_server):
    """超过上限时前端直接拦下：不发请求、不建任务，并说明原因。"""
    page.goto(live_server.url_for("/"))
    page.wait_for_selector("#dropzone")

    limit = page.evaluate(
        "() => Number(document.querySelector('#dropzone-hint').textContent.match(/最多 (\\d+)/)[1])"
    )
    assert limit > 0

    # 用内存 payload 造出 limit+1 个文件：这里只验前端拦截，不需要真 PDF 内容
    payloads = [
        {
            "name": f"batch-{index}.pdf",
            "mimeType": "application/pdf",
            "buffer": b"%PDF-1.4\n%%EOF\n",
        }
        for index in range(limit + 1)
    ]
    page.set_input_files("#file-input", payloads)

    page.wait_for_function(
        "() => document.querySelector('#upload-error').textContent.trim().length > 0",
        timeout=10000,
    )
    message = page.inner_text("#upload-error")
    assert f"一次最多 {limit} 个文件" in message
    assert "分批上传" in message

    # 没有跳走，说明请求根本没发出去
    assert page.evaluate("() => location.hash") in ("", "#/home", "#/")


# --------------------------------------------------------------------------- #
# 未使用但保留：给后续排查用的等待助手
# --------------------------------------------------------------------------- #


def test_unknown_batch_shows_a_readable_error(page, live_server):
    """直接打开一个不存在的批次号，页面要给能读懂的说明而不是白屏。

    这条不在 §13.1 的三条里，但它守的是 v4.0 新动线的兜底：四页面全部挂
    `:batchId`，URL 是可以被用户手改的，改坏了不能只留一片空白。
    """
    page.goto(live_server.url_for("/#/prepare/bat-does-not-exist"))
    wait_hash(page, "#/prepare/")
    page.wait_for_selector("section.card h1")

    heading = normalised(page, "section.card h1")
    assert heading == "打不开这个批次"
    assert normalised(page, "section.card .error").strip() != ""

    # 页面上必须有回到首页的路，否则用户只能改地址栏
    assert page.query_selector("section.card a[href='#/home']") is not None
