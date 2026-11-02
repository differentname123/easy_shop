# -*- coding: utf-8 -*-
"""
商品数据仓储模块 (MongoDB: products 集合)
负责商品数据的高效读写、索引保障与爆品库检索。
"""
import re
import datetime
from typing import Dict, Any, List, Optional, Tuple
from pymongo import UpdateOne
from app.mobile_web.repositories.base_repo import BaseRepository
from app.mobile_web.config import logger


class ProductRepository(BaseRepository):
    COLLECTION_NAME = "products"

    def _init_indexes(self):
        try:
            self.col.create_index([("platform", 1), ("product_id", 1)], background=True)
            self.col.create_index("goods_sign", background=True)
        except Exception as e:
            logger.warning(f"商品集合索引创建提示: {e}")

    @staticmethod
    def _calc_commission(activity_price: float, raw_commission: Any) -> Tuple[float, float]:
        """统一佣金比例与金额计算逻辑"""
        act_price = float(activity_price or 0.0)
        commission_amount = round(float(raw_commission or 0.0), 2)
        if commission_amount > 0 and act_price > 0:
            commission_rate = round((commission_amount / act_price) * 100, 1)
        else:
            commission_rate = 10.0
            if act_price > 0 and commission_amount == 0.0:
                commission_amount = round(act_price * (commission_rate / 100.0), 2)
        return commission_rate, commission_amount

    def query_boom_products(
        self,
        platform: str = "all",
        category: Optional[str] = None,
        keyword: Optional[str] = None,
        sort_by: str = "latest",
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        分页查询爆单库商品（游标探测无慢查询 count_documents）
        """
        page = max(1, min(50, int(page or 1)))
        page_size = max(1, min(50, int(page_size or 20)))
        skip = (page - 1) * page_size

        filter_query: Dict[str, Any] = {}

        if platform and platform.lower() != "all":
            filter_query["platform"] = platform.lower()

        if category and category.strip() and category != "全部":
            filter_query["category"] = category.strip()

        if keyword and keyword.strip():
            safe_kw = re.escape(keyword.strip()[:30])
            filter_query["name"] = {"$regex": safe_kw, "$options": "i"}

        sort_mapping = {
            "latest": [("updated_at", -1)],
            "save_desc": [("saved_price", -1), ("updated_at", -1)],
            "price_asc": [("activity_price", 1)],
            "price_desc": [("activity_price", -1)],
        }
        sort_spec = sort_mapping.get(sort_by, [("updated_at", -1)])

        try:
            cursor = (
                self.col.find(filter_query)
                .sort(sort_spec)
                .skip(skip)
                .limit(page_size + 1)
                .max_time_ms(2000)
            )
            raw_docs = list(cursor)
            has_more = len(raw_docs) > page_size
            items = [self._normalize_doc_summary(doc) for doc in raw_docs[:page_size]]

            return {
                "status": "success",
                "total_count": len(items) if keyword else (skip + len(raw_docs)),
                "page": page,
                "page_size": page_size,
                "has_more": has_more,
                "data": items,
            }
        except Exception as e:
            logger.error(f"[ProductRepository.query_boom_products] 查询异常: {e}")
            return {
                "status": "error",
                "error_msg": str(e),
                "total_count": 0,
                "page": page,
                "page_size": page_size,
                "has_more": False,
                "data": [],
            }

    def _normalize_doc_summary(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """极简爆品卡片摘要对象：专供列表与搜索秒开"""
        activity_price = doc.get("activity_price") or doc.get("price") or 0.0
        original_price = doc.get("original_price") or activity_price
        saved_price = doc.get("saved_price") or max(0.0, round(original_price - activity_price, 2))

        commission_rate, commission_amount = self._calc_commission(activity_price, doc.get("commission_amount"))

        updated_at_dt = doc.get("updated_at")
        updated_at_str = updated_at_dt.strftime("%Y-%m-%d %H:%M") if hasattr(updated_at_dt, "strftime") else str(updated_at_dt or "")

        return {
            "platform": doc.get("platform", "pdd"),
            "platform_name": "拼多多" if doc.get("platform") == "pdd" else ("淘宝" if doc.get("platform") == "taobao" else "京东"),
            "product_id": str(doc.get("product_id", "")),
            "goods_id": str(doc.get("product_id", "")),
            "goods_sign": doc.get("goods_sign") or "",
            "name": doc.get("name", "未命名商品"),
            "brand": doc.get("brand") or "官方甄选",
            "category": doc.get("category") or "精选爆品",
            "price": round(float(activity_price), 2),
            "original_price": round(float(original_price), 2),
            "saved_price": round(float(saved_price), 2),
            "coupon_amount": round(float(saved_price), 2),
            "has_coupon": saved_price > 0,
            "commission_rate": commission_rate,
            "commission_amount": commission_amount,
            "sales_tip": doc.get("sales_tip") or "热销中",
            "image_url": doc.get("image_url") or "",
            "updated_at": updated_at_str,
        }

    def _normalize_doc(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """将 Mongo 原生商品文档转化为统一、规范且富含详情的对象"""
        doc = dict(doc)
        doc.pop("_id", None)
        activity_price = doc.get("activity_price") or doc.get("price") or 0.0
        original_price = doc.get("original_price") or activity_price
        saved_price = doc.get("saved_price") or max(0.0, round(original_price - activity_price, 2))

        # 提取高清轮播图列表
        gallery = []
        stored_carousel = doc.get("carousel_images")
        if stored_carousel and isinstance(stored_carousel, list):
            gallery = [u for u in stored_carousel if u and isinstance(u, str) and u.startswith("http")]

        main_img = doc.get("image_url")
        if not gallery and main_img and isinstance(main_img, str) and main_img.startswith("http"):
            gallery = [main_img]

        clean_gallery = list(dict.fromkeys(gallery))

        commission_rate, commission_amount = self._calc_commission(activity_price, doc.get("commission_amount"))

        updated_at_dt = doc.get("updated_at")
        updated_at_str = updated_at_dt.strftime("%Y-%m-%d %H:%M") if hasattr(updated_at_dt, "strftime") else str(updated_at_dt or "")

        brand_name = doc.get("brand") or "官方甄选"
        category_name = doc.get("category") or "爆款精选"
        name = doc.get("name", "未命名商品")

        return {
            "platform": doc.get("platform", "pdd"),
            "platform_name": "拼多多" if doc.get("platform") == "pdd" else ("淘宝" if doc.get("platform") == "taobao" else "京东"),
            "product_id": str(doc.get("product_id", "")),
            "goods_id": str(doc.get("product_id", "")),
            "goods_sign": doc.get("goods_sign") or "",
            "name": name,
            "desc": doc.get("desc") or f"{name}，{brand_name}品牌好物推荐，全网低价直购，已享专属活动立减！",
            "brand": brand_name,
            "brand_name": brand_name,
            "category": category_name,
            "category_name": category_name,
            "price": round(float(activity_price), 2),
            "original_price": round(float(original_price), 2),
            "saved_price": round(float(saved_price), 2),
            "coupon_amount": round(float(saved_price), 2),
            "has_coupon": saved_price > 0,
            "commission_rate": commission_rate,
            "commission_amount": commission_amount,
            "sales_tip": doc.get("sales_tip") or "热销中",
            "image_url": main_img or "",
            "carousel_images": clean_gallery,
            "product_url": doc.get("product_url", ""),
            "updated_at": updated_at_str,
            "detail_tags": ["官方正品", "退货包运费", "极速发货", "假一赔十"],
            "service_guarantees": [
                "🛡️ 正品保障 · 假一赔十",
                "📦 退货包运费 · 极速退款",
                "⚡ 48小时闪电极速发货",
                "💰 全网爆款官方活动补贴"
            ],
            "shop_ratings": {
                "desc_score": "4.8 (高)",
                "serv_score": "4.9 (高)",
                "lgst_score": "4.8 (高)"
            },
            "specs": [
                {"label": "商品品牌", "value": brand_name},
                {"label": "所属品类", "value": category_name},
                {"label": "发货承诺", "value": "48小时内极速出库"},
                {"label": "售后服务", "value": "支持7天无理由退换 (赠送运费险)"},
                {"label": "当前销量", "value": doc.get("sales_tip") or "超10万件热销"}
            ],
            "coupon_info": {
                "coupon_remain_quantity": 8900,
                "coupon_total_quantity": 10000,
                "valid_tip": "优惠券今日有效，先到先得"
            }
        }

    def get_product_detail(self, goods_id_or_sign: str) -> Optional[Dict[str, Any]]:
        """按 product_id 或 goods_sign 从数据库检索并返回详情"""
        if not goods_id_or_sign:
            return None
        key = str(goods_id_or_sign).strip()
        conds = [{"product_id": key}, {"goods_sign": key}]
        try:
            doc = self.col.find_one({"$or": conds})
            if doc:
                return self._normalize_doc(doc)
        except Exception as e:
            logger.warning(f"从 Mongo 反查商品详情失败: {e}")
        return None

    def get_categories(self, platform: str = "all") -> List[str]:
        """获取类目列表"""
        filter_query = {}
        if platform and platform.lower() != "all":
            filter_query["platform"] = platform.lower()
        try:
            cats = self.col.distinct("category", filter_query)
            return [c for c in cats if c and str(c).strip()]
        except Exception as e:
            logger.warning(f"获取类目失败: {e}")
            return ["全部", "食品", "美妆", "百货", "数码", "服饰"]

    def upsert_home_products(
        self,
        goods_list: List[Dict[str, Any]],
        channel_type: Optional[int] = None
    ) -> int:
        """
        批量更新/插入首页官方推荐流商品到 MongoDB products 集合
        隔绝大模型开销标记 (need_reformat: False, format_status: 'success')
        """
        if not goods_list or not isinstance(goods_list, list):
            return 0

        now = datetime.datetime.now()
        operations = []

        for item in goods_list:
            goods_sign = str(item.get("goods_sign") or "").strip()
            goods_id = str(item.get("goods_id") or item.get("product_id") or "").strip()
            if not goods_sign and not goods_id:
                continue

            platform = item.get("platform") or "pdd"
            if goods_id:
                filter_spec = {"platform": platform, "product_id": goods_id}
            else:
                filter_spec = {"platform": platform, "goods_sign": goods_sign}

            price = float(item.get("price") or item.get("min_group_price_yuan") or 0.0)
            orig_price = float(item.get("original_price") or price)
            coupon = float(item.get("coupon_amount") or item.get("coupon_discount_yuan") or 0.0)
            commission = float(item.get("commission_amount") or item.get("estimated_commission_yuan") or 0.0)

            update_data = {
                "name": item.get("name") or item.get("goods_name") or "",
                "platform": platform,
                "goods_sign": goods_sign,
                "product_id": goods_id,
                "price": orig_price,
                "activity_price": price,
                "original_price": orig_price,
                "saved_price": coupon,
                "has_coupon": coupon > 0,
                "coupon_amount": coupon,
                "commission_amount": commission,
                "sales_tip": str(item.get("sales_tip") or "0"),
                "image_url": item.get("image_url") or item.get("goods_image_url") or item.get("goods_thumbnail_url") or "",
                "brand": item.get("brand_name") or item.get("brand") or "官方优选",
                "category": item.get("category_name") or item.get("category") or "精选好物",
                "updated_at": now,
                "source": "pdd_recommend_stream",
                "need_reformat": False,
                "format_status": "success",
                "format_retry_count": 999,
            }
            if channel_type is not None:
                update_data["channel_type"] = channel_type

            operations.append(
                UpdateOne(filter_spec, {"$set": update_data, "$setOnInsert": {"created_at": now}}, upsert=True)
            )

        if not operations:
            return 0

        try:
            result = self.col.bulk_write(operations, ordered=False)
            upserted_count = result.upserted_count + result.modified_count
            logger.info(f"[ProductRepository.upsert_home_products] 成功入库/更新 {upserted_count} 件首页推荐商品")
            return upserted_count
        except Exception as e:
            logger.warning(f"[ProductRepository.upsert_home_products] 批量入库异常: {e}")
            return 0

    def upsert_product_detail(self, detail: Dict[str, Any]) -> bool:
        """将深度商品详情写入或更新到数据库"""
        if not detail or not isinstance(detail, dict):
            return False
        goods_sign = str(detail.get("goods_sign") or "").strip()
        goods_id = str(detail.get("goods_id") or detail.get("product_id") or "").strip()
        if not goods_sign and not goods_id:
            return False

        platform = detail.get("platform") or "pdd"
        if goods_id:
            filter_spec = {"platform": platform, "product_id": goods_id}
        else:
            filter_spec = {"platform": platform, "goods_sign": goods_sign}
        now = datetime.datetime.now()

        update_doc = {
            "name": detail.get("name") or "",
            "desc": detail.get("desc") or "",
            "platform": detail.get("platform") or "pdd",
            "goods_sign": goods_sign,
            "product_id": goods_id,
            "price": float(detail.get("original_price") or detail.get("price") or 0.0),
            "activity_price": float(detail.get("price") or 0.0),
            "original_price": float(detail.get("original_price") or detail.get("price") or 0.0),
            "saved_price": float(detail.get("coupon_amount") or 0.0),
            "coupon_amount": float(detail.get("coupon_amount") or 0.0),
            "has_coupon": bool(detail.get("has_coupon")),
            "commission_amount": float(detail.get("commission_amount") or 0.0),
            "sales_tip": str(detail.get("sales_tip") or "0"),
            "image_url": detail.get("image_url") or "",
            "carousel_images": detail.get("carousel_images") or [],
            "brand": detail.get("brand_name") or detail.get("brand") or "官方优选",
            "category": detail.get("category_name") or detail.get("category") or "精选好物",
            "mall_name": detail.get("mall_name") or "品牌官方店",
            "specs": detail.get("specs") or [],
            "service_guarantees": detail.get("service_guarantees") or [],
            "shop_ratings": detail.get("shop_ratings") or {},
            "coupon_info": detail.get("coupon_info") or {},
            "updated_at": now,
            "need_reformat": False,
            "format_status": "success",
            "format_retry_count": 999,
        }

        try:
            self.col.update_one(filter_spec, {"$set": update_doc, "$setOnInsert": {"created_at": now}}, upsert=True)
            return True
        except Exception as e:
            logger.warning(f"[ProductRepository.upsert_product_detail] 详情入库异常: {e}")
            return False

    def get_promotion_link_cache(
        self,
        platform: str,
        goods_id: Optional[str] = None,
        goods_sign: Optional[str] = None,
        goods_key: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """从 products 集合中读取站长官方推广链接缓存"""
        gid = goods_id or (goods_key if goods_key and str(goods_key).isdigit() else None)
        gsign = goods_sign or (goods_key if goods_key and not str(goods_key).isdigit() else None)

        if gid and str(gid).isdigit():
            filter_spec = {"platform": platform, "product_id": str(gid)}
        elif gsign:
            filter_spec = {"platform": platform, "goods_sign": str(gsign)}
        else:
            return None

        try:
            doc = self.col.find_one(filter_spec)
            if doc:
                promo_data = doc.get("default_promotion")
                if isinstance(promo_data, dict) and promo_data.get("final_link"):
                    promo_ts = doc.get("default_promotion_updated_at")
                    if promo_ts:
                        try:
                            import time
                            doc_ts = promo_ts.timestamp() if hasattr(promo_ts, "timestamp") else 0
                            if (time.time() - doc_ts) > 3 * 86400:
                                return None
                        except Exception:
                            pass
                    result = dict(promo_data)
                    result["from_cache"] = True
                    return result
        except Exception as e:
            logger.warning(f"读取 products 表官方推广缓存异常: {e}")
        return None

    def save_promotion_link_cache(
        self,
        platform: str,
        data: Dict[str, Any],
        goods_id: Optional[str] = None,
        goods_sign: Optional[str] = None,
        goods_key: Optional[str] = None
    ) -> bool:
        """持久化保存未登录用户（站长官方位）的转链结果"""
        if not data or not isinstance(data, dict):
            return False

        now = datetime.datetime.now()
        cache_data = dict(data)
        cache_data.pop("from_cache", None)

        gid = goods_id or (goods_key if goods_key and str(goods_key).isdigit() else None)
        gsign = goods_sign or (goods_key if goods_key and not str(goods_key).isdigit() else None)

        if gid and str(gid).isdigit():
            filter_spec = {"platform": platform, "product_id": str(gid)}
        elif gsign:
            filter_spec = {"platform": platform, "goods_sign": str(gsign)}
        else:
            return False

        update_set = {
            "default_promotion": cache_data,
            "default_promotion_updated_at": now,
        }
        if gsign:
            update_set["goods_sign"] = str(gsign)
        if gid:
            update_set["product_id"] = str(gid)
        update_set["platform"] = platform

        try:
            self.col.update_one(
                filter_spec,
                {"$set": update_set, "$setOnInsert": {"created_at": now}},
                upsert=True
            )
            return True
        except Exception as e:
            logger.warning(f"保存 products 表官方推广缓存异常: {e}")
            return False
