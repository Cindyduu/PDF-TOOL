"""阶段 K 门槛：启动器能探测端口、只绑回环、健康检查真的答 200。

分两层验证，理由不同：

- **纯函数层**（`pick_port` / `parse_args` / `_serve` 传给 uvicorn 的 host /
  `static_dir` 的三级解析）——毫秒级，失败时能一眼看出是哪个决策错了。
- **真进程层**（`subprocess` 起 `run.py`）——只有真起一遍，才能证明
  「uvicorn 装配 + 静态资源挂载 + 健康检查」这三件事在**和打包后同一个入口**下
  都成立。用 TestClient 测不到 `uvicorn.Config` 的 host/port 绑定。

真进程用例的纪律：30 秒内必须出结果；失败时把子进程输出贴进断言消息；
`finally` 里先 `terminate` 再 `kill`，不留僵尸进程。
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator

import pytest

import run
from app.config import ENV_STATIC_DIR, Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = PROJECT_ROOT / "run.py"

PROCESS_TIMEOUT = 30.0


# --------------------------------------------------------------------------- #
# 端口工具
# --------------------------------------------------------------------------- #


def free_port() -> int:
    """拿一个当前空闲的端口号：绑 0 让系统分配，然后立刻释放。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def free_span(count: int) -> int:
    """找一段连续 `count` 个都空闲的端口，返回起始端口。

    端口占用情况随机器状态变化，写死端口号的测试早晚会在别人的机器上红。
    """
    for base in range(20000, 60000 - count):
        if all(run.is_port_free("127.0.0.1", base + offset) for offset in range(count)):
            return base
    raise RuntimeError(f"找不到连续 {count} 个空闲端口")


