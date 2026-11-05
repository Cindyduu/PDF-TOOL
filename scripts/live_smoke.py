"""真实服务器冒烟：起 uvicorn → 上传 → 配置 → 开始 → 结果 → 下载 → 独立校验产物。

与 tests 的区别：走真实 HTTP + 真实 uvicorn 进程，不依赖 TestClient 的进程内假设。

用法：
    python scripts/live_smoke.py                          # 用内置 fixture
    python scripts/live_smoke.py 某份真实文件.pdf          # 用外部文件
    python scripts/live_smoke.py 某份真实文件.pdf --exempt 1,2 --tag keep
    python scripts/live_smoke.py 某份真实文件.pdf --keep-output  # 留下产物
    python scripts/live_smoke.py 某份真实文件.pdf --exe dist/PDFTool/PDFTool.exe  # 验打包产物

**校验不读应用自己写的报告**：报告说「改好了」不算数，必须重新打开下载下来的
那份文件，用 pikepdf 逐条检查书签/链接的目的地是不是 `[页 /XYZ l t null]`。
报告与产物不一致时以产物为准，脚本返回非零。

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

import httpx
import pikepdf
from pikepdf import Array, Dictionary

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "pdfs" / "text_blocks.pdf"

TERMINAL = {"completed", "completed_with_warnings", "failed", "interrupted"}


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


def run_flow(base: str, source: Path, *, exempt: list[int], tag: str) -> dict:
    """上传 → 配置 → 开始 → 轮询。返回最终 job 数据。"""
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

    payload = {
        "exempt_pages_raw": ",".join(str(page) for page in exempt),
        "tag_choice": tag,
    }
    response = httpx.patch(base + f"/api/jobs/{job_id}/config", json=payload, timeout=60.0)
    response.raise_for_status()

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


def check_output(source: Path, output: Path, *, exempt: list[int]) -> list[str]:
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
        for page_index, page in enumerate(out.pages):
            if page_index in exempt_zero:
                continue
            for annot in page.get("/Annots", []):
                if str(annot.get("/Subtype", "")) != "/Link":
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
        print(
            f"页面内链接：产物受检 {link_checked} 条"
            f"（另有 {link_external} 条外链跳过）"
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
        leftover = 0
        links_kept = 0
        for page in out.pages:
            for annot in page.get("/Annots", []):
                if str(annot.get("/Subtype", "")) == "/Link":
                    links_kept += 1
                else:
                    leftover += 1
        if leftover:
            problems.append(f"仍有 {leftover} 个非链接注释未展平")

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
            f"| 非链接注释残留={leftover} | 保留的链接={links_kept} "
            f"| 线性化={out.is_linearized}"
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
        "--tag",
        default="keep",
        choices=("keep", "generate", "skip", "regenerate"),
        help="标签处理方式，默认 keep（保留原有标签）",
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
    print(f"豁免页：{exempt or '无'} | 标签：{args.tag} | 端口：{port}")

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

        data = run_flow(base, source, exempt=exempt, tag=args.tag)
        job_id = data["id"]
        print(f"处理状态：{data['status']}")

        report = httpx.get(base + f"/api/jobs/{job_id}/report", timeout=60.0).json()
        print("\n九项结果：")
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
        problems = check_output(source, output, exempt=exempt)

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
