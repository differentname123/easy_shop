# -*- coding: utf-8 -*-
"""
商品数据仓储模块 (MongoDB: products 集合)
用于爆单推广库商品的多维度筛选、聚合排序与分页查询。
"""
from typing import Dict, Any, List, Optional
from common.mongo_db.mongo_base import gen_db_object
from app.mobile_web.config import logger


class ProductRepository:
    COLLECTION_NAME = "products"

    def __init__(self, db_instance=None):
        self._db = db_instance or gen_db_object()

    @property
    def col(self):
        return self._db.get_collection(self.COLLECTION_NAME)

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
        分页查询爆单库商品
        :param platform: 平台代码 (all, pdd, taobao, jd)
        :param category: 商品类目/Tab
        :param keyword: 标题或品牌模糊搜索
        :param sort_by: latest(最新) | save_desc(省最多) | price_asc(价格低) | price_desc(价格高)
        :param page: 页码
        :param page_size: 每页数量
        """
        page = max(1, int(page or 1))
        page_size = max(1, min(100, int(page_size or 20)))
        skip = (page - 1) * page_size

        filter_query: Dict[str, Any] = {}

        # 平台筛选
        if platform and platform.lower() != "all":
            filter_query["platform"] = platform.lower()

        # 分类筛选
        if category and category.strip() and category != "全部":
            filter_query["category"] = category.strip()

        # 关键词模糊搜索
        if keyword and keyword.strip():
            filter_query["name"] = {"$regex": keyword.strip(), "$options": "i"}

        # 排序规则
        sort_mapping = {
            "latest": [("updated_at", -1)],
            "save_desc": [("saved_price", -1), ("updated_at", -1)],
            "price_asc": [("activity_price", 1)],
            "price_desc": [("activity_price", -1)],
        }
        sort_spec = sort_mapping.get(sort_by, [("updated_at", -1)])

        try:
            total_count = self.col.count_documents(filter_query)
            cursor = self.col.find(filter_query).sort(sort_spec).skip(skip).limit(page_size)

            items = []
            for doc in cursor:
                doc.pop("_id", None)
                activity_price = doc.get("activity_price") or doc.get("price") or 0.0
                original_price = doc.get("original_price") or activity_price
                saved_price = doc.get("saved_price") or max(0.0, round(original_price - activity_price, 2))

                # 处理更新时间
                updated_at_dt = doc.get("updated_at")
                updated_at_str = updated_at_dt.strftime("%Y-%m-%d %H:%M") if hasattr(updated_at_dt, "strftime") else str(updated_at_dt or "")

                items.append({
                    "platform": doc.get("platform", "pdd"),
                    "platform_name": "拼多多" if doc.get("platform") == "pdd" else ("淘宝" if doc.get("platform") == "taobao" else "京东"),
                    "product_id": str(doc.get("product_id", "")),
                    "goods_id": str(doc.get("product_id", "")),
                    "goods_sign": doc.get("goods_sign") or "",
                    "name": doc.get("name", "未命名商品"),
                    "brand": doc.get("brand", ""),
                    "category": doc.get("category", "爆款精选"),
                    "price": round(float(activity_price), 2),
                    "original_price": round(float(original_price), 2),
                    "saved_price": round(float(saved_price), 2),
                    "coupon_amount": round(float(saved_price), 2),
                    "has_coupon": saved_price > 0,
                    "sales_tip": doc.get("sales_tip") or "热销中",
                    "image_url": doc.get("image_url") or "",
                    "product_url": doc.get("product_url") or "",
                    "promotion_url": doc.get("promotion_url") or "",
                    "updated_at": updated_at_str,
                })

            return {
                "status": "success",
                "total_count": total_count,
                "page": page,
                "page_size": page_size,
                "has_more": (skip + len(items)) < total_count,
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

    def get_categories(self, platform: str = "all") -> List[str]:
        """获取已有商品的类目列表，用于分类快速导航"""
        filter_query = {}
        if platform and platform.lower() != "all":
            filter_query["platform"] = platform.lower()
        try:
            cats = self.col.distinct("category", filter_query)
            return [c for c in cats if c and str(c).strip()]
        except Exception as e:
            logger.warning(f"获取类目失败: {e}")
            return ["全部", "食品", "美妆", "百货", "数码", "服饰"]
