"""处理器共享上下文。

每个处理器拿到的都是一份「已经打开、可写」的 `pikepdf.Pdf`，
加上从这份文件里现场算出来的页码映射与命名目标表。
处理器之间不共享可变状态，顺序由流水线决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import pikepdf

from app.pdf.destinations import PageResolver, collect_named_dests

__all__ = ["OpContext"]


@dataclass(frozen=True)
class OpContext:
    page_count: int
    exempt_pages: frozenset[int]
    resolver: PageResolver
    named: dict[str, Any]

    @classmethod
    def build(
        cls, pdf: pikepdf.Pdf, exempt_pages: Iterable[int] = ()
    ) -> "OpContext":
        return cls(
            page_count=len(pdf.pages),
            exempt_pages=frozenset(int(page) for page in exempt_pages),
            resolver=PageResolver(pdf),
            named=collect_named_dests(pdf),
        )

    def is_exempt(self, page_number: int | None) -> bool:
        return page_number is not None and page_number in self.exempt_pages
