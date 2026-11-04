"""豁免页规格解析：`1,3,8-12`。

规则（与 PRD R2/R1/R9 的豁免语义配套）：
- 页码从 1 开始，`0`、负数、反向区间、空片段、非数字都拒绝；
- 允许中英文逗号与顿号分隔，允许片段两侧空白（` 1 , 3 ` 合法）；
- 结果去重并按页码升序规范化，相邻页码合并成区间（`8,9,10` -> `8-10`）；
- 提供页数时校验越界，报错要说明最大页数，而不是笼统一句"页码不合法"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.errors import AppError, ErrorCode

_SEPARATORS = re.compile(r"[,，、]")
_DIGITS = re.compile(r"^\d+$")

NEXT_ACTION = "请按“1,3,8-12”的格式填写，页码从 1 开始且不超过总页数"


@dataclass(frozen=True)
class PageSpec:
    pages: tuple[int, ...]
    normalized: str

    def __bool__(self) -> bool:
        return bool(self.pages)

    def __contains__(self, page: int) -> bool:
        return page in self.pages

    def as_list(self) -> list[int]:
        return list(self.pages)


def _invalid(raw: str, token: str, message: str, **details) -> AppError:
    payload = {"raw": raw, "token": token}
    payload.update(details)
    return AppError(
        ErrorCode.INVALID_PAGES,
        message,
        details=payload,
        next_action=NEXT_ACTION,
        http_status=400,
    )


def _parse_token(raw: str, token: str, page_count: int | None) -> range:
    if "-" in token:
        start_text, _, end_text = token.partition("-")
        start_text, end_text = start_text.strip(), end_text.strip()
        if not start_text or not end_text:
            raise _invalid(raw, token, f"页码区间不完整：{token}")
        if not _DIGITS.match(start_text) or not _DIGITS.match(end_text):
            raise _invalid(raw, token, f"页码区间必须是数字：{token}")
        start, end = int(start_text), int(end_text)
        if start == 0 or end == 0:
            raise _invalid(raw, token, "页码从 1 开始，不能是 0")
        if start > end:
            raise _invalid(raw, token, f"页码区间起点大于终点：{token}")
    else:
        if not _DIGITS.match(token):
            raise _invalid(raw, token, f"无法识别的页码：{token}")
        start = end = int(token)
        if start == 0:
            raise _invalid(raw, token, "页码从 1 开始，不能是 0")

    if page_count is not None and end > page_count:
        raise _invalid(
            raw,
            token,
            f"页码超出范围：文档共 {page_count} 页",
            page_count=page_count,
        )
    return range(start, end + 1)


def _normalize(pages: list[int]) -> str:
    if not pages:
        return ""
    chunks: list[str] = []
    start = prev = pages[0]
    for page in pages[1:]:
        if page == prev + 1:
            prev = page
            continue
        chunks.append(f"{start}-{prev}" if prev > start else str(start))
        start = prev = page
    chunks.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(chunks)


def parse_page_spec(raw: str | None, page_count: int | None = None) -> PageSpec:
    text = (raw or "").strip()
    if not text:
        return PageSpec(pages=(), normalized="")

    pages: set[int] = set()
    for piece in _SEPARATORS.split(text):
        token = piece.strip()
        if not token:
            raise _invalid(raw or text, token, "存在空白页码片段，请检查逗号")
        pages.update(_parse_token(raw or text, token, page_count))

    ordered = sorted(pages)
    return PageSpec(pages=tuple(ordered), normalized=_normalize(ordered))
