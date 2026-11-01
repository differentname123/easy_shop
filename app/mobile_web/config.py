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

# Web 服务配置
WEB_HOST = "0.0.0.0"
WEB_PORT = 8088
