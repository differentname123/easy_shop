# -*- coding: utf-8 -*-
"""
移动端服务全局配置模块
"""
import os
from common.common_utils import get_config, setup_logger

logger = setup_logger(app_name="mobile_web_service")

def safe_get_config(key: str, default: str = "") -> str:
    try:
        val = get_config(key)
        return val if val is not None else default
    except KeyError:
        return default

# 拼多多进宝配置 (默认使用 myself 账号组)
PDD_CLIENT_ID = safe_get_config("myself_pdd_client_id")
PDD_CLIENT_SECRET = safe_get_config("myself_pdd_client_secret")
PDD_PID = safe_get_config("myself_pdd_pid")

# 淘宝客配置 (预留)
TB_APP_KEY = safe_get_config("myself_taobao_app_key")
TB_APP_SECRET = safe_get_config("myself_taobao_app_secret")
TB_PID = safe_get_config("myself_taobao_pid")

# 京东联盟配置 (预留)
JD_APP_KEY = safe_get_config("jd_app_key")
JD_APP_SECRET = safe_get_config("jd_app_secret")

# MongoDB 配置
MONGO_HOST = get_config("local_mongo_host") or "localhost"
MONGO_PORT = int(get_config("local_mongo_port") or 27017)
MONGO_USER = get_config("local_mongo_user") or ""
MONGO_PASSWORD = get_config("local_mongo_password") or ""
MONGO_DB_NAME = get_config("local_mongo_db_name") or "crawler_data"

# Web 服务与安全配置
WEB_HOST = "127.0.0.1"
WEB_PORT = 8000
AUTH_SECRET = os.environ.get("WEB_AUTH_SECRET") or safe_get_config("web_auth_secret", "easy_shop_secure_auth_secret_key_2026_v2")

# 业务安全与防刷约束常量
ORDER_SYNC_INTERVAL = 180       # 增量订单拉取全局最小冷却间隔 (秒)
ORDER_SYNC_MAX_PAGES = 5        # 单次增量拉单最大翻页数
CACHE_MAX_RECOMMEND = 256       # 首页推荐流缓存最大条目
CACHE_MAX_DETAIL = 2048         # 商品详情缓存最大条目
CACHE_MAX_SEARCH = 1024         # 搜索缓存最大条目
CACHE_MAX_PROMOTION = 4096      # 推手专属转链缓存最大条目
CACHE_MAX_CATEGORY = 32         # 类目缓存最大条目

