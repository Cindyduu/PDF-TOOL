"""报告渲染（R23、R28）。

两个渲染器都是**纯函数**：入参是已经算好的数据，出参是一个完整的 HTML 字符串。
没有文件 IO，没有数据库，没有全局状态。端点每次请求现渲染，报告不落盘
（R23 的最后一条：避免磁盘堆积）。

模块分工：

    base.py              转义、样式、文档外壳、免责声明
    verify_html.py       校验报告（批次级 + 单文件）
    process_log_html.py  处理日志

需要「生成报告文件」的地方（`worker` 里勾了「生成处理日志」时）自己调
渲染函数再写盘，本包不替它决定路径与编码。
"""

from __future__ import annotations

from app.report.base import REPORT_DISCLAIMER, escape
from app.report.process_log_html import render_process_log
from app.report.verify_html import (
    PHASE_TITLES,
    render_batch_report,
    render_file_report,
)

__all__ = [
    "PHASE_TITLES",
    "REPORT_DISCLAIMER",
    "escape",
    "render_batch_report",
    "render_file_report",
    "render_process_log",
]
