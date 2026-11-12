"""报告渲染的公共骨架：转义、样式、文档外壳、免责声明。

两个渲染器（校验报告 / 处理日志）共用这一份，理由不是省代码：

- R23 与 R28 都要求「内联 CSS、无外部依赖、双击可离线打开」。各自抄一份
  CSS，迟早有一份漏掉某条规则，导出的两份文件在断网机器上长得不一样。
- AC-R23-E2 要求报告里不出现结论性表述，AC-R23-N4 要求断网可读。这两件事
  必须由同一个出口保证，否则"哪个渲染器忘了"就成了靠人记的事。

**所有插入 HTML 的值都必须经过 `text()`**。文件名、字体名、URI 都可能含
`<`、`&`、`"`（AC-R23-E1 的用例就是一个叫 `<script>alert(1)</script>` 的文件），
所以这里不提供任何"原样输出"的旁路——要拼结构就拼常量字符串，要拼数据就走 `text()`。
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Iterable, Sequence

__all__ = [
    "REPORT_DISCLAIMER",
    "escape",
    "format_bytes",
    "format_duration",
    "parse_iso",
    "render_document",
    "text",
    "value_text",
]

# R23 规定的页脚原文，一个字都不能改。
REPORT_DISCLAIMER = "本报告仅反映工具可自动检测的项目，不代表文件完全合规。"


def escape(value: Any) -> str:
    """HTML 转义。`quote=True` 连引号一起转，属性值里也安全。"""
    return html.escape(str(value), quote=True)


# 语义别名：调用处写 `text(...)` 比 `escape(...)` 更贴近「这是一段要显示的文本」。
text = escape


def value_text(value: Any) -> str:
    """把任意值渲染成一行可读文本（已转义）。

    处理结果里的 `before` / `after` 可能是字符串（"12 个链接"）、数字、
    布尔、列表或字典。报告不能因为某个处理器多返回了一个字段就崩，
    也不能把 Python 的 `repr` 原样倒给用户看（那是给开发者看的）。
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return escape(value)
    if isinstance(value, str):
        return escape(value) if value else "—"
    if isinstance(value, dict):
        if not value:
            return "—"
        parts = [f"{key}={_scalar(item)}" for key, item in value.items()]
        return escape("，".join(parts))
    if isinstance(value, (list, tuple, set)):
        if not value:
            return "—"
        return escape("，".join(_scalar(item) for item in value))
    return escape(value)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "—"
    if isinstance(value, (dict, list, tuple, set)):
        return "…"
    return str(value)


def format_bytes(size: Any) -> str:
    """字节数。小数字保留一位，大文件给整数——报告里没人关心 .3 个 KB。"""
    try:
        number = int(size)
    except (TypeError, ValueError):
        return "—"
    if number < 0:
        return "—"
    if number < 1024:
        return f"{number} 字节"
    for unit in ("KB", "MB", "GB"):
        number /= 1024.0
        if number < 1024 or unit == "GB":
            return f"{number:.1f} {unit}" if number < 100 else f"{number:.0f} {unit}"
    return f"{number:.1f} GB"  # pragma: no cover - 上面的分支已覆盖


def parse_iso(value: Any) -> datetime | None:
    """解析 `utcnow_iso()` 那种带 `Z` 的字符串；解析不了就返回 None。

    不做「猜一个时间」的兜底：耗时算错比不显示耗时更糟。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def format_duration(start: Any, end: Any) -> str:
    """两段时间戳之间的耗时。任一端缺失或非法就给破折号。"""
    first, second = parse_iso(start), parse_iso(end)
    if first is None or second is None:
        return "—"
    seconds = (second - first).total_seconds()
    if seconds < 0:
        return "—"
    if seconds < 1:
        return f"{int(seconds * 1000)} 毫秒"
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


# --------------------------------------------------------------------------- #
# 样式与外壳
# --------------------------------------------------------------------------- #

# 全部内联，没有任何 `@import` / `<link>` / 网络字体（AC-R23-N4）。
# 配色刻意做成"黑白灰 + 三色状态"，因为报告会被打印、会进附件，
# 彩色大面积背景在灰度打印下会糊成一片。
_CSS = """
*, *::before, *::after { box-sizing: border-box; }
html { color-scheme: light; }
body {
  margin: 0;
  padding: 32px 24px 56px;
  background: #ffffff;
  color: #1f2328;
  font-family: "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
    "Source Han Sans SC", "Segoe UI", Arial, sans-serif;
  font-size: 14px;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}
