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

# 商品详情内存缓存：goods_key -> (cache_timestamp, data_dict)
_DETAIL_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
DETAIL_CACHE_TTL = 600


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
        获取首页官方推荐榜单（带内存级毫秒缓存机制），并自动将商品入库/更新
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
            # 【核心功能】：首页商品自动批量入库沉淀，保证价格优惠数据最新
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
        首页搜索联盟商品，并自动入库
        """
        adapter = get_adapter(platform)
        res = adapter.search_goods(
            keyword=keyword,
            page=page,
            page_size=page_size,
            uid=uid
        )
        if res.get("status") == "success" and res.get("data"):
            try:
                self.repo.upsert_home_products(res["data"])
            except Exception as e:
                logger.warning(f"搜索商品自动入库忽略异常: {e}")
        return res

    def get_goods_detail(
        self,
        platform: str = "pdd",
        goods_id_or_sign: str = "",
        uid: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取官方商品完整详情：
        1. 优先查内存缓存 (10分钟内 0ms 瞬间返回)
        2. 查本地数据库 (如果已入库过且包含轮播图 specs 等深度详情)
        3. 调官方平台 API 拉取最新详情
        4. 获取成功后自动沉淀入库并更新内存缓存
        """
        goods_key = str(goods_id_or_sign).strip()
        now = time.time()

        # 1. 内存高速缓存 (仅当缓存内包含多张全量轮播图才直接返回)
        if goods_key in _DETAIL_CACHE:
            ts, cached_res = _DETAIL_CACHE[goods_key]
            if (now - ts) < DETAIL_CACHE_TTL:
                c_imgs = cached_res.get("data", {}).get("carousel_images", [])
                if len(c_imgs) > 1:
                    logger.debug(f"[内存缓存命中] 商品详情: {goods_key}")
                    return cached_res

        # 2. 本地数据库检索：若本地已有全量高清实拍轮播图（> 1 张），直接 0ms 瞬间返回
        db_item = self.repo.get_product_detail(goods_key)
        if db_item and db_item.get("carousel_images") and len(db_item.get("carousel_images", [])) > 1:
            ret = {"status": "success", "data": db_item, "source": "database"}
            _DETAIL_CACHE[goods_key] = (now, ret)
            return ret

        # 3. 调官方 API 拉取
        adapter = get_adapter(platform)
        res = adapter.get_goods_detail(goods_id_or_sign=goods_id_or_sign, uid=uid)
        if res.get("status") == "success" and res.get("data"):
            # 自动沉淀入库
            try:
                self.repo.upsert_product_detail(res["data"])
            except Exception as e:
                logger.warning(f"商品详情入库忽略异常: {e}")
            _DETAIL_CACHE[goods_key] = (now, res)
            return res

        # 4. 兜底：若官方 API 失败或限流，返回数据库已有基础数据
        if db_item:
            ret = {"status": "success", "data": db_item, "source": "database_fallback"}
            _DETAIL_CACHE[goods_key] = (now, ret)
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
        获取爆单库分类列表
        """
        return self.repo.get_categories(platform=platform)
