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
                item_detail = self._normalize_doc(doc)
                items.append(item_detail)

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

    def _normalize_doc(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """将 Mongo 原生商品文档转化为统一、规范且富含详情的对象"""
        import urllib.parse
        doc.pop("_id", None)
        activity_price = doc.get("activity_price") or doc.get("price") or 0.0
        original_price = doc.get("original_price") or activity_price
        saved_price = doc.get("saved_price") or max(0.0, round(original_price - activity_price, 2))

        # 提取轮播图片
        gallery = []
        main_img = doc.get("image_url")
        if main_img:
            gallery.append(main_img)

        # 解析 product_url 中的 _oak_gallery
        purl = doc.get("product_url", "")
        if "_oak_gallery=" in purl:
            try:
                parsed = urllib.parse.urlparse(purl)
                qs = urllib.parse.parse_qs(parsed.query)
                for g_val in qs.get("_oak_gallery", []):
                    if g_val and g_val.startswith("http"):
                        gallery.append(g_val)
            except Exception:
                pass

        # 解析 sku_info 中的小图
        sku_list = doc.get("sku_info") or []
        if isinstance(sku_list, list):
            for sku in sku_list:
                if isinstance(sku, dict):
                    s_img = sku.get("小图地址") or sku.get("image_url") or ""
                    if s_img and s_img.startswith("http"):
                        gallery.append(s_img)

        # 去重保持顺序
        clean_gallery = list(dict.fromkeys(gallery))
        if not clean_gallery and main_img:
            clean_gallery = [main_img]

        # 估算返利佣金
        commission = doc.get("estimated_commission_yuan")
        if not commission:
            commission = round(float(activity_price) * 0.1, 2)
            if commission < 0.1:
                commission = 0.50

        # 处理更新时间
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
            "commission_rate": 10.0,
            "commission_amount": commission,
            "sales_tip": doc.get("sales_tip") or "热销中",
            "image_url": main_img or "",
            "carousel_images": clean_gallery,
            "product_url": purl,
            "promotion_url": doc.get("promotion_url") or purl,
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
        """按 product_id 或 goods_sign 从数据库直接检索并返回规范化商品详情"""
        if not goods_id_or_sign:
            return None
        key = str(goods_id_or_sign).strip()
        conds = [{"product_id": key}, {"goods_sign": key}]
        if key.isdigit():
            conds.append({"product_id": int(key)})
        try:
            doc = self.col.find_one({"$or": conds})
            if doc:
                return self._normalize_doc(doc)
        except Exception as e:
            logger.warning(f"从 Mongo 反查商品详情失败: {e}")
        return None

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