.doc { max-width: 1040px; margin: 0 auto; }
h1 { margin: 0 0 4px; font-size: 20px; line-height: 1.3; }
h2 { margin: 28px 0 12px; font-size: 15px; }
h3 { margin: 20px 0 8px; font-size: 14px; color: #374151; }
p.sub { margin: 0 0 20px; color: #6b7280; font-size: 12px; }
dl.meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 8px 24px;
  margin: 0 0 24px;
  padding: 14px 16px;
  background: #f7f8fa;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
}
dl.meta div { display: flex; gap: 8px; min-width: 0; }
dl.meta dt { color: #6b7280; white-space: nowrap; }
dl.meta dd { margin: 0; font-weight: 600; word-break: break-all; }
.tiles { display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 8px; }
.tile {
  flex: 1 1 130px;
  padding: 10px 14px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
}
.tile b { display: block; font-size: 20px; line-height: 1.4; }
.tile span { color: #6b7280; font-size: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 7px 10px; text-align: left; vertical-align: top; border-bottom: 1px solid #eceef1; }
th { background: #f7f8fa; color: #374151; font-weight: 600; white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
td.num { white-space: nowrap; font-variant-numeric: tabular-nums; }
details.file {
  margin: 0 0 12px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  overflow: hidden;
}
details.file > summary {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  align-items: baseline;
  padding: 10px 14px;
  background: #f7f8fa;
  cursor: pointer;
}
details.file > summary::marker { content: ""; }
details.file > summary::before { content: "\\25B8"; color: #9ca3af; }
details.file[open] > summary::before { content: "\\25BE"; }
details.file[open] > summary { border-bottom: 1px solid #e5e7eb; }
.fname { font-weight: 600; word-break: break-all; }
.dim { color: #6b7280; font-size: 12px; }
.body-pad { padding: 12px 14px; }
.tag {
  display: inline-block;
  padding: 0 6px;
  border-radius: 3px;
  font-size: 12px;
  font-weight: 600;
  border: 1px solid transparent;
  white-space: nowrap;
}
.tag.pass { color: #0f5132; background: #d1e7dd; }
.tag.warning { color: #664d03; background: #fff3cd; }
.tag.fail { color: #842029; background: #f8d7da; }
.tag.skipped, .tag.plain { color: #41464b; background: #e2e3e5; }
.tag.adjusted { color: #0f5132; background: #d1e7dd; }
.tag.unchanged { color: #41464b; background: #e2e3e5; }
.tag.not_applicable { color: #41464b; background: #e2e3e5; }
.tag.review_required { color: #664d03; background: #fff3cd; }
.tag.failed { color: #842029; background: #f8d7da; }
ul.findings { margin: 4px 0 0; padding-left: 18px; color: #4b5563; font-size: 12px; }
ul.findings li { margin: 2px 0; }
ul.findings li .lvl { font-weight: 600; }
.empty { padding: 14px; color: #6b7280; border: 1px dashed #d1d5db; border-radius: 6px; }
footer.doc-foot {
  margin-top: 32px;
  padding-top: 14px;
  border-top: 1px solid #e5e7eb;
  color: #6b7280;
  font-size: 12px;
}
footer.doc-foot p { margin: 4px 0; }
footer.doc-foot .disclaimer { color: #4b5563; }
@media print {
  body { padding: 0; font-size: 12px; }
  details.file { break-inside: avoid; }
  details.file > summary::before { content: ""; }
  .tile { break-inside: avoid; }
}
"""


def render_document(
    *,
    title: str,
    heading: str,
    subtitle: str,
    meta: Sequence[tuple[str, Any]],
    body: str,
    footer: Iterable[Any],
) -> str:
    """拼出完整的 HTML 单文件。

    `body` 是**已经拼好的、已转义的**片段——它由本模块的调用方用 `text()`
    构造，不接受外部原样输入。其余参数一律在这里转义。
    """
    meta_html = "".join(
        f"<div><dt>{text(label)}</dt><dd>{value_text(value)}</dd></div>"
        for label, value in meta
    )
    footer_lines = "".join(f"<p>{value_text(line)}</p>" for line in footer)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        f"<title>{text(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="doc">\n'
        "<header>\n"
        f"<h1>{text(heading)}</h1>\n"
        f'<p class="sub">{text(subtitle)}</p>\n'
        f'<dl class="meta">{meta_html}</dl>\n'
        "</header>\n"
        "<main>\n"
        f"{body}\n"
        "</main>\n"
        "<footer class=\"doc-foot\">\n"
        f"{footer_lines}\n"
        "</footer>\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )
