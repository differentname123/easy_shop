# -*- coding: utf-8 -*-
"""
淘宝联盟适配器 (标准化预留)
后续接入淘宝客 TOP API 或好单库转链接口时直接在此处扩充。
"""
from typing import Dict, Any, List, Optional
from app.mobile_web.adapters.base import BasePlatformAdapter
from app.mobile_web.config import TB_APP_KEY, TB_APP_SECRET, TB_PID, logger


class TaobaoAdapter(BasePlatformAdapter):
    platform_code = "taobao"
    platform_name = "淘宝"

    def __init__(self, app_key: str = TB_APP_KEY, app_secret: str = TB_APP_SECRET, pid: str = TB_PID):
        self.app_key = app_key
        self.app_secret = app_secret
        self.pid = pid

    def get_recommend_goods(
        self,
        channel_type: int = 1,
        offset: int = 0,
        limit: int = 20,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """淘宝推荐榜单（预留接入 taobao.tbk.dg.optimus.material）"""
        return {
            "status": "success",
            "total_count": 0,
            "offset": offset,
            "limit": limit,
            "data": [],
            "msg": "淘宝推荐接口已就绪，等待正式联盟物料ID配置"
        }

    def search_goods(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """淘宝商品搜索（预留接入 taobao.tbk.dg.material.optional）"""
        return {
            "status": "success",
            "total_count": 0,
            "page": page,
            "page_size": page_size,
            "data": [],
            "msg": "淘宝搜索接口已就绪"
        }

    def get_goods_detail(
        self,
        goods_id_or_sign: str,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """淘宝商品详情（预留）"""
        return {
            "status": "error",
            "error_msg": "淘宝官方商品详情待配置正式联盟物料接口",
            "data": None
        }

    def convert_promotion_url(
        self,
        goods_id_or_sign: str,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        淘宝高佣转链
        后续结合 relation_id / special_id 或好单库转链 API
        """
        return {
            "status": "success",
            "link_type": "淘宝淘口令",
            "url": f"https://s.click.taobao.com/t?item={goods_id_or_sign}",
            "short_url": f"https://s.click.taobao.com/t?item={goods_id_or_sign}",
            "tao_token": "￥(淘口令待接入)￥",
            "raw": {}
        }

    def check_authority(self, uid: str) -> Dict[str, Any]:
        """淘宝渠道/会员关系授权查询 (taobao.tbk.sc.publisher.info.get)"""
        return {
            "status": "success",
            "is_authorized": False,
            "bind": 0,
            "msg": "淘宝渠道关系授权接入中"
        }

    def generate_authority_url(self, uid: str, **kwargs) -> Dict[str, Any]:
        """淘宝渠道备案邀请淘口令/H5链接 (taobao.tbk.sc.invitecode.get)"""
        return {
            "status": "success",
            "auth_url": "https://pub.alimama.com/",
            "short_url": "https://pub.alimama.com/",
            "msg": "淘宝授权功能已预留"
        }

    def fetch_increment_orders(
        self,
        start_time: int,
        end_time: int,
        page: int = 1,
        page_size: int = 50,
        **kwargs
    ) -> Dict[str, Any]:
        """淘宝订单增量拉取（预留接入 taobao.tbk.order.details.get）"""
        return {
            "status": "success",
            "order_list": [],
            "total_count": 0,
            "msg": "淘宝订单增量接口待配置"
        }
