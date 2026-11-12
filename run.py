"""本地启动器：探测端口 → 起服务 → 等健康检查 → 开浏览器。

这个文件是**唯一**的启动入口（源码运行和 PyInstaller 打包后都走它），
所以它只做启动相关的事：不碰业务逻辑，也不重复装配 FastAPI。

三条硬约束：

1. **只绑 `127.0.0.1`**。没有 `--host` 参数：这是个本机单用户工具，
   一旦能绑 `0.0.0.0`，某个用户迟早会在公共网络上开着它。
2. **端口冲突要给人话**。默认在 `DEFAULT_PORT` 之后扫一段找空位并说明换了哪个；
   `--strict-port` 时不换端口，直接报错退出（脚本/固定书签场景需要确定性）。
3. **先健康检查再开浏览器**。服务还没监听就 `webbrowser.open`，用户会看到一个
   「无法访问此网站」的白页，然后以为工具坏了。

`sys._MEIPASS` 兼容：冻结态下 `run.py` 与 `app/` 被打进同一个临时解包目录，
`resource_root()` 用于在日志里说清资源到底从哪读的（静态目录本身由
`app.config.Settings.static_dir` 解析）。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from app.config import DEFAULT_HOST, DEFAULT_PORT, Settings, load_settings

HOST = DEFAULT_HOST  # 127.0.0.1，刻意不做成可配置项
PORT_SCAN_LIMIT = 20
HEALTH_PATH = "/api/health"
HEALTH_TIMEOUT = 30.0
HEALTH_POLL_INTERVAL = 0.2

EXIT_OK = 0
EXIT_PORT_UNAVAILABLE = 2


class PortUnavailable(RuntimeError):
    """没有可用端口。消息本身就是给用户看的，调用方直接打印。"""


# --------------------------------------------------------------------------- #
# 端口
# --------------------------------------------------------------------------- #


def is_port_free(host: str, port: int) -> bool:
    """能不能在 `host:port` 上 bind。

    Windows 上不能用 `SO_REUSEADDR` 探测：它允许绑定一个**已被监听**的端口，
    探测就永远返回 True。这里用 `SO_EXCLUSIVEADDRUSE`（Windows 专有）才测得准。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # pragma: no cover - 平台差异
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(
    host: str = HOST,
    preferred: int = DEFAULT_PORT,
    limit: int = PORT_SCAN_LIMIT,
) -> int:
    """从 `preferred` 起找第一个空端口；一个都没有就抛 `PortUnavailable`。"""
    limit = max(int(limit), 1)
    last = min(preferred + limit - 1, 65535)
    for candidate in range(preferred, last + 1):
        if is_port_free(host, candidate):
            return candidate

    if preferred == last:
        detail = f"端口 {preferred} 已被占用"
    else:
        detail = f"端口 {preferred}-{last} 都被占用"
    raise PortUnavailable(
        f"{detail}，无法启动服务。请关闭占用该端口的程序，或用 --port 指定其他端口。"
    )


# --------------------------------------------------------------------------- #
# 资源与配置
# --------------------------------------------------------------------------- #


def resource_root() -> Path:
    """资源根目录：冻结态用 `sys._MEIPASS`，源码态用本文件所在目录。"""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parent


def build_settings(var_dir: Path | str | None, port: int) -> Settings:
    """装配配置。`Settings.validate()` 会再拒绝一次非回环 host。"""
    settings = load_settings(var_dir, port=port)
    settings.validate()
    return settings


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #


def wait_for_health(base_url: str, timeout: float = HEALTH_TIMEOUT) -> dict[str, Any]:
    """轮询 `/api/health` 直到 200。超时抛 `TimeoutError`（消息含 URL 与耗时）。"""
    url = base_url.rstrip("/") + HEALTH_PATH
    deadline = time.monotonic() + timeout
    last_error = "尚未收到响应"

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
                last_error = f"HTTP {response.status}"
        except (urllib.error.URLError, OSError, ValueError) as error:
            last_error = str(error)
        time.sleep(HEALTH_POLL_INTERVAL)

    raise TimeoutError(f"服务在 {timeout:.0f} 秒内没有就绪（{url}）：{last_error}")


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #


def _serve(settings: Settings, *, open_browser: bool = True) -> int:
    import uvicorn

    from app.main import create_app

    app = create_app(settings=settings, start_services=True)
    config = uvicorn.Config(app, host=HOST, port=settings.port, log_level="info")
    server = uvicorn.Server(config)
    url = f"http://{HOST}:{settings.port}"

    if open_browser:
        def _open_when_ready() -> None:
            try:
                wait_for_health(url)
            except TimeoutError as error:
                print(f"未能自动打开浏览器：{error}", file=sys.stderr)
                return
            webbrowser.open(url)

        threading.Thread(target=_open_when_ready, name="open-browser", daemon=True).start()

    print(f"正在启动：{url}")
    print(f"数据目录：{settings.var_dir}")
    print(f"资源目录：{resource_root()}")
    print("关闭这个窗口即可停止服务。")
    server.run()
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="PDF 自动设置工具",
        description="在本机 127.0.0.1 上启动 PDF 自动设置工具的界面。",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="首选端口（默认 8765）")
    parser.add_argument(
        "--var-dir", type=Path, default=None, help="数据目录（默认当前目录下的 var）"
    )
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument(
        "--strict-port",
        action="store_true",
        help="端口被占用时直接报错退出，不自动换端口",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not 1 <= args.port <= 65535:
        print(f"启动失败：端口 {args.port} 不合法，应在 1-65535 之间。", file=sys.stderr)
        return EXIT_PORT_UNAVAILABLE

    try:
        port = pick_port(HOST, args.port, 1 if args.strict_port else PORT_SCAN_LIMIT)
    except PortUnavailable as error:
        print(f"启动失败：{error}", file=sys.stderr)
        return EXIT_PORT_UNAVAILABLE

    if port != args.port:
        print(f"端口 {args.port} 已被占用，改用 {port}")

    try:
        settings = build_settings(args.var_dir, port)
    except (ValueError, OSError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        return EXIT_PORT_UNAVAILABLE

    try:
        return _serve(settings, open_browser=not args.no_browser)
    except KeyboardInterrupt:  # pragma: no cover - 交互式退出
        print("\n已停止。")
        return EXIT_OK
    except OSError as error:
        print(f"启动失败：{error}（端口 {port}）", file=sys.stderr)
        return EXIT_PORT_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
