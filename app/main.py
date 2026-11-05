"""FastAPI 应用装配。

只负责：错误模型、静态资源、路由挂载、健康检查。业务逻辑在 services 与 pdf 包。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import APP_NAME, APP_VERSION, Settings, load_settings
from app.context import AppContext
from app.core.logging import configure_logging, get_logger
from app.errors import AppError, ErrorCode


def register_error_handlers(app: FastAPI) -> None:
    log = get_logger("http")

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        log.info("AppError %s %s -> %s", request.method, request.url.path, exc.code)
        return JSONResponse(exc.to_dict(), status_code=exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        payload = AppError(
            ErrorCode.INVALID_CONFIG,
            "请求参数不正确",
            details={"errors": _jsonable_errors(exc.errors())},
            next_action="检查提交的内容后重试",
            http_status=422,
        )
        return JSONResponse(payload.to_dict(), status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("未处理异常 %s %s", request.method, request.url.path)
        payload = AppError(
            ErrorCode.INTERNAL_ERROR,
            "服务器内部错误",
            details={"type": type(exc).__name__, "message": str(exc)},
            next_action="重试一次；若仍失败，请查看 var/logs/app.log",
            http_status=500,
        )
        return JSONResponse(payload.to_dict(), status_code=500)


def _jsonable_errors(errors: list[dict]) -> list[dict]:
    result = []
    for item in errors:
        cleaned = {}
        for key, value in item.items():
            try:
                import json

                json.dumps(value)
                cleaned[key] = value
            except (TypeError, ValueError):
                cleaned[key] = str(value)
        result.append(cleaned)
    return result


def create_app(
    settings: Settings | None = None,
    var_dir: Path | str | None = None,
    start_services: bool = True,
) -> FastAPI:
    settings = settings or load_settings(var_dir)
    configure_logging(settings.logs_dir)
    ctx = AppContext(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_services:
            ctx.start()
        try:
            yield
        finally:
            ctx.stop()

    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    register_error_handlers(app)

    # ---- 健康检查 -------------------------------------------------------
    @app.get("/api/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "app": APP_NAME,
            "version": settings.app_version,
            "generation_id": ctx.generation_id,
            "schema_version": settings.schema_version,
        }

    # ---- 前端文案契约 ---------------------------------------------------
    @app.get("/api/meta")
    async def meta() -> dict:
        """界面用到的全部枚举文案与图标。

        前端不自己维护一份中文映射表——那样只要后端改一个措辞，
        界面就会和报告里的说法对不上。这里把唯一来源（schemas）原样吐出去。
        """
        from app.schemas import (
            COUNT_KEYS,
            COUNT_LABELS,
            JOB_STATUSES,
            JOB_STATUS_ICONS,
            JOB_STATUS_LABELS,
            OPERATION_TITLES,
            RESULT_STATUSES,
            RESULT_STATUS_ICONS,
            RESULT_STATUS_LABELS,
            STAGE_LABELS,
            STAGE_NAMES,
            TAG_CHOICE_LABELS,
            TAG_CHOICES,
            TAG_STATUS_LABELS,
            TAG_STATUSES,
        )
        from app.pdf import view_labels
        from app.services.jobs import DISCLAIMER

        return {
            "app_name": APP_NAME,
            "app_version": settings.app_version,
            "schema_version": settings.schema_version,
            "disclaimer": DISCLAIMER,
            "job_statuses": list(JOB_STATUSES),
            "job_status_labels": JOB_STATUS_LABELS,
            "job_status_icons": JOB_STATUS_ICONS,
            "stage_names": list(STAGE_NAMES),
            "stage_labels": STAGE_LABELS,
            "result_statuses": list(RESULT_STATUSES),
            "result_status_labels": RESULT_STATUS_LABELS,
            "result_status_icons": RESULT_STATUS_ICONS,
            "operation_titles": OPERATION_TITLES,
            "count_keys": list(COUNT_KEYS),
            "count_labels": COUNT_LABELS,
            "tag_statuses": list(TAG_STATUSES),
            "tag_status_labels": TAG_STATUS_LABELS,
            "tag_choices": list(TAG_CHOICES),
            "tag_choice_labels": TAG_CHOICE_LABELS,
            # 批量上限来自后端配置：界面提示的数字必须和真正生效的校验一致，
            # 否则用户按界面提示选 20 个却被告知"最多 10 个"。
            "max_batch_files": settings.limits.max_batch_files,
            # 初始视图三项的 Acrobat 口径文字。放在这里而不是前端各写一份：
            # 两份表迟早分叉，分叉的那天用户就又要"在 Acrobat 里对不上号"了。
            "view_labels": view_labels.meta_payload(),
        }

    # ---- 业务路由（后续阶段注册） ---------------------------------------
    from app.api import backups as backups_api
    from app.api import jobs as jobs_api

    app.include_router(jobs_api.router)
    app.include_router(backups_api.router)

    # ---- 静态资源 -------------------------------------------------------
    static_dir = settings.static_dir
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    return app
