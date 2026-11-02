# -*- coding: utf-8 -*-
"""
商品调度业务服务
统一管理首页推荐流、关键词搜索（带安全有界缓存）与爆单推广商品库的数据聚合。
"""
import time
from typing import Dict, Any, List, Optional
from app.mobile_web.adapters import get_adapter
from app.mobile_web.repositories.product_repo import ProductRepository
from app.mobile_web.cache_utils import ThreadSafeTTLCache
from app.mobile_web.config import (
    CACHE_MAX_RECOMMEND,
    CACHE_MAX_DETAIL,
    CACHE_MAX_SEARCH,
    CACHE_MAX_CATEGORY,
    logger,
)

# 线程安全、有界容量的 LRU+TTL 内存缓存容器
_RECOMMEND_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_RECOMMEND, ttl=300)   # 首页推荐流缓存 5 分钟
_DETAIL_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_DETAIL, ttl=600)         # 商品详情缓存 10 分钟
_SEARCH_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_SEARCH, ttl=180)         # 关键词搜索缓存 3 分钟
_CATEGORY_CACHE = ThreadSafeTTLCache(maxsize=CACHE_MAX_CATEGORY, ttl=600)     # 爆单库类目缓存 10 分钟


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
        获取首页官方推荐榜单（带内存级有界缓存），并自动将官方标品沉淀入库
        """
        # 缓存 Key 不包含 uid，全平台共享同一份标品榜单缓存
        cache_key = f"{platform}_{channel_type}_{offset}_{limit}"

        if not force_refresh:
            cached_res = _RECOMMEND_CACHE.get(cache_key)
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
            _RECOMMEND_CACHE.set(cache_key, res)
            # 首页官方标品自动入库沉淀，内部显式标记 need_reformat=False，阻止后台大模型清洗
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
        首页搜索联盟商品（带内存级有界缓存）
        【关键重构】：
        1. 关键词截断至前 30 字符；
        2. 缓存 Key 移除 uid，全站用户共享搜索缓存，大幅提升命中率并降低外部 API 消耗；
        3. 彻底删除用户搜索结果自动写库逻辑，防止长尾冷门词污染爆单库并触发大模型额外费用。
        """
        norm_kw = (keyword or "").strip()[:30].lower()
        cache_key = f"{platform}_{norm_kw}_{page}_{page_size}"

        cached_res = _SEARCH_CACHE.get(cache_key)
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
            _SEARCH_CACHE.set(cache_key, res)
            # 用户搜索结果仅在内存缓存中服务前端，绝不落库污染 products 集合与触发 LLM 费用
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
        2. 查本地数据库 (若已存在且包含轮播图等深度详情)
        3. 调官方平台 API 拉取最新详情
        4. 获取成功后自动沉淀入库并更新内存缓存
        """
        goods_key = str(goods_id_or_sign).strip()[:64]

        # 1. 内存高速缓存 (仅当包含多张轮播图时直接返回)
        cached_res = _DETAIL_CACHE.get(goods_key)
        if cached_res is not None:
            c_imgs = cached_res.get("data", {}).get("carousel_images", [])
            if len(c_imgs) > 1:
                logger.debug(f"[内存缓存命中] 商品详情: {goods_key}")
                return cached_res

        # 2. 本地数据库检索：若本地已有全量高清轮播图（> 1 张），直接返回
        db_item = self.repo.get_product_detail(goods_key)
        if db_item and db_item.get("carousel_images") and len(db_item.get("carousel_images", [])) > 1:
            ret = {"status": "success", "data": db_item, "source": "database"}
            _DETAIL_CACHE.set(goods_key, ret)
            return ret

        # 3. 调官方 API 拉取
        adapter = get_adapter(platform)
        res = adapter.get_goods_detail(goods_id_or_sign=goods_id_or_sign, uid=uid)
        if res.get("status") == "success" and res.get("data"):
            try:
                self.repo.upsert_product_detail(res["data"])
            except Exception as e:
                logger.warning(f"商品详情入库忽略异常: {e}")
            _DETAIL_CACHE.set(goods_key, res)
            return res

        # 4. 兜底：若官方 API 失败或限流，返回数据库已有基础数据
        if db_item:
            ret = {"status": "success", "data": db_item, "source": "database_fallback"}
            _DETAIL_CACHE.set(goods_key, ret)
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
        获取爆单库分类列表（带 10 分钟内存有界缓存，避免高频对 MongoDB 集合执行全表 distinct）
        """
        plat = (platform or "all").lower()
        cached = _CATEGORY_CACHE.get(plat)
        if cached is not None:
            return cached

        cats = self.repo.get_categories(platform=platform)
        if cats:
            _CATEGORY_CACHE.set(plat, cats)
        return cats
