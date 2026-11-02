# -*- coding: utf-8 -*-
"""
适配器注册与获取工厂
"""
from typing import Dict
from app.mobile_web.adapters.base import BasePlatformAdapter
from app.mobile_web.adapters.pdd_adapter import PddAdapter
from app.mobile_web.adapters.tb_adapter import TaobaoAdapter
from app.mobile_web.adapters.jd_adapter import JdAdapter

_ADAPTERS: Dict[str, BasePlatformAdapter] = {
    "pdd": PddAdapter(),
    "tb": TaobaoAdapter(),
    "taobao": TaobaoAdapter(),
    "jd": JdAdapter(),
}


def get_adapter(platform: str) -> BasePlatformAdapter:
    """获取指定平台的适配器，未知平台默认回退到 pdd"""
    plat = (platform or "pdd").lower()
    adapter = _ADAPTERS.get(plat)
    if not adapter:
        raise ValueError(f"不支持的平台: {platform}")
    return adapter


def get_all_platforms():
    """获取当前已支持/已注册的平台列表"""
    return [
        {"code": "pdd", "name": "拼多多", "active": True},
        {"code": "taobao", "name": "淘宝", "active": True},
        {"code": "jd", "name": "京东", "active": True},
    ]
