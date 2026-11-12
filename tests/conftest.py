"""测试公共夹具。

所有测试都在 tmp_path 下的独立 var 目录里跑，绝不触碰项目真实 var/。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, load_settings  # noqa: E402
from app.context import AppContext  # noqa: E402


@pytest.fixture
def var_dir(tmp_path: Path) -> Path:
    return tmp_path / "var"


@pytest.fixture
def settings(var_dir: Path) -> Settings:
    return load_settings(var_dir)


@pytest.fixture
def ctx(settings: Settings) -> AppContext:
    return AppContext(settings)


@pytest.fixture
def client(var_dir: Path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(var_dir=var_dir, start_services=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fixture_pdfs() -> Path:
    from tests.fixtures.make_fixtures import ensure_fixtures

    return ensure_fixtures()
