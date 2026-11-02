# -*- coding: utf-8 -*-
"""
多平台适配器注册与获取工厂
"""
from typing import Dict, List, Any
from app.mobile_web.adapters.base import BasePlatformAdapter
from app.mobile_web.adapters.pdd_adapter import PddAdapter
from app.mobile_web.adapters.tb_adapter import TaobaoAdapter
from app.mobile_web.adapters.jd_adapter import JdAdapter

# 单例注册表（支持惰性获取与扩展注册）
_ADAPTER_REGISTRY: Dict[str, BasePlatformAdapter] = {}


def register_adapter(platform_code: str, adapter_instance: BasePlatformAdapter):
    """动态注册或覆盖平台适配器"""
    _ADAPTER_REGISTRY[platform_code.lower()] = adapter_instance


def get_adapter(platform: str) -> BasePlatformAdapter:
    """获取指定平台的适配器，未知平台回退到 pdd，支持快速缓存"""
    plat = (platform or "pdd").lower()
    if plat not in _ADAPTER_REGISTRY:
        if plat in ("tb", "taobao"):
            _ADAPTER_REGISTRY[plat] = TaobaoAdapter()
        elif plat == "jd":
            _ADAPTER_REGISTRY[plat] = JdAdapter()
        elif plat == "pdd":
            _ADAPTER_REGISTRY[plat] = PddAdapter()
        else:
            raise ValueError(f"不支持的平台: {platform}")

    return _ADAPTER_REGISTRY[plat]


def get_all_platforms() -> List[Dict[str, Any]]:
    """获取当前已支持/已注册的平台元信息列表"""
    return [
        {"code": "pdd", "name": "拼多多", "active": True},
        {"code": "taobao", "name": "淘宝", "active": True},
        {"code": "jd", "name": "京东", "active": True},
    ]