@contextmanager
def occupied(port: int) -> Iterator[socket.socket]:
    """占住一个端口（bind + listen），模拟「别的程序已经在用」。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        yield sock
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# 子进程封装
# --------------------------------------------------------------------------- #


class Launcher:
    """跑一个真的 `run.py`，把 stdout 按行收进队列，方便按需等待某一行。

    `-u` 是必须的：子进程的 stdout 是管道（非 tty），Python 默认块缓冲，
    不打 `-u` 的话「端口已被占用，改用 X」这句话会卡在缓冲区里等到进程退出，
    测试就只能靠超时失败。
    """

    def __init__(self, args: list[str], cwd: Path = PROJECT_ROOT) -> None:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"  # 子进程按 UTF-8 写，父进程按 UTF-8 读
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(LAUNCHER), *args],
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.lines: list[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        self._reader = threading.Thread(target=self._pump, name="launcher-stdout", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.rstrip("\r\n")
            self.lines.append(line)
            self._queue.put(line)

    def output(self) -> str:
        body = "\n".join(self.lines[-40:]) or "（子进程没有任何输出）"
        return f"子进程输出：\n{body}"

    def wait_for_line(self, needle: str, timeout: float = PROCESS_TIMEOUT) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{timeout:.0f} 秒内没等到包含 {needle!r} 的输出。\n{self.output()}")
            try:
                line = self._queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise AssertionError(
                        f"启动器已退出（退出码 {self.proc.returncode}），没等到 {needle!r}。\n"
                        f"{self.output()}"
                    ) from None
                continue
            if needle in line:
                return line

    def stop(self) -> None:
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 只在服务卡死时走到
            self.proc.kill()
            self.proc.wait(timeout=10)


@contextmanager
def launch(args: list[str], cwd: Path = PROJECT_ROOT) -> Iterator[Launcher]:
    launcher = Launcher(args, cwd=cwd)
    try:
        yield launcher
    finally:
        launcher.stop()


def health(port: int, launcher: Launcher | None = None, timeout: float = PROCESS_TIMEOUT) -> dict:
    """等 `/api/health` 返回 200。超时或进程提前退出都抛带上下文的 AssertionError。"""
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.monotonic() + timeout
    last_error = "尚未发起请求"

    while time.monotonic() < deadline:
        if launcher is not None and launcher.proc.poll() is not None:
            raise AssertionError(
                f"启动器在服务就绪前退出（退出码 {launcher.proc.returncode}）。\n{launcher.output()}"
            )
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
                last_error = f"HTTP {response.status}"
        except (urllib.error.URLError, OSError, ValueError) as error:
            last_error = f"{type(error).__name__}: {error}"
        time.sleep(0.2)

    suffix = f"\n{launcher.output()}" if launcher is not None else ""
    raise AssertionError(f"{timeout:.0f} 秒内 {url} 没有返回 200：{last_error}{suffix}")


# --------------------------------------------------------------------------- #
# 端口探测
# --------------------------------------------------------------------------- #


def test_pick_port_returns_preferred_when_free() -> None:
    port = free_port()
    assert run.is_port_free("127.0.0.1", port) is True
    assert run.pick_port("127.0.0.1", port, 1) == port


def test_pick_port_skips_occupied_port() -> None:
    port = free_span(2)
    with occupied(port):
        assert run.is_port_free("127.0.0.1", port) is False
        chosen = run.pick_port("127.0.0.1", port, run.PORT_SCAN_LIMIT)
        assert chosen != port
        assert chosen > port  # 只向后扫，不回头抢更小的端口
        # 返回的端口必须真的能绑：不是「跳过一个又给了另一个占用中的」
        assert run.is_port_free("127.0.0.1", chosen) is True


def test_pick_port_reports_readable_error_when_all_busy() -> None:
    base = free_span(3)
    with ExitStack() as stack:
        for offset in range(3):
            stack.enter_context(occupied(base + offset))

        with pytest.raises(run.PortUnavailable) as excinfo:
            run.pick_port("127.0.0.1", base, 3)

    message = str(excinfo.value)
    assert str(base) in message
    # 错误消息必须同时给出「发生了什么」和「该怎么办」
    assert "--port" in message
    assert "占用" in message


# --------------------------------------------------------------------------- #
# 只绑回环
# --------------------------------------------------------------------------- #


def test_only_loopback_is_used(tmp_path, monkeypatch) -> None:
    assert run.HOST == "127.0.0.1"

    # 没有 --host 这个参数：不能靠命令行把服务暴露到 0.0.0.0
    with pytest.raises(SystemExit):
        run.parse_args(["--host", "0.0.0.0"])

    # 就算有人手搓 Settings 绕过启动器，validate() 也要拒绝
    with pytest.raises(ValueError):
        Settings(var_dir=tmp_path / "var", host="0.0.0.0").validate()

    # 真正交给 uvicorn 的 host 必须是回环地址
    captured: dict[str, object] = {}
    import uvicorn

    def fake_run(self, *args, **kwargs):
        captured["host"] = self.config.host
        captured["port"] = self.config.port
        return None

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)

    port = free_port()
    settings = run.build_settings(tmp_path / "var", port)
    assert run._serve(settings, open_browser=False) == run.EXIT_OK

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == port


# --------------------------------------------------------------------------- #
# main() 的接线
# --------------------------------------------------------------------------- #


def test_main_wires_var_dir_and_port(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    opened: list[str] = []

    def fake_serve(settings: Settings, *, open_browser: bool = True) -> int:
        captured["settings"] = settings
        captured["open_browser"] = open_browser
        return run.EXIT_OK

    monkeypatch.setattr(run, "_serve", fake_serve)
    monkeypatch.setattr(run.webbrowser, "open", lambda url: opened.append(url))

    port = free_port()
    var_dir = tmp_path / "自定义数据目录"
    code = run.main(["--port", str(port), "--var-dir", str(var_dir), "--no-browser"])

    assert code == run.EXIT_OK
    settings = captured["settings"]
    assert isinstance(settings, Settings)
    assert settings.var_dir == var_dir.resolve()
    assert settings.port == port
    assert captured["open_browser"] is False
    assert opened == []


def test_strict_port_fails_with_readable_error(capsys, monkeypatch) -> None:
    port = free_span(2)
    with occupied(port):
        # --strict-port 下不允许换端口：脚本和固定书签依赖端口确定性
        code = run.main(["--port", str(port), "--strict-port", "--no-browser"])

    assert code == run.EXIT_PORT_UNAVAILABLE
    stderr = capsys.readouterr().err
    assert str(port) in stderr
    assert "占用" in stderr
    assert "--port" in stderr


def test_invalid_port_is_rejected(capsys) -> None:
    assert run.main(["--port", "0", "--no-browser"]) == run.EXIT_PORT_UNAVAILABLE
    assert "1-65535" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 真进程
# --------------------------------------------------------------------------- #


def test_health_endpoint_answers_200(tmp_path) -> None:
    port = free_port()
    var_dir = tmp_path / "var"

    with launch(["--port", str(port), "--var-dir", str(var_dir), "--no-browser"]) as launcher:
        payload = health(port, launcher)

        assert payload["status"] == "ok"
        assert payload["version"]
        assert payload["generation_id"].startswith("gen-")
        # 真进程里也要能读到首页，说明静态资源在启动器入口下挂载成功
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5.0) as response:
            assert response.status == 200
            body = response.read().decode("utf-8")
        assert "<!doctype html" in body.lower()

    assert (var_dir / "active.json").is_file()


def test_port_conflict_switches_and_reports(tmp_path) -> None:
    base = free_span(2)
    var_dir = tmp_path / "var"

    with occupied(base):
        with launch(["--port", str(base), "--var-dir", str(var_dir), "--no-browser"]) as launcher:
            line = launcher.wait_for_line("改用")
            assert str(base) in line

            # 从「改用 8766」里取出真实端口，而不是猜 base+1（base+1 也可能被占）
            new_port = int(line.rsplit("改用", 1)[1].strip())
            assert new_port != base

            payload = health(new_port, launcher)
            assert payload["status"] == "ok"


# --------------------------------------------------------------------------- #
# 静态资源路径
# --------------------------------------------------------------------------- #


def test_static_dir_honours_meipass(tmp_path, monkeypatch) -> None:
    bundled = tmp_path / "bundle"
    bundled_static = bundled / "app" / "static"
    bundled_static.mkdir(parents=True)
    (bundled_static / "index.html").write_text("<!doctype html>", encoding="utf-8")

    # 冻结态：PyInstaller 把 app/static 释放到 sys._MEIPASS 下
    monkeypatch.delenv(ENV_STATIC_DIR, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundled), raising=False)
    assert Settings(var_dir=tmp_path / "var").static_dir == bundled_static

    # 显式覆盖优先于冻结态（排障时能把资源指到别处）
    override = tmp_path / "override"
    override.mkdir()
    monkeypatch.setenv(ENV_STATIC_DIR, str(override))
    assert Settings(var_dir=tmp_path / "var").static_dir == override.resolve()

    # 源码态：既没覆盖也没 _MEIPASS，就回到 app/static
    monkeypatch.delenv(ENV_STATIC_DIR)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    source = Settings(var_dir=tmp_path / "var").static_dir
    assert source.name == "static"
    assert (source / "index.html").is_file()


def test_static_dir_falls_back_when_meipass_lacks_static(tmp_path, monkeypatch) -> None:
    # _MEIPASS 存在但没有 app/static（比如 --add-data 漏了）：不能返回一个不存在的路径
    monkeypatch.delenv(ENV_STATIC_DIR, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "empty-bundle"), raising=False)
    resolved = Settings(var_dir=tmp_path / "var").static_dir
    assert resolved.is_dir()
    assert (resolved / "index.html").is_file()
