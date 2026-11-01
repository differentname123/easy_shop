# -*- coding: utf-8 -*-
"""
多平台联盟抽象适配器基类
定义统一的导购商品查询、搜索、推广生链、推手授权备案协议。
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional


class BasePlatformAdapter(ABC):
    """
    电商导购联盟适配器抽象基类
    """
    platform_code: str = ""
    platform_name: str = ""

    @abstractmethod
    def get_recommend_goods(
        self,
        channel_type: int = 5,
        offset: int = 0,
        limit: int = 20,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        获取官方推荐/热销榜单商品列表
        :param channel_type: 推荐频道代码
        :param offset: 分页偏移量
        :param limit: 每页数量
        :param uid: 推手/用户唯一UID
        :return: {"status": "success", "total_count": int, "data": List[Dict]}
        """
        pass

    @abstractmethod
    def search_goods(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        根据关键词搜索联盟商品
        :param keyword: 关键词
        :param page: 页码
        :param page_size: 每页条数
        :param uid: 推手/用户唯一UID
        :return: {"status": "success", "data": List[Dict]}
        """
        pass

    @abstractmethod
    def convert_promotion_url(
        self,
        goods_id_or_sign: str,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        根据商品生成附带推手 UID 的推广转链
        :param goods_id_or_sign: 商品ID或加密商品Sign
        :param uid: 用户唯一识别码（用于后续订单归因与返利计算）
        :return: {"status": "success", "url": str, "short_url": str, "mobile_url": str, "we_app_info": dict, "raw": dict}
        """
        pass

    @abstractmethod
    def check_authority(self, uid: str) -> Dict[str, Any]:
        """
        检查指定用户在该平台的推广位备案/授权状态
        :param uid: 用户唯一识别码
        :return: {"status": "success", "is_authorized": bool, "bind": int}
        """
        pass

    @abstractmethod
    def generate_authority_url(self, uid: str, **kwargs) -> Dict[str, Any]:
        """
        生成该用户在该平台专属的备案授权链接
        :param uid: 用户唯一识别码
        :return: {"status": "success", "auth_url": str, "short_url": str, "we_app_info": dict}
        """
        pass
