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
