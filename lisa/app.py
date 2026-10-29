import asyncio
import logging
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from lisa import (
    account_deletion,
    auth,
    characters,
    contact_policy,
    group_records,
    images,
    life_plans,
    memory,
    onboarding,
    operations,
    retrieval_config,
    scheduling,
    shared,
    sharing,
    style_corpus,
    style_distillation,
    style_profiles,
    works,
)
from lisa.db import make_engine
from lisa.runtime_status import ApiMeasurements, cleanup_observations, process_pulse
from lisa.settings import ROOT, Settings

logger = logging.getLogger("lisa.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings)
    measurements = ApiMeasurements()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with process_pulse(engine, settings, ["api"], details=measurements.snapshot):
            await asyncio.to_thread(
                cleanup_observations, engine, settings.operations_retention_days
            )
            yield
        engine.dispose()

    app = FastAPI(title="LISA", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid4().hex
        started = perf_counter()
        status = 500
        try:
            if (
                request.url.path.startswith("/api/")
                and request.method not in {"GET", "HEAD", "OPTIONS"}
                and request.headers.get("X-LISA-Request") != "1"
            ):
                response = JSONResponse({"detail": "请通过 LISA 页面提交操作。"}, status_code=403)
            else:
                response = await call_next(request)
            status = response.status_code
        finally:
            measurements.record(status, (perf_counter() - started) * 1000)
            # Only a registered route template, never URL values, tokens or private bodies.
            route = getattr(request.scope.get("route"), "path", "unmatched")
            logger.info(
                "request=%s method=%s route=%s status=%s duration_ms=%.1f",
                request.state.request_id,
                request.method,
                route,
                status,
                (perf_counter() - started) * 1000,
            )
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        logger.error("request=%s database_error=%s", request.state.request_id, type(exc).__name__)
        return JSONResponse(
            {
                "detail": "服务暂时无法保存或读取数据，请稍后重试。",
                "request_id": request.state.request_id,
            },
            status_code=503,
        )

    @app.get("/api/health")
    def health():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(operations.router)
    app.include_router(account_deletion.router)
    app.include_router(onboarding.router)
    app.include_router(works.router)
    app.include_router(images.router)
    app.include_router(sharing.router)
    app.include_router(memory.router)
    app.include_router(life_plans.router)
    app.include_router(shared.router)
    app.include_router(scheduling.router)
    app.include_router(contact_policy.router)
    app.include_router(retrieval_config.router)
    app.include_router(characters.router)
    app.include_router(style_corpus.router)
    app.include_router(style_profiles.router)
    app.include_router(style_distillation.router)
    app.include_router(group_records.router)

    @app.get("/api/service-status")
    def service_status(user=Depends(auth.current_user)):
        if (
            not (settings.chat_enabled or settings.chat_test_enabled)
            or not settings.model_api_key.get_secret_value()
        ):
            return {"status": "unavailable", "message": "正常对话尚未开放，绑定与性格设置已保存。"}
        return {
            "status": "ready",
            "message": (
                "正常私聊已开放。请用已绑定 QQ 直接发送消息。"
                "图片、语音等内容暂无法读取，请用文字说明。"
                if settings.chat_enabled
                else "对话测试已开放。请用已绑定 QQ 发送“测试对话 你想说的话”。"
                "初始化前的消息不会自动补答。"
            ),
        }

    web_dist = ROOT / "runtime" / "dist"
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app
