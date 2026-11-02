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
        # 深分页防穿透：当 offset 达到平台上限（如400）时，返回标准结构而非报错
        if offset >= 400:
            return {
                "status": "success",
                "total_count": 400,
                "offset": offset,
                "limit": limit,
                "has_more": False,
                "data": [],
            }

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

        if res.get("status") == "success":
            data_list = res.get("data", [])
            if "has_more" not in res:
                res["has_more"] = len(data_list) >= limit and (offset + limit < 400)
            if data_list:
                RECOMMEND_CACHE.set(cache_key, res)
                try:
                    self.repo.upsert_home_products(data_list, channel_type=channel_type)
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
        goods_id: Optional[str] = None,
        goods_sign: Optional[str] = None,
        uid: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        获取官方商品完整详情：
        1. 双键内存有界缓存 (goods_id 与 goods_sign)，任意命中均秒级响应
        2. 查本地数据库 (若已存在全量多图直接秒开)
        3. 调官方平台 API 拉取最新详情并多键写入缓存与持久化库
        4. 兜底返回数据库已有基础数据
        """
        gid = str(goods_id).strip() if goods_id else ""
        gsign = str(goods_sign).strip() if goods_sign else ""
        raw_key = str(goods_id_or_sign).strip() if goods_id_or_sign else ""

        if not gid and raw_key and raw_key.isdigit():
            gid = raw_key
        if not gsign and raw_key and not raw_key.isdigit():
            gsign = raw_key

        probe_keys = [k for k in (gsign, gid, raw_key) if k]

        # 1. 内存高速缓存查询（支持双键）
        for k in probe_keys:
            for cache_key in (k, f"{platform}_{k}"):
                cached_res = DETAIL_CACHE.get(cache_key)
                if cached_res is not None:
                    c_imgs = cached_res.get("data", {}).get("carousel_images", [])
                    if len(c_imgs) > 1 or cached_res.get("data", {}).get("goods_id"):
                        logger.debug(f"[内存缓存命中] 商品详情: {cache_key}")
                        return cached_res

        # 2. 本地数据库检索（支持 goods_id 与 goods_sign 双键）
        db_item = None
        for k in probe_keys:
            db_item = self.repo.get_product_detail(k)
            if db_item:
                break

        if db_item and db_item.get("carousel_images") and len(db_item.get("carousel_images", [])) > 1:
            ret = {"status": "success", "data": db_item, "source": "database"}
            self._cache_detail_dual_keys(platform, ret, gsign, gid, raw_key)
            return ret

        # 3. 调官方平台适配器拉取
        adapter = get_adapter(platform)
        primary_key = gsign or gid or raw_key
        res = adapter.get_goods_detail(
            goods_id_or_sign=primary_key,
            goods_id=gid or None,
            goods_sign=gsign or None,
            uid=uid,
            **kwargs
        )
        if res.get("status") == "success" and res.get("data"):
            try:
                self.repo.upsert_product_detail(res["data"])
            except Exception as e:
                logger.warning(f"商品详情入库忽略异常: {e}")
            ret_data = res["data"]
            self._cache_detail_dual_keys(
                platform,
                res,
                gsign,
                gid,
                raw_key,
                ret_data.get("goods_sign"),
                ret_data.get("goods_id"),
            )
            return res

        # 4. 降级兜底
        if db_item:
            ret = {"status": "success", "data": db_item, "source": "database_fallback"}
            self._cache_detail_dual_keys(platform, ret, gsign, gid, raw_key)
            return ret

        return res

    def _cache_detail_dual_keys(self, platform: str, res: dict, *keys):
        """统一对所有关联标识写入双键缓存"""
        seen = set()
        for k in keys:
            if k and str(k).strip():
                clean_k = str(k).strip()
                for ckey in (clean_k, f"{platform}_{clean_k}"):
                    if ckey not in seen:
                        DETAIL_CACHE.set(ckey, res)
                        seen.add(ckey)

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
