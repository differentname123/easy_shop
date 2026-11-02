# -*- coding: utf-8 -*-
"""
商品调度业务服务
统一管理首页推荐流、关键词搜索（带安全有界缓存）与爆单推广商品库的数据聚合。
"""
from typing import Dict, Any, List, Optional
from app.mobile_web.adapters import get_adapter
from app.mobile_web.repositories.product_repo import ProductRepository
from app.mobile_web.cache import (
    RECOMMEND_CACHE,
    DETAIL_CACHE,
    SEARCH_CACHE,
    CATEGORY_CACHE,
)
from app.mobile_web.config import logger

# 模块级向后兼容别名（供单测与依赖方直接访问）
_RECOMMEND_CACHE = RECOMMEND_CACHE
_DETAIL_CACHE = DETAIL_CACHE
_SEARCH_CACHE = SEARCH_CACHE
_CATEGORY_CACHE = CATEGORY_CACHE


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
        获取首页官方推荐榜单（全站共享缓存），并沉淀官方标品入库
        """
        cache_key = f"{platform}_{channel_type}_{offset}_{limit}"

        if not force_refresh:
            cached_res = RECOMMEND_CACHE.get(cache_key)
            if cached_res is not None:
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
            RECOMMEND_CACHE.set(cache_key, res)
            try:
                self.repo.upsert_home_products(res["data"], channel_type=channel_type)
            except Exception as e:
                logger.warning(f"首页商品自动入库忽略异常: {e}")

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
        首页搜索联盟商品（共享有界缓存，不入库污染爆单库）
        """
        norm_kw = (keyword or "").strip()[:30].lower()
        cache_key = f"{platform}_{norm_kw}_{page}_{page_size}"

        cached_res = SEARCH_CACHE.get(cache_key)
        if cached_res is not None:
            logger.debug(f"[缓存命中] 首页搜索商品: {cache_key}")
            return cached_res

        adapter = get_adapter(platform)
        res = adapter.search_goods(
            keyword=norm_kw,
            page=page,
            page_size=page_size,
            uid=uid
        )
        if res.get("status") == "success" and res.get("data"):
            SEARCH_CACHE.set(cache_key, res)

        return res

    def get_goods_detail(
        self,
        platform: str = "pdd",
        goods_id_or_sign: str = "",
        uid: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取官方商品完整详情：
        1. 优先查内存有界缓存
        2. 查本地数据库 (若已存在全量多图直接秒开)
        3. 调官方平台 API 拉取最新详情并入库
        4. 兜底返回数据库已有基础数据
        """
        goods_key = str(goods_id_or_sign).strip()[:64]

        # 1. 内存高速缓存
        cached_res = DETAIL_CACHE.get(goods_key)
        if cached_res is not None:
            c_imgs = cached_res.get("data", {}).get("carousel_images", [])
            if len(c_imgs) > 1:
                logger.debug(f"[内存缓存命中] 商品详情: {goods_key}")
                return cached_res

        # 2. 本地数据库检索
        db_item = self.repo.get_product_detail(goods_key)
        if db_item and db_item.get("carousel_images") and len(db_item.get("carousel_images", [])) > 1:
            ret = {"status": "success", "data": db_item, "source": "database"}
            DETAIL_CACHE.set(goods_key, ret)
            return ret

        # 3. 调官方 API 拉取
        adapter = get_adapter(platform)
        res = adapter.get_goods_detail(goods_id_or_sign=goods_id_or_sign, uid=uid)
        if res.get("status") == "success" and res.get("data"):
            try:
                self.repo.upsert_product_detail(res["data"])
            except Exception as e:
                logger.warning(f"商品详情入库忽略异常: {e}")
            DETAIL_CACHE.set(goods_key, res)
            return res

        # 4. 兜底
        if db_item:
            ret = {"status": "success", "data": db_item, "source": "database_fallback"}
            DETAIL_CACHE.set(goods_key, ret)
            return ret

        return res

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
        查询爆单推广库商品
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
        获取爆单库分类列表（带 10 分钟内存有界缓存）
        """
        plat = (platform or "all").lower()
        cached = CATEGORY_CACHE.get(plat)
        if cached is not None:
            return cached

        cats = self.repo.get_categories(platform=platform)
        if cats:
            CATEGORY_CACHE.set(plat, cats)
        return cats
