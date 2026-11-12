"""`scripts/compare_smoke_logs.py`：比对工具本身必须能报错，而不是永远说「一致」。

这个脚本的用途是给 M7-7 的「diff=0」提供可复现证据。它有两种失败方式，两种都
比没有这个脚本更糟：

1. **规则写太宽**：把「修改=1」里的数字也当成随机量抹掉，于是「源码态改了 1 处、
   产物改了 2 处」被判定为一致——这是**假绿灯**，比红更危险；
2. **规则写太窄**：generation_id、job id 没抹干净，每次跑都红，人就开始忽略它。

所以下面既验「该抹的抹掉了」，也验「不该抹的一个字都不许动」。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "compare_smoke_logs.py"


@pytest.fixture(scope="module")
def comparator():
    spec = importlib.util.spec_from_file_location("compare_smoke_logs_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: 一份最小但形状真实的冒烟日志：含启动方式、generation_id、job id、端口。
_SOURCE_LOG = """\
启动方式：run.py（源码态）
health: {"status": "ok", "generation_id": "gen-20261112T012837009Z-852585"}
上传成功 job=job-39353ea877847218 标签识别=untagged
服务地址 http://127.0.0.1:8899
后置校验：
  toc                      fail
  page_labels              warning
  fast_web_view            adjusted         检查=1 修改=1
✓ 全部检查通过
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# normalize：该抹的抹掉
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("启动方式：run.py（源码态）", "启动方式：<MODE>"),
        ("启动方式：PDFTool.exe", "启动方式：<MODE>"),
        ("gen-20261112T012837009Z-852585", "gen-<ID>"),
        ("job-39353ea877847218", "job-<ID>"),
        ("http://127.0.0.1:8899", "http://127.0.0.1:<PORT>"),
    ],
)
def test_normalize_masks_each_volatile_value(comparator, raw: str, expected: str) -> None:
    """四类随机量逐类验，避免「规则被误删了但整体用例还绿」。

    只跑整份日志对比的话，删掉其中任何一条规则都可能仍然一致——因为那份日志
    里恰好只有一处该值。逐类点名才能钉住每一条规则都在生效。
    """
    assert comparator.normalize(raw) == expected


def test_normalize_does_not_touch_real_numbers(comparator) -> None:
    """计数、页号、结论一个字都不能动——这是防「假绿灯」的核心一条。

    把 `检查=1 修改=1` 归一化掉，就等于宣称「工具改了几处」不重要。可它恰恰是
    用户唯一能核对的东西。
    """
    line = "fast_web_view            adjusted         检查=1 修改=1 已符合=0 豁免=0 失败=0"
    assert comparator.normalize(line) == line


def test_normalize_keeps_pass_and_fail_apart(comparator) -> None:
    """`pass` 与 `fail` 归一化后仍必须不同，否则整份比对失去意义。"""
    assert comparator.normalize("  toc  pass") != comparator.normalize("  toc  fail")


def test_normalize_unifies_line_endings(comparator) -> None:
    """CRLF 与 LF 视为同一份内容。

    源码态日志由 `print` 写 CRLF、手工造的是 LF。不统一的话每一行都报差异，
    满屏 diff 全是噪声，读的人只会学会忽略这个脚本的输出。
    """
    assert comparator.normalize("a\r\nb\r\n") == comparator.normalize("a\nb\n")


def test_normalize_is_idempotent(comparator) -> None:
    """归一化两次等于一次——否则占位符自己会被下一轮规则再改一遍。"""
    once = comparator.normalize(_SOURCE_LOG)
    assert comparator.normalize(once) == once


# --------------------------------------------------------------------------- #
# unified_diff / main：一致就静默，不一致要指名道姓
# --------------------------------------------------------------------------- #


def test_identical_after_normalization_returns_no_diff(comparator) -> None:
    """除随机量外完全相同的两份日志 → 无差异（这就是 M7-7 要的 diff=0）。"""
    packaged = _SOURCE_LOG.replace("run.py（源码态）", "PDFTool.exe").replace(
        "gen-20261112T012837009Z-852585", "gen-20261112T013033126Z-8fe885"
    ).replace("job-39353ea877847218", "job-cf29217617a9f8b4")

    diff = comparator.unified_diff(
        _SOURCE_LOG, packaged, source_name="src", packaged_name="exe"
    )
    assert diff == ""


def test_a_single_changed_status_is_reported(comparator) -> None:
    """产物少改了一项（`adjusted` → `unchanged`）必须被报出来。

    这是打包后最容易出现的真实退化：某个处理项在 exe 里静默失效。
    """
    packaged = _SOURCE_LOG.replace("adjusted         检查=1 修改=1", "unchanged         检查=1 修改=0")

    diff = comparator.unified_diff(
        _SOURCE_LOG, packaged, source_name="src", packaged_name="exe"
    )
    assert diff, "状态变了却判为一致"
    assert "adjusted" in diff and "unchanged" in diff


def test_main_exits_zero_when_only_volatile_lines_differ(comparator, tmp_path, capsys) -> None:
    source = _write(tmp_path / "src.log", _SOURCE_LOG)
    packaged = _write(
        tmp_path / "exe.log",
        _SOURCE_LOG.replace("启动方式：run.py（源码态）", "启动方式：PDFTool.exe")
        .replace("job-39353ea877847218", "job-0123456789abcdef")
        .replace("127.0.0.1:8899", "127.0.0.1:8901"),
    )

    assert comparator.main([str(source), str(packaged)]) == 0
    out = capsys.readouterr().out
    assert "逐字一致" in out
    # 两种启动方式都要打出来：用户得看见「这次比的确实是两种形态」
    assert "run.py" in out and "PDFTool.exe" in out


def test_main_exits_one_and_prints_diff_on_real_difference(comparator, tmp_path, capsys) -> None:
    source = _write(tmp_path / "src.log", _SOURCE_LOG)
    packaged = _write(tmp_path / "exe.log", _SOURCE_LOG.replace("✓ 全部检查通过", "✗ 2 项检查未通过"))

    assert comparator.main([str(source), str(packaged)]) == 1
    out = capsys.readouterr().out
    assert "归一化后仍有差异" in out
    assert "全部检查通过" in out


def test_main_exits_two_when_a_log_is_missing(comparator, tmp_path, capsys) -> None:
    """读不了文件必须和「有差异」区分开：退出码 2 而不是 1。

    混在一起的话，脚本坏了（路径写错）会被读成「打包退化」，排查方向直接跑偏。
    """
    source = _write(tmp_path / "src.log", _SOURCE_LOG)
    assert comparator.main([str(source), str(tmp_path / "nope.log")]) == 2
    assert "读不了日志" in capsys.readouterr().err


def test_main_warns_when_both_logs_are_the_same_mode(comparator, tmp_path, capsys) -> None:
    """两份都跑源码态时提示「比不出打包差异」。

    这种情形下 diff=0 证明的是「源码态稳定」，不是「打包没改行为」。不提示的话，
    这个脚本会给人一个方向错误的绿灯。
    """
    source = _write(tmp_path / "a.log", _SOURCE_LOG)
    same = _write(tmp_path / "b.log", _SOURCE_LOG)

    assert comparator.main([str(source), str(same)]) == 0
    assert "比不出" in capsys.readouterr().err

    # 显式允许后就不再啰嗦（跑两次源码态做稳定性回归时用）
    assert comparator.main([str(source), str(same), "--allow-same-mode"]) == 0
    assert "比不出" not in capsys.readouterr().err
