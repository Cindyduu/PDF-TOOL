"""R20 / R41 字体嵌入校验。

**不做嵌入修复**（§1.3 第 8 条）——嵌入需要字体文件本身，工具手里没有。

v4.3（R41）修掉了两个让这一项形同虚设的缺陷：

1. 原来 `if _is_standard(name): continue` 把标准 14 字体整个豁免掉。可真实文件
   用的**就是** Helvetica，于是这一项永远 `pass`。现在标准 14 未嵌入同样报 `fail`
   ——eCTD 要求全字体嵌入，阅读器自带字形不等于文件自带字形（D-19）。
2. 原来 `embedded = any(key in descriptor ...)` 只看 `/FontFile*` 这个键**在不在**，
   不看里面是什么。样本里那个 26 字节的假流也能通过。现在要**校验字体程序结构**，
   判定口径对齐 Acrobat「文件→属性→字体」里字体名后的「（嵌入字符集）」。

几个容易写错的点：

- **资源可能继承**：`/Resources` 常常只写在 `/Pages` 节点上，页面自己
  没有。只看 `page.obj["/Resources"]` 会得出"全都没字体"的假结论，
  所以这里沿 `/Parent` 往上找。
- **Type0 复合字体**：`/FontDescriptor` 挂在 `/DescendantFonts[0]` 上，
  不在 `/Font` 本身。漏了这一步，所有中文文档都会被判成未嵌入。
- **Type3**：字形是页面里的过程，没有字体文件可嵌入，按合规处理。
- **子集前缀**（`ABCDEF+Arial`）要去掉，否则同一个字体在报告里出现
  六次，每次名字都不一样。
- **有效性只做结构判断**，不做全量解析：长度下限 + 魔数。真实世界最小的
  子集化 CFF 也有几百字节，64 字节的下限不会误伤。
"""

from __future__ import annotations

from typing import Any

import pikepdf
from pikepdf import Array, Dictionary

from app.pdf.checks import CheckContext, CheckResult, Finding, make_result

__all__ = ["run", "STANDARD_14", "MAX_PAGES_LISTED", "MIN_FONT_PROGRAM_BYTES"]

#: 标准 14 字体的族名前缀。
#:
#: v4.3 起**不再作为豁免条件**（D-19），只用于在明细里点明「标准 14 字体同样
#: 要求嵌入」——用户看到 Helvetica 被报错时，需要知道这不是误判。
STANDARD_14: tuple[str, ...] = (
    "Helvetica",
    "Times",
    "Courier",
    "Symbol",
    "ZapfDingbats",
)

#: 明细里每种字体最多列几个页号（AC-R20-N2）。
MAX_PAGES_LISTED = 10

FONT_FILE_KEYS = ("/FontFile", "/FontFile2", "/FontFile3")

#: 字体程序的最小字节数。
#:
#: 26 字节的占位流在真实世界里不存在（最小的子集化 CFF 也有数百字节），
#: 用这个下限就能挡掉「键在但内容是假的」这一整类。
MIN_FONT_PROGRAM_BYTES = 64

#: sfnt（TrueType / OpenType）的四种合法开头。
SFNT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO")


def _font_name(value: Any) -> str:
    """把 `/BaseFont` 转成纯字体名。

    `/BaseFont` 是 PDF 的 Name 对象，`str()` 出来带前导斜杠（`/Helvetica`）。
    不去掉这个斜杠，标准 14 字体的族名就匹配不上，明细里的名字也会带斜杠。
    """
    name = str(value or "")
    return name[1:] if name.startswith("/") else name


def _strip_subset(name: str) -> str:
    """去掉子集前缀：`ABCDEF+Arial` -> `Arial`。"""
    if len(name) > 7 and name[6] == "+":
        return name[7:]
    return name


def _is_standard(name: str) -> bool:
    return any(name.startswith(family) for family in STANDARD_14)


