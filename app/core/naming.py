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


def _ascii_fallback(filename: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    )
    ascii_name = "".join(
        ch if (ch.isalnum() or ch in "._- ") else "_" for ch in ascii_name
    ).strip()
    if not ascii_name.lower().endswith(".pdf"):
        ascii_name = f"{Path(ascii_name or 'document').stem}.pdf"
    return ascii_name or DEFAULT_PDF_NAME


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    """构造 Content-Disposition，同时给 ASCII 回退与 RFC 5987 编码名。"""
    if disposition not in {"inline", "attachment"}:
        disposition = "attachment"
    safe = sanitize_filename(filename)
    # 下载文件名不允许携带目录
    safe = safe.replace("/", "_").replace("\\", "_")
    fallback = _ascii_fallback(safe)
    encoded = quote(safe, safe="")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
