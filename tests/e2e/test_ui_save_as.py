"""下载时选择保存位置与改文件名（R12）的端到端用例。

S1 部分（文件名规则）放在 e2e 目录却一条界面都不点：这套规则写在浏览器端的
ES 模块里，项目没有 Node 工具链，也不该为了几条字符串规则引一个。做法是开一个
真实页面、`import` 那个模块直接调函数——既覆盖了规则本身，也顺带证明模块在
浏览器里真的能加载（语法错、导出名写错都会立刻红）。

S2 起是真点界面：走完上传 → 处理 → 结果页，验证「另存为」面板的预填、
校验、取消与改名下载。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.e2e.test_ui_flow import PDF_NAME, run_to_result

pytestmark = pytest.mark.e2e

MODULE = "/static/js/filename.js"

# (输入, 期望的最终文件名, 期望的修正提示里必须出现的一段话；None 表示不该有提示)
VALID_CASES = [
    ("报告.pdf", "报告.pdf", None),
    ("报告", "报告.pdf", "已自动补上 .pdf"),
    ("  报告.pdf  ", "报告.pdf", "首尾空格"),
    ("报告.", "报告.pdf", "已自动补上 .pdf"),
    ("报告.PDF", "报告.PDF", None),
    ("报告.doc", "报告.pdf", "已改为 .pdf"),
    ("报告..pdf", "报告.pdf", "末尾的点或空格"),
    ("a.b.c.pdf", "a.b.c.pdf", None),
    ("中文 名字（终版）.pdf", "中文 名字（终版）.pdf", None),
    ("报告\x00.pdf", "报告.pdf", None),  # 粘贴带进的控制字符直接丢掉
    (".hidden.pdf", ".hidden.pdf", None),
    ("CONSOLE.pdf", "CONSOLE.pdf", None),  # 保留名只认完全相等，不能误判
    ("x" * 150 + ".pdf", "x" * 150 + ".pdf", None),  # 长度上限刚好够
]

# (输入, 期望的错误提示里必须出现的一段话)
INVALID_CASES = [
    ("", "不能为空"),
    ("   ", "不能为空"),
    ("a<b.pdf", "<"),
    ("a>b.pdf", ">"),
    ('a"b.pdf', '"'),
    ("a|b.pdf", "|"),
    ("a?b.pdf", "?"),
    ("a*b.pdf", "*"),
    ("a:b.pdf", ":"),
    ("sub/dir.pdf", "路径分隔符"),
    ("..\\etc\\passwd.pdf", "路径分隔符"),
    ("CON.pdf", "保留名"),
    ("con.pdf", "保留名"),
    ("COM1.pdf", "保留名"),
    ("lpt9.pdf", "保留名"),
    (".pdf", "只有扩展名"),
    ("x" * 151 + ".pdf", "过长"),
]


def _validate_many(page, live_server, values: list[str]) -> list[dict]:
    page.goto(live_server.url_for("/"))
    return page.evaluate(
        """async ({ module, values }) => {
            const m = await import(module);
            return values.map((value) => m.validateName(value));
        }""",
        {"module": MODULE, "values": values},
    )


def test_auto_fixable_names_are_normalised_with_a_notice(page, live_server):
    results = _validate_many(page, live_server, [case[0] for case in VALID_CASES])
    assert len(results) == len(VALID_CASES)

    for (raw, expected_name, expected_notice), result in zip(VALID_CASES, results):
        label = f"输入 {raw!r}"
        assert result["ok"] is True, f"{label} 应通过，实际报错：{result['error']}"
        assert result["error"] == "", f"{label} 通过时不该带错误文案"
        assert result["name"] == expected_name, f"{label} 期望 {expected_name!r}，得到 {result['name']!r}"
        if expected_notice is None:
            assert result["notice"] == "", f"{label} 不该有修正提示，实际 {result['notice']!r}"
        else:
            assert expected_notice in result["notice"], (
                f"{label} 的修正提示应含 {expected_notice!r}，实际 {result['notice']!r}"
            )


def test_unfixable_names_are_rejected_with_a_reason(page, live_server):
    results = _validate_many(page, live_server, [case[0] for case in INVALID_CASES])
    assert len(results) == len(INVALID_CASES)

    for (raw, expected_error), result in zip(INVALID_CASES, results):
        label = f"输入 {raw!r}"
        assert result["ok"] is False, f"{label} 应被拒绝，实际通过了并给出 {result['name']!r}"
        assert expected_error in result["error"], (
            f"{label} 的报错应含 {expected_error!r}，实际 {result['error']!r}"
        )
        # 拒绝时不能顺手给一个"看起来能用"的名字，否则界面容易误用
        assert result["notice"] == "", f"{label} 被拒绝时不该带修正提示"


def test_module_exposes_the_same_length_limit_as_the_backend(page, live_server):
    """上限两边必须一致：前端放过去、后端截断，等于静默改名。"""
    from app.core import naming

    page.goto(live_server.url_for("/"))
    limit = page.evaluate(
        "async (module) => (await import(module)).MAX_STEM_CHARS", MODULE
    )
    assert limit == naming.MAX_FILENAME_CHARS


# --------------------------------------------------------------------------- #
# S2：另存为面板
# --------------------------------------------------------------------------- #

DIALOG = "dialog.save-as[open]"

# 打桩用的三种行为 + 一种「假装浏览器没这个 API」。
# 真实 picker 会弹系统窗口，Playwright 点不动，所以主路径只能靠桩验证：
# 桩收到的 options 和写进去的字节，就是我们能断言的全部事实。
INSTALL_PICKER = """
(mode) => {
  if (mode === 'none') {
    // 不能 delete：showSaveFilePicker 挂在 Window.prototype 上，删实例上的属性
    // 删不掉原型链上那个，typeof 仍然是 'function'。赋 undefined 才真的挡住。
    window.showSaveFilePicker = undefined;
    return;
  }
  window.showSaveFilePicker = async (options) => {
    if (mode === 'abort') throw new DOMException('用户取消', 'AbortError');
    if (mode === 'denied') throw new DOMException('需要用户激活', 'NotAllowedError');
    window.__picker = { options, chunks: [], closed: false, aborted: false };
    return {
      createWritable: async () =>
        new WritableStream({
          write(chunk) {
            window.__picker.chunks.push(chunk);
          },
          close() {
            const chunks = window.__picker.chunks;
            window.__picker.size = chunks.reduce((n, c) => n + c.byteLength, 0);
            window.__picker.head = new TextDecoder().decode(chunks[0].slice(0, 5));
            window.__picker.closed = true;
          },
          abort() {
            window.__picker.aborted = true;
          },
        }),
    };
  };
}
"""


def install_picker(page, mode: str) -> None:
    page.evaluate(INSTALL_PICKER, mode)


def open_save_as(page, live_server, fixture_pdfs, picker: str | None = None) -> str:
    """跑到结果页并点开「另存为」面板，返回 job_id。

    picker 不为 None 时先给 window.showSaveFilePicker 打桩，必须在点下载之前装，
    因为面板打开时就会按这个能力决定位置提示的文案。
    """
    job_id = run_to_result(page, live_server, fixture_pdfs / PDF_NAME)
    if picker is not None:
        install_picker(page, picker)
    page.click("#download-output")
    page.wait_for_selector(DIALOG)
    return job_id


def test_download_opens_a_prefilled_save_as_dialog(page, live_server, fixture_pdfs):
    open_save_as(page, live_server, fixture_pdfs)

    assert page.inner_text("#save-as-original") == PDF_NAME
    assert re.search(r"\d", page.inner_text("#save-as-size")), "大小不该是占位符"

    value = page.input_value("#save-as-name")
    assert value.endswith(".pdf")
    assert "_已处理_" in value, f"应预填后端生成的输出名，实际 {value!r}"

    # 预填名全选：想改名直接打字覆盖，不用先手动清空
    start, end, length = page.evaluate(
        "() => { const i = document.querySelector('#save-as-name');"
        " return [i.selectionStart, i.selectionEnd, i.value.length]; }"
    )
    assert (start, end) == (0, length)

    assert page.is_enabled("#save-as-confirm")
    assert page.inner_text("#save-as-error") == ""

    # 元素仍然是带 href 的 <a>：右键「链接另存为」和 JS 失效时的退路都靠它
    href = page.get_attribute("#download-output", "href")
    assert "/api/jobs/" in href and "kind=output" in href


def test_invalid_name_disables_save_and_blocks_enter(page, live_server, fixture_pdfs):
    open_save_as(page, live_server, fixture_pdfs)

    page.fill("#save-as-name", "a<b")
    assert page.is_disabled("#save-as-confirm")
    assert "<" in page.inner_text("#save-as-error")

    # 只禁用按钮挡不住在输入框里按回车，提交事件必须被拦住
    page.press("#save-as-name", "Enter")
    page.wait_for_timeout(300)
    assert page.query_selector(DIALOG) is not None, "名字非法时不该被回车关掉"

    page.fill("#save-as-name", "报告")
    assert page.is_enabled("#save-as-confirm")
    assert "已自动补上 .pdf" in page.inner_text("#save-as-notice")
    assert page.inner_text("#save-as-error") == ""


def test_cancel_closes_the_dialog_and_downloads_nothing(page, live_server, fixture_pdfs):
    downloads = []
    page.on("download", lambda item: downloads.append(item))
    open_save_as(page, live_server, fixture_pdfs)

    page.click("#save-as-cancel")
    page.wait_for_selector(DIALOG, state="hidden")
    page.wait_for_timeout(300)

    assert downloads == [], "取消不该触发任何下载"
    assert page.inner_text("#result-action-error") == ""


def test_escape_closes_the_dialog_without_downloading(page, live_server, fixture_pdfs):
    downloads = []
    page.on("download", lambda item: downloads.append(item))
    open_save_as(page, live_server, fixture_pdfs)

    page.keyboard.press("Escape")
    page.wait_for_selector(DIALOG, state="hidden")
    page.wait_for_timeout(300)

    assert downloads == [], "Esc 关掉面板不该触发下载"


def test_confirming_a_renamed_file_downloads_with_that_name(page, live_server, fixture_pdfs):
    # 明确装上「浏览器没有这个 API」的桩：这条验的就是降级路径，
    # 不能让它因为 headless Chrome 恰好支持 picker 而跑到另一条分支上去
    open_save_as(page, live_server, fixture_pdfs, picker="none")

    page.fill("#save-as-name", "测试改名")
    with page.expect_download() as info:
        page.click("#save-as-confirm")
    download = info.value

    assert page.query_selector(DIALOG) is None, "确认后应关掉面板"
    assert download.suggested_filename == "测试改名.pdf"
    saved = Path(download.path())
    assert saved.read_bytes()[:5] == b"%PDF-", "下载到的必须真的是 PDF"


# --------------------------------------------------------------------------- #
# S3：真正落盘（picker 主路径 + 降级）
# --------------------------------------------------------------------------- #


def _picker_state(page) -> dict:
    page.wait_for_function(
        "() => window.__picker && window.__picker.closed === true", timeout=15000
    )
    return page.evaluate(
        """() => ({
            options: window.__picker.options,
            size: window.__picker.size,
            head: window.__picker.head,
            aborted: window.__picker.aborted,
        })"""
    )


def test_picker_gets_the_chosen_name_and_streams_the_pdf(page, live_server, fixture_pdfs):
    downloads = []
    page.on("download", lambda item: downloads.append(item))
    job_id = open_save_as(page, live_server, fixture_pdfs, picker="stub")

    page.fill("#save-as-name", "测试改名")
    page.click("#save-as-confirm")
    state = _picker_state(page)

    assert state["options"]["suggestedName"] == "测试改名.pdf"
    # 文件类型收窄到 .pdf，用户不能在系统窗口里把它存成 .txt
    assert state["options"]["types"][0]["accept"]["application/pdf"] == [".pdf"]

    expected = page.request.get(
        live_server.url_for(f"/api/jobs/{job_id}/file?kind=output")
    )
    assert expected.ok
    body = expected.body()
    assert state["size"] == len(body), "写进文件的字节数必须和服务端吐的一致"
    assert state["head"] == "%PDF-", "落盘的必须真的是 PDF"
    assert state["aborted"] is False
    assert downloads == [], "走了 picker 就不该再触发一次浏览器下载"


def test_picker_cancel_leaves_no_file_and_no_error(page, live_server, fixture_pdfs):
    open_save_as(page, live_server, fixture_pdfs, picker="abort")

    page.click("#save-as-confirm")
    page.wait_for_selector(DIALOG, state="hidden")
    page.wait_for_timeout(300)

    assert page.query_selector("#result-action-error").inner_text().strip() == ""
    assert page.evaluate("() => window.__picker === undefined"), "取消不该建写入流"


def test_picker_refusal_falls_back_to_a_named_download(page, live_server, fixture_pdfs):
    """用户激活过期或策略禁用时，不能让用户白点一次。"""
    open_save_as(page, live_server, fixture_pdfs, picker="denied")

    page.fill("#save-as-name", "退路也要改名")
    with page.expect_download() as info:
        page.click("#save-as-confirm")

    assert info.value.suggested_filename == "退路也要改名.pdf"
    assert page.query_selector("#result-action-error").inner_text().strip() == ""


def test_location_hint_follows_browser_capability(page, live_server, fixture_pdfs):
    """提示不能说谎：能选位置就说能，不能就直说存到默认目录。"""
    open_save_as(page, live_server, fixture_pdfs, picker="stub")
    assert "选择保存位置" in page.inner_text("#save-as-location")
    page.keyboard.press("Escape")
    page.wait_for_selector(DIALOG, state="hidden")

    install_picker(page, "none")
    page.click("#download-output")
    page.wait_for_selector(DIALOG)
    hint = page.inner_text("#save-as-location")
    assert "默认下载目录" in hint
    assert "选择保存位置" not in hint


def test_failed_fetch_reports_and_leaves_no_file(page, live_server, fixture_pdfs):
    job_id = open_save_as(page, live_server, fixture_pdfs, picker="stub")
    page.route(
        f"**/api/jobs/{job_id}/file*",
        lambda route: route.fulfill(status=500, body="boom"),
    )

    page.click("#save-as-confirm")
    page.wait_for_function(
        "() => document.querySelector('#result-action-error').textContent.trim().length > 0",
        timeout=15000,
    )

    assert "保存失败" in page.inner_text("#result-action-error")
    # 先拿响应再建写入流：fetch 挂了就不该在磁盘上留下半个文件
    assert page.evaluate("() => window.__picker.closed") is False
    assert page.evaluate("() => window.__picker.chunks.length") == 0
