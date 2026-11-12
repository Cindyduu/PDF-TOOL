"""文件名安全化与下载名构造。

规则：只取 basename、去掉目录与控制字符、替换 Windows 非法字符、
限制长度、Unicode 原样保留（不做拼音化）。
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

INVALID_CHARS = '<>:"/\\|?*'
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_FILENAME_CHARS = 150
DEFAULT_PDF_NAME = "document.pdf"


def sanitize_filename(name: str | None, fallback: str = DEFAULT_PDF_NAME) -> str:
    """把任意用户输入变成安全的单段文件名。"""
    raw = unicodedata.normalize("NFC", (name or ""))
    raw = raw.replace("\\", "/")
    raw = raw.split("/")[-1]  # 丢弃目录部分
    raw = raw.split(":")[-1] if raw.count(":") == 1 and len(raw.split(":")[0]) == 1 else raw
    raw = "".join(ch for ch in raw if ord(ch) >= 32 and ord(ch) != 127)
    for char in INVALID_CHARS:
        raw = raw.replace(char, "_")
    raw = raw.strip().strip(".").strip()
    if not raw:
        return fallback

    stem, dot, ext = raw.rpartition(".")
    if not dot:
        stem, ext = raw, ""
    # Windows 不允许主名以空格或点结尾（"report .pdf" 实际会存成 "report.pdf"）
    stem = stem.rstrip(" .")
    ext = ext.strip(" .")
    if not stem:
        return fallback
    if stem.upper() in RESERVED_NAMES:
        stem = f"_{stem}"
    if len(stem) > MAX_FILENAME_CHARS:
        stem = stem[:MAX_FILENAME_CHARS]
    result = f"{stem}.{ext}" if ext else stem
    return result or fallback


def ensure_pdf_suffix(name: str) -> str:
    cleaned = sanitize_filename(name)
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf"
    return cleaned


def preferred_download_name(requested: str | None, default: str) -> str:
    """用户自报的下载名优先，但必须能清洗出一个像样的主名，否则用默认名。

    为什么要单独一个函数而不是直接用 sanitize_filename：后者总会兜底成
    document.pdf，而下载端点需要区分「用户没给名字」和「用户给的东西没法
    用」——这两种情况都该落到任务自己的默认名上，而不是凭空冒出一个
    document.pdf，让用户以为文件叫这个名字。
    """
    if not requested:
        return default
    # 先看剥掉路径、非法字符和控制字符之后还剩不剩字母数字。
    # 只剩分隔符或空白（比如 "///"、"  "）就说明用户没给出可用的主名。
    probe = requested.replace("\\", "/").split("/")[-1]
    if not any(ch.isalnum() for ch in probe):
        return default
    return ensure_pdf_suffix(requested)


def short_id(value: str, length: int = 6) -> str:
    tail = "".join(ch for ch in value if ch.isalnum())[-length:]
    return tail or "000000"


def timestamp_slug(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")


def build_output_name(
    original_name: str,
    job_id: str,
    moment: datetime | None = None,
) -> str:
    """输出文件名：安全主名 + 已处理标记 + 时间 + 任务短 ID。"""
    safe = ensure_pdf_suffix(original_name)
    stem = Path(safe).stem
    if len(stem) > 90:
        stem = stem[:90]
    return f"{stem}_已处理_{timestamp_slug(moment)}_{short_id(job_id)}.pdf"


def _ascii_fallback(filename: str, suffix: str = ".pdf") -> str:
    """给纯中文（或非 ASCII）文件名造一个能放进 `filename=` 的退路。

    `suffix` 必须跟着真实类型走：HTML 报告如果退回成 `document.pdf`，
    Windows 会拿 PDF 阅读器去打开一个 HTML 文件，用户看到的是空白页，
    还以为是导出坏了。
    """
    ascii_name = (
        unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    )
    ascii_name = "".join(
        ch if (ch.isalnum() or ch in "._- ") else "_" for ch in ascii_name
    ).strip()
    if not ascii_name.lower().endswith(suffix):
        ascii_name = f"{Path(ascii_name or 'document').stem}{suffix}"
    return ascii_name or f"document{suffix}"


def _content_disposition(filename: str, suffix: str, disposition: str) -> str:
    if disposition not in {"inline", "attachment"}:
        disposition = "attachment"
    safe = sanitize_filename(filename, fallback=f"document{suffix}")
    # 下载文件名不允许携带目录
    safe = safe.replace("/", "_").replace("\\", "_")
    fallback = _ascii_fallback(safe, suffix)
    encoded = quote(safe, safe="")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    """构造 Content-Disposition，同时给 ASCII 回退与 RFC 5987 编码名。"""
    return _content_disposition(filename, ".pdf", disposition)


def html_content_disposition(filename: str, disposition: str = "attachment") -> str:
    """HTML 报告用的 Content-Disposition：回退名保留 `.html`，不强行改成 `.pdf`。"""
    return _content_disposition(filename, ".html", disposition)
