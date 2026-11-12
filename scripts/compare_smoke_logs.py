"""比较两份冒烟日志：源码态 vs 打包产物，排除「必然不同」的行后要求逐字一致。

M7-7 的完成标准之一是「与源码态日志 diff=0」。这句话如果只存在于某次对话的
临时命令里，下次回归就没人能复现——`diff` 直接跑出来必然有差异，而差异里混着
环境随机量，看不出是真问题还是噪声。所以把「哪些行允许不同」写成代码：

    python scripts/live_smoke.py --port 8899          > var/smoke-src.log 2>&1
    python scripts/live_smoke.py --exe dist/... --port 8899 > var/smoke-exe.log 2>&1
    python scripts/compare_smoke_logs.py var/smoke-src.log var/smoke-exe.log

退出码：0 = 归一化后完全一致；1 = 有真实差异（会打印 unified diff）；2 = 文件读不了。

## 允许不同的只有四类，且每类都必须是**实测出现过**的

1. `启动方式：…` —— 一个跑 `run.py`，一个跑 `PDFTool.exe`。这行存在的意义就是
   告诉你两条路径真的不同；把它排除掉不代表忽略差异，反而说明差异是预期的。
2. `generation_id`（`gen-<时间戳>-<随机后缀>`）—— 每次启动都会变。
3. `job-<十六进制>` —— 每次上传都会新建。
4. `127.0.0.1:<端口>` —— 两次跑可以用不同端口。

**不收录**「耗时」「文件大小」「绝对路径」这类规则：这次日志里根本没有这些行，
先写进去只会让将来的比对在真出现差异时悄悄放过。规则要跟着实测走，不是跟着
想象走——多一条规则，就多一个藏住真问题的缝隙。
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

#: 允许不同的行 / 片段。顺序无关，互不重叠。
#:
#: `启动方式` 用整行替换而不是只换后半段：冒号后面是中文全角，两种写法都有，
#: 整行匹配最稳。
_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^启动方式：.*$", re.MULTILINE), "启动方式：<MODE>"),
    (re.compile(r"gen-\d{8}T\d+Z-[0-9a-f]+"), "gen-<ID>"),
    (re.compile(r"job-[0-9a-f]{8,}"), "job-<ID>"),
    (re.compile(r"127\.0\.0\.1:\d+"), "127.0.0.1:<PORT>"),
)

#: 从原始日志里读回「启动方式」，只用于打印，不参与比较。
_MODE_LINE = re.compile(r"^启动方式：(.*)$", re.MULTILINE)


def normalize(text: str) -> str:
    """把环境随机量替换成占位符，其余一字不动。

    统一换行放在最前面：Windows 上 `print` 写 CRLF，手工造的日志可能是 LF，
    不统一就会在每一行末尾报差异——那种 diff 全是噪声，读的人只会学会忽略它。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for pattern, replacement in _NORMALIZERS:
        text = pattern.sub(replacement, text)
    return text


def startup_modes(text: str) -> list[str]:
    """日志里记录的启动方式，用于告诉用户「这次比的确实是两种形态」。"""
    return [line.strip() for line in _MODE_LINE.findall(text)]


def unified_diff(source: str, packaged: str, *, source_name: str, packaged_name: str) -> str:
    """归一化后的 unified diff；一致时返回空串。"""
    left = normalize(source).splitlines(keepends=True)
    right = normalize(packaged).splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(left, right, fromfile=source_name, tofile=packaged_name, n=3)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="比较源码态与打包产物的冒烟日志（排除环境随机量）",
    )
    parser.add_argument("source_log", type=Path, help="源码态日志")
    parser.add_argument("packaged_log", type=Path, help="打包产物日志")
    parser.add_argument(
        "--allow-same-mode",
        action="store_true",
        help="允许两份日志的启动方式相同（默认会提示：那样比不出打包差异）",
    )
    args = parser.parse_args(argv)

    try:
        source_text = args.source_log.read_text(encoding="utf-8", errors="replace")
        packaged_text = args.packaged_log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"读不了日志：{exc}", file=sys.stderr)
        return 2

    source_modes = startup_modes(source_text)
    packaged_modes = startup_modes(packaged_text)
    print(f"{args.source_log}  启动方式：{'、'.join(source_modes) or '（未记录）'}")
    print(f"{args.packaged_log}  启动方式：{'、'.join(packaged_modes) or '（未记录）'}")

    # 两份都跑源码态也能「diff=0」，但那证明的是「源码态很稳定」，不是
    # 「打包没改变行为」。这一点必须提示，否则这个脚本会给出假的绿灯。
    if not args.allow_same_mode and source_modes and source_modes == packaged_modes:
        print(
            "提示：两份日志的启动方式相同，比不出「打包前后行为一致」。"
            "要验打包请给其中一份加 --exe。",
            file=sys.stderr,
        )

    diff = unified_diff(
        source_text,
        packaged_text,
        source_name=str(args.source_log),
        packaged_name=str(args.packaged_log),
    )
    if diff:
        print("\n归一化后仍有差异：\n")
        print(diff, end="")
        return 1

    lines = len(normalize(source_text).splitlines())
    print(f"\n归一化后逐字一致（{lines} 行，仅排除启动方式 / generation_id / job id / 端口）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
