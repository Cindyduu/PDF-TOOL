"""独立复核：处理前后的 PDF，内容到底有没有变。

**为什么还要这个脚本**

工具内部已经有 R36 内容一致性护栏（`app/pdf/content_fingerprint.py`），每次处理都会
自动跑，不一致就不落盘。但它有三个天生的局限：

1. **同一套代码**——护栏和处理器出自同一个仓库、同一批假设。判据写错，两边一起错。
2. **同一个进程**——护栏跑在 `run_pipeline` 里，处理器的内存状态都在它手上。
3. **只给结论，不给旁证**——结果页说「一致」，要申报的人没法据此复核；他需要的是
   能自己重跑一遍的东西。

所以这个脚本**故意不 import 护栏**：它自己用 PyMuPDF 取文字与页框、用 pikepdf 取
图片与字体，重新提取一遍再比。两条独立实现都指向「一致」，才叫证据。测试里有一条
`test_script_does_not_import_the_guard` 专门钉住这条独立性——哪天有人图省事改成
`from app.pdf.content_fingerprint import ...`，测试会红。

**比什么、怎么判**

| 类别 | 比什么 | 不同时 |
| --- | --- | --- |
| 结构 | 页数、每页 MediaBox 宽高、`/Rotate` | 失败 |
| 图片 | 每页 `/Subtype /Image` 的宽高 + 解码字节哈希 | 失败 |
| 字体 | 每页 `/Font` 的（资源名、BaseFont、是否嵌入） | 失败 |
| 文字 | 每页 `get_text("text")` 空白归一化后逐字比 | 失败 |
| 页面绘制指令 | 每页 `/Contents` 解码字节哈希 | **只报，不判失败** |

最后一行是有意的。`link_color` 把链接文字改成蓝色，必然写 `/Contents`，可文字一个字
都没变。这个脚本不知道用户勾了哪些处理项，所以它不能把「绘制指令变了」判成失败——
但它必须**报出来**，让人自己判断。反过来，文字/页数/尺寸/图片/字体这五项一旦变了，
任何「只改格式」的处理都不该造成，直接失败。

**只读**

不写、不改、不移动任何文件。要出机器可读的结果就 `--json 路径`，那是新建一份报告，
不碰被比的 PDF。

用法：

    python scripts/verify_content_same.py 原件目录 产物目录
    python scripts/verify_content_same.py --before 原件.pdf --after 产物.pdf
    python scripts/verify_content_same.py 原件目录 产物目录 --json 复核报告.json

退出码：0 = 全部一致（含「仅绘制指令变化」）；1 = 有文字/结构差异或缺对应文件；
2 = 用法错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Stream

__all__ = [
    "FAILING_VERDICTS",
    "LABELS",
    "FileFacts",
    "PageFacts",
    "compare_pair",
    "collect_pairs",
    "main",
    "read_pdf",
    "render_report",
    "summarize",
]

# --------------------------------------------------------------------------- #
# 判定档位
# --------------------------------------------------------------------------- #

#: 内容与版式都没变。
SAME = "same"
#: 文字没变，只有页面绘制指令（`/Contents` 字节）变了。内链变蓝属于这一类。
PAINT_ONLY = "paint_only"
#: 文字层有差异——这是真正的「内容被改了」。
TEXT = "text"
#: 页数 / 尺寸 / 旋转 / 图片 / 字体有差异。
STRUCTURE = "structure"
#: 原件有、产物目录里找不到对应的文件。
MISSING = "missing"
#: 文件打不开（加密、损坏、不是 PDF）。
UNREADABLE = "unreadable"

LABELS: dict[str, str] = {
    SAME: "一致",
    PAINT_ONLY: "一致（绘制指令有变化）",
    TEXT: "文字有差异",
    STRUCTURE: "结构有差异",
    MISSING: "缺对应文件",
    UNREADABLE: "读不了",
}

#: 会让退出码变成 1 的档位。`PAINT_ONLY` 不在里面——它是预期内的变化。
FAILING_VERDICTS = frozenset({TEXT, STRUCTURE, MISSING, UNREADABLE})

#: 文本差异明细里，首个差异点前后各展示多少字符。
_CONTEXT_CHARS = 24


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PageFacts:
    number: int
    width: float
    height: float
    rotate: int
    text: str
    content_hash: str
    images: tuple[str, ...]
    fonts: tuple[str, ...]


@dataclass(frozen=True)
class FileFacts:
    path: Path
    page_count: int = 0
    pages: tuple[PageFacts, ...] = ()
    error: str | None = None

    @property
    def readable(self) -> bool:
        return self.error is None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize(text: str) -> str:
    """抹掉空白差异后再比。

    换行、连续空格、行尾空白在不同 PDF 生成器与解析器版本之间不稳定，但
    「有没有这几个字」是稳定的。要抓的是文字被改，不是排版细节。
    """
    return " ".join(text.split())


def _content_bytes(page: Any) -> bytes:
    contents = page.obj.get("/Contents")
    if contents is None:
        return b""
    if isinstance(contents, Stream):
        try:
            return contents.read_bytes()
        except Exception:  # noqa: BLE001 - 坏流按空算，差异会被报出来
            return b""
    if isinstance(contents, Array):
        chunks: list[bytes] = []
        for item in contents:
            if isinstance(item, Stream):
                try:
                    chunks.append(item.read_bytes())
                except Exception:  # noqa: BLE001
                    continue
        return b"\n".join(chunks)
    return b""


def _page_images(page: Any) -> tuple[str, ...]:
    """图片摘要：宽高 + 解码字节哈希。**不含资源名**。

    展平注释会给页面加 `/FlatAnnot1` 这类表单 XObject，名字一变摘要就变，
    那不是图片变了。宽高 + 像素字节足以抓住「图被换掉」。
    """
    try:
        resources = page.resources
    except Exception:  # noqa: BLE001
        return ()
    if not isinstance(resources, Dictionary):
        return ()
    xobjects = resources.get("/XObject")
    if not isinstance(xobjects, Dictionary):
        return ()

    entries: list[str] = []
    for _name, obj in xobjects.items():
        if not isinstance(obj, Stream):
            continue
        if str(obj.get("/Subtype") or "") != "/Image":
            continue
        try:
            width = int(obj.get("/Width") or 0)
            height = int(obj.get("/Height") or 0)
            payload = _sha256(obj.read_bytes())
        except Exception:  # noqa: BLE001
            continue
        entries.append(f"{width}x{height}:{payload}")
    return tuple(sorted(entries))


_EMBED_KEYS = ("/FontFile", "/FontFile2", "/FontFile3")


def _page_fonts(page: Any) -> tuple[str, ...]:
    """字体摘要：资源名 + BaseFont + 是否嵌入。

    资源名参与比对是有意的——字体资源被换名会影响渲染，那不是「只改格式」。
    """
    try:
        resources = page.resources
    except Exception:  # noqa: BLE001
        return ()
    if not isinstance(resources, Dictionary):
        return ()
    fonts = resources.get("/Font")
    if not isinstance(fonts, Dictionary):
        return ()

    entries: list[str] = []
    for name, font in fonts.items():
        if not isinstance(font, Dictionary):
            continue
        base = str(font.get("/BaseFont") or "?")
        descriptor = font.get("/FontDescriptor")
        embedded = isinstance(descriptor, Dictionary) and any(
            key in descriptor for key in _EMBED_KEYS
        )
        entries.append(f"{name}|{base}|{int(embedded)}")
    return tuple(sorted(entries))


def read_pdf(path: Path | str) -> FileFacts:
    """把一个文件读成可比对的事实。任何异常收进 `error`，不往外抛。"""
    target = Path(path)

    # 文字与页框走 PyMuPDF：`mediabox` 是未旋转的原始 MediaBox，`rotation` 单独给，
    # 两个值分开比，旋转被改也能定位到具体页。
    texts: list[str] = []
    boxes: list[tuple[float, float, int]] = []
    try:
        with pymupdf.open(str(target)) as doc:
            for page in doc:
                texts.append(page.get_text("text"))
                rect = page.mediabox
                boxes.append(
                    (
                        round(float(rect.width), 3),
                        round(float(rect.height), 3),
                        int(page.rotation) % 360,
                    )
                )
    except Exception as exc:  # noqa: BLE001 - 读不了就如实记下
        return FileFacts(path=target, error=f"{type(exc).__name__}: {exc}")

    # 内容流、图片、字体走 pikepdf：这三样要的是对象结构，不是渲染结果。
    try:
        with pikepdf.open(target) as pdf:
            pages: list[PageFacts] = []
            for index, page in enumerate(pdf.pages):
                width, height, rotate = boxes[index] if index < len(boxes) else (0.0, 0.0, 0)
                text = texts[index] if index < len(texts) else ""
                pages.append(
                    PageFacts(
                        number=index + 1,
                        width=width,
                        height=height,
                        rotate=rotate,
                        text=text,
                        content_hash=_sha256(_content_bytes(page)),
                        images=_page_images(page),
                        fonts=_page_fonts(page),
                    )
                )
    except Exception as exc:  # noqa: BLE001
        return FileFacts(path=target, error=f"{type(exc).__name__}: {exc}")

    return FileFacts(path=target, page_count=len(pages), pages=tuple(pages))


# --------------------------------------------------------------------------- #
# 比对
# --------------------------------------------------------------------------- #


def _text_context(old: str, new: str) -> tuple[str, str]:
    """返回首个差异点前后的两段文字，供人一眼看出改了什么。"""
    left, right = _normalize(old), _normalize(new)
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    start = max(0, index - 8)
    end = index + _CONTEXT_CHARS
    return left[start:end], right[start:end]


def compare_pair(
    relative: str,
    before: FileFacts,
    after: FileFacts | None,
    *,
    note: str = "",
) -> dict[str, Any]:
    """比对一份文件，返回可直接进报告/JSON 的字典。

    `after` 为 `None` 表示产物目录里找不到对应文件——这本身就是一种结论，
    不能当成「没差异」跳过。
    """
    record: dict[str, Any] = {
        "relative": relative,
        "before_path": str(before.path),
        "after_path": str(after.path) if after else None,
        "page_count": {"before": before.page_count, "after": after.page_count if after else 0},
        "text_diff_pages": [],
        "structure_problems": [],
        "paint_only_pages": [],
        "note": note,
    }

    if after is None:
        record.update(verdict=MISSING, label=LABELS[MISSING])
        record["structure_problems"].append("产物目录里没有同名文件")
        return record

    if not before.readable or not after.readable:
        record.update(verdict=UNREADABLE, label=LABELS[UNREADABLE])
        detail = before.error or after.error or "未知原因"
        record["structure_problems"].append(f"无法读取：{detail}")
        return record

    # ---- 结构：页数、尺寸、旋转、图片、字体 ----
    if before.page_count != after.page_count:
        record["structure_problems"].append(
            f"页数变化：{before.page_count} → {after.page_count}"
        )
    for old, new in zip(before.pages, after.pages):
        if old.width != new.width or old.height != new.height:
            record["structure_problems"].append(
                f"第 {old.number} 页尺寸变化：{old.width}×{old.height} → {new.width}×{new.height}"
            )
        if old.rotate != new.rotate:
            record["structure_problems"].append(
                f"第 {old.number} 页旋转角变化：{old.rotate} → {new.rotate}"
            )
        if old.images != new.images:
            record["structure_problems"].append(f"第 {old.number} 页图片资源变化")
        if old.fonts != new.fonts:
            record["structure_problems"].append(f"第 {old.number} 页字体资源变化")

    # ---- 文字与绘制指令：逐页比 ----
    for old, new in zip(before.pages, after.pages):
        if _normalize(old.text) != _normalize(new.text):
            old_text, new_text = _text_context(old.text, new.text)
            record["text_diff_pages"].append(
                {
                    "page": old.number,
                    "chars": {"before": len(_normalize(old.text)), "after": len(_normalize(new.text))},
                    "before": old_text,
                    "after": new_text,
                }
            )
        elif old.content_hash != new.content_hash:
            record["paint_only_pages"].append(old.number)

    # 结构差异优先：页数/尺寸/图片/字体变了，再说文字没变也没意义。
    if record["structure_problems"]:
        verdict = STRUCTURE
    elif record["text_diff_pages"]:
        verdict = TEXT
    elif record["paint_only_pages"]:
        verdict = PAINT_ONLY
    else:
        verdict = SAME

    record.update(verdict=verdict, label=LABELS[verdict])
    return record


# --------------------------------------------------------------------------- #
# 配对
# --------------------------------------------------------------------------- #


def _pdf_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"
    )


def collect_pairs(
    before_root: Path | str, after_root: Path | str
) -> tuple[list[dict[str, Any]], list[Path]]:
    """把两边的 PDF 配成对，返回（配对列表, 产物里多出来的文件）。

    配对顺序：先按相对路径精确匹配；匹配不上再按文件名找，且**只有唯一命中**
    才认——同名文件不止一份时宁可报「缺对应文件」，也不能随便挑一个比，那会
    把「拿错了文件」伪装成「一致」。
    """
    before = Path(before_root)
    after = Path(after_root)

    if before.is_file():
        target = after if after.is_file() else None
        return ([{"relative": before.name, "before": before, "after": target, "note": ""}], [])

    pairs: list[dict[str, Any]] = []
    used: set[Path] = set()
    after_files = _pdf_files(after)

    for path in _pdf_files(before):
        relative = path.relative_to(before).as_posix()
        target = after / path.relative_to(before)
        note = ""
        if not target.is_file():
            same_name = [item for item in after_files if item.name == path.name]
            if len(same_name) == 1:
                target = same_name[0]
                note = f"按文件名配对（产物里路径为 {target.relative_to(after).as_posix()}）"
            else:
                target = None
                if len(same_name) > 1:
                    note = f"产物里有 {len(same_name)} 份同名文件，无法确定对应哪一份"
        if target is not None:
            used.add(target.resolve())
        pairs.append({"relative": relative, "before": path, "after": target, "note": note})

    extra = [item for item in after_files if item.resolve() not in used]
    return pairs, extra


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in LABELS}
    for record in records:
        counts[record["verdict"]] = counts.get(record["verdict"], 0) + 1
    return counts


def _counts_text(counts: dict[str, int]) -> str:
    parts = [f"{LABELS[key]} {counts[key]}" for key in LABELS if counts.get(key)]
    return "，".join(parts) if parts else "没有可比对的文件"


def render_report(
    records: Sequence[dict[str, Any]],
    extra: Sequence[Path] = (),
    *,
    before_root: str = "",
    after_root: str = "",
    quiet: bool = False,
) -> str:
    counts = summarize(records)
    ok = not any(record["verdict"] in FAILING_VERDICTS for record in records)
    lines: list[str] = []

    lines.append("独立复核：处理前后内容是否一致")
    lines.append(f"原件：{before_root}")
    lines.append(f"产物：{after_root}")
    lines.append("")

    if not quiet:
        lines.append("结论 ｜ 文件 ｜ 页数")
        for record in records:
            pages = record["page_count"]
            lines.append(
                f"{record['label']} ｜ {record['relative']} ｜ "
                f"{pages['before']} → {pages['after']}"
            )
        lines.append("")

    details = [record for record in records if record["verdict"] != SAME]
    if details:
        lines.append("差异明细")
        lines.append("=" * 8)
        for record in details:
            lines.append(f"{record['relative']}  【{record['label']}】")
            if record.get("note"):
                lines.append(f"  说明：{record['note']}")
            for problem in record["structure_problems"]:
                lines.append(f"  {problem}")
            for item in record["text_diff_pages"]:
                chars = item["chars"]
                lines.append(
                    f"  第 {item['page']} 页文字不同（原件 {chars['before']} 字 / "
                    f"产物 {chars['after']} 字）"
                )
                lines.append(f"    原件：…{item['before']}…")
                lines.append(f"    产物：…{item['after']}…")
            if record["paint_only_pages"]:
                shown = "、".join(str(page) for page in record["paint_only_pages"][:10])
                if len(record["paint_only_pages"]) > 10:
                    shown += f" 等 {len(record['paint_only_pages'])} 页"
                lines.append(f"  页面绘制指令有变化：第 {shown} 页（文字未变，不计为失败）")
            lines.append("")

    if extra:
        lines.append("产物里多出来的文件（不影响结论）")
        for path in extra:
            lines.append(f"  {path}")
        lines.append("")

    lines.append(f"共 {len(records)} 组：{_counts_text(counts)}")
    lines.append("结论：" + ("全部一致" if ok else "存在差异，详见上方明细"))
    lines.append(
        "说明：「绘制指令有变化」指页面 /Contents 字节变了，但文字、页数、尺寸、"
        "图片、字体都没变。内链变蓝（link_color）属于这一类，不计为失败。"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_content_same.py",
        description="独立复核：处理前后的 PDF 内容有没有变（只读，不修改任何文件）",
    )
    parser.add_argument("before_pos", nargs="?", metavar="原件", help="原件目录或单个 PDF")
    parser.add_argument("after_pos", nargs="?", metavar="产物", help="产物目录或单个 PDF")
    parser.add_argument("--before", dest="before_opt", help="原件（与位置参数二选一）")
    parser.add_argument("--after", dest="after_opt", help="产物（与位置参数二选一）")
    parser.add_argument("--json", dest="json_path", help="把结果另存为 JSON 报告")
    parser.add_argument("--quiet", action="store_true", help="只打差异与汇总，不列全部文件")
    return parser


def _emit(text: str) -> None:
    # Windows 控制台默认 cp936，中文与「→」在部分环境下会炸；统一按 UTF-8 输出，
    # 编不出来的字符降级成问号而不是抛 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    print(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    before_arg = args.before_opt or args.before_pos
    after_arg = args.after_opt or args.after_pos
    if not before_arg or not after_arg:
        parser.print_usage(sys.stderr)
        _emit("错误：需要同时给出原件与产物（位置参数或 --before/--after）")
        return 2

    before_root = Path(before_arg)
    after_root = Path(after_arg)
    if not before_root.exists():
        _emit(f"错误：找不到原件路径 {before_root}")
        return 2
    if not after_root.exists():
        _emit(f"错误：找不到产物路径 {after_root}")
        return 2

    pairs, extra = collect_pairs(before_root, after_root)
    if not pairs:
        _emit(f"错误：{before_root} 里没有 PDF 可比对")
        return 2

    records = [
        compare_pair(
            pair["relative"],
            read_pdf(pair["before"]),
            read_pdf(pair["after"]) if pair["after"] is not None else None,
            note=pair["note"],
        )
        for pair in pairs
    ]

    _emit(
        render_report(
            records,
            extra,
            before_root=str(before_root),
            after_root=str(after_root),
            quiet=args.quiet,
        )
    )

    if args.json_path:
        payload = {
            "before_root": str(before_root),
            "after_root": str(after_root),
            "ok": not any(record["verdict"] in FAILING_VERDICTS for record in records),
            "summary": summarize(records),
            "files": records,
            "extra_files": [str(path) for path in extra],
        }
        target = Path(args.json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _emit(f"JSON 报告已写入 {target}")

    return 0 if all(record["verdict"] not in FAILING_VERDICTS for record in records) else 1


if __name__ == "__main__":
    sys.exit(main())
