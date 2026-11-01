# -*- coding: utf-8 -*-
"""
商品调度业务服务
统一管理首页推荐流与爆单推广商品库的数据聚合。
"""
from typing import Dict, Any, List, Optional
from app.mobile_web.adapters import get_adapter
from app.mobile_web.repositories.product_repo import ProductRepository
from app.mobile_web.config import logger


class ProductService:
    def __init__(self, product_repo: Optional[ProductRepository] = None):
        self.repo = product_repo or ProductRepository()

    def get_home_recommendations(
        self,
        channel_type: int = 5,
        offset: int = 0,
        limit: int = 20,
        uid: Optional[str] = None,
        platform: str = "pdd",
    ) -> Dict[str, Any]:
        """
        获取首页官方推荐榜单（当前主供多多进宝，未来支持多平台切换）
        """
        adapter = get_adapter(platform)
        return adapter.get_recommend_goods(
            channel_type=channel_type,
            offset=offset,
            limit=limit,
            uid=uid
        )

    def search_home_goods(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        uid: Optional[str] = None,
        platform: str = "pdd",
    ) -> Dict[str, Any]:
        """
        首页搜索联盟商品
        """
        adapter = get_adapter(platform)
        return adapter.search_goods(
            keyword=keyword,
            page=page,
            page_size=page_size,
            uid=uid
        )

    def get_boom_products(
        self,
        platform: str = "all",
        category: Optional[str] = None,
        keyword: Optional[str] = None,
        sort_by: str = "latest",
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        查询爆单推广库（数据库存量商品）
        """
        return self.repo.query_boom_products(
            platform=platform,
            category=category,
            keyword=keyword,
            sort_by=sort_by,
            page=page,
            page_size=page_size,
        )

    def get_boom_categories(self, platform: str = "all") -> List[str]:
        """
        获取爆单库分类列表
        """
        return self.repo.get_categories(platform=platform)
