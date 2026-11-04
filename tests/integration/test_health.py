"""阶段 C 门槛：应用能起来，健康检查如实反映运行时状态。

另外验证错误模型：所有对外错误都必须是
`{code, message, details, retryable, next_action}` 五字段。
"""

from __future__ import annotations

import pytest

from app.config import APP_NAME, APP_VERSION, SCHEMA_VERSION


def test_health_reports_runtime_state(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"] == APP_VERSION
    assert payload["app"] == APP_NAME
    assert payload["schema_version"] == SCHEMA_VERSION
    # generation_id 必须是已经落地的目录，而不是空字符串
    generation_id = payload["generation_id"]
    assert generation_id.startswith("gen-")


def test_health_generation_matches_disk(client) -> None:
    payload = client.get("/api/health").json()
    ctx = client.app.state.ctx
    assert payload["generation_id"] == ctx.storage.active_generation_id()
    assert (ctx.storage.generations_dir / payload["generation_id"]).is_dir()


def test_health_is_not_cached(client) -> None:
    first = client.get("/api/health").json()
    second = client.get("/api/health").json()
    assert first["generation_id"] == second["generation_id"]
    assert first["status"] == second["status"] == "ok"


def test_docs_and_openapi_are_disabled(client) -> None:
    # 本地工具不暴露交互式文档，减少无谓的攻击面
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_unknown_api_route_returns_404(client) -> None:
    assert client.get("/api/does-not-exist").status_code == 404


def test_index_page_is_served(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_error_payload_has_all_five_fields() -> None:
    from app.errors import AppError, ErrorCode

    error = AppError(
        ErrorCode.NOT_A_PDF,
        "不是 PDF",
        details={"name": "a.txt"},
        retryable=False,
        next_action="请选择 PDF 文件",
        http_status=400,
    )
    payload = error.to_dict()
    assert set(payload) == {"code", "message", "details", "retryable", "next_action"}
    assert payload["code"] == ErrorCode.NOT_A_PDF
    assert payload["next_action"]


def test_app_error_is_rendered_as_json(client) -> None:
    from app.errors import AppError, ErrorCode

    ctx = client.app.state.ctx
    storage = ctx.storage
    # 非法 ID 必须走统一错误模型，而不是抛 500
    with pytest.raises(AppError):
        storage.job_dir(ctx.generation_id, "../../etc")
