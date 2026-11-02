# -*- coding: utf-8 -*-
"""商品应用服务：平台调度、缓存与本地商品库编排，不解释平台原始字段。"""
import time
import threading
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from app.mobile_web.adapters import get_adapter, normalize_platform
from app.mobile_web.models.product import ProductRef
from app.mobile_web.repositories.product_repo import ProductRepository
from app.mobile_web.cache import RECOMMEND_CACHE, DETAIL_CACHE, SEARCH_CACHE, CATEGORY_CACHE
from app.mobile_web.config import PRODUCT_DETAIL_TTL, logger

# 兼容统一缓存分域别名，供测试及向后兼容使用
_RECOMMEND_CACHE = RECOMMEND_CACHE
_DETAIL_CACHE = DETAIL_CACHE
_SEARCH_CACHE = SEARCH_CACHE
_CATEGORY_CACHE = CATEGORY_CACHE


class ProductService:
    def __init__(self, product_repo: Optional[ProductRepository] = None):
        self.repo = product_repo if product_repo is not None else ProductRepository()

    def get_home_recommendations(self, channel_type=5, offset=0, limit=20, uid=None,
                                 platform="pdd", force_refresh=False) -> Dict[str, Any]:
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        cache_key = (plat, uid or "", channel_type, offset, limit)
        if not force_refresh:
            cached = RECOMMEND_CACHE.get(cache_key)
            if cached is not None:
                return cached
        result = adapter.get_recommend_goods(channel_type=channel_type, offset=offset, limit=limit, uid=uid)
        if result.get("status") == "success":
            items = result.get("data") or []
            result.setdefault("has_more", len(items) >= limit)
            RECOMMEND_CACHE.set(cache_key, result)
            if items:
                def _bg_upsert():
                    try:
                        self.repo.upsert_home_products(items, channel_type=channel_type)
                    except Exception as exc:
                        logger.warning(f"推荐商品入库失败，不影响在线结果: {exc}")
                threading.Thread(target=_bg_upsert, name="bg-upsert-home-products", daemon=True).start()
        return result

    def search_home_goods(self, keyword, page=1, page_size=20, uid=None, platform="pdd") -> Dict[str, Any]:
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        keyword = (keyword or "").strip()[:30]
        cache_key = (plat, uid or "", keyword, page, page_size)
        cached = SEARCH_CACHE.get(cache_key)
        if cached is not None:
            return cached
        result = adapter.search_goods(keyword=keyword, page=page, page_size=page_size, uid=uid)
        if result.get("status") == "success":
            SEARCH_CACHE.set(cache_key, result)
        return result

    @staticmethod
    def _detail_is_fresh(item: dict) -> bool:
        # 摘要 updated_at 会被推荐流不断更新，不能作为完整详情的新鲜度。
        updated = item.get("detail_updated_at")
        if isinstance(updated, datetime):
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            timestamp = updated.timestamp()
        elif isinstance(updated, (int, float)):
            timestamp = float(updated)
        elif isinstance(updated, str):
            try:
                parsed = datetime.fromisoformat(updated)
                timestamp = (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)).timestamp()
            except ValueError:
                return False
        else:
            return False
        return 0 <= time.time() - timestamp < PRODUCT_DETAIL_TTL

    @staticmethod
    def _cache_detail(platform: str, result: dict, uid=None):
        """只缓存来源确认的 ID/Sign；绝不盲信请求提供的另一别名。"""
        item = result.get("data") or {}
        ref = ProductRef(platform, item.get("goods_id") or item.get("product_id"), item.get("goods_sign"))
        for key in ref.cache_keys(uid):
            DETAIL_CACHE.set(key, result)

    def get_goods_detail(self, platform="pdd", goods_id_or_sign="", goods_id=None,
                         goods_sign=None, uid=None, **kwargs) -> Dict[str, Any]:
        plat = normalize_platform(platform)
        adapter = get_adapter(plat)
        if adapter.active is False:
            return adapter.get_goods_detail(goods_id_or_sign=goods_id_or_sign)
        ref = adapter.resolve_product_ref(goods_id_or_sign, goods_id, goods_sign)
        if not ref.key:
            return {"status": "error", "error_msg": "请提供商品 ID 或 Sign", "error_code": "missing_product_identity"}

        # 同时提供不一致的 Sign/ID 时只按主标识查，不在另一个键中命中错误商品。
        lookup_ref = ProductRef(plat, "" if ref.goods_sign else ref.goods_id, ref.goods_sign)
        for key in lookup_ref.cache_keys(uid):
            cached = DETAIL_CACHE.get(key)
            if cached is not None:
                return cached

        item = self.repo.get_product_detail(ref.key, platform=plat, key_type="sign" if ref.goods_sign else "id")
        if item and self._detail_is_fresh(item):
            result = {"status": "success", "data": item, "source": "database"}
            self._cache_detail(plat, result, uid)
            return result

        result = adapter.get_goods_detail(goods_id_or_sign=ref.key, goods_id=ref.goods_id or None,
                                          goods_sign=ref.goods_sign or None, uid=uid, **kwargs)
        if result.get("status") == "success" and result.get("data"):
            data = result["data"]
            if normalize_platform(data.get("platform") or plat) != plat:
                return {"status": "error", "error_msg": "商品返回平台不匹配", "error_code": "product_identity_mismatch"}
            data.setdefault("platform", plat)
            try:
                self.repo.upsert_product_detail(data)
            except Exception as exc:
                logger.warning(f"商品详情入库失败，不影响在线结果: {exc}")
            self._cache_detail(plat, result, uid)
            return result
        if item:
            # 上游故障仅作本次降级，不缓存整段 TTL，以便恢复后立即重试最新价格。
            return {"status": "success", "data": item, "source": "database_fallback"}
        return result

    def get_boom_products(self, platform="all", category=None, keyword=None, sort_by="latest",
                          page=1, page_size=20) -> Dict[str, Any]:
        return self.repo.query_boom_products(platform=normalize_platform(platform, "all"), category=category,
                                              keyword=keyword, sort_by=sort_by, page=page, page_size=page_size)

    def get_boom_categories(self, platform="all") -> List[str]:
        plat = normalize_platform(platform, "all")
        cached = CATEGORY_CACHE.get(plat)
        if cached is not None:
            return cached
        categories = self.repo.get_categories(platform=plat)
        if categories:
            CATEGORY_CACHE.set(plat, categories)
        return categories