def _descriptor(font: Any) -> Any:
    """取字体描述符；Type0 去后代字体上找。"""
    desc = font.get("/FontDescriptor")
    if desc is not None:
        return desc
    descendants = font.get("/DescendantFonts")
    if isinstance(descendants, Array) and len(descendants):
        child = descendants[0]
        if isinstance(child, Dictionary):
            return child.get("/FontDescriptor")
    return None


def _resources_of(page_obj: Any) -> Any:
    """沿 `/Parent` 找 `/Resources`（页面可能继承页树节点的资源）。"""
    node = page_obj
    for _ in range(32):  # 防环
        resources = node.get("/Resources")
        if resources is not None:
            return resources
        parent = node.get("/Parent")
        if not isinstance(parent, Dictionary):
            return None
        node = parent
    return None


def _font_program(descriptor: Any) -> tuple[str, Any] | None:
    """返回 `(键名, 流)`；三个键一个都没有时返回 `None`。"""
    if not isinstance(descriptor, Dictionary):
        return None
    for key in FONT_FILE_KEYS:
        value = descriptor.get(key)
        if value is not None:
            return key, value
    return None


def _sfnt_is_valid(data: bytes) -> bool:
    """TrueType / OpenType：魔数对得上，且表目录能装进流里。"""
    if len(data) < 12:
        return False
    if data[:4] not in SFNT_MAGIC:
        return False
    if data[:4] == b"ttcf":
        # 字体集合：表目录在偏移 12 之后，偏移量另有约定，这里不深究。
        return True
    num_tables = int.from_bytes(data[4:6], "big")
    if not 1 <= num_tables <= 512:
        return False
    return 12 + 16 * num_tables <= len(data)


def _cff_is_valid(data: bytes) -> bool:
    """CFF：major 版本 1 或 2，头部长度落在合理区间。"""
    if len(data) < 4:
        return False
    major = data[0]
    header_size = data[2]
    return 1 <= major <= 2 and 4 <= header_size <= 32


def _program_is_valid(key: str, data: bytes) -> bool:
    """按键位选判据。`/FontFile3` 允许装 OpenType，所以两种头都认。"""
    if len(data) < MIN_FONT_PROGRAM_BYTES:
        return False
    if key == "/FontFile":
        return data[:2] == b"\x80\x01" or data[:2] == b"%!"
    if key == "/FontFile2":
        return _sfnt_is_valid(data)
    # /FontFile3：`/Subtype /OpenType` 时是真 sfnt，否则是裸 CFF。
    if data[:4] in SFNT_MAGIC:
        return _sfnt_is_valid(data)
    return _cff_is_valid(data)


def _pdfa_note(pdf: pikepdf.Pdf) -> str | None:
    """PDF/A 只能近似判断：看 `/Root /Metadata` 里有没有 `pdfaid`。"""
    try:
        metadata = pdf.Root.get("/Metadata")
        if metadata is None:
            return None
        data = metadata.read_bytes() if hasattr(metadata, "read_bytes") else b""
        if b"pdfaid" in data:
            return "疑似 PDF/A，本工具不做正式合规判定"
    except Exception:  # noqa: BLE001 - 元数据坏了不影响字体结论
        return None
    return None


def _judge(font: Any) -> tuple[str, str] | None:
    """判一个字体：返回 `(成因, 明细)` 或 `None`（合规）。

    成因只有两种，都要区分开——用户拿到报告要知道是「根本没嵌」还是
    「嵌了个假的」，前者要重新导出，后者要换导出工具。
    """
    descriptor = _descriptor(font)
    if not isinstance(descriptor, Dictionary):
        return "missing", "缺少 /FontDescriptor，无法携带字体程序"

    program = _font_program(descriptor)
    if program is None:
        return "missing", "/FontDescriptor 下无字体程序（/FontFile、/FontFile2、/FontFile3 均不存在）"

    key, stream = program
    try:
        data = stream.read_bytes() if hasattr(stream, "read_bytes") else b""
    except Exception as exc:  # noqa: BLE001 - AC-R41-E2：读不出来按无效处理
        return "invalid", f"{key} 读取失败（{type(exc).__name__}），视同未嵌入"

    if len(data) < MIN_FONT_PROGRAM_BYTES:
        return "invalid", (
            f"{key} 仅 {len(data)} 字节，短于最小长度 {MIN_FONT_PROGRAM_BYTES} 字节，"
            "不是有效的字体程序"
        )
    if not _program_is_valid(key, data):
        return "invalid", f"{key} 共 {len(data)} 字节，但头部不是有效的字体程序魔数"
    return None


