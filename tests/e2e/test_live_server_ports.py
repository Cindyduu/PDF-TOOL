"""`live_server` 的端口选择：撞上 Chromium 受限端口要自己重开。

这条用例**不起浏览器**，验的是夹具本身。之所以要验：端口是内核随机分的，
受限端口只在偶然命中时才出问题——全量跑过一次 `http://127.0.0.1:6000/`
（X11，Chromium 直接 `ERR_UNSAFE_PORT`），当时的表现是一条毫不相干的界面
用例变红。靠「下次跑全量留意一下」是发现不了的，只能把重开逻辑本身钉住。
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from tests.e2e import conftest

pytestmark = pytest.mark.e2e


def _health(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def test_blocked_ports_are_recognised() -> None:
    """6000（X11）是实测撞到的那个；高位端口不能被误判。"""
    assert conftest._port_blocked(6000)
    assert conftest._port_blocked(5060)
    assert not conftest._port_blocked(8765)
    assert not conftest._port_blocked(50000)


def test_live_server_reopens_when_first_port_is_blocked(monkeypatch, request) -> None:
    """把第一次判定强行置为「受限」，夹具必须关掉重开，且新端口能真的服务。"""
    calls: list[int] = []
    real = conftest._port_blocked

    def fake(port: int) -> bool:
        calls.append(port)
        # 只伪造第一次：制造一次「必须重开」的情形，之后照真实名单走
        return True if len(calls) == 1 else real(port)

    monkeypatch.setattr(conftest, "_port_blocked", fake)
    server = request.getfixturevalue("live_server")

    assert len(calls) >= 2, "撞到受限端口后没有重开，用例会被 ERR_UNSAFE_PORT 拖红"
    assert not real(calls[-1]), "重开之后又落在受限端口上"
    # 端口活着不等于服务活着：重启过的 uvicorn 可能绑定失败但线程没退出
    assert _health(server.url_for("/api/health"))["status"] == "ok"
