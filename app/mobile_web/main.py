# -*- coding: utf-8 -*-
"""
移动端电商导购与多平台返利微服务入口
针对手机端及微信小程序 web-view 优化
"""
import os
import sys
import time
import urllib.parse

# 确保项目根目录在 sys.path 中
_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn

from contextlib import asynccontextmanager
from app.mobile_web.config import WEB_HOST, WEB_PORT, logger
from app.mobile_web.api.home import router as home_router
from app.mobile_web.api.goods import router as goods_router
from app.mobile_web.api.user import router as user_router
from app.mobile_web.api.promote import router as promote_router
from app.mobile_web.api.order import router as order_router

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务生命周期管理：启动时在后台预热拼多多网关 HTTP 连接池，消除用户首次访问的 TCP/TLS 握手延迟"""
    import threading
    def _warm():
        try:
            from app.pdd_utils import _PDD_SESSION
            _PDD_SESSION.head("https://gw-api.pinduoduo.com", timeout=3.0)
            logger.info("[网关连接池] 拼多多 HTTP 连接池预热成功，随时响应极速请求")
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()
    yield


app = FastAPI(
    title="移动端电商导购与返利服务",
    description="支持拼多多进宝官方推荐、爆单库商品、多平台推手渠道备案授权及带UID佣金转链",
    version="2.0.0",
    lifespan=lifespan
)

# 允许跨域（方便微信环境、小程序 web-view 及本地开发调试）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 开启 GZip 压缩
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def request_log_middleware(request, call_next):
    """
    全局请求日志中间件：
    1. 自动对 URL query 解码（避免中文参数被 raw urlencoded 显示为乱码）
    2. 统计请求毫秒级耗时，排查接口性能瓶颈
    3. 统一使用 setup_logger 格式，自动添加标准时间戳并持久化写入 logs/mobile_web_service.log
    """
    start_time = time.time()
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        client_ip = x_forwarded_for.split(",")[0].strip()
    elif request.client:
        client_ip = request.client.host
    else:
        client_ip = "127.0.0.1"

    raw_query = request.url.query
    unquoted_query = urllib.parse.unquote(raw_query) if raw_query else ""
    full_path = request.url.path + (f"?{unquoted_query}" if unquoted_query else "")

    response = await call_next(request)
    duration_ms = (time.time() - start_time) * 1000

    # 持久化记录到日志系统
    logger.info(f"[{client_ip}] {request.method} {full_path} -> {response.status_code} ({duration_ms:.1f}ms)")
    return response


# 注册 API 路由
app.include_router(home_router)
app.include_router(goods_router)
app.include_router(user_router)
app.include_router(promote_router)
app.include_router(order_router)

# 挂载静态资源
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")




@app.get("/favicon.ico")
def favicon():
    """静音处理浏览器默认 favicon 请求，避免 404 刷屏"""
    return Response(content=b"", media_type="image/x-icon")


@app.get("/", response_class=HTMLResponse)
def index_page():
    """移动端单页应用入口（直接适配微信小程序 web-view 嵌入）"""
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h2>移动端页面正在构建中...</h2>")


@app.get("/detail", response_class=HTMLResponse)
def detail_page():
    """移动端独立商品详情页（全屏大屏展示全量图文、参数及专属推手生链）"""
    detail_file = os.path.join(STATIC_DIR, "detail.html")
    if os.path.exists(detail_file):
        with open(detail_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h2>商品详情页构建中...</h2>")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "mobile_web_service"}


if __name__ == "__main__":
    logger.info(f"启动移动端导购与返利服务: http://{WEB_HOST}:{WEB_PORT}")
    # 关闭 uvicorn 默认无时间戳且未解码的 access_log，由全局中间件统一接管并持久化
    uvicorn.run("app.mobile_web.main:app", host=WEB_HOST, port=WEB_PORT, reload=True, access_log=False)


