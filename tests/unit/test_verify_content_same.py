"""`scripts/verify_content_same.py`：独立复核脚本。

这个脚本的价值全在「独立」两个字上。如果它只是把 R36 护栏再跑一遍，那它就是
一张假的第三方证明——用户拿它去申报，等于自己给自己盖章。所以这里除了逐条验
判据，还专门验一件事：**脚本不许 import 护栏**（`test_script_does_not_import_the_guard`）。

判据分五类，各自的失败方式不一样，测试也分开钉：

- 结构（页数/尺寸/旋转/图片/字体）变了 → 必须失败。任何「只改格式」的处理都不该
  动这些。
- 文字变了 → 必须失败，并且要指到页号、给出首个差异点的前后文。只说「有差异」
  等于让用户自己去 40 页里找那一个字。
- 只有 `/Contents` 字节变了、文字没变 → **不许判失败**。内链变蓝必然写内容流，
  把它判红，用户每次处理都会看到假警报，然后就学会忽略这个工具的所有红。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from pikepdf import Dictionary, Name, Stream

from tests.fixtures.make_fixtures import add_page, new_pdf, save

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_content_same.py"


def run_cli(*args: str, timeout: float = 120.0):
    """跑脚本 CLI，把 stdout/stderr **落到文件**再读回来。

    刻意不用 `capture_output=True`：Windows 上管道写端一旦被别的长命子进程继承，
    `communicate()` 就等不到 EOF——被测进程早就退出了，pytest 还挂在那儿等。
    全量跑时前面有 live server、playwright 的残留子进程，这个坑真实发生过：
    一个 0.3 秒跑完的用例把整套测试卡了五分钟，只换来一次 faulthandler 转储。

    写文件没有 EOF 依赖；`timeout` 兜底负责「真的卡住」时快速失败，而不是永久阻塞。
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "stdout.txt"
        err_path = Path(tmp) / "stderr.txt"
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            code = subprocess.run(
                [sys.executable, str(SCRIPT), *args],
                stdout=out,
                stderr=err,
                timeout=timeout,
            ).returncode
        return subprocess.CompletedProcess(
            args=args,
            returncode=code,
            stdout=out_path.read_text("utf-8", errors="replace"),
            stderr=err_path.read_text("utf-8", errors="replace"),
        )



