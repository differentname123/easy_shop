# -*- coding: utf-8 -*-
"""移动端服务装配入口：HTTP 基础设施、路由与静态页面。"""
import os
import sys
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
import uvicorn

from app.mobile_web.config import WEB_HOST, WEB_PORT, logger
from app.mobile_web.adapters import get_adapter, get_all_platforms, PlatformUnavailableError
from app.mobile_web.api.home import router as home_router
from app.mobile_web.api.goods import router as goods_router
from app.mobile_web.api.user import router as user_router
from app.mobile_web.api.promote import router as promote_router
from app.mobile_web.api.order import router as order_router

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    def warm_connections():
        for platform in get_all_platforms():
            if platform["active"]:
                try:
                    get_adapter(platform["code"]).warmup()
                except Exception as exc:
                    logger.debug(f"[{platform['code']}] 连接预热失败: {exc}")
        try:
            from app.mobile_web.api.home import product_service
            product_service.get_home_recommendations(channel_type=5, offset=0, limit=20)
            logger.info("[lifespan] 首页默认推荐商品与缓存预热就绪")
        except Exception as exc:
            logger.warning(f"[lifespan] 首页推荐预热异常: {exc}")
    threading.Thread(target=warm_connections, name="platform-warmup", daemon=True).start()
    yield


def _request_path(request) -> str:
    """保留中文查询日志可读性，但不记录兼容查询参数中的认证凭据。"""
    sensitive = {"token", "auth_token", "access_token", "password", "authorization"}
    pairs = urllib.parse.parse_qsl(request.url.query, keep_blank_values=True)
    query = "&".join(f"{key}={'[REDACTED]' if key.lower() in sensitive else value}" for key, value in pairs)
    return (request.url.path + ("?" + query if query else "")).replace("\n", "\\n").replace("\r", "\\r")


def _html_page(filename: str, fallback: str):
    path = os.path.join(STATIC_DIR, filename)
    if os.path.isfile(path):
        return FileResponse(path, media_type="text/html")
    return HTMLResponse(f"<h2>{fallback}</h2>")


def create_app() -> FastAPI:
    application = FastAPI(
        title="移动端电商导购与返利服务",
        description="官方推荐、爆单库、推手授权与带 UID 佣金转链",
        version="2.0.0",
        lifespan=lifespan,
    )
    application.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                               allow_methods=["*"], allow_headers=["*"])
    application.add_middleware(GZipMiddleware, minimum_size=500)

    @application.exception_handler(PlatformUnavailableError)
    async def unavailable_platform(request, exc):
        return JSONResponse(status_code=400, content={
            "status": "error", "error_code": "platform_unavailable", "error_msg": str(exc),
        })

    @application.middleware("http")
    async def request_log_middleware(request, call_next):
        started = time.perf_counter()
        forwarded = request.headers.get("x-forwarded-for")
        client = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "127.0.0.1")
        response = await call_next(request)
        duration = (time.perf_counter() - started) * 1000
        logger.info(f"[{client}] {request.method} {_request_path(request)} -> {response.status_code} ({duration:.1f}ms)")
        return response

    for router in (home_router, goods_router, user_router, promote_router, order_router):
        application.include_router(router)
    if os.path.isdir(STATIC_DIR):
        application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.get("/favicon.ico")
    def favicon():
        return Response(content=b"", media_type="image/x-icon")

    @application.get("/", response_class=HTMLResponse)
    def index_page():
        return _html_page("index.html", "移动端页面正在构建中...")

    @application.get("/detail", response_class=HTMLResponse)
    def detail_page():
        return _html_page("detail.html", "商品详情页构建中...")

    @application.get("/health")
    def health_check():
        return {"status": "ok", "service": "mobile_web_service"}

    return application


app = create_app()

if __name__ == "__main__":
    logger.info(f"启动移动端导购与返利服务: http://{WEB_HOST}:{WEB_PORT}")
    uvicorn.run("app.mobile_web.main:app", host=WEB_HOST, port=WEB_PORT, reload=True, access_log=False)
