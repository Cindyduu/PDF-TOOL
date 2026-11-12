"""E2E 夹具：真起一个 uvicorn，真开一个 Chromium。

为什么不用 TestClient：E2E 要验的是"浏览器里能不能走完流程"，
包括 hash 路由、刷新恢复、前进后退、下载——这些 TestClient 一个都验不到。

服务起在随机端口、随机 var 目录，测试之间互不干扰；
没装浏览器时显式 skip 并写清原因（不静默跳过）。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

#: Chromium 拒绝访问的端口（`net::ERR_UNSAFE_PORT`）。
#:
#: 端口是操作系统随机分配的，绝大多数时候落在 49152 以上没事，但 Windows 上
#: 动态端口范围可以被改成 1024 起，于是随机到 6000（X11）这类端口的机会不为零。
#: 一旦随机到，`page.goto()` 直接抛 `ERR_UNSAFE_PORT`，用例红得莫名其妙——
#: 实测就撞过一次（`http://127.0.0.1:6000/`）。这份名单抄自 Chromium 的
#: `net/base/port_util.cc`，只收录 1–10080 里真实可能被随机到的那些。
_UNSAFE_PORTS = frozenset(
    {
        1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69,
        77, 79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119,
        123, 135, 137, 138, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514,
        515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989,
        990, 993, 995, 1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000,
        6566, 6665, 6666, 6667, 6668, 6669, 6697, 10080,
    }
)

#: 随机到受限端口时最多重开几次。连续三次都撞上同一类端口的概率可以忽略，
#: 但真发生了要让它红，而不是无限重试把问题藏起来。
_MAX_PORT_ATTEMPTS = 3


def _port_blocked(port: int) -> bool:
    return port in _UNSAFE_PORTS


class LiveServer:
    """跑在后台线程里的真实服务。"""

    def __init__(self, url: str, var_dir: Path, settings) -> None:
        self.url = url
        self.var_dir = var_dir
        self.settings = settings

    def url_for(self, path: str = "/") -> str:
        return f"{self.url}{path}"

    def job_dir(self, job_id: str) -> Path:
        return self.settings.generations_dir / self.generation_id / "jobs" / job_id

    @property
    def generation_id(self) -> str:
        import json

        payload = json.loads((self.var_dir / "active.json").read_text(encoding="utf-8"))
        return payload["generation_id"]


@pytest.fixture
def live_server(var_dir: Path) -> LiveServer:
    import uvicorn

    from app.config import load_settings
    from app.main import create_app

    settings = load_settings(var_dir)

    for attempt in range(1, _MAX_PORT_ATTEMPTS + 1):
        app = create_app(settings=settings, start_services=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="uvicorn-e2e", daemon=True)
        thread.start()

        deadline = time.time() + 30
        while time.time() < deadline:
            if server.started:
                break
            time.sleep(0.05)
        else:  # pragma: no cover - 只有环境坏掉才会走到
            pytest.fail("uvicorn 未能在 30 秒内启动")

        port = server.servers[0].sockets[0].getsockname()[1]
        if not _port_blocked(port):
            break

        # 撞上 Chromium 的受限端口。端口是内核分的，改不了，只能关掉重开一个。
        # 这里不 fail：受限端口是环境的随机性，不是被测代码的问题。
        server.should_exit = True
        thread.join(timeout=20)
    else:  # pragma: no cover - 概率极低，但真发生了要显式报出来
        pytest.fail(
            f"连续 {_MAX_PORT_ATTEMPTS} 次随机到的端口都在 Chromium 受限名单里，e2e 未能启动"
        )

    yield LiveServer(f"http://127.0.0.1:{port}", var_dir, settings)

    server.should_exit = True
    thread.join(timeout=20)


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - 环境缺失要显式报告
            message = str(exc)
            # 只有"二进制确实没装"才算 skip。启动超时、进程被拦、端口异常这些
            # 都是环境坏了——那种情况必须让用例红，否则整个 e2e 会静默消失、
            # 套件还返回 0，等于靠 skip 把没跑过的测试算成通过。
            # （实测遇到过一次 180 秒启动超时，就是被这里当成"没装"跳过的。）
            if "Executable doesn't exist" in message or "playwright install" in message:
                pytest.skip(
                    "未安装 Playwright Chromium，e2e 未执行；"
                    f"请先运行 `python -m playwright install chromium`。原因：{message}"
                )
            pytest.fail(
                "Chromium 存在但启动失败，e2e 未执行——这是环境问题，不是缺浏览器："
                f"{type(exc).__name__}: {message}"
            )
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    yield page
    context.close()
