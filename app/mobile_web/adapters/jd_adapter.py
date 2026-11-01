# -*- coding: utf-8 -*-
"""
京东联盟适配器 (标准化预留)
后续接入京东联盟京粉 API (jd.union.open.goods.query / jd.union.open.promotion.common.get)。
"""
from typing import Dict, Any, List, Optional
from app.mobile_web.adapters.base import BasePlatformAdapter
from app.mobile_web.config import JD_APP_KEY, JD_APP_SECRET, logger


class JdAdapter(BasePlatformAdapter):
    platform_code = "jd"
    platform_name = "京东"

    def __init__(self, app_key: str = JD_APP_KEY, app_secret: str = JD_APP_SECRET):
        self.app_key = app_key
        self.app_secret = app_secret

    def get_recommend_goods(
        self,
        channel_type: int = 1,
        offset: int = 0,
        limit: int = 20,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """京东京喜/好券/排行榜推荐（预留）"""
        return {
            "status": "success",
            "total_count": 0,
            "offset": offset,
            "limit": limit,
            "data": [],
            "msg": "京东推荐接口已就绪"
        }

    def search_goods(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """京东联盟商品搜索（预留）"""
        return {
            "status": "success",
            "total_count": 0,
            "page": page,
            "page_size": page_size,
            "data": [],
            "msg": "京东搜索接口已就绪"
        }

    def get_goods_detail(
        self,
        goods_id_or_sign: str,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """京东商品详情（预留）"""
        return {
            "status": "success",
            "data": {
                "platform": "jd",
                "platform_name": "京东",
                "goods_id": str(goods_id_or_sign),
                "name": "京东精选好物",
                "price": 88.0,
                "original_price": 108.0,
                "coupon_amount": 20.0,
                "has_coupon": True,
                "carousel_images": [],
                "detail_tags": ["京东自营", "次日达"],
                "desc": "品质保障 售后无忧",
            }
        }

    def convert_promotion_url(
        self,
        goods_id_or_sign: str,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        京东转链 (subUnionId 传递 uid)
        """
        return {
            "status": "success",
            "link_type": "京东推广链接",
            "url": f"https://u.jd.com/item?sku={goods_id_or_sign}&subUnionId={uid or ''}",
            "short_url": f"https://u.jd.com/item?sku={goods_id_or_sign}",
            "raw": {}
        }

    def check_authority(self, uid: str) -> Dict[str, Any]:
        """京东推客授权查询"""
        return {
            "status": "success",
            "is_authorized": False,
            "bind": 0,
            "msg": "京东推客授权接入中"
        }

    def generate_authority_url(self, uid: str, **kwargs) -> Dict[str, Any]:
        """京东推客授权链接"""
        return {
            "status": "success",
            "auth_url": "https://union.jd.com/",
            "short_url": "https://union.jd.com/",
            "msg": "京东授权功能已预留"
        }
