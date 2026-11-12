"""真实服务器冒烟：起 uvicorn → 上传 → 前置校验 → 处理 → 后置校验 → 下载 → 独立校验产物。

与 tests 的区别：走真实 HTTP + 真实 uvicorn 进程，不依赖 TestClient 的进程内假设。

用法：
    python scripts/live_smoke.py                          # 用内置 fixture
    python scripts/live_smoke.py 某份真实文件.pdf          # 用外部文件
    python scripts/live_smoke.py 某份真实文件.pdf --exempt 1,2
    python scripts/live_smoke.py 某份真实文件.pdf --keep-output  # 留下产物
    python scripts/live_smoke.py 某份真实文件.pdf --exe dist/PDFTool/PDFTool.exe  # 验打包产物
    python scripts/live_smoke.py --no-flatten             # 不勾「注释扁平化」

v4.0 起标签由系统自动识别（`tag_detection`），不再有「标签四选一」，
所以这里也不再传 `tag_choice`——传了会被 `extra="forbid"` 直接判 422。

§15.1 要求的五条断言全部落在这里，任何一条不过就返回非零：

1. **前置校验前后原文件 sha256 一致**（AC-R13-N4）：直接读磁盘上那份落盘原件，
   校验前算一次、校验后再算一次，并和任务记录里的值对一次；
2. **输出 PDF 的 Link `/C` 全为 `[0,0,1]`**（AC-R25-N1）；
3. **非 Link 注释数为 0**（勾选扁平化时，AC-R26-N2）；
4. **Link 数与输入一致**（AC-R25-N4）；
5. **后置校验能跑完十二项且不抛异常**（AC-R27-N1）。

**校验不读应用自己写的报告**：报告说「改好了」不算数，必须重新打开下载下来的
那份文件，用 pikepdf 逐条检查书签/链接的目的地是不是 `[页 /XYZ l t null]`、
链接颜色是不是纯蓝、注释是不是真的没了。报告与产物不一致时以产物为准。

退出码：0 = 全部检查通过；1 = 有检查失败；2 = 流程跑不起来（上传/处理/下载出错）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import fitz
import httpx
import pikepdf
from pikepdf import Array, Dictionary

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 让脚本直接跑也能读到应用的常量，避免两处各写一份
    sys.path.insert(0, str(ROOT))

from app.schemas import (  # noqa: E402 - 必须在 sys.path 补齐之后导入
    DEFAULT_PROCESS_GROUPS,
    VERIFY_KEYS,
    VERIFY_STATUSES,
    worst_verify_status,
)

# 默认样本必须是**书签、内链、非链接注释三者都有**的文件。
# `text_blocks.pdf` 一个都没有，拿它当默认会让「链接颜色」「注释展平」
# 两条断言变成空断言——跑出「✓ 全部检查通过」，其实一条链接都没查过。
#
# 为什么不是 `exempt_mix.pdf`：它的链接矩形悬在空白处、Helvetica 又省了
# `/Widths`，两件事叠在一起，`content_color` 在这份样本上**一个文字都改不到**。
# R35 改的是文字填充色，样本里没有可改的文字，这条断言就永远是空断言。
# `smoke_links.pdf` 专为冒烟造：矩形压在文字上、字体带 `/Widths`。
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "pdfs" / "smoke_links.pdf"

TERMINAL = {"completed", "completed_with_warnings", "failed", "interrupted"}

#: 与 `app.pdf.link_color.TARGET_COLOR` 一致：RGB 纯蓝。
LINK_TARGET_COLOR = (0.0, 0.0, 1.0)

#: 同一个蓝色的整数写法，`fitz` 的 span 用这个。
#:
#: 两个常量并存不是重复：`/C` 是 PDF 对象里的浮点数组，span 颜色是渲染层
#: 折算出来的 sRGB 整数。v4.1 的 R35 改的是**文字填充色**，所以光看 `/C`
#: 已经证明不了「用户能看见蓝色」——必须两种都查。
LINK_TARGET_RGB_INT = 0x0000FF

#: 后置校验里「处理项确实能改变」的项。
#:
#: 只有这些项判 `fail` 才说明处理没做到位。其余项量的是**源文件内容**——
#: 文件名不合规、字体没嵌入、首页没有目录——工具按需求一律不动它们
#: （§1.3 三条不做：不生成目录、不嵌字体、不改文件名），判 `fail` 只说明
#: 这份样本本身不合规。把它们算成冒烟失败，会让冒烟在正常产物上永远红着。
#:
#: `annotations` 是唯一一项：勾了扁平化之后产物里就不该再有高亮和便签。
def processable_verify_keys(*, flatten: bool) -> frozenset[str]:
    """返回本次运行中「fail 算工具责任」的校验键。"""
    return frozenset({"annotations"}) if flatten else frozenset()


#: 原件落盘名，对应 `app.services.jobs.SOURCE_FILE_NAME`。
SOURCE_FILE_NAME = "original.pdf"


# --------------------------------------------------------------------------- #
# 服务器
# --------------------------------------------------------------------------- #


def wait_health(base: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(base + "/api/health", timeout=2.0)
            if response.status_code == 200:
                return response.json()
            last = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)
        time.sleep(0.3)
    raise RuntimeError(f"健康检查超时：{last}")


def wait_not_in(
    base: str, job_id: str, states: set[str], *, timeout: float, what: str
) -> dict:
    """轮询到任务状态**离开** `states` 为止。

    比「等它变成某个值」更稳：校验可能因为并发/重试落到 `failed` 而不是
    `verified`，等具体值会一直等到超时，把「校验失败」误报成「卡死」。
    """
    deadline = time.monotonic() + timeout
    data: dict = {}
    while time.monotonic() < deadline:
        data = httpx.get(base + f"/api/jobs/{job_id}", timeout=20.0).json()
        if data.get("status") not in states:
            return data
        time.sleep(0.3)
    raise RuntimeError(f"{what}超时（{timeout:.0f} 秒未结束），当前状态 {data.get('status')}")


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def job_source_path(var_dir: Path, job_id: str) -> Path:
    """找到这份文件在服务端落盘的原件。

    服务端自己会复核 sha256，但那是「被测方自证」。冒烟脚本必须自己读一遍
    磁盘上的字节——`AC-R13-N4` 说的是"文件真的没变"，不是"服务端说没变"。
    """
    matches = list(var_dir.glob(f"generations/*/jobs/{job_id}/{SOURCE_FILE_NAME}"))
    if not matches:
        raise RuntimeError(f"找不到 {job_id} 的原件落盘位置（var_dir={var_dir}）")
    return matches[0]


def run_flow(
    base: str, source: Path, *, exempt: list[int], flatten: bool, var_dir: Path
) -> dict:
    """上传 → 配置 → 前置校验 → 开始处理 → 轮询。返回最终 job 数据 + 取证信息。

    v4.0 把动线拆成两段（R13）：没跑前置校验就 `POST /start` 会拿到
    `verify_not_ready`。脚本必须按真实动线走——否则它验的是一条用户根本
    走不通的路径，通过了也没有意义。

    返回值在 job 数据之外多带三个键（`_source_path` / `_sha_before` / `_sha_after`），
    供 `main` 做 sha256 断言；它们以 `_` 开头，不会被误当成接口字段。
    """
    with source.open("rb") as handle:
        response = httpx.post(
            base + "/api/jobs",
            files={"files": (source.name, handle, "application/pdf")},
            timeout=180.0,
        )
    response.raise_for_status()
    job = response.json()
    job_id = job["id"]
    print(f"上传成功 job={job_id} 标签识别={job['tag_detection']['status']}")

    process_groups = list(DEFAULT_PROCESS_GROUPS)
    if flatten:
        process_groups.append("g6_flatten")
    payload = {
        "exempt_pages_raw": ",".join(str(page) for page in exempt),
        "process_items": process_groups,
    }
    response = httpx.patch(base + f"/api/jobs/{job_id}/config", json=payload, timeout=60.0)
    response.raise_for_status()
    print(f"处理项：{', '.join(process_groups)}")

    # --- 前置校验前：记下原件指纹 ---
    source_path = job_source_path(var_dir, job_id)
    sha_before = _sha256(source_path)
    recorded = (job.get("source") or {}).get("sha256")
    if recorded and recorded != sha_before:
        raise RuntimeError(
            f"落盘原件与上传时记录的不一致：磁盘 {sha_before[:12]}… vs 记录 {recorded[:12]}…"
        )

    # --- 第一段：前置校验（只读） ---
    response = httpx.post(
        base + f"/api/jobs/batch/{job['batch_id']}/verify", timeout=300.0
    )
    response.raise_for_status()
    verified = wait_not_in(
        base,
        job_id,
        {"verify_queued", "verifying"},
        timeout=300,
        what="前置校验",
    )
    if verified.get("status") != "verified":
        raise RuntimeError(f"前置校验没有通过：状态 {verified.get('status')}")

    # --- 前置校验后：原件必须一字节不差（AC-R13-N4） ---
    sha_after = _sha256(source_path)

    report = httpx.get(base + f"/api/jobs/{job_id}/report", timeout=60.0).json()
    precheck = report.get("verify") or {}
    # 校验结果里没有 `conclusion` 字段，只有十项 + 四态计数。
    # 结论要从计数推出来，别去取一个不存在的键——那会永远打印 None。
    print(
        f"前置校验：{worst_verify_status(precheck.get('items') or [])}"
        f"（{precheck.get('summary')}）"
    )

    # --- 第二段：处理（写） ---
    response = httpx.post(base + f"/api/jobs/{job_id}/start", timeout=60.0)
    response.raise_for_status()

    deadline = time.monotonic() + 300
    data: dict = {}
    while time.monotonic() < deadline:
        data = httpx.get(base + f"/api/jobs/{job_id}", timeout=20.0).json()
        if data["status"] in TERMINAL:
            break
        time.sleep(0.4)
    else:  # pragma: no cover - 只有处理卡死才会走到
        raise RuntimeError("处理超时（300 秒未结束）")

    data["_source_path"] = str(source_path)
    data["_sha_before"] = sha_before
    data["_sha_after"] = sha_after
    return data


# --------------------------------------------------------------------------- #
# 独立校验：只看磁盘上的字节
# --------------------------------------------------------------------------- #


def _search_name_tree(node: object, name: str) -> Array | None:
    """在命名目标树里递归找 `name`，返回目的地数组。

    名字树可以平铺（`/Names` 键值对数组），也可以分叉（`/Kids`）。
    两种都要走，否则真实文件里稍大一点的名字树就会漏查——
    漏查的表现是「一条都没检查，却报全部通过」。
    """
    if not isinstance(node, Dictionary):
        return None
    flat = node.get("/Names")
    if isinstance(flat, Array):
        for index in range(0, len(flat) - 1, 2):
            if str(flat[index]) != name:
                continue
            value = flat[index + 1]
            if isinstance(value, Array):
                return value
            if isinstance(value, Dictionary) and "/D" in value:
                inner = value.get("/D")
                return inner if isinstance(inner, Array) else None
            return None
    kids = node.get("/Kids")
    if isinstance(kids, Array):
        for kid in kids:
            found = _search_name_tree(kid, name)
            if found is not None:
                return found
    return None


def _resolve_named(pdf: pikepdf.Pdf, raw: object) -> Array | None:
    """把命名目标（字符串或名字）换成目的地数组。"""
    name = str(raw)
    names = pdf.Root.get("/Names")
    if isinstance(names, Dictionary):
        dests = names.get("/Dests")
        if isinstance(dests, Dictionary):
            found = _search_name_tree(dests, name)
            if found is not None:
                return found
    # 老式写法：目录里直接挂一个 /Dests 字典
    old = pdf.Root.get("/Dests")
    if isinstance(old, Dictionary):
        value = old.get("/" + name)
        if isinstance(value, Array):
            return value
        if isinstance(value, Dictionary):
            inner = value.get("/D")
            return inner if isinstance(inner, Array) else None
    return None


def _page_dest(
    holder: object, pdf: pikepdf.Pdf
) -> tuple[Array | None, str | None]:
    """从注释或大纲项里取页面目的地。

    返回 `(目的地数组, None)` 表示取到；`(None, None)` 表示这不是页面跳转
    （URI、Launch 等外链，不归本项管）；`(None, 原因)` 表示本该是页面跳转
    但解析不出来——这种必须报出来，不能静默跳过。
    """
    raw: object = None
    if "/Dest" in holder:
        raw = holder.get("/Dest")
    else:
        action = holder.get("/A")
        if isinstance(action, Dictionary):
            if str(action.get("/S", "")) != "/GoTo":
                return None, None
            raw = action.get("/D")

    if raw is None:
        return None, None
    if isinstance(raw, Array):
        return raw, None
    resolved = _resolve_named(pdf, raw)
    if resolved is None:
        return None, f"的命名目标「{raw}」在名字树里找不到"
    return resolved, None


def _iter_outline(pdf: pikepdf.Pdf):
    outlines = pdf.Root.get("/Outlines")
    if not isinstance(outlines, Dictionary):
        return
    stack = [outlines.get("/First")]
    seen: set[tuple[int, int]] = set()
    while stack:
        node = stack.pop()
        while node is not None and isinstance(node, Dictionary):
            key = node.objgen
            if key != (0, 0):
                if key in seen:
                    break
                seen.add(key)
            yield node
            child = node.get("/First")
            if isinstance(child, Dictionary):
                stack.append(child)
            node = node.get("/Next")


def _is_inherit_zoom(dest: object) -> tuple[bool, str]:
    """`[页 /XYZ l t null]` 才算承前缩放；其余都算没改。"""
    if not isinstance(dest, Array) or len(dest) < 2:
        return False, f"目的地不是有效数组：{dest!r}"
    fit = str(dest[1])
    if fit != "/XYZ":
        return False, f"目的地类型仍是 {fit}，不是 /XYZ"
    if len(dest) < 5:
        return False, "目的地缺少缩放位"
    if dest[4] is not None:
        return False, f"缩放位不是 null，而是 {dest[4]}"
    return True, ""


def _target_page(dest: object, pdf: pikepdf.Pdf) -> int | None:
    """目的地的第一个元素是页对象或页索引，统一换成 0 基页号。"""
    if not isinstance(dest, Array) or not len(dest):
        return None
    first = dest[0]
    if isinstance(first, int):
        return int(first)
    try:
        for index, page in enumerate(pdf.pages):
            if page.objgen == first.objgen:
                return index
    except Exception:  # noqa: BLE001
        return None
    return None


def _as_color(value: object) -> tuple[float, ...] | None:
    """把 `/C` 读成浮点元组；结构不对返回 `None`（调用方按"颜色不对"处理）。"""
    if not isinstance(value, Array):
        return None
    try:
        return tuple(round(float(item), 6) for item in value)
    except (TypeError, ValueError):
        return None


def _link_text_color(path: Path) -> tuple[int, int, list[str]]:
    """数「被内链覆盖的文字 span 里，有多少段是纯蓝」。

    返回 `(覆盖段数, 其中蓝色段数, 前几条非蓝样本)`。

    为什么不是「整份文件有没有蓝色文字」：源文件本来就可能带蓝色文字（标题、
    强调句），那个口径会把「链接一个字都没改」判成通过。也不能要求「所有文字
    都变蓝」——目录标题「目录」二字按需求必须保持黑色。

    口径定成「链接矩形盖住的 span」有两个好处：与实现同源（实现就是按
    `link_rects()` 找覆盖范围），且对没有文字层的样本天然为空——空集合不算
    失败，不会在纯图样本上制造假阳性。
    """
    covered = blue = 0
    samples: list[str] = []

    with fitz.open(path) as doc:
        for page_index, page in enumerate(doc):
            # 外链（URI）不归 R35 管，只取页内跳转。
            rects = [
                fitz.Rect(link["from"])
                for link in page.get_links()
                if link.get("kind") != fitz.LINK_URI and link.get("from")
            ]
            if not rects:
                continue
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text") or ""
                        if not text.strip():
                            continue
                        x0, y0, x1, y1 = span.get("bbox") or (0, 0, 0, 0)
                        center = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
                        if not any(center in rect for rect in rects):
                            continue
                        covered += 1
                        if span.get("color") == LINK_TARGET_RGB_INT:
                            blue += 1
                        elif len(samples) < 5:
                            samples.append(f"第 {page_index + 1} 页「{text.strip()[:20]}」")

    return covered, blue, samples


def check_output(
    source: Path, output: Path, *, exempt: list[int], flatten: bool = True
) -> list[str]:
    """返回失败原因列表；空列表表示全部通过。"""
    problems: list[str] = []
    exempt_zero = {page - 1 for page in exempt}  # 内部按 0 基

    with pikepdf.open(source) as src, pikepdf.open(output) as out:
        if len(src.pages) != len(out.pages):
            problems.append(f"页数变了：{len(src.pages)} → {len(out.pages)}")

        # --- 书签 ---
        src_items = list(_iter_outline(src))
        out_items = list(_iter_outline(out))
        if len(src_items) != len(out_items):
            problems.append(f"书签数变了：{len(src_items)} → {len(out_items)}")
        checked = 0
        for node in out_items:
            dest, why = _page_dest(node, out)
            if why is not None:
                title = str(node.get("/Title", "(无标题)"))[:30]
                problems.append(f"书签「{title}」{why}")
                continue
            if dest is None:
                continue
            page = _target_page(dest, out)
            if page is not None and page in exempt_zero:
                continue
            checked += 1
            ok, reason = _is_inherit_zoom(dest)
            if not ok:
                title = str(node.get("/Title", "(无标题)"))[:30]
                problems.append(f"书签「{title}」{reason}")
        print(f"书签：源 {len(src_items)} 个，产物受检 {checked} 个")

        # --- 页面内链接 ---
        # 目的地有两种写法：注释自己的 `/Dest`（常是命名目标），或 `/A /GoTo /D`。
        # 只认后者会漏掉整批链接却仍然报「通过」——RECIST 的 32 条就是这样漏的。
        link_checked = 0
        link_external = 0
        link_total = 0
        link_bad_color = 0
        for page_index, page in enumerate(out.pages):
            for annot in page.get("/Annots", []):
                if not isinstance(annot, Dictionary):
                    continue
                if str(annot.get("/Subtype", "")) != "/Link":
                    continue
                link_total += 1

                # 颜色与豁免无关：R25 没有豁免语义，每一条链接都必须变蓝。
                color = _as_color(annot.get("/C"))
                if color != LINK_TARGET_COLOR:
                    link_bad_color += 1
                    if link_bad_color <= 5:
                        problems.append(
                            f"第 {page_index + 1} 页的链接颜色不是 [0, 0, 1]："
                            f"{list(color) if color else annot.get('/C')!r}"
                        )

                if page_index in exempt_zero:
                    continue
                dest, why = _page_dest(annot, out)
                if why is not None:
                    problems.append(f"第 {page_index + 1} 页的链接{why}")
                    continue
                if dest is None:
                    link_external += 1  # URI / Launch 等外链，不归本项管
                    continue
                link_checked += 1
                ok, reason = _is_inherit_zoom(dest)
                if not ok:
                    problems.append(f"第 {page_index + 1} 页的链接{reason}")

        # 链接对象本身一个都不能少（AC-R25-N4）：改色只该动 `/C`。
        src_links = sum(
            1
            for page in src.pages
            for annot in page.get("/Annots", [])
            if isinstance(annot, Dictionary) and str(annot.get("/Subtype", "")) == "/Link"
        )
        if src_links != link_total:
            problems.append(f"链接数变了：{src_links} → {link_total}")
        if link_bad_color > 5:
            problems.append(f"另有 {link_bad_color - 5} 条链接颜色不是纯蓝")
        if not src_links:
            # 源文件本来就没有链接，颜色断言在这份文件上是空的。
            # 这不算失败，但必须说出来——否则「✓ 全部检查通过」会被读成
            # 「链接颜色查过了，没问题」。默认样本不会走到这里。
            print("  ！源文件没有链接，链接颜色断言在本份文件上不成立")

        print(
            f"页面内链接：源 {src_links} 条 → 产物 {link_total} 条，"
            f"产物受检 {link_checked} 条（另有 {link_external} 条外链跳过），"
            f"颜色不符 {link_bad_color} 条"
        )

        # --- 链接文字颜色（R35）---
        # 上面查的是 `/C`，只说明「链接注释被标成蓝色」。多数阅读器默认不画
        # 链接边框，所以 `/C` 全对用户也可能一点变化都看不到——v4.0 就是这样
        # 被判「内链颜色并未修正」。真正的验收点是文字填充色，只能读 span。
        covered, blue, samples = _link_text_color(output)
        if covered and not blue:
            problems.append(
                f"链接覆盖的 {covered} 段文字一段都没变蓝：内链颜色没有被改（R35）"
                + (f"（如 {'、'.join(samples)}）" if samples else "")
            )
        print(f"链接文字颜色：链接覆盖的文字 {covered} 段，其中纯蓝 {blue} 段")
        if not covered and link_total:
            # 有链接但一段文字都没被覆盖：要么整页没有文字层，要么字体缺字宽
            # 信息、推算不出文字位置（`content_color` 会给出对应警告）。
            # 这不算失败，但必须说出来——否则「✓ 全部检查通过」会被读成
            # 「文字真的变蓝了，我查过」。
            print(
                "  ！本次样本上没有可检查的链接文字，文字着色这条断言不成立"
                "（源文件无文字层，或字体缺 /Widths）"
            )
        if covered and blue < covered:
            # 部分未变蓝不判失败：实现按字号/字宽匹配文字 run，个别跨链接边界的
            # span 可能只被覆盖一半。但必须说出来，不能让它悄悄消失。
            print(
                f"  ！另有 {covered - blue} 段未变蓝（可能跨越链接边界）："
                + "、".join(samples)
            )

        # --- 初始视图三项 ---
        page_mode = str(out.Root.get("/PageMode", "/UseNone"))
        layout = str(out.Root.get("/PageLayout", "/SinglePage"))
        if src_items and page_mode != "/UseOutlines":
            problems.append(f"导览标签不是「书签面板和页面」：{page_mode}")
        if layout != "/SinglePage":
            problems.append(f"页面布局不是「默认」：{layout}")

        # --- 注释展平 ---
        # 链接注释**不在展平范围内**：它是书签/链接能不能点的载体，
        # 展平掉链接等于把文档变成死图。只有其余类型的注释才算残留。
        # 豁免页本来就不展平（`ctx.is_exempt`），所以残留要按页排除，
        # 否则一开豁免就会报"没展平"，把正常行为当成缺陷。
        leftover = 0
        exempt_leftover = 0
        links_kept = 0
        for page_index, page in enumerate(out.pages):
            for annot in page.get("/Annots", []):
                if not isinstance(annot, Dictionary):
                    continue
                if str(annot.get("/Subtype", "")) == "/Link":
                    links_kept += 1
                    continue
                if page_index in exempt_zero:
                    exempt_leftover += 1
                else:
                    leftover += 1
        src_non_link = sum(
            1
            for page in src.pages
            for annot in page.get("/Annots", [])
            if isinstance(annot, Dictionary)
            and str(annot.get("/Subtype", "")) != "/Link"
        )
        if flatten and leftover:
            problems.append(f"仍有 {leftover} 个非链接注释未展平")
        # 只有「源文件本来有、产物里一个不剩」才算被误改。
        # 拿「产物里没有注释」直接判失败是不对的：源文件本来就没有注释的文档
        # 很常见，那样会把正常结果报成缺陷。
        if not flatten and src_non_link and not leftover and not exempt_leftover:
            problems.append(
                f"没有勾选扁平化，但源文件里的 {src_non_link} 个非链接注释一个不剩，产物被误改"
            )

        # --- 元数据 ---
        if "/Info" in out.trailer:
            problems.append("DocInfo 未删除")
        if "/Metadata" in out.Root:
            problems.append("XMP 元数据流未移除")

        # --- 线性化 ---
        if not out.is_linearized:
            problems.append("未线性化（Fast Web View 未生效）")

        print(
            f"初始视图：PageMode={page_mode} PageLayout={layout} "
            f"| 非链接注释残留={leftover}（豁免页保留 {exempt_leftover}） "
            f"| 保留的链接={links_kept} | 线性化={out.is_linearized}"
        )
    return problems


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def build_command(exe: Path | None, *, port: int, var_dir: Path) -> list[str]:
    """拼出启动命令。

    打包后的 exe 自带解释器，不能再拿 `sys.executable` 去跑 `run.py`；
    源码态则必须用当前解释器，否则测的是别的 Python。
    """
    head = [str(exe)] if exe is not None else [sys.executable, "run.py"]
    return [*head, "--no-browser", "--port", str(port), "--var-dir", str(var_dir)]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="起真实服务跑一遍完整流程，并独立校验产物",
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        default=str(DEFAULT_FIXTURE),
        help=f"要处理的 PDF（默认内置样本 {DEFAULT_FIXTURE.name}）",
    )
    parser.add_argument("--port", type=int, default=8899, help="服务端口，默认 8899")
    parser.add_argument(
        "--exempt",
        default="",
        help="豁免页，逗号分隔，从 1 开始计数，例如 1,2；默认不豁免",
    )
    parser.add_argument(
        "--keep-output",
        action="store_true",
        help="把处理后的文件留在 --out 指定的位置，便于人工打开核对",
    )
    parser.add_argument(
        "--out",
        default="",
        help="配合 --keep-output 指定产物路径，默认输出到当前目录的 <文件名>_smoke.pdf",
    )
    parser.add_argument(
        "--exe",
        default="",
        help="改成启动打包后的 PDFTool.exe（验证打包产物），不填则直接跑 run.py",
    )
    parser.add_argument(
        "--no-flatten",
        action="store_true",
        help="不勾「注释扁平化」：此时产物里**应该**还留着非链接注释",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = Path(args.pdf).expanduser().resolve()
    if not source.is_file():
        print(f"文件不存在：{source}", file=sys.stderr)
        return 2

    exe = Path(args.exe).expanduser().resolve() if args.exe else None
    if exe is not None and not exe.is_file():
        print(f"可执行文件不存在：{exe}", file=sys.stderr)
        return 2

    exempt = [int(part) for part in args.exempt.replace("，", ",").split(",") if part.strip()]
    port = args.port
    base = f"http://127.0.0.1:{port}"
    var_dir = Path(tempfile.mkdtemp(prefix="pdftool-smoke-"))
    cwd = exe.parent if exe is not None else ROOT

    print(f"源文件：{source}（{source.stat().st_size} 字节）")
    print(f"启动方式：{exe.name if exe is not None else 'run.py（源码态）'}")
    print(f"豁免页：{exempt or '无'} | 端口：{port}")
    print(f"注释扁平化：{'不勾（产物应保留注释）' if args.no_flatten else '勾选'}")

    # 服务端日志必须落到文件，不能用 PIPE 挂着不读：
    # 页数多的真实文件会产生大量访问日志，管道缓冲（Windows 约 64KB）填满后
    # 服务进程会阻塞在写 stdout 上，整个冒烟就卡死——小样本永远触发不了。
    log_path = var_dir / "server.log"
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        build_command(exe, port=port, var_dir=var_dir),
        cwd=str(cwd),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        health = wait_health(base)
        print("health:", json.dumps(health, ensure_ascii=False))

        data = run_flow(
            base, source, exempt=exempt, flatten=not args.no_flatten, var_dir=var_dir
        )
        job_id = data["id"]
        print(f"处理状态：{data['status']}")
        problems: list[str] = []

        # --- 断言 1：前置校验没动原件（AC-R13-N4）---
        # 读的是磁盘上那份落盘原件的字节，不是服务端回报的字段。
        if data["_sha_before"] != data["_sha_after"]:
            problems.append(
                "前置校验改动了原文件（AC-R13-N4）："
                f"{data['_sha_before'][:12]}… → {data['_sha_after'][:12]}…"
            )
        else:
            print(f"原件指纹：{data['_sha_before'][:12]}… 校验前后一致")

        report = httpx.get(base + f"/api/jobs/{job_id}/report", timeout=60.0).json()
        print("\n十项结果：")
        for item in report["results"]:
            counts = item["counts"]
            print(
                f"  {item['key']:<24} {item['status']:<16} "
                f"检查={counts['checked']} 修改={counts['modified']} "
                f"已符合={counts['compliant']} 豁免={counts['exempt']} "
                f"不适用={counts['not_applicable']} 失败={counts['failed']}"
            )
            if item.get("message"):
                print(f"      说明：{item['message']}")
            for warning in item.get("warnings", []):
                print(f"      警告：{warning}")

        # --- 断言 5：后置校验跑完十二项、不抛异常（AC-R27-N1）---
        # 只看"跑完了没有"：后置校验整块失败时服务端会写一份十二项全 fail 的结果
        # （AC-R27-E1），结构上依然齐全，所以不能靠"有没有结果"判断它有没有崩。
        post = report.get("post_verify")
        processable = processable_verify_keys(flatten=not args.no_flatten)
        print("\n后置校验（对产物再跑一遍十二项）：")
        if not post:
            problems.append("后置校验没有结果（AC-R27-N1）")
        else:
            post_items = post.get("items") or []
            post_keys = [item.get("key") for item in post_items]
            missing = [key for key in VERIFY_KEYS if key not in post_keys]
            if missing:
                problems.append(f"后置校验少跑了 {len(missing)} 项：{', '.join(missing)}")
            for item in post_items:
                key = str(item.get("key"))
                status = item.get("status")
                print(f"  {key:<24} {status}")
                if status not in VERIFY_STATUSES:
                    problems.append(f"后置校验「{key}」的状态 {status!r} 不是四态之一")
                elif status == "fail":
                    if key in processable:
                        problems.append(
                            f"后置校验判定「{key}」fail："
                            f"{item.get('summary') or '（无说明）'}"
                        )
                    else:
                        # 内容类项：工具不动这些，fail 是样本属性，不是处理缺陷。
                        # 但要打出来——静默跳过等于假装它通过了。
                        print(
                            f"      （「{key}」量的是源文件内容，工具按需求不修，"
                            f"不计入冒烟失败）"
                        )

        response = httpx.get(base + f"/api/jobs/{job_id}/file?kind=output", timeout=180.0)
        response.raise_for_status()
        output = var_dir / "downloaded.pdf"
        output.write_bytes(response.content)
        print(f"\n下载产物：{len(response.content)} 字节")

        if args.keep_output:
            target = Path(args.out).expanduser().resolve() if args.out else Path.cwd() / (
                source.stem + "_smoke.pdf"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(output, target)
            print(f"产物已保留：{target}")

        print("\n独立校验（重新打开产物逐条查，不看应用报告）：")
        problems += check_output(
            source, output, exempt=exempt, flatten=not args.no_flatten
        )

        if data["status"] in {"failed", "interrupted"}:
            problems.append(f"处理状态是 {data['status']}，不是完成")

        if problems:
            print(f"\n✗ 有 {len(problems)} 项检查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1

        print("\n✓ 全部检查通过")
        return 0
    except Exception as exc:  # noqa: BLE001 - 冒烟脚本要给出人话原因
        print(f"\n✗ 流程未能跑通：{type(exc).__name__}: {exc}", file=sys.stderr)
        tail = _log_tail(log_path, lines=20)
        if tail:
            print("\n服务端日志末尾：", file=sys.stderr)
            print(tail, file=sys.stderr)
        return 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        log_file.close()


def _log_tail(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


if __name__ == "__main__":
    raise SystemExit(main())