def _scan(pdf: pikepdf.Pdf) -> dict[str, dict[str, Any]]:
    """返回 `{字体名: {"pages": [...], "kinds": set(), "detail": str}}`。

    结构异常时抛异常，由调用方转 `fail`（AC-R20-E1 / AC-R41-E1）。
    """
    issues: dict[str, dict[str, Any]] = {}
    for index, page in enumerate(pdf.pages, start=1):
        resources = _resources_of(page.obj)
        if not isinstance(resources, Dictionary):
            continue
        fonts = resources.get("/Font")
        if fonts is None:
            continue
        if not isinstance(fonts, Dictionary):
            raise ValueError("/Font 不是字典")
        for _key, font in fonts.items():
            if not isinstance(font, Dictionary):
                raise ValueError("/Font 下的项不是字典")
            if str(font.get("/Subtype") or "") == "/Type3":
                continue  # Type3 字形在页面过程里，无字体文件可嵌
            raw = _font_name(font.get("/BaseFont")) or "(未命名字体)"
            name = _strip_subset(raw)

            verdict = _judge(font)
            if verdict is None:
                continue

            kind, detail = verdict
            if _is_standard(name):
                detail += "；标准 14 字体同样要求嵌入"
            bucket = issues.setdefault(name, {"pages": [], "kinds": set(), "detail": detail})
            if index not in bucket["pages"]:
                bucket["pages"].append(index)
            bucket["kinds"].add(kind)
            # 「程序无效」比「根本没嵌」更具体，优先保留它的明细。
            if kind == "invalid":
                bucket["detail"] = detail
    return issues


def run(ctx: CheckContext) -> CheckResult:
    try:
        issues = _scan(ctx.pdf)
    except Exception as exc:  # noqa: BLE001 - AC-R20-E1：单项失败不拖垮整体
        return make_result(
            "font_embedding",
            "fail",
            "字体资源读取失败",
            [
                Finding(
                    level="fail",
                    message="字体资源读取失败",
                    detail=f"{type(exc).__name__}：{exc}".strip(),
                )
            ],
        )

    findings: list[Finding] = []
    note = _pdfa_note(ctx.pdf)

    if not issues:
        if note:
            findings.append(Finding(level="pass", message=note))
        return make_result(
            "font_embedding",
            "pass",
            "全部字体均已嵌入",
            findings
            or [Finding(level="pass", message="未发现未嵌入的字体")],
        )

    for name, info in sorted(issues.items()):
        pages: list[int] = info["pages"]
        listed = pages[:MAX_PAGES_LISTED]
        page_text = "、".join(str(item) for item in listed)
        if "invalid" in info["kinds"]:
            message = f"字体「{name}」的嵌入程序无效（第 {page_text} 页）"
        else:
            message = f"字体「{name}」未嵌入（第 {page_text} 页）"
        detail = info["detail"]
        if len(pages) > MAX_PAGES_LISTED:
            detail += f"（共 {len(pages)} 页，仅列前 {MAX_PAGES_LISTED} 页）"
        findings.append(
            Finding(level="fail", message=message, page=listed[0], detail=detail)
        )
    if note:
        findings.append(Finding(level="warning", message=note))

    return make_result(
        "font_embedding",
        "fail",
        f"{len(issues)} 种字体未嵌入或嵌入无效",
        findings,
    )
