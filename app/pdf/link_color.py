"""R35 内链颜色修正——流水线入口（薄层）。

v4.0 的 R25 只把 `Link` 注释的 `/C` 设成蓝色，而阅读器默认不显示链接边框，
所以用户看不到任何变化（原话：「处理项内链颜色并未修正」）。v4.1 的 R35 换了
做法：**真的去改链接覆盖范围内文字的填充色**，`/C` 只是顺带同步。

真正的实现全在 `app/pdf/content_color.py`，这里只保留两件事：

1. `process` / `describe` 的转发，让 `pipeline.py` 的注册表不用改；
2. `TARGET_COLOR` 与 `VISIBILITY_NOTE` 两个旧名字，避免外部引用断掉。
   `VISIBILITY_NOTE` 的旧文案（「本项只改链接对象颜色，不影响正文文字」）
   在 R35 下已经是错的，所以它现在指向 `content_color.TEXT_NOTE`。

**为什么不在这个文件里留着旧实现**：两套逻辑同时存在时，「到底哪套在跑」会变成
一个需要读代码才能回答的问题。测试也容易只覆盖到没在用的那套。留一个转发层，
行为只有一处定义。
"""

from __future__ import annotations

from app.pdf.content_color import (
    TEXT_NOTE,
    TARGET_RGB,
    broken_links,
    describe,
    process,
)

__all__ = [
    "TARGET_COLOR",
    "VISIBILITY_NOTE",
    "broken_links",
    "describe",
    "process",
]

#: 目标颜色：RGB 纯蓝。旧名，等同于 `content_color.TARGET_RGB`。
TARGET_COLOR: tuple[float, float, float] = TARGET_RGB

#: 结果页必须附带的说明。旧名，R35 起指向 `content_color.TEXT_NOTE`。
VISIBILITY_NOTE = TEXT_NOTE
