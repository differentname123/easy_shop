# -*- coding: utf-8 -*-
"""
移动端电商导购与多平台返利微服务入口
针对手机端及微信小程序 web-view 优化
"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn

from app.mobile_web.config import WEB_HOST, WEB_PORT, logger
from app.mobile_web.api.home import router as home_router
from app.mobile_web.api.goods import router as goods_router
from app.mobile_web.api.user import router as user_router
from app.mobile_web.api.promote import router as promote_router

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(
    title="移动端电商导购与返利服务",
    description="支持拼多多进宝官方推荐、爆单库商品、多平台推手渠道备案授权及带UID佣金转链",
    version="2.0.0"
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

# 注册 API 路由
app.include_router(home_router)
app.include_router(goods_router)
app.include_router(user_router)
app.include_router(promote_router)

# 挂载静态资源
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index_page():
    """移动端单页应用入口（直接适配微信小程序 web-view 嵌入）"""
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h2>移动端页面正在构建中...</h2>")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "mobile_web_service"}


if __name__ == "__main__":
    logger.info(f"启动移动端导购与返利服务: http://{WEB_HOST}:{WEB_PORT}")
    uvicorn.run("app.mobile_web.main:app", host=WEB_HOST, port=WEB_PORT, reload=False)
