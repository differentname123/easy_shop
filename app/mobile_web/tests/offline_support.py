# -*- coding: utf-8 -*-
"""测试/预览专用进程隔离；必须在导入应用之前安装，生产入口不得使用。"""
import logging
import os
from unittest.mock import patch

BLOCKED_CALLS = []
_PATCHES = []
_CONFIG = {
    "myself_pdd_client_id": "offline-client",
    "myself_pdd_client_secret": "offline-secret",
    "myself_pdd_pid": "offline-pid",
    "local_mongo_host": "localhost",
    "local_mongo_port": 27017,
    "local_mongo_user": "",
    "local_mongo_password": "",
    "local_mongo_db_name": "mobile_web_offline",
    "web_auth_secret": "mobile-web-offline-test-secret-not-for-deployment",
}


def _logger(*args, app_name="offline", **kwargs):
    logger = logging.getLogger(f"mobile_web.offline.{app_name}")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def _deny(resource):
    def blocked(*args, **kwargs):
        BLOCKED_CALLS.append(resource)
        raise AssertionError(f"离线验证禁止访问真实资源: {resource}")
    return blocked


def assert_no_blocked_calls():
    assert BLOCKED_CALLS == [], f"离线验证曾尝试访问真实资源: {BLOCKED_CALLS}"


def install_offline_environment():
    """覆盖导入副作用及外部资源出口；只在此测试进程生效。"""
    if _PATCHES:
        return
    replacements = (
        patch("common.common_utils.get_config", side_effect=lambda key: _CONFIG.get(key, "")),
        patch("common.common_utils.setup_logger", side_effect=_logger),
        patch.dict(os.environ, {"WEB_AUTH_SECRET": _CONFIG["web_auth_secret"]}),
        patch("pymongo.MongoClient", new=_deny("MongoClient")),
        patch("requests.sessions.Session.request", new=_deny("platform HTTP")),
    )
    for replacement in replacements:
        replacement.start()
        _PATCHES.append(replacement)
