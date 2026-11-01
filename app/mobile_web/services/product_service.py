# -*- coding: utf-8 -*-
"""
商品调度业务服务
统一管理首页推荐流（含内存级高性能秒级缓存）与爆单推广商品库的数据聚合。
"""
import time
from typing import Dict, Any, List, Optional, Tuple
from app.mobile_web.adapters import get_adapter
from app.mobile_web.repositories.product_repo import ProductRepository
from common.common_utils import setup_logger

logger = setup_logger(app_name="mobile_web")

# 首页官方推荐商品内存缓存：key -> (cache_timestamp, data_dict)
# 默认缓存 300 秒 (5分钟)，大幅提升首页打开速度，解决 10s 延迟痛点
_RECOMMEND_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
CACHE_TTL = 300


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
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        获取首页官方推荐榜单（带内存级毫秒缓存机制）
        """
        cache_key = f"{platform}_{channel_type}_{offset}_{limit}"
        now = time.time()

        # 检查并命中缓存（只有非强制刷新时）
        if not force_refresh and cache_key in _RECOMMEND_CACHE:
            ts, cached_res = _RECOMMEND_CACHE[cache_key]
            if (now - ts) < CACHE_TTL:
                logger.debug(f"[缓存命中] 首页推荐商品: {cache_key}")
                return cached_res

        adapter = get_adapter(platform)
        res = adapter.get_recommend_goods(
            channel_type=channel_type,
            offset=offset,
            limit=limit,
            uid=uid
        )

        if res.get("status") == "success" and res.get("data"):
            _RECOMMEND_CACHE[cache_key] = (now, res)

        return res

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