@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("verify_content_same_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先登记进 sys.modules 再 exec：脚本用了 `from __future__ import annotations`
    # + dataclass，dataclasses 解析注解时要回查 `sys.modules[cls.__module__]`，
    # 查不到就抛 `'NoneType' object has no attribute '__dict__'`。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 造样本
# --------------------------------------------------------------------------- #


def _text_ops(text: str) -> bytes:
    return f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET\n".encode("ascii")


def _make_pdf(
    path: Path,
    *,
    pages: int = 1,
    text: str | None = None,
    rotate: int = 0,
    mediabox: tuple[int, int, int, int] | None = None,
) -> Path:
    pdf = new_pdf()
    for index in range(pages):
        body = _text_ops(text if text is not None else f"Page {index + 1}")
        add_page(pdf, body, rotate=rotate, mediabox=mediabox)
    return save(pdf, path)


def _make_pdf_with_image(path: Path, pixel: bytes = b"\x00\x00\x00") -> Path:
    pdf = new_pdf()
    page = add_page(pdf, _text_ops("Figure 1"))
    image = Stream(
        pdf,
        pixel * 4,
        Type=Name("/XObject"),
        Subtype=Name("/Image"),
        Width=2,
        Height=2,
        ColorSpace=Name("/DeviceRGB"),
        BitsPerComponent=8,
    )
    page.Resources.XObject = Dictionary(Im1=image)
    return save(pdf, path)


def _pair(checker, tmp_path, before: Path, after: Path, relative: str = "doc.pdf") -> dict:
    return checker.compare_pair(
        relative, checker.read_pdf(before), checker.read_pdf(after)
    )


# --------------------------------------------------------------------------- #
# 一致
# --------------------------------------------------------------------------- #


def test_identical_files_are_reported_same(checker, tmp_path):
    source = _make_pdf(tmp_path / "a.pdf", pages=3)
    copy = _make_pdf(tmp_path / "b.pdf", pages=3)

    record = _pair(checker, tmp_path, source, copy)

    assert record["verdict"] == checker.SAME
    assert record["label"] == "一致"
    assert record["page_count"] == {"before": 3, "after": 3}
    assert record["text_diff_pages"] == []
    assert record["structure_problems"] == []
    assert record["verdict"] not in checker.FAILING_VERDICTS


# --------------------------------------------------------------------------- #
# 文字
# --------------------------------------------------------------------------- #


def test_text_change_is_detected_with_page_and_context(checker, tmp_path):
    """改一个字也必须抓到，而且要能告诉人改在哪。"""
    source = _make_pdf(tmp_path / "a.pdf", pages=2, text="Dose 10 mg daily")
    changed = _make_pdf(tmp_path / "b.pdf", pages=2, text="Dose 18 mg daily")

    record = _pair(checker, tmp_path, source, changed)

    assert record["verdict"] == checker.TEXT
    assert record["verdict"] in checker.FAILING_VERDICTS
    pages = [item["page"] for item in record["text_diff_pages"]]
    assert pages == [1, 2], "两页都改了，两页都要报，不能只报第一页就收工"
    first = record["text_diff_pages"][0]
    assert first["before"] != first["after"]
    assert "10" in first["before"] and "18" in first["after"]
    # 字数一样但字不同——这正是最该抓住的情况（剂量 10 改成 18），
    # 只比字数的实现会漏掉它。
    assert first["chars"]["before"] == first["chars"]["after"] == len("Dose 10 mg daily")


def test_whitespace_only_change_is_not_a_text_difference(checker, tmp_path):
    """换行与空格不算改内容——不同生成器的换行位置本来就飘。"""
    source = new_pdf()
    add_page(source, b"BT /F1 12 Tf 72 700 Td (Alpha) Tj 0 -20 Td (Beta) Tj ET\n")
    save(source, tmp_path / "a.pdf")

    other = new_pdf()
    add_page(other, b"BT /F1 12 Tf 72 700 Td (Alpha) Tj ET\nBT /F1 12 Tf 72 680 Td (Beta) Tj ET\n")
    save(other, tmp_path / "b.pdf")

    record = _pair(checker, tmp_path, tmp_path / "a.pdf", tmp_path / "b.pdf")

    assert record["verdict"] in (checker.SAME, checker.PAINT_ONLY)
    assert record["text_diff_pages"] == []


# --------------------------------------------------------------------------- #
# 结构
# --------------------------------------------------------------------------- #


def test_page_count_change_is_structure(checker, tmp_path):
    source = _make_pdf(tmp_path / "a.pdf", pages=2)
    other = _make_pdf(tmp_path / "b.pdf", pages=3)

    record = _pair(checker, tmp_path, source, other)

    assert record["verdict"] == checker.STRUCTURE
    assert record["page_count"] == {"before": 2, "after": 3}
    assert any("页数变化" in item for item in record["structure_problems"])


def test_page_size_change_is_structure(checker, tmp_path):
    source = _make_pdf(tmp_path / "a.pdf", mediabox=(0, 0, 595, 842))
    other = _make_pdf(tmp_path / "b.pdf", mediabox=(0, 0, 612, 792))

    record = _pair(checker, tmp_path, source, other)

    assert record["verdict"] == checker.STRUCTURE
    assert any("尺寸变化" in item for item in record["structure_problems"])


def test_rotation_change_is_structure(checker, tmp_path):
    source = _make_pdf(tmp_path / "a.pdf", rotate=0)
    other = _make_pdf(tmp_path / "b.pdf", rotate=90)

    record = _pair(checker, tmp_path, source, other)

    assert record["verdict"] == checker.STRUCTURE
    assert any("旋转角变化" in item for item in record["structure_problems"])


def test_image_bytes_change_is_structure(checker, tmp_path):
    """图被换了，哪怕尺寸没变也要抓到——像素哈希就是为这个。"""
    source = _make_pdf_with_image(tmp_path / "a.pdf", pixel=b"\x00\x00\x00")
    other = _make_pdf_with_image(tmp_path / "b.pdf", pixel=b"\xff\x00\x00")

    record = _pair(checker, tmp_path, source, other)

    assert record["verdict"] == checker.STRUCTURE
    assert any("图片资源变化" in item for item in record["structure_problems"])


def test_font_embedding_change_is_structure(checker, tmp_path):
    """同一段文字、同一个字体名，一个嵌入一个没嵌——渲染结果可能一样，
    但「只改格式」不该动字体，必须报出来。"""
    embedded = new_pdf()
    add_page(embedded, _text_ops("Body"), embed_font=True)
    save(embedded, tmp_path / "a.pdf")

    bare = new_pdf()
    add_page(bare, _text_ops("Body"), embed_font=False)
    save(bare, tmp_path / "b.pdf")

    record = _pair(checker, tmp_path, tmp_path / "a.pdf", tmp_path / "b.pdf")

    assert record["verdict"] == checker.STRUCTURE
    assert any("字体资源变化" in item for item in record["structure_problems"])


# --------------------------------------------------------------------------- #
# 绘制指令
# --------------------------------------------------------------------------- #


def test_content_stream_only_change_is_not_a_failure(checker, tmp_path):
    """内链变蓝就是这个形状：文字一字未改，`/Contents` 多了颜色操作符。

    判成失败的话，用户每次处理都会看到红，然后学会忽略所有红。
    """
    source = new_pdf()
    add_page(source, b"BT /F1 12 Tf 72 700 Td (Same words) Tj ET\n")
    save(source, tmp_path / "a.pdf")

    painted = new_pdf()
    add_page(painted, b"0 0 1 rg BT /F1 12 Tf 72 700 Td (Same words) Tj ET\n")
    save(painted, tmp_path / "b.pdf")

    record = _pair(checker, tmp_path, tmp_path / "a.pdf", tmp_path / "b.pdf")

    assert record["verdict"] == checker.PAINT_ONLY
    assert record["verdict"] not in checker.FAILING_VERDICTS
    assert record["paint_only_pages"] == [1]
    assert record["text_diff_pages"] == []
    assert record["label"] == "一致（绘制指令有变化）"


def test_structure_beats_paint_only(checker, tmp_path):
    """页数变了就别再谈「只是绘制指令」——结构差异优先级更高。"""
    source = _make_pdf(tmp_path / "a.pdf", pages=1)
    other = _make_pdf(tmp_path / "b.pdf", pages=2)

    record = _pair(checker, tmp_path, source, other)

    assert record["verdict"] == checker.STRUCTURE


# --------------------------------------------------------------------------- #
# 配对
# --------------------------------------------------------------------------- #


def test_missing_counterpart_is_a_failure(checker, tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    _make_pdf(before_dir / "only.pdf")
    after_dir.mkdir()

    pairs, extra = checker.collect_pairs(before_dir, after_dir)
    record = checker.compare_pair(
        pairs[0]["relative"],
        checker.read_pdf(pairs[0]["before"]),
        None if pairs[0]["after"] is None else checker.read_pdf(pairs[0]["after"]),
    )

    assert record["verdict"] == checker.MISSING
    assert record["verdict"] in checker.FAILING_VERDICTS
    assert extra == []


def test_extra_files_are_listed_but_do_not_fail(checker, tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    _make_pdf(before_dir / "doc.pdf")
    _make_pdf(after_dir / "doc.pdf")
    _make_pdf(after_dir / "leftover.pdf")

    pairs, extra = checker.collect_pairs(before_dir, after_dir)

    assert len(pairs) == 1
    assert [path.name for path in extra] == ["leftover.pdf"]
    record = checker.compare_pair(
        pairs[0]["relative"], checker.read_pdf(pairs[0]["before"]), checker.read_pdf(pairs[0]["after"])
    )
    assert record["verdict"] == checker.SAME


def test_pairing_by_unique_filename_is_announced(checker, tmp_path):
    """产物被摊平到一个目录时，按文件名配上，但必须在报告里说清楚。"""
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    _make_pdf(before_dir / "sub" / "doc.pdf")
    _make_pdf(after_dir / "doc.pdf")

    pairs, _extra = checker.collect_pairs(before_dir, after_dir)

    assert pairs[0]["after"] is not None
    assert "按文件名配对" in pairs[0]["note"]


def test_ambiguous_same_name_is_not_guessed(checker, tmp_path):
    """同名文件不止一份时宁可报「缺对应文件」。

    随便挑一个来比，会把「拿错了文件」伪装成「一致」——这是最坏的一种假绿灯。
    """
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    _make_pdf(before_dir / "doc.pdf")
    _make_pdf(after_dir / "run1" / "doc.pdf")
    _make_pdf(after_dir / "run2" / "doc.pdf")

    pairs, _extra = checker.collect_pairs(before_dir, after_dir)

    assert pairs[0]["after"] is None
    assert "无法确定" in pairs[0]["note"]


def test_single_file_pairing(checker, tmp_path):
    source = _make_pdf(tmp_path / "a.pdf")
    other = _make_pdf(tmp_path / "b.pdf")

    pairs, extra = checker.collect_pairs(source, other)

    assert len(pairs) == 1
    assert pairs[0]["before"] == source
    assert pairs[0]["after"] == other
    assert extra == []


# --------------------------------------------------------------------------- #
# 读不了
# --------------------------------------------------------------------------- #


def test_unreadable_pdf_is_reported_not_raised(checker, tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a pdf at all")

    facts = checker.read_pdf(broken)

    assert not facts.readable
    assert facts.error
    record = checker.compare_pair("broken.pdf", facts, None)
    assert record["verdict"] == checker.MISSING  # 产物也没配对


def test_unreadable_after_file_is_a_failure(checker, tmp_path):
    source = _make_pdf(tmp_path / "a.pdf")
    broken = tmp_path / "b.pdf"
    broken.write_bytes(b"%PDF-1.7 truncated")

    record = _pair(checker, tmp_path, source, broken)

    assert record["verdict"] == checker.UNREADABLE
    assert record["verdict"] in checker.FAILING_VERDICTS


# --------------------------------------------------------------------------- #
# 报告与退出码
# --------------------------------------------------------------------------- #


def test_summarize_counts_every_verdict(checker):
    records = [
        {"verdict": checker.SAME},
        {"verdict": checker.SAME},
        {"verdict": checker.PAINT_ONLY},
        {"verdict": checker.TEXT},
    ]

    counts = checker.summarize(records)

    assert counts[checker.SAME] == 2
    assert counts[checker.PAINT_ONLY] == 1
    assert counts[checker.TEXT] == 1
    assert counts[checker.STRUCTURE] == 0


def test_report_text_states_the_conclusion(checker, tmp_path):
    source = _make_pdf(tmp_path / "a.pdf")
    copy = _make_pdf(tmp_path / "b.pdf")
    record = _pair(checker, tmp_path, source, copy)

    text = checker.render_report(
        [record], before_root=str(tmp_path), after_root=str(tmp_path)
    )

    assert "全部一致" in text
    assert "绘制指令" in text, "必须在报告里解释「绘制指令有变化」是什么，否则没人看得懂"


def test_report_lists_text_difference_details(checker, tmp_path):
    source = _make_pdf(tmp_path / "a.pdf", text="Total 42")
    changed = _make_pdf(tmp_path / "b.pdf", text="Total 24")
    record = _pair(checker, tmp_path, source, changed)

    text = checker.render_report([record], before_root="x", after_root="y")

    assert "存在差异" in text
    assert "第 1 页文字不同" in text
    assert "原件：" in text and "产物：" in text


def test_cli_returns_zero_when_identical(tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    _make_pdf(before_dir / "doc.pdf")
    _make_pdf(after_dir / "doc.pdf")

    result = run_cli(str(before_dir), str(after_dir))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "全部一致" in result.stdout


def test_cli_returns_one_when_text_differs(tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    _make_pdf(before_dir / "doc.pdf", text="Dose 10 mg")
    _make_pdf(after_dir / "doc.pdf", text="Dose 18 mg")

    result = run_cli(str(before_dir), str(after_dir))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "文字有差异" in result.stdout


def test_cli_returns_two_on_usage_error(tmp_path):
    result = run_cli(str(tmp_path))

    assert result.returncode == 2


def test_cli_writes_json_report(tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    _make_pdf(before_dir / "doc.pdf", pages=2)
    _make_pdf(after_dir / "doc.pdf", pages=2)
    target = tmp_path / "report.json"

    result = run_cli(str(before_dir), str(after_dir), "--json", str(target))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["summary"]["same"] == 1
    assert payload["files"][0]["relative"] == "doc.pdf"


# --------------------------------------------------------------------------- #
# 独立性（这个文件存在的理由）
# --------------------------------------------------------------------------- #


def test_script_does_not_import_the_guard():
    """脚本不许 import `app.pdf.content_fingerprint`，也不许 import 任何 `app.*`。

    用 AST 查导入而不是字符串查找：文档里提到护栏的文件名是正常的（还要解释
    为什么不复用它），真正要禁的是**代码依赖**。哪天有人图省事改成
    `from app.pdf.content_fingerprint import compare`，两条验证路径就合并成一条，
    这个脚本也就失去意义了。
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    offenders = sorted(
        name for name in imported if name == "app" or name.startswith("app.")
    )
    assert not offenders, f"独立复核脚本不得依赖应用代码，发现了：{offenders}"
